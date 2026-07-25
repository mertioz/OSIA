#!/usr/bin/env python3
"""
OSIA Browser Tools — CDP-controlled real browser.

OSIA drives a *real, visible* Chromium-based browser (Brave by default)
through the Chrome DevTools Protocol. On the first ``navigate()`` call,
the browser is launched with::

    <browser> --remote-debugging-port=9222 --user-data-dir=<OSIA profile>

then Playwright connects via ``connect_over_cdp("http://localhost:9222")``.
The OSIA-managed profile persists cookies / sessions / extensions across
runs, so logins survive between conversations.

Note on Firefox: Firefox does NOT support the Chrome DevTools Protocol, so
it is driven over *WebDriver BiDi* instead. When the user asks for Firefox
(``open_browser("firefox")``), OSIA launches the system Firefox through
Playwright (``firefox.launch_persistent_context`` with
``channel="moz-firefox"``) against a persistent OSIA profile at
``/home/OSIA/.osia/firefox-profile``, so cookies / logins survive between
OSIA uses. The Playwright Page API is identical for both protocols, so every
page tool (navigate, click, find_and_click, scroll, screenshot, …) works
transparently regardless of which backend is active; ``_active_browser_type``
records the current one ("cdp" for Chromium browsers, "bidi" for Firefox).
If no CDP-capable browser is installed we fall back to Firefox BiDi, and if
neither is available we surface a clear error.
"""

import base64
import asyncio
import concurrent.futures
import inspect
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from osia.tools.browser_forms import FORM_MAX_FIELDS_DEFAULT, SemanticFormSession

# Timeout exceptions that mean "the page is just slow" — never a hard failure.
# Playwright's TimeoutError is caught alongside the builtin for safety across
# Playwright versions (newer versions subclass the builtin, older don't).
_TIMEOUT_ERRORS = (PlaywrightTimeoutError, TimeoutError)

# psutil powers the stale-instance scan (Bug 1): it spots a browser left over
# from a previous OSIA session that still holds one of OSIA's dedicated
# profiles, so we can clear it before launching instead of spawning a duplicate
# window. psutil is a declared dependency, but the import is guarded so a
# missing psutil only disables the optimization — it never breaks a launch.
try:
    import psutil

    _PSUTIL_OK = True
except Exception:  # pragma: no cover - psutil is a declared dependency
    psutil = None
    _PSUTIL_OK = False

# ── Configuration ──────────────────────────────────────────────────────────

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"

# Max time to wait for a freshly launched browser to confirm it is ready
# (the CDP debugger reachable on CDP_PORT for Chromium; the BiDi session is
# established synchronously by Playwright). Fast models call the page tools the
# instant they ask for a browser, so this must be generous enough that a
# slow-but-fine startup is never misreported as "the browser failed to open"
# (Bug 2). Return success the moment the browser is ready; only report failure
# after the full timeout.
BROWSER_READY_TIMEOUT_S = 15
# Poll cadence while waiting for the browser to come up.
_BROWSER_READY_POLL_S = 0.5

# Every browser OSIA launches gets a dedicated profile under ~/.osia/ so it
# never touches (or opens a window in) the user's personal browser profiles.
OSIA_DATA_DIR = str(Path.home() / ".osia")
OSIA_PROFILE_DIR = f"{OSIA_DATA_DIR}/brave-profile"          # CDP (Brave/Chromium)
FIREFOX_PROFILE_DIR = f"{OSIA_DATA_DIR}/firefox-profile"     # Firefox (non-CDP)

# Browser candidates, ordered by preference. Brave is tried first: the
# /usr/bin wrapper scripts are preferred over the raw /opt/brave.com/brave/brave
# binary, which can die with a fatal error when launched directly on some
# installs. Each name is resolved against /usr/bin/, /snap/bin/, and $PATH
# (via shutil.which()) by ``_find_binary``; absolute paths are checked with
# os.path.exists().
_BROWSER_CANDIDATES = [
    "brave-browser",
    "brave",
    "/opt/brave.com/brave/brave",
    "chromium-browser",
    "chromium",
    "google-chrome",
    "google-chrome-stable",
    "firefox",
    "opera",
    "microsoft-edge",
]

# Windows install locations (env vars expanded at import time). Chrome/Brave
# commonly install under both %ProgramFiles(x86)% and %LOCALAPPDATA% as well as
# %ProgramFiles%; all variants are added so existing resolution finds them.
if sys.platform == "win32":
    _BROWSER_CANDIDATES = [
        os.path.expandvars(p) for p in (
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        )
    ] + _BROWSER_CANDIDATES

# Map common user-facing browser names ("open Firefox", "open Chrome") to
# the binary candidates to try for that browser, in preference order.
_BROWSER_NAME_MAP = {
    "brave": ["brave-browser", "brave", "/opt/brave.com/brave/brave"],
    "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
    "chromium": ["chromium-browser", "chromium"],
    "firefox": ["firefox", "firefox-esr"],
    "opera": ["opera"],
    "edge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
}

# On Windows, prepend the full install paths (env vars expanded) so the name
# resolver finds installed browsers without altering Linux/Mac candidate lists.
if sys.platform == "win32":
    _WINDOWS_NAME_PATHS = {
        "brave": [
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
        "chrome": [
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ],
        "edge": [
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        ],
        "firefox": [
            r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        ],
    }
    for _key, _paths in _WINDOWS_NAME_PATHS.items():
        _BROWSER_NAME_MAP[_key] = (
            [os.path.expandvars(p) for p in _paths] + _BROWSER_NAME_MAP[_key]
        )

# Browsers that can NOT be driven over the Chrome DevTools Protocol
# (non-Chromium engines). They are skipped for CDP automation; Firefox is
# instead driven over WebDriver BiDi (see ``_ensure_firefox_bidi``).
_NON_CDP_BROWSERS = {"firefox", "firefox-esr"}


# ── Module state ───────────────────────────────────────────────────────────

# Launch-in-flight guard retained by the unchanged CDP process launcher.
_cdp_launching = False


# ── CDP lifecycle ──────────────────────────────────────────────────────────

def _detach_kwargs() -> dict:
    """Platform-correct ``subprocess.Popen`` kwargs to detach the child.

    ``start_new_session`` is POSIX-only; on Windows use the detached/new-group
    creation flags instead so launching a browser never crashes.
    """
    if sys.platform == "win32":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _is_cdp_alive() -> bool:
    """Return True if a CDP debugger is responding on ``CDP_PORT``."""
    try:
        with socket.create_connection(("localhost", CDP_PORT), timeout=0.5):
            return True
    except (OSError, socket.timeout):
        return False


def _find_binary(name: str) -> str | None:
    """Resolve a browser binary name (or absolute path) to an executable path.

    Bare names are checked in ``/usr/bin/`` and ``/snap/bin/`` first, then
    against ``$PATH`` with ``shutil.which()``. Absolute paths are checked
    with ``os.path.exists()``.
    """
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    if sys.platform != "win32":
        for directory in ("/usr/bin", "/snap/bin"):
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return path
    return shutil.which(name)


def resolve_browser_name(name: str) -> str | None:
    """Map a user-facing browser name ("firefox", "chrome", "edge", …) to
    the path of an installed binary, or ``None`` if that browser isn't
    installed.

    Falls back to treating ``name`` as a raw binary name, so e.g.
    "google-chrome-stable" also resolves.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    candidates = _BROWSER_NAME_MAP.get(key, [key])
    for candidate in candidates:
        path = _find_binary(candidate)
        if path:
            return path
    return None


def _canonical_browser_name(binary: str) -> str:
    """Map a resolved binary path back to its user-facing name:
    "/usr/bin/google-chrome" -> "chrome", "/usr/bin/firefox-esr" -> "firefox".
    Falls back to the binary's basename for unknown browsers.
    """
    base = os.path.basename(binary)
    for canonical, candidates in _BROWSER_NAME_MAP.items():
        if any(os.path.basename(c) == base for c in candidates):
            return canonical
    return base


def _profile_args(binary: str) -> list[str]:
    """Return the CLI flags that point ``binary`` at a dedicated OSIA profile,
    creating the profile directory if needed.

    This is what keeps every browser OSIA launches isolated from the user's
    personal profiles:

    - Firefox  -> ``--profile <dir> --no-remote`` (a profile *path*, not a name;
      ``--no-remote`` stops it from handing the URL to an already-running
      personal Firefox instance)
    - Brave    -> the same ``--user-data-dir`` profile the CDP path uses, so
      logins are shared whether Brave was opened for automation or by name
    - Other Chromium-based browsers (chrome, chromium, opera, edge) ->
      ``--user-data-dir ~/.osia/browser-profile-<name>``
    """
    name = _canonical_browser_name(binary)
    if name == "firefox":
        os.makedirs(FIREFOX_PROFILE_DIR, exist_ok=True)
        return ["--profile", FIREFOX_PROFILE_DIR, "--no-remote"]
    if name == "brave":
        os.makedirs(OSIA_PROFILE_DIR, exist_ok=True)
        return [f"--user-data-dir={OSIA_PROFILE_DIR}"]
    profile_dir = f"{OSIA_DATA_DIR}/browser-profile-{name}"
    os.makedirs(profile_dir, exist_ok=True)
    return [f"--user-data-dir={profile_dir}"]


def _detect_chromium_browser() -> str | None:
    """Return the path of the first installed CDP-capable browser from
    ``_BROWSER_CANDIDATES``, or ``None`` if none are installed.

    Non-Chromium browsers (Firefox) are skipped — they cannot be driven
    over the Chrome DevTools Protocol.
    """
    for candidate in _BROWSER_CANDIDATES:
        if os.path.basename(candidate) in _NON_CDP_BROWSERS:
            continue
        path = _find_binary(candidate)
        if path:
            return path
    return None


def _detect_firefox() -> str | None:
    """Return the path of the installed Firefox binary, or ``None`` if
    Firefox isn't installed.

    Checks /usr/bin/firefox, /snap/bin/firefox, and $PATH (shutil.which),
    for both "firefox" and "firefox-esr" (via ``_find_binary``).
    """
    for candidate in _BROWSER_NAME_MAP["firefox"]:
        path = _find_binary(candidate)
        if path:
            return path
    return None


def _wait_for_cdp(timeout: float = BROWSER_READY_TIMEOUT_S) -> bool:
    """Poll until a CDP debugger responds on ``CDP_PORT`` or ``timeout``
    elapses, checking every ``_BROWSER_READY_POLL_S`` seconds. Returns
    ``True`` as soon as the debugger is reachable, ``False`` only once the
    full timeout has passed.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_cdp_alive():
            return True
        time.sleep(_BROWSER_READY_POLL_S)
    return False


# Executable-name stems that identify a browser process. The stale-profile
# scan is gated on these so it only ever targets a real browser — never an
# unrelated process (a shell, an editor, OSIA itself) that merely mentions the
# profile path on its command line.
_BROWSER_PROC_TOKENS = (
    "firefox", "brave", "chrome", "chromium", "opera", "msedge", "microsoft-edge",
)


def _looks_like_browser(name: str, exe: str) -> bool:
    """True if a process's name/executable identifies it as a known browser."""
    blob = f"{name or ''} {os.path.basename(exe or '')}".lower()
    return any(tok in blob for tok in _BROWSER_PROC_TOKENS)


def _processes_using_profile(profile_dir: str):
    """Return the running *browser* processes whose command line references
    ``profile_dir`` — a browser still holding one of OSIA's dedicated
    profiles. This catches a stale/zombie browser left over from a previous
    OSIA session: OSIA has no Playwright handle to drive it, yet it holds the
    profile lock, so launching a fresh browser against the same profile is
    exactly what spawns a duplicate window (Bug 1).

    Two conditions must BOTH hold, so the scan never targets an unrelated
    process: the process must look like a browser (``_looks_like_browser``)
    AND its command line must point at ``profile_dir``. Best-effort: returns
    ``[]`` when psutil is unavailable so the scan can never break a launch.

    Assumes a single OSIA instance (the normal deployment): a match is treated
    as a stale browser this OSIA owns but lost its handle to. If two OSIA
    processes ever shared a profile, this could match the other's live browser
    — out of scope here, since OSIA runs one instance per machine.
    """
    if not _PSUTIL_OK:
        return []
    target = os.path.normpath(profile_dir)
    self_pid = os.getpid()
    matches = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            info = proc.info
            if info.get("pid") == self_pid:
                continue
            if not _looks_like_browser(info.get("name"), info.get("exe")):
                continue
            cmdline = info.get("cmdline") or []
        except Exception:
            continue
        for arg in cmdline:
            if arg and (profile_dir in arg or target in os.path.normpath(arg)):
                matches.append(proc)
                break
    return matches


def _terminate_processes(procs) -> None:
    """Terminate ``procs`` cleanly: SIGTERM, a brief grace period, then
    SIGKILL for any survivor. Best-effort — a permission error on one process
    must never block the launch that follows.
    """
    if not _PSUTIL_OK or not procs:
        return
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=3)
    except Exception:
        alive = procs
    if not alive:
        return
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    # SIGKILL returns immediately while the OS tears the process down; the
    # profile lock (Chromium SingletonLock / Firefox .parentlock) is released
    # only once the process is fully reaped. Wait for that before returning so
    # the immediate relaunch never races a still-locked profile (Bug 1).
    try:
        psutil.wait_procs(alive, timeout=3)
    except Exception:
        pass


def _launch_cdp_browser(binary: str | None = None) -> tuple[bool, str]:
    """Spawn Brave (or another Chromium browser if Brave isn't installed)
    against OSIA's dedicated profile at ``/home/OSIA/.osia/brave-profile``.

    NEVER launches a second instance: if a CDP debugger already responds on
    ``CDP_PORT`` the existing window is reused, and if another launch is
    already in flight (``_cdp_launching``) this call waits for that launch
    instead of spawning its own process.

    ``binary`` optionally forces a specific Chromium binary (used by
    ``open_browser`` when the user asks for a browser by name); by default
    the first installed candidate is used.

    The dedicated profile keeps OSIA's cookies, sessions, and history
    isolated from — and persistent across restarts independently of —
    the user's personal Brave instance.

    Returns ``(success, message)``.
    """
    global _cdp_launching

    # Reuse: a CDP browser is already up — never open a second window.
    if _is_cdp_alive():
        return True, f"reusing CDP browser already running on port {CDP_PORT}"

    # Another call is already launching — wait for its debugger to come
    # online instead of racing it with a second process.
    if _cdp_launching:
        if _wait_for_cdp(BROWSER_READY_TIMEOUT_S):
            return True, f"reusing CDP browser launched by a concurrent call on port {CDP_PORT}"
        return False, (
            f"another CDP launch was in progress but the debugger never came "
            f"online on port {CDP_PORT}"
        )

    browser = binary or _detect_chromium_browser()
    if not browser:
        return False, (
            "No CDP-capable browser found — install brave-browser, chromium, "
            "google-chrome, opera, or microsoft-edge (Firefox does not support CDP)"
        )
    os.makedirs(OSIA_PROFILE_DIR, exist_ok=True)
    cmd = [
        browser,
        f"--user-data-dir={OSIA_PROFILE_DIR}",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    # Hard guarantee: a CDP browser must NEVER start in the user's personal
    # profile. If the flag is ever dropped from cmd, fail loudly here instead
    # of silently hijacking (and remote-debug-exposing) the user's profile.
    assert f"--user-data-dir={OSIA_PROFILE_DIR}" in cmd, (
        "refusing to launch CDP browser without --user-data-dir pointing at "
        f"the OSIA profile ({OSIA_PROFILE_DIR})"
    )
    # No CDP debugger answered above, yet a Chromium browser from a previous
    # OSIA session may still be holding our dedicated profile without exposing
    # the debug port (e.g. a plain-application fallback launch). Its profile
    # lock would make this fresh launch attach silently or never expose the
    # port — kill the stale, undriveable instance cleanly first so the new
    # window is the one we control (Bug 1).
    stale = _processes_using_profile(OSIA_PROFILE_DIR)
    if stale:
        _terminate_processes(stale)
    _cdp_launching = True
    try:
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_detach_kwargs(),
            )
        except Exception as e:
            return False, f"failed to launch {browser}: {e}"

        # Poll for the debugger socket — Chromium typically needs ~0.5-2s, but
        # a cold start can take longer; wait up to BROWSER_READY_TIMEOUT_S and
        # succeed the moment the debugger answers (Bug 2).
        if _wait_for_cdp(BROWSER_READY_TIMEOUT_S):
            return True, f"launched {os.path.basename(browser)} with CDP on port {CDP_PORT}"
        return False, (
            f"launched {browser} but its CDP debugger never came online on "
            f"port {CDP_PORT} within {BROWSER_READY_TIMEOUT_S}s"
        )
    finally:
        _cdp_launching = False


class _OwnerState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    RECONNECTING = "RECONNECTING"
    CLOSING = "CLOSING"
    FAILED = "FAILED"


class _OwnerCallTimeout(TimeoutError):
    pass


_OWNER_START_TIMEOUT_S = 25.0
_OWNER_CALL_TIMEOUT_S = 45.0
_OWNER_CANCEL_TIMEOUT_S = 2.0
_OWNER_SHUTDOWN_TIMEOUT_S = 25.0
_POPUP_EVENT_WINDOW_MS = 150


class _AsyncBrowserOwner:
    """Own every Playwright object on one continuously running asyncio loop."""

    def __init__(self):
        self._condition = threading.Condition()
        self._snapshot_lock = threading.Lock()
        self._state = _OwnerState.STOPPED
        self._thread = None
        self._loop = None
        self._owner_thread_id = None
        self._owner_native_thread_id = None
        self._startup_error = None
        self._closing_requested = False
        self._close_epoch = 0
        self._command_lock = None
        self._active_task = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._active_page = None
        self._active_browser_type = None
        self._registry = {}
        self._registry_context = None
        self._registry_generation = 0
        self._registry_sequence = 0
        self._context_handlers = None
        self._registry_browser = None
        self._browser_disconnected_handler = None
        self._page_tombstones = set()
        self._form_session = SemanticFormSession()
        self._disconnecting = False
        self._startup_count = 0
        self._driver_stop_count = 0
        self._published = {}
        self._publish()

    @property
    def state(self):
        with self._condition:
            return self._state

    def _set_state(self, state, error=None):
        with self._condition:
            self._state = state
            self._startup_error = error
            self._condition.notify_all()
        self._publish()

    def _assert_owner_thread(self):
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("Playwright operation attempted outside browser owner")

    def _publish(self):
        if (
            self._owner_thread_id is not None
            and threading.get_ident() != self._owner_thread_id
        ):
            with self._snapshot_lock:
                current = dict(self._published)
                current.update(
                    state=self._state.value,
                    threadAlive=bool(self._thread and self._thread.is_alive()),
                )
                self._published = current
            return
        page = self._active_page
        try:
            active_url = page.url if page is not None and not page.is_closed() else None
        except Exception:
            active_url = None
        with self._snapshot_lock:
            self._published = {
                "state": self._state.value,
                "threadId": self._owner_thread_id,
                "nativeThreadId": self._owner_native_thread_id,
                "threadAlive": bool(self._thread and self._thread.is_alive()),
                "backend": self._active_browser_type,
                "activeUrl": active_url,
                "registryCount": len(self._registry),
                "generation": self._registry_generation,
                "startupCount": self._startup_count,
                "driverStopCount": self._driver_stop_count,
            }

    def snapshot(self):
        with self._snapshot_lock:
            return dict(self._published)

    async def _startup_async(self):
        self._assert_owner_thread()
        self._command_lock = asyncio.Lock()
        self._playwright = await asyncio.wait_for(
            async_playwright().start(), timeout=_OWNER_START_TIMEOUT_S
        )
        self._startup_count += 1

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._owner_thread_id = threading.get_ident()
        self._owner_native_thread_id = threading.get_native_id()
        self._loop = loop
        try:
            loop.run_until_complete(self._startup_async())
        except BaseException as exc:
            closing = self._closing_requested
            self._set_state(
                _OwnerState.CLOSING if closing else _OwnerState.FAILED,
                None if closing else exc,
            )
            try:
                loop.run_until_complete(self._shutdown_resources())
            except BaseException:
                pass
            loop.close()
            self._owner_thread_id = None
            self._owner_native_thread_id = None
            self._loop = None
            if closing:
                self._set_state(_OwnerState.STOPPED)
            else:
                self._publish()
            return
        if self._closing_requested:
            self._set_state(_OwnerState.CLOSING)
            loop.call_soon(loop.stop)
        else:
            self._set_state(_OwnerState.READY)
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._shutdown_resources())
            except BaseException:
                pass
            loop.close()
            self._owner_thread_id = None
            self._owner_native_thread_id = None
            self._loop = None
            final = _OwnerState.STOPPED if self._closing_requested else _OwnerState.FAILED
            error = None if self._closing_requested else RuntimeError(
                "browser owner event loop stopped unexpectedly"
            )
            self._set_state(final, error)

    def _ensure_started(self):
        deadline = time.monotonic() + _OWNER_START_TIMEOUT_S
        with self._condition:
            close_epoch = self._close_epoch
            while True:
                if self._close_epoch != close_epoch:
                    raise RuntimeError("browser owner closed while the call was waiting")
                if self._state in (_OwnerState.READY, _OwnerState.RECONNECTING):
                    return self._loop
                if self._state in (_OwnerState.STOPPED, _OwnerState.FAILED):
                    if self._thread is not None and self._thread.is_alive():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError("browser owner did not finish stopping")
                        self._condition.wait(min(remaining, 0.1))
                        continue
                    self._closing_requested = False
                    self._state = _OwnerState.STARTING
                    self._startup_error = None
                    self._thread = threading.Thread(
                        target=self._thread_main,
                        name="osia-playwright-owner",
                        daemon=True,
                    )
                    self._thread.start()
                elif self._state == _OwnerState.CLOSING:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("browser owner is still closing")
                    self._condition.wait(min(remaining, 0.1))
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("browser owner startup timed out")
                self._condition.wait(remaining)
                if self._state == _OwnerState.FAILED:
                    raise RuntimeError(
                        f"browser owner startup failed: {self._startup_error}"
                    )

    async def _execute(self, operation, args, kwargs, acknowledgement):
        self._assert_owner_thread()
        try:
            async with self._command_lock:
                if self._state == _OwnerState.CLOSING:
                    raise RuntimeError("browser owner is closing")
                self._active_task = asyncio.current_task()
                handler = getattr(self, f"_op_{operation}")
                return await handler(*args, **kwargs)
        finally:
            if self._active_task is asyncio.current_task():
                self._active_task = None
            acknowledgement.set()

    def call(self, operation, *args, timeout=None, **kwargs):
        if threading.get_ident() == self._owner_thread_id:
            raise RuntimeError("recursive browser-owner submission is forbidden")
        loop = self._ensure_started()
        acknowledgement = threading.Event()
        future = asyncio.run_coroutine_threadsafe(
            self._execute(operation, args, kwargs, acknowledgement), loop
        )
        outer_timeout = _OWNER_CALL_TIMEOUT_S if timeout is None else timeout
        try:
            return future.result(timeout=outer_timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            acknowledgement.wait(_OWNER_CANCEL_TIMEOUT_S)
            self.close()
            raise _OwnerCallTimeout(
                f"browser operation timed out after {outer_timeout:g}s"
            ) from exc

    def _cancel_active(self):
        self._assert_owner_thread()
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()

    async def _close_on_loop(self):
        self._assert_owner_thread()
        async with self._command_lock:
            await self._shutdown_resources()
        self._loop.call_soon(self._loop.stop)

    def close(self):
        if threading.get_ident() == self._owner_thread_id:
            raise RuntimeError("browser owner cannot synchronously close itself")
        with self._condition:
            if self._state == _OwnerState.STOPPED:
                return
            if self._state == _OwnerState.FAILED and not (
                self._thread and self._thread.is_alive()
            ):
                self._state = _OwnerState.STOPPED
                self._condition.notify_all()
                self._publish()
                return
            previous_state = self._state
            loop = self._loop
            thread = self._thread
            self._closing_requested = True
            self._close_epoch += 1
            self._state = _OwnerState.CLOSING
            self._condition.notify_all()
        self._publish()
        if (
            previous_state in (_OwnerState.READY, _OwnerState.RECONNECTING)
            and loop is not None
            and not loop.is_closed()
        ):
            loop.call_soon_threadsafe(self._cancel_active)
            future = asyncio.run_coroutine_threadsafe(self._close_on_loop(), loop)
            try:
                future.result(timeout=_OWNER_SHUTDOWN_TIMEOUT_S)
            except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_OWNER_SHUTDOWN_TIMEOUT_S)
            if thread.is_alive():
                raise RuntimeError("browser owner thread did not terminate")
        self._publish()

    @staticmethod
    def _emitter_off(emitter, event, handler):
        if emitter is None or handler is None:
            return
        try:
            emitter.off(event, handler)
        except Exception:
            pass

    @staticmethod
    def _page_is_closed(page):
        try:
            return page is None or page.is_closed()
        except Exception:
            return True

    @staticmethod
    def _page_opener(page):
        try:
            opener = getattr(page, "opener", None)
            return None if callable(opener) or inspect.isawaitable(opener) else opener
        except Exception:
            return None

    def _schedule_opener_resolution(self, page, context, generation):
        candidate = getattr(page, "opener", None)
        if not callable(candidate):
            return

        async def resolve():
            try:
                opener = await candidate()
            except Exception:
                return
            if (
                context is self._registry_context
                and generation == self._registry_generation
                and id(page) in self._registry
            ):
                self._registry[id(page)]["opener"] = opener
                self._publish()

        asyncio.create_task(resolve())

    def _most_recent_live_page(self):
        live = [
            item for item in self._registry.values()
            if not self._page_is_closed(item["page"])
        ]
        return max(live, key=lambda item: item["order"])["page"] if live else None

    def _detach_page_handlers(self):
        for item in tuple(self._registry.values()):
            for event, handler in item["handlers"].items():
                self._emitter_off(item["page"], event, handler)

    def _reset_registry(self, detach=True):
        self._assert_owner_thread()
        if detach:
            if self._context_handlers is not None:
                self._emitter_off(
                    self._registry_context, "page", self._context_handlers.get("page")
                )
                self._emitter_off(
                    self._registry_context, "close", self._context_handlers.get("close")
                )
            self._detach_page_handlers()
        self._registry.clear()
        self._page_tombstones.clear()
        self._form_session.reset()
        self._active_page = None
        self._registry_context = None
        self._context_handlers = None
        self._registry_generation += 1
        self._publish()

    def _remove_page(self, page, generation):
        self._assert_owner_thread()
        if generation != self._registry_generation:
            return
        key = id(page)
        item = self._registry.pop(key, None)
        self._page_tombstones.add(key)
        self._form_session.invalidate_page(page)
        if item is None:
            return
        for event, handler in item["handlers"].items():
            self._emitter_off(page, event, handler)
        for other in self._registry.values():
            if other.get("opener") is page:
                other["opener"] = None
        if self._active_page is page:
            opener = item.get("opener")
            if (
                opener is not None
                and id(opener) in self._registry
                and not self._page_is_closed(opener)
            ):
                self._active_page = opener
            else:
                self._active_page = self._most_recent_live_page()
        self._publish()

    def _handle_frame_navigated(self, page, generation):
        self._assert_owner_thread()
        if generation != self._registry_generation or id(page) not in self._registry:
            return
        self._form_session.invalidate_page(page)
        self._publish()

    def _register_page(
        self, page, *, context, generation, opener=None, make_active=False
    ):
        self._assert_owner_thread()
        if page is None or self._page_is_closed(page):
            return None
        if (
            context is not self._registry_context
            or generation != self._registry_generation
            or id(page) in self._page_tombstones
        ):
            return None
        key = id(page)
        item = self._registry.get(key)
        if item is None:
            self._registry_sequence += 1
            handlers = {
                "popup": lambda popup: self._register_page(
                    popup,
                    context=context,
                    generation=generation,
                    opener=page,
                    make_active=True,
                ),
                "close": lambda *_args: self._remove_page(page, generation),
                "crash": lambda *_args: self._remove_page(page, generation),
                "framenavigated": lambda *_args: self._handle_frame_navigated(
                    page, generation
                ),
            }
            item = {
                "page": page,
                "order": self._registry_sequence,
                "opener": opener if opener is not None else self._page_opener(page),
                "handlers": handlers,
            }
            self._registry[key] = item
            if item["opener"] is None:
                self._schedule_opener_resolution(page, context, generation)
            for event, handler in handlers.items():
                try:
                    page.on(event, handler)
                except Exception:
                    pass
        elif opener is not None:
            item["opener"] = opener
        if make_active:
            if self._active_page is not None and self._active_page is not page:
                self._form_session.invalidate_page(self._active_page)
            self._active_page = page
        self._publish()
        return page

    def _handle_context_close(self, context, generation):
        self._assert_owner_thread()
        if context is not self._registry_context or generation != self._registry_generation:
            return
        self._reset_registry()
        if self._context is context:
            self._context = None
        self._publish()

    def _register_context(self, context):
        self._assert_owner_thread()
        if context is self._registry_context:
            return self._registry_generation
        self._reset_registry()
        self._registry_context = context
        generation = self._registry_generation
        handlers = {
            "page": lambda page: self._register_page(
                page,
                context=context,
                generation=generation,
                opener=self._page_opener(page),
                make_active=True,
            ),
            "close": lambda *_args: self._handle_context_close(context, generation),
        }
        self._context_handlers = handlers
        for event, handler in handlers.items():
            try:
                context.on(event, handler)
            except Exception:
                pass
        try:
            pages = list(context.pages)
        except Exception:
            pages = []
        for page in pages:
            self._register_page(
                page,
                context=context,
                generation=generation,
                opener=self._page_opener(page),
                make_active=False,
            )
        if self._active_page is None:
            self._active_page = self._most_recent_live_page()
        self._publish()
        return generation

    def _reconcile_pages(self, context):
        self._assert_owner_thread()
        generation = self._register_context(context)
        try:
            pages = list(context.pages)
        except Exception:
            self._handle_context_close(context, generation)
            return None
        ids = {id(page) for page in pages}
        self._page_tombstones.intersection_update(ids)
        for page in pages:
            self._register_page(
                page,
                context=context,
                generation=generation,
                opener=self._page_opener(page),
                make_active=False,
            )
        for key, item in tuple(self._registry.items()):
            if key not in ids or self._page_is_closed(item["page"]):
                self._remove_page(item["page"], generation)
        if (
            self._active_page is None
            or id(self._active_page) not in self._registry
            or self._page_is_closed(self._active_page)
        ):
            self._active_page = self._most_recent_live_page()
        self._publish()
        return self._active_page

    async def _ensure_context_page(self, context):
        page = self._reconcile_pages(context)
        if page is not None:
            return page
        page = await context.new_page()
        generation = self._register_context(context)
        return self._register_page(
            page,
            context=context,
            generation=generation,
            opener=None,
            make_active=True,
        ) or page

    def _reset_browser_registration(self):
        self._emitter_off(
            self._registry_browser,
            "disconnected",
            self._browser_disconnected_handler,
        )
        self._registry_browser = None
        self._browser_disconnected_handler = None

    def _handle_browser_disconnected(self, browser):
        self._assert_owner_thread()
        if browser is not self._registry_browser:
            return
        self._reset_registry()
        self._reset_browser_registration()
        if self._browser is browser:
            self._browser = None
            self._context = None
            self._active_browser_type = None
        if not self._disconnecting and self._state != _OwnerState.CLOSING:
            self._set_state(_OwnerState.RECONNECTING)
        self._publish()

    def _register_browser(self, browser):
        self._assert_owner_thread()
        if browser is None or browser is self._registry_browser:
            return
        self._reset_browser_registration()
        handler = lambda *_args: self._handle_browser_disconnected(browser)
        self._registry_browser = browser
        self._browser_disconnected_handler = handler
        try:
            browser.on("disconnected", handler)
        except Exception:
            pass

    def _is_bidi_alive(self):
        if self._context is not None:
            try:
                _ = self._context.pages
                return True
            except Exception:
                pass
        if self._browser is not None:
            try:
                return self._browser.is_connected()
            except Exception:
                return False
        return False

    async def _ensure_cdp_browser(self, binary=None):
        self._assert_owner_thread()
        if not _is_cdp_alive():
            ok, message = _launch_cdp_browser(binary)
            if not ok:
                raise RuntimeError(message)
        if self._browser is None or not self._browser.is_connected():
            self._set_state(_OwnerState.RECONNECTING)
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
        self._register_browser(self._browser)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else await self._browser.new_context()
        self._active_browser_type = "cdp"
        page = await self._ensure_context_page(self._context)
        self._set_state(_OwnerState.READY)
        return page

    async def _ensure_firefox_bidi(self):
        self._assert_owner_thread()
        if self._active_browser_type == "bidi" and self._is_bidi_alive():
            self._register_browser(self._browser)
            return await self._ensure_context_page(self._context)
        firefox = _detect_firefox()
        if not firefox:
            raise RuntimeError(
                "Firefox is not installed — looked for 'firefox'/'firefox-esr' "
                "in /usr/bin, /snap/bin, and $PATH"
            )
        os.makedirs(FIREFOX_PROFILE_DIR, exist_ok=True)
        stale = _processes_using_profile(FIREFOX_PROFILE_DIR)
        if stale:
            _terminate_processes(stale)
        self._set_state(_OwnerState.RECONNECTING)
        self._context = await self._playwright.firefox.launch_persistent_context(
            FIREFOX_PROFILE_DIR,
            headless=False,
            channel="moz-firefox",
            executable_path=firefox,
        )
        self._browser = self._context.browser
        self._active_browser_type = "bidi"
        self._register_browser(self._browser)
        self._register_context(self._context)
        page = await self._ensure_context_page(self._context)
        self._set_state(_OwnerState.READY)
        return page

    async def _ensure_browser(self):
        self._assert_owner_thread()
        if self._context is not None:
            page = self._reconcile_pages(self._context)
            if page is not None:
                return page
        if self._active_browser_type == "bidi":
            return await self._ensure_firefox_bidi()
        if (
            self._active_browser_type is None
            and not _is_cdp_alive()
            and _detect_chromium_browser() is None
            and _detect_firefox() is not None
        ):
            return await self._ensure_firefox_bidi()
        return await self._ensure_cdp_browser()

    async def _disconnect_active_browser(self):
        self._assert_owner_thread()
        browser, context = self._browser, self._context
        self._disconnecting = True
        self._reset_browser_registration()
        self._reset_registry()
        self._browser = None
        self._context = None
        self._active_browser_type = None
        try:
            if browser is not None:
                await browser.close()
            elif context is not None:
                await context.close()
        except Exception:
            pass
        finally:
            self._disconnecting = False
            self._publish()

    async def _shutdown_resources(self):
        self._assert_owner_thread()
        current = asyncio.current_task()
        active = self._active_task
        if active is not None and active is not current and not active.done():
            active.cancel()
            try:
                await active
            except BaseException:
                pass
        await self._disconnect_active_browser()
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            try:
                await playwright.stop()
            finally:
                self._driver_stop_count += 1
        self._publish()

    async def _screenshot_b64(self, page):
        try:
            png = await page.screenshot(type="png", full_page=False)
            return base64.b64encode(png).decode("ascii")
        except Exception as exc:
            print(f"[OSIA Browser] screenshot capture failed: {exc}")
            return None

    async def _result(self, text, page):
        return {"text": text, "screenshot": await self._screenshot_b64(page)}

    async def _page_url(self, page):
        if self._active_browser_type == "bidi":
            try:
                return await page.evaluate("location.href")
            except Exception:
                pass
        return page.url

    async def _after_interaction_page(self, fallback):
        try:
            await fallback.wait_for_timeout(_POPUP_EVENT_WINDOW_MS)
        except Exception:
            await asyncio.sleep(0)
        if self._context is not None:
            return self._reconcile_pages(self._context) or fallback
        return self._active_page or fallback

    async def _bidi_history_step(self, page, delta):
        before = await page.evaluate("location.href")
        await page.evaluate("history.back()" if delta < 0 else "history.forward()")
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            try:
                current = await page.evaluate("location.href")
            except Exception:
                continue
            if current != before:
                return current
        return None

    async def _op_navigate(self, url):
        url = _normalize_url(url)
        try:
            page = await self._ensure_browser()
        except Exception as exc:
            return f"ERROR: the browser did not open — {exc}"
        try:
            await page.goto(url, wait_until="commit", timeout=10000)
        except _TIMEOUT_ERRORS:
            _raise_browser_window(self._active_browser_type)
            return await self._result(
                f"Navigated to: {await self._page_url(page)} (still loading — "
                "page may still be loading resources)",
                page,
            )
        except Exception as exc:
            return f"ERROR navigating to {url}: {exc}"
        note = ""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            note = " (DOM still loading — page may still be loading resources)"
        try:
            await page.bring_to_front()
        except Exception:
            pass
        _raise_browser_window(self._active_browser_type)
        return await self._result(
            f"Navigated to: {await self._page_url(page)}{note}", page
        )

    async def _op_click(self, selector):
        try:
            page = await self._ensure_browser()
            await page.click(selector, timeout=5000)
            page = await self._after_interaction_page(page)
            return await self._result(f"Clicked element: {selector}", page)
        except Exception as exc:
            return f"ERROR clicking {selector}: {exc}"

    async def _op_fill(self, selector, value):
        try:
            page = await self._ensure_browser()
            await page.fill(selector, value, timeout=5000)
            return f"Filled {selector} with: {value}"
        except Exception as exc:
            return f"ERROR filling {selector}: {exc}"

    async def _op_read_page(self):
        try:
            page = await self._ensure_browser()
            text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            if not text:
                text = "(empty page)"
            if len(text) > 4000:
                text = text[:4000] + "\n... [TRUNCATED]"
            return f"Page content:\n{text}"
        except Exception as exc:
            return f"ERROR reading page: {exc}"

    async def _op_get_current_url(self):
        try:
            page = await self._ensure_browser()
            return f"Current URL: {await self._page_url(page)}"
        except Exception as exc:
            return f"ERROR reading current URL: {exc}"

    async def _op_scroll(self, direction="down", amount=600):
        direction = (direction or "down").lower()
        if direction not in ("up", "down"):
            return f"ERROR scroll: direction must be 'up' or 'down', got {direction!r}"
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return f"ERROR scroll: amount must be an integer, got {amount!r}"
        delta = -amount if direction == "up" else amount
        try:
            page = await self._ensure_browser()
            await page.evaluate(f"window.scrollBy(0, {delta})")
            return f"Scrolled {direction} by {abs(delta)}px"
        except Exception as exc:
            return f"ERROR scrolling: {exc}"

    async def _op_go_back(self):
        try:
            page = await self._ensure_browser()
            if self._active_browser_type == "bidi":
                url = await self._bidi_history_step(page, -1)
                if url is None:
                    return await self._result("No previous page in history.", page)
                return await self._result(f"Went back to: {url}", page)
            response = await page.go_back(timeout=5000)
            if response is None:
                return await self._result("No previous page in history.", page)
            return await self._result(f"Went back to: {page.url}", page)
        except Exception as exc:
            return f"ERROR going back: {exc}"

    async def _op_go_forward(self):
        try:
            page = await self._ensure_browser()
            if self._active_browser_type == "bidi":
                url = await self._bidi_history_step(page, 1)
                if url is None:
                    return await self._result("No forward page in history.", page)
                return await self._result(f"Went forward to: {url}", page)
            response = await page.go_forward(timeout=5000)
            if response is None:
                return await self._result("No forward page in history.", page)
            return await self._result(f"Went forward to: {page.url}", page)
        except Exception as exc:
            return f"ERROR going forward: {exc}"

    async def _op_open_firefox_bidi(self, url):
        try:
            if self._active_browser_type == "cdp":
                await self._disconnect_active_browser()
            page = await self._ensure_firefox_bidi()
        except Exception as exc:
            print(f"[OSIA Browser] Firefox BiDi launch failed: {exc}")
            await self._disconnect_active_browser()
            return None
        try:
            await page.bring_to_front()
        except Exception:
            pass
        _raise_browser_window(self._active_browser_type)
        if not url:
            return await self._result("Opened Firefox (driven over WebDriver BiDi)", page)
        try:
            await page.goto(
                _normalize_url(url), wait_until="domcontentloaded", timeout=20000
            )
            return await self._result(
                f"Opened Firefox (driven over WebDriver BiDi) at {page.url}", page
            )
        except Exception as exc:
            return await self._result(
                "Opened Firefox (driven over WebDriver BiDi) but navigating "
                f"to {url!r} failed: {exc}",
                page,
            )

    async def _op_open_cdp_browser(self, binary, url):
        reused = _is_cdp_alive()
        try:
            if self._active_browser_type == "bidi":
                await self._disconnect_active_browser()
            if not reused:
                ok, message = _launch_cdp_browser(binary)
                if not ok:
                    print(f"[OSIA Browser] CDP launch failed: {message}")
                    return None
            page = await self._ensure_cdp_browser(binary)
        except Exception as exc:
            print(f"[OSIA Browser] CDP connect failed: {exc}")
            return None
        label = _canonical_browser_name(binary)
        verb = "Reusing existing" if reused else "Opened"
        try:
            await page.bring_to_front()
        except Exception:
            pass
        _raise_browser_window(self._active_browser_type)
        if not url:
            return await self._result(
                f"{verb} {label} window (CDP on port {CDP_PORT})", page
            )
        try:
            await page.goto(
                _normalize_url(url), wait_until="domcontentloaded", timeout=20000
            )
            return await self._result(
                f"{verb} {label} window at {await self._page_url(page)}", page
            )
        except Exception as exc:
            return await self._result(
                f"{verb} {label} window but navigating to {url!r} failed: {exc}",
                page,
            )

    async def _op_open_new_tab(self, url):
        url = _normalize_url(url)
        try:
            await self._ensure_browser()
            page = await self._context.new_page()
            generation = self._register_context(self._context)
            self._register_page(
                page,
                context=self._context,
                generation=generation,
                opener=None,
                make_active=True,
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            self._active_page = page
            self._publish()
            return f"Opened new tab: {page.url}"
        except Exception as exc:
            return f"ERROR opening new tab {url}: {exc}"

    async def _resolve_clickable(self, page, target_norm, target_tokens, desktop):
        best = {"loc": None, "score": 0.0, "label": ""}

        async def consider(locator, primary, minimum):
            try:
                count = await locator.count()
            except Exception:
                return
            for index in range(min(count, _DOM_MAX_MATCHES)):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    label = (await candidate.evaluate(_DOM_LABEL_JS) or "").strip()
                except Exception:
                    label = ""
                if not label:
                    continue
                score = desktop._label_score(target_norm, target_tokens, label)
                if primary and score > 0:
                    score += 0.05
                if score < minimum:
                    continue
                if (
                    score > best["score"]
                    or (
                        score == best["score"]
                        and best["loc"] is not None
                        and 0 < len(label) < len(best["label"])
                    )
                ):
                    best.update(loc=candidate, score=score, label=label)

        for role in _DOM_CLICK_ROLES:
            try:
                locator = page.get_by_role(role, name=target_norm)
            except Exception:
                continue
            await consider(
                locator, role in _DOM_PRIMARY_ROLES, desktop._UIA_MIN_SCORE
            )
            if best["score"] >= 1.0:
                break
        if best["loc"] is None:
            try:
                await consider(page.get_by_text(target_norm), False, 1.0)
            except Exception:
                pass
        return best["loc"], best["label"], best["score"]

    async def _op_find_and_click(self, text):
        if not text:
            return "ERROR find_and_click: text is required"
        try:
            page = await self._ensure_browser()
        except Exception as exc:
            return f"ERROR clicking element with text {text!r}: {exc}"
        locator, label = None, ""
        try:
            from osia.tools import desktop_control as desktop

            target_norm = desktop._normalize_label(text)
            if target_norm:
                locator, label, _score = await self._resolve_clickable(
                    page, target_norm, frozenset(target_norm.split()), desktop
                )
        except Exception:
            locator = None
        try:
            try:
                before = await page.screenshot(type="png", full_page=False)
            except Exception:
                before = None
            what = None
            if locator is not None:
                try:
                    await locator.click(timeout=5000)
                    what = f"{text!r} (matched {label!r})" if label else f"{text!r}"
                except Exception:
                    what = None
            if what is None:
                await page.get_by_text(text, exact=False).first.click(timeout=5000)
                what = f"{text!r}"
            page = await self._after_interaction_page(page)
            try:
                after = await page.screenshot(type="png", full_page=False)
            except Exception:
                after = None
            note = ""
            if before is not None and after is not None and before == after:
                note = " — but the page did not visibly change; the click may have missed."
            shot = (
                base64.b64encode(after).decode("ascii")
                if after is not None
                else await self._screenshot_b64(page)
            )
            return {
                "text": f"Clicked element with text: {what}.{note}",
                "screenshot": shot,
            }
        except Exception as exc:
            return f"ERROR clicking element with text {text!r}: {exc}"

    async def _op_dom_click(self, description):
        if not description or not str(description).strip():
            return None
        description = str(description).strip()
        try:
            page = await self._ensure_browser()
            from osia.tools import desktop_control as desktop

            target_norm = desktop._normalize_label(description)
            if not target_norm:
                return None
            locator, label, _score = await self._resolve_clickable(
                page, target_norm, frozenset(target_norm.split()), desktop
            )
            if locator is None:
                return None
            try:
                await locator.click(timeout=5000)
            except Exception:
                return None
            page = await self._after_interaction_page(page)
            try:
                url = await self._page_url(page)
            except Exception:
                url = ""
            where = f" on {url}" if url else ""
            matched = f" (matched {label!r})" if label else ""
            return f'Clicked "{description}" via the page DOM{matched}{where}.'
        except Exception:
            return None

    async def _op_wait_for_load(self):
        try:
            page = await self._ensure_browser()
        except Exception as exc:
            return f"ERROR waiting for load: {exc}"
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
            return f"Page loaded: {await self._page_url(page)}"
        except _TIMEOUT_ERRORS:
            return (
                f"Page loaded (timeout): {await self._page_url(page)} — "
                "page may still be loading resources"
            )
        except Exception as exc:
            return f"ERROR waiting for load: {exc}"

    async def _op_get_page_text(self):
        try:
            page = await self._ensure_browser()
            text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            if not text:
                return "(empty page)"
            if len(text) > 8000:
                text = text[:8000] + "\n... [TRUNCATED]"
            return text
        except Exception as exc:
            return f"ERROR reading page: {exc}"

    async def _op_scroll_down(self, amount=None):
        try:
            page = await self._ensure_browser()
            if amount is None:
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                return "Scrolled down one viewport"
            try:
                value = int(amount)
            except (TypeError, ValueError):
                return f"ERROR scroll_down: amount must be an integer, got {amount!r}"
            await page.evaluate(f"window.scrollBy(0, {value})")
            return f"Scrolled down {value}px"
        except Exception as exc:
            return f"ERROR scrolling: {exc}"

    async def _op_type_text(self, selector, text):
        try:
            page = await self._ensure_browser()
            await page.click(selector, timeout=5000)
            await page.keyboard.type(text)
            return f"Typed into {selector}: {text}"
        except Exception as exc:
            return f"ERROR typing into {selector}: {exc}"

    async def _op_press_key(self, key):
        if not key:
            return "ERROR press_key: key is required"
        try:
            page = await self._ensure_browser()
            await page.keyboard.press(key)
            return f"Pressed key: {key}"
        except Exception as exc:
            return f"ERROR pressing key {key}: {exc}"

    async def _selector_present(self, page, selector):
        value = str(selector).strip()
        if not value:
            return False
        try:
            if await page.query_selector(value) is not None:
                return True
        except Exception:
            pass
        try:
            from osia.tools import desktop_control as desktop

            target_norm = desktop._normalize_label(value)
            if not target_norm:
                return False
            locator, _label, score = await self._resolve_clickable(
                page, target_norm, frozenset(target_norm.split()), desktop
            )
            return locator is not None and score > 0
        except Exception:
            return False

    async def _op_web_state(self, expect_url=None, expect_text=None, expect_selector=None):
        if self._context is None or self._active_page is None:
            return {"available": False, "reason": "no live page"}
        page = self._reconcile_pages(self._context)
        if page is None:
            return {"available": False, "reason": "no live page"}
        try:
            url = await self._page_url(page)
        except Exception:
            url = None
        checks = {}
        if expect_url:
            checks["url_contains"] = bool(
                url and str(expect_url).strip().lower() in url.lower()
            )
        if expect_text:
            try:
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
            except Exception:
                text = ""
            checks["text_contains"] = bool(
                text and str(expect_text).strip().lower() in text.lower()
            )
        if expect_selector:
            checks["selector_found"] = await self._selector_present(
                page, expect_selector
            )
        return {"available": True, "url": url, "checks": checks}

    async def _op_inspect_form_fields(
        self,
        include_hidden=False,
        include_disabled=False,
        max_fields=FORM_MAX_FIELDS_DEFAULT,
    ):
        try:
            page = await self._ensure_browser()
        except Exception:
            return 'ERROR: {"category":"page_unavailable","ok":false}'
        return await self._form_session.inspect(
            page=page,
            context_generation=self._registry_generation,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            max_fields=max_fields,
        )

    async def _op_fill_form_field(self, field_ref, value):
        try:
            page = await self._ensure_browser()
        except Exception:
            return 'ERROR: {"category":"page_unavailable","ok":false}'
        return await self._form_session.fill(
            page=page,
            context_generation=self._registry_generation,
            field_ref=field_ref,
            value=value,
        )


_owner = _AsyncBrowserOwner()




# ── OS-window raise (Windows) ──────────────────────────────────────────────

# Cap on the foreground confirm-poll when raising the browser's OS window, so a
# refused focus can only ever add a short, bounded pause to a navigation.
_RAISE_FOCUS_TIMEOUT_S = 2.0


def _raise_browser_window(browser_type: str | None = None) -> None:
    """Best-effort, Windows-only: bring the automated browser's OS window to the
    foreground after a successful connect/navigate.

    ``page.bring_to_front()`` (kept, called just before this) only activates the
    TAB inside the browser — it never raises the OS window above other apps.
    Browser-only flows never needed more (``page.screenshot()`` is a CDP/BiDi
    capture, independent of what is in front), but when the agent mixes the page
    tools with the desktop tools, a background browser window means
    take_screenshot captures — and desktop_click/desktop_type act on — whatever
    app sits in front instead.

    The window is identified by the process actually holding OSIA's dedicated
    browser profile (``_processes_using_profile`` — never brand guessing), and it
    is raised only when the matched window BELONGS to that process, so the user's
    personal browser window (same brand, different profile) is never hijacked; if
    the user's own same-brand window sits above OSIA's, this is a no-op. Raising
    reuses desktop_control.find_app_window / focus_app_window — the same
    machinery open_application uses. Never raises; a failed raise never blocks
    or fails the navigation.
    """
    if sys.platform != "win32":
        return
    try:
        from osia.tools import desktop_control as _dc

        profile = FIREFOX_PROFILE_DIR if browser_type == "bidi" else OSIA_PROFILE_DIR
        pids, bases = set(), []
        for proc in _processes_using_profile(profile):
            try:
                pids.add(int(proc.pid))
                base = os.path.splitext(proc.info.get("name") or "")[0].strip().lower()
            except Exception:
                continue
            if base and base not in bases:
                bases.append(base)
        if not pids:
            return
        fg = _dc.foreground_snapshot()
        if fg is not None and fg[2] in pids:
            return  # our browser window is already the foreground window
        for base in bases:
            win = _dc.find_app_window(base)  # topmost visible window of that exe
            if win is not None and win[2] in pids:
                _dc.focus_app_window(win, timeout=_RAISE_FOCUS_TIMEOUT_S)
                return
    except Exception:
        pass


# ── URL helper ─────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://", "file://", "about:")):
        return "https://" + url
    return url


# ── Existing tools (now backed by the real browser via CDP) ────────────────



# ── WOR-84: web DOM-first clicking ──────────────────────────────────────────
# When OSIA's OWN browser is the foreground window and a live Playwright page is
# driving it, a click intent the model expresses in plain words ("the Suivant
# button") is resolved and clicked THROUGH the DOM — a real ``locator.click()``
# in the visible browser, pixel-exact, no coordinate guessing. ``dom_click`` is
# called by ``agent.tool_desktop_click`` BEFORE its vision locate and WOR-46 UIA
# branch; it returns a success string on a confident DOM resolution, or ``None``
# on ANY miss so the caller falls through to those native paths unchanged. It
# submits the work against the live active page on the single async owner loop;
# it never opens a second CDP connection, launches another browser, or touches
# the profile-isolation code.

# Interactive ARIA roles a "click" intent can target, most-clickable first.
_DOM_CLICK_ROLES = (
    "button", "link", "menuitem", "tab", "option",
    "checkbox", "radio", "switch", "treeitem", "combobox", "textbox",
)
# Roles that are *primary* click targets — a tiny score bonus breaks a near-tie
# toward one of these (mirrors the UIA _UIA_CLICKABLE_TYPES tie-break).
_DOM_PRIMARY_ROLES = frozenset({"button", "link", "menuitem", "tab", "option"})
# Cap matches scored per role / text query so a generic target can't trigger a
# huge per-element IPC walk; a real control is among the first few matches.
_DOM_MAX_MATCHES = 12
# JS approximating an element's ACCESSIBLE NAME for fuzzy scoring, in the order
# the platform computes it: aria-label / aria-labelledby, then an associated
# <label>, then visible text, then placeholder — and only LAST the field's value
# or title. Reading `value` before the label would score a text input by what the
# user TYPED instead of its name (Finding WOR-84/4); keeping it last fixes that
# while still letting an <input type=submit value="Send"> button match on value.
_DOM_LABEL_JS = (
    "el => { const t = s => (s || '').trim();"
    " let n = t(el.getAttribute && el.getAttribute('aria-label')); if (n) return n;"
    " const lb = el.getAttribute && el.getAttribute('aria-labelledby');"
    " if (lb && el.ownerDocument) { const p = lb.split(/\\s+/)"
    "   .map(id => { const e = el.ownerDocument.getElementById(id);"
    "                return e ? t(e.innerText || e.textContent) : ''; })"
    "   .filter(Boolean); if (p.length) return p.join(' '); }"
    " if (el.labels && el.labels.length) { const s = t(el.labels[0].innerText"
    "   || el.labels[0].textContent); if (s) return s; }"
    " let s = t(el.innerText) || t(el.textContent); if (s) return s;"
    " s = t(el.getAttribute && el.getAttribute('placeholder')); if (s) return s;"
    " s = t(el.value); if (s) return s;"
    " s = t(el.getAttribute && el.getAttribute('title')); if (s) return s;"
    " return ''; }"
)




def _foreground_is_osia_browser(browser_type: str | None = None) -> bool:
    """True only when the OS foreground window belongs to OSIA's OWN automated
    browser — the one holding OSIA's dedicated profile. This is the half of the
    WOR-84 DOM-first gate that guarantees a DOM click on the owner's live active
    page acts on the window the user is actually looking at.

    It is NOT enough that the foreground is *a* browser: the user's personal
    Chrome/Brave/Firefox shares the same process name, and clicking OSIA's
    possibly-backgrounded automated page while the user's own window is in front
    would click the wrong surface. So we require the foreground
    window's PID to be one of the processes whose command line references OSIA's
    dedicated profile dir (``_processes_using_profile`` — the same identity scan
    the launcher uses), for the profile of the ACTIVE backend (Firefox/BiDi vs
    Chromium/CDP).

    Conservative by design: when no reliable PID is exposed (e.g. Wayland, where
    ``_foreground_window`` returns pid 0) or psutil is unavailable, OSIA's own
    browser cannot be positively identified, so this returns False and the caller
    uses the vision path (unchanged pre-WOR-84 behavior — never a wrong click).
    Never raises.
    """
    if not _PSUTIL_OK:
        return False
    try:
        from osia.tools import desktop_control as _dc
        fw = _dc._foreground_window()
    except Exception:
        return False
    if not fw:
        return False
    _wid, _title, pid = fw
    if not pid:
        return False
    profile = FIREFOX_PROFILE_DIR if browser_type == "bidi" else OSIA_PROFILE_DIR
    try:
        own_pids = {p.pid for p in _processes_using_profile(profile)}
        return int(pid) in own_pids
    except Exception:
        return False




# ── Shutdown ───────────────────────────────────────────────────────────────



# ── Synchronous public bridge to the dedicated async owner ────────────────

def _owner_error(prefix: str, error: Exception) -> str:
    return f"ERROR {prefix}: {error}"


def navigate(url: str):
    try:
        return _owner.call("navigate", url)
    except Exception as exc:
        return f"ERROR: the browser did not open — {exc}"


def click(selector: str):
    try:
        return _owner.call("click", selector)
    except Exception as exc:
        return _owner_error(f"clicking {selector}", exc)


def fill(selector: str, value: str) -> str:
    try:
        return _owner.call("fill", selector, value)
    except Exception as exc:
        return _owner_error(f"filling {selector}", exc)


def read_page() -> str:
    try:
        return _owner.call("read_page")
    except Exception as exc:
        return _owner_error("reading page", exc)


def get_current_url() -> str:
    try:
        return _owner.call("get_current_url")
    except Exception as exc:
        return _owner_error("reading current URL", exc)


def scroll(direction: str = "down", amount: int = 600) -> str:
    try:
        return _owner.call("scroll", direction, amount)
    except Exception as exc:
        return _owner_error("scrolling", exc)


def go_back():
    try:
        return _owner.call("go_back")
    except Exception as exc:
        return _owner_error("going back", exc)


def go_forward():
    try:
        return _owner.call("go_forward")
    except Exception as exc:
        return _owner_error("going forward", exc)


def open_browser(name: str, url: str | None = None):
    binary = resolve_browser_name(name)
    if not binary:
        known = ", ".join(sorted(_BROWSER_NAME_MAP))
        return f"ERROR: browser {name!r} is not installed (known browsers: {known})"
    note = ""
    try:
        if _canonical_browser_name(binary) == "firefox":
            result = _owner.call("open_firefox_bidi", url)
            note = " (WebDriver BiDi unavailable — page tools cannot drive this window)"
        else:
            result = _owner.call("open_cdp_browser", binary, url)
            note = " (CDP unavailable — page tools cannot drive this window)"
    except Exception as exc:
        print(f"[OSIA Browser] automated browser launch failed: {exc}")
        result = None
    if result is not None:
        return result
    cmd = [binary, *_profile_args(binary)]
    if url:
        cmd.append(_normalize_url(url))
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detach_kwargs(),
        )
    except Exception as exc:
        return (
            "ERROR: the browser did not open — failed to launch "
            f"{os.path.basename(binary)}: {exc}"
        )
    suffix = f" at {_normalize_url(url)}" if url else ""
    return f"Opened {os.path.basename(binary)}{suffix}{note}"


def open_new_tab(url: str) -> str:
    try:
        return _owner.call("open_new_tab", url)
    except Exception as exc:
        return _owner_error(f"opening new tab {_normalize_url(url)}", exc)


def find_and_click(text: str):
    try:
        return _owner.call("find_and_click", text)
    except Exception as exc:
        return _owner_error(f"clicking element with text {text!r}", exc)


def dom_click(description: str):
    snapshot = _owner.snapshot()
    if not snapshot.get("activeUrl"):
        return None
    if not _foreground_is_osia_browser(snapshot.get("backend")):
        return None
    try:
        return _owner.call("dom_click", description)
    except Exception:
        return None


def wait_for_load() -> str:
    try:
        return _owner.call("wait_for_load")
    except Exception as exc:
        return _owner_error("waiting for load", exc)


def get_page_text() -> str:
    try:
        return _owner.call("get_page_text")
    except Exception as exc:
        return _owner_error("reading page", exc)


def scroll_down(amount: int | None = None) -> str:
    try:
        return _owner.call("scroll_down", amount)
    except Exception as exc:
        return _owner_error("scrolling", exc)


def type_text(selector: str, text: str) -> str:
    try:
        return _owner.call("type_text", selector, text)
    except Exception as exc:
        return _owner_error(f"typing into {selector}", exc)


def press_key(key: str) -> str:
    try:
        return _owner.call("press_key", key)
    except Exception as exc:
        return _owner_error(f"pressing key {key}", exc)


def inspect_form_fields(
    include_hidden: bool = False,
    include_disabled: bool = False,
    max_fields: int = FORM_MAX_FIELDS_DEFAULT,
) -> str:
    """Return bounded semantic metadata for fields on the authoritative page."""
    try:
        return _owner.call(
            "inspect_form_fields",
            include_hidden,
            include_disabled,
            max_fields,
            timeout=10.0,
        )
    except Exception:
        return 'ERROR: {"category":"inspection_failed","ok":false}'


def fill_form_field(field_ref: str, value) -> str:
    """Fill one opaque field reference and prove the resulting state."""
    try:
        return _owner.call(
            "fill_form_field", field_ref, value, timeout=12.0,
        )
    except Exception:
        return 'ERROR: {"category":"fill_failed","ok":false}'


def get_web_state(expect_url=None, expect_text=None, expect_selector=None) -> dict:
    """Read browser state without exposing a Playwright object to the caller."""
    snapshot = _owner.snapshot()
    if not snapshot.get("activeUrl"):
        return {"available": False, "reason": "no live page"}
    if not _foreground_is_osia_browser(snapshot.get("backend")):
        return {
            "available": False,
            "reason": "OSIA is not driving its own browser in the foreground",
        }
    try:
        return _owner.call(
            "web_state", expect_url, expect_text, expect_selector
        )
    except Exception as exc:
        return {"available": False, "reason": f"page read failed: {exc}"}


def _browser_owner_snapshot() -> dict:
    """Primitive-only diagnostics used by focused lifecycle tests."""
    return _owner.snapshot()


def _dom_gate_ok() -> bool:
    snapshot = _owner.snapshot()
    return bool(
        snapshot.get("activeUrl")
        and _foreground_is_osia_browser(snapshot.get("backend"))
    )


def close_browser():
    try:
        _owner.close()
    except Exception as exc:
        print(f"[OSIA Browser] owner shutdown failed: {exc}")
