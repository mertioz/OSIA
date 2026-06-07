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
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

# Timeout exceptions that mean "the page is just slow" — never a hard failure.
# Playwright's TimeoutError is caught alongside the builtin for safety across
# Playwright versions (newer versions subclass the builtin, older don't).
_TIMEOUT_ERRORS = (PlaywrightTimeoutError, TimeoutError)

# ── Configuration ──────────────────────────────────────────────────────────

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"

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

_playwright = None
_browser = None        # Playwright Browser (CDP-connected Chromium, or BiDi-launched Firefox)
_context = None        # Playwright BrowserContext
_page = None           # Active Playwright Page
# Which protocol the page tools are currently driving:
#   "cdp"  -> Chromium-based browser connected over the Chrome DevTools Protocol
#   "bidi" -> Firefox launched through Playwright over WebDriver BiDi
#   None   -> no browser connected yet (next tool call picks one)
_active_browser_type = None
_lock = threading.Lock()
# Launch-in-flight flags. They prevent the race where two tool calls (e.g.
# the agent calling open_browser twice in one batch) both see "no browser
# running" before the first launch finishes, and each spawn a window.
_cdp_launching = False
_firefox_launching = False


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


def _wait_for_cdp(timeout: float) -> bool:
    """Poll until a CDP debugger responds on ``CDP_PORT`` or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_cdp_alive():
            return True
        time.sleep(0.2)
    return False


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
        if _wait_for_cdp(15.0):
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

        # Poll for the debugger socket — Chromium typically needs ~0.5-2s.
        if _wait_for_cdp(10.0):
            return True, f"launched {os.path.basename(browser)} with CDP on port {CDP_PORT}"
        return False, (
            f"launched {browser} but CDP did not come online on port {CDP_PORT} within 10s"
        )
    finally:
        _cdp_launching = False


def _ensure_browser():
    """Ensure the active automated browser is running and return an active
    Page. Recovers transparently if the previously cached page was closed
    (e.g. the user closed the tab).

    Dispatches on ``_active_browser_type``:
      - "bidi"        -> Firefox launched through Playwright (WebDriver BiDi)
      - "cdp" / None  -> Chromium browser connected over CDP (the default);
                         if no CDP-capable browser exists but Firefox does,
                         falls back to Firefox over BiDi.

    The returned Page exposes the same Playwright API for both backends, so
    every page tool works regardless of which browser type is active.
    """
    global _page

    # Fast path: existing page is still alive.
    if _page is not None:
        try:
            _ = _page.url  # raises if the page was closed
            if not _page.is_closed():
                return _page
            _page = None  # fall through to reconnect / re-acquire
        except Exception:
            _page = None  # fall through to reconnect / re-acquire

    if _active_browser_type == "bidi":
        return _ensure_firefox_bidi()
    if (
        _active_browser_type is None
        and not _is_cdp_alive()
        and _detect_chromium_browser() is None
        and _detect_firefox() is not None
    ):
        # No CDP browser running or installed, but Firefox is available:
        # drive Firefox over WebDriver BiDi instead of erroring out.
        return _ensure_firefox_bidi()
    return _ensure_cdp_browser()


def _ensure_cdp_browser():
    """Ensure the CDP browser is running, connect Playwright if needed,
    and return an active Page.
    """
    global _playwright, _browser, _context, _page, _active_browser_type

    # Make sure a CDP-enabled browser process is running.
    if not _is_cdp_alive():
        ok, msg = _launch_cdp_browser()
        if not ok:
            raise RuntimeError(msg)

    # Start Playwright once, connect over CDP every time we lost the browser.
    if _playwright is None:
        _playwright = sync_playwright().start()
    if _browser is None or not getattr(_browser, "is_connected", lambda: False)():
        _browser = _playwright.chromium.connect_over_cdp(CDP_URL)

    # CDP exposes the existing browser context(s); use the first one.
    contexts = _browser.contexts
    _context = contexts[0] if contexts else _browser.new_context()

    # Reuse an existing page if there is one; otherwise open a fresh tab.
    pages = _context.pages
    _page = pages[-1] if pages else _context.new_page()
    _active_browser_type = "cdp"
    return _page


# ── Firefox over WebDriver BiDi ────────────────────────────────────────────

def _is_bidi_alive() -> bool:
    """Return True if the BiDi-launched Firefox is still running and connected.

    The context check comes first: ``launch_persistent_context()`` may hand
    back a ``Browser`` whose ``is_connected()`` is unreliable (or ``None``)
    on some Playwright versions, and treating a live Firefox as dead is what
    causes duplicate windows.
    """
    if _context is not None:
        try:
            _ = _context.pages  # raises once the context is closed
            return True
        except Exception:
            pass
    if _browser is not None:
        try:
            return _browser.is_connected()
        except Exception:
            return False
    return False


def _ensure_firefox_bidi():
    """Ensure the Playwright-launched Firefox (WebDriver BiDi) is running and
    return an active Page. (Re)launches Firefox if it isn't running or was
    closed by the user.

    NEVER launches a second instance: if Firefox is already open and
    controlled (``_active_browser_type == "bidi"`` and the context is alive)
    the existing window is reused, and if another launch is already in
    flight (``_firefox_launching``) this call waits for it instead of
    spawning its own Firefox process.
    """
    global _playwright, _browser, _context, _page, _active_browser_type
    global _firefox_launching

    if _playwright is None:
        _playwright = sync_playwright().start()

    # Reuse: Firefox is already open and controlled over BiDi.
    if _active_browser_type == "bidi" and _is_bidi_alive():
        pages = _context.pages
        _page = pages[-1] if pages else _context.new_page()
        return _page

    if not _is_bidi_alive():
        # Another call is already launching Firefox — wait for it to finish
        # rather than racing it with a second window.
        if _firefox_launching:
            deadline = time.time() + 30.0
            while time.time() < deadline and _firefox_launching:
                time.sleep(0.2)
            if _is_bidi_alive():
                pages = _context.pages
                _page = pages[-1] if pages else _context.new_page()
                return _page
            raise RuntimeError(
                "another Firefox BiDi launch was in progress but never came online"
            )

        firefox = _detect_firefox()
        if not firefox:
            raise RuntimeError(
                "Firefox is not installed — looked for 'firefox'/'firefox-esr' "
                "in /usr/bin, /snap/bin, and $PATH"
            )
        os.makedirs(FIREFOX_PROFILE_DIR, exist_ok=True)
        _firefox_launching = True
        try:
            # launch_persistent_context() is Playwright's supported way to run
            # Firefox with `--profile <dir>` (passing --profile via args= is
            # rejected by Playwright), so cookies / logins persist between OSIA
            # uses. channel="moz-firefox" selects the WebDriver BiDi protocol
            # against the stock system Firefox resolved by _detect_firefox()
            # instead of Playwright's bundled (Juggler-patched) build.
            _context = _playwright.firefox.launch_persistent_context(
                FIREFOX_PROFILE_DIR,
                headless=False,
                channel="moz-firefox",
                executable_path=firefox,
            )
            _browser = _context.browser  # may be None on older Playwright versions
            _active_browser_type = "bidi"
        finally:
            _firefox_launching = False

    # Reuse an existing page if there is one; otherwise open a fresh tab.
    pages = _context.pages
    _page = pages[-1] if pages else _context.new_page()
    return _page


def _disconnect_active_browser():
    """Drop the current Playwright browser connection and reset the
    automation state. Must be called while holding ``_lock``.

    For a CDP connection this only disconnects — the Chromium browser keeps
    running. For a BiDi-launched Firefox this quits the browser (OSIA owns
    that process); its cookies / sessions persist in the OSIA Firefox profile.
    """
    global _browser, _context, _page, _active_browser_type
    try:
        if _browser is not None:
            _browser.close()
        elif _context is not None:
            _context.close()
    except Exception:
        pass
    _browser = None
    _context = None
    _page = None
    _active_browser_type = None


# ── Screenshot helper ──────────────────────────────────────────────────────

def _screenshot_b64(page) -> str | None:
    """Capture a viewport PNG of ``page`` and return it as a base64 string.
    Returns ``None`` on failure so callers can degrade gracefully — a
    missing screenshot must never break a successful navigation tool.
    """
    try:
        png = page.screenshot(type="png", full_page=False)
        return base64.b64encode(png).decode("ascii")
    except Exception as e:
        print(f"[OSIA Browser] screenshot capture failed: {e}")
        return None


def _result(text: str, page) -> dict:
    """Standard success envelope for navigation tools: text + screenshot
    so the agent can see the post-action page state.
    """
    return {"text": text, "screenshot": _screenshot_b64(page)}


def _page_url(page) -> str:
    """Return the page's current URL.

    On the BiDi backend ``page.url`` goes stale after a history traversal
    (Firefox doesn't emit the navigation events Playwright uses to track
    those), so ask the page itself. On CDP this is exactly ``page.url``.
    """
    if _active_browser_type == "bidi":
        try:
            return page.evaluate("location.href")
        except Exception:
            pass
    return page.url


# ── URL helper ─────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://", "file://", "about:")):
        return "https://" + url
    return url


# ── Existing tools (now backed by the real browser via CDP) ────────────────

def navigate(url: str):
    """Navigate the real browser to ``url``. Launches Brave/Chromium with
    ``--remote-debugging-port=9222`` if not already running, then drives
    the page via CDP. Returns ``{"text": ..., "screenshot": ...}`` on
    success so the agent can visually verify the page that loaded.

    The navigation waits only until it commits (response received), then
    gives the DOM a short window (3s) to become ready. A slow page is NOT
    an error and never blocks the agent: the result is returned right away
    and the page keeps loading in the background, so there is no need to
    call ``wait_for_load()`` after navigating.
    """
    url = _normalize_url(url)
    with _lock:
        try:
            page = _ensure_browser()
        except Exception as e:
            return f"ERROR navigating to {url}: {e}"
        try:
            page.goto(url, wait_until="commit", timeout=10000)
        except _TIMEOUT_ERRORS:
            # Slow server — the navigation keeps going in the browser.
            return _result(
                f"Navigated to: {_page_url(page)} (still loading — "
                "page may still be loading resources)",
                page,
            )
        except Exception as e:
            return f"ERROR navigating to {url}: {e}"
        # Built-in short wait so the agent never has to wait manually after
        # navigate: up to 3s for the DOM, then continue regardless.
        note = ""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            note = " (DOM still loading — page may still be loading resources)"
        try:
            page.bring_to_front()
        except Exception:
            pass
        return _result(f"Navigated to: {_page_url(page)}{note}", page)


def click(selector: str):
    """Click an element matching a CSS selector. Returns a screenshot of
    the post-click state so the agent can confirm the click landed.
    """
    with _lock:
        try:
            page = _ensure_browser()
            page.click(selector, timeout=5000)
            return _result(f"Clicked element: {selector}", page)
        except Exception as e:
            return f"ERROR clicking {selector}: {e}"


def fill(selector: str, value: str) -> str:
    """Fill an input element matching a CSS selector with a value."""
    with _lock:
        try:
            page = _ensure_browser()
            page.fill(selector, value, timeout=5000)
            return f"Filled {selector} with: {value}"
        except Exception as e:
            return f"ERROR filling {selector}: {e}"


def read_page() -> str:
    """Return the visible text content of the current page (truncated to 4000 chars)."""
    with _lock:
        try:
            page = _ensure_browser()
            text = page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            if not text:
                text = "(empty page)"
            if len(text) > 4000:
                text = text[:4000] + "\n... [TRUNCATED]"
            return f"Page content:\n{text}"
        except Exception as e:
            return f"ERROR reading page: {e}"


def get_current_url() -> str:
    """Return the URL of the active page."""
    with _lock:
        try:
            page = _ensure_browser()
            return f"Current URL: {_page_url(page)}"
        except Exception as e:
            return f"ERROR reading current URL: {e}"


def scroll(direction: str = "down", amount: int = 600) -> str:
    """Scroll the active page. ``direction`` is 'up' or 'down'; ``amount`` is pixels."""
    direction = (direction or "down").lower()
    if direction not in ("up", "down"):
        return f"ERROR scroll: direction must be 'up' or 'down', got {direction!r}"
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return f"ERROR scroll: amount must be an integer, got {amount!r}"
    dy = -amount if direction == "up" else amount
    with _lock:
        try:
            page = _ensure_browser()
            page.evaluate(f"window.scrollBy(0, {dy})")
            return f"Scrolled {direction} by {abs(dy)}px"
        except Exception as e:
            return f"ERROR scrolling: {e}"


def _bidi_history_step(page, delta: int) -> str | None:
    """History navigation for the BiDi backend.

    Firefox over BiDi performs history traversals but doesn't emit the
    navigation events Playwright waits for, so ``page.go_back()`` /
    ``page.go_forward()`` time out. Drive history from page JS instead and
    poll ``location.href`` until it changes.

    Returns the new URL, or ``None`` if the URL never changed (no history
    entry in that direction).
    """
    before = page.evaluate("location.href")
    page.evaluate("history.back()" if delta < 0 else "history.forward()")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            current = page.evaluate("location.href")
        except Exception:
            continue  # document mid-swap; retry
        if current != before:
            return current
    return None


def go_back():
    """Navigate back in the active page's history. Returns a screenshot
    of the resulting page state.
    """
    with _lock:
        try:
            page = _ensure_browser()
            if _active_browser_type == "bidi":
                url = _bidi_history_step(page, -1)
                if url is None:
                    return _result("No previous page in history.", page)
                return _result(f"Went back to: {url}", page)
            response = page.go_back(timeout=5000)
            if response is None:
                return _result("No previous page in history.", page)
            return _result(f"Went back to: {page.url}", page)
        except Exception as e:
            return f"ERROR going back: {e}"


def go_forward():
    """Navigate forward in the active page's history. Returns a screenshot
    of the resulting page state.
    """
    with _lock:
        try:
            page = _ensure_browser()
            if _active_browser_type == "bidi":
                url = _bidi_history_step(page, 1)
                if url is None:
                    return _result("No forward page in history.", page)
                return _result(f"Went forward to: {url}", page)
            response = page.go_forward(timeout=5000)
            if response is None:
                return _result("No forward page in history.", page)
            return _result(f"Went forward to: {page.url}", page)
        except Exception as e:
            return f"ERROR going forward: {e}"


def _open_firefox_bidi(url: str | None):
    """Launch (or reuse) the BiDi-driven Firefox, optionally navigate to
    ``url``, and make Firefox the active automated browser so every page
    tool drives it from now on.

    Returns the tool result on success, or ``None`` if the BiDi launch
    failed (the caller then falls back to a plain application launch).
    """
    with _lock:
        try:
            # Hand off from CDP if it was active; the Chromium browser keeps
            # running, we just stop driving it.
            if _active_browser_type == "cdp":
                _disconnect_active_browser()
            page = _ensure_firefox_bidi()
        except Exception as e:
            print(f"[OSIA Browser] Firefox BiDi launch failed: {e}")
            # Reset so the page tools don't keep retrying a broken BiDi setup.
            _disconnect_active_browser()
            return None

        # Firefox is up and driven over BiDi. From here on, a navigation
        # failure is reported but does NOT invalidate the launch — the
        # window stays open and the page tools keep driving it.
        try:
            page.bring_to_front()
        except Exception:
            pass
        if not url:
            return _result("Opened Firefox (driven over WebDriver BiDi)", page)
        try:
            page.goto(_normalize_url(url), wait_until="domcontentloaded", timeout=20000)
            return _result(
                f"Opened Firefox (driven over WebDriver BiDi) at {page.url}", page
            )
        except Exception as e:
            return _result(
                f"Opened Firefox (driven over WebDriver BiDi) but navigating "
                f"to {url!r} failed: {e}",
                page,
            )


def _open_cdp_browser(binary: str, url: str | None):
    """Open (or reuse) a CDP-driven Chromium browser, optionally navigate to
    ``url``, and make it the active automated browser.

    If a CDP browser is already alive on port ``CDP_PORT`` the existing
    window is reused — a second window is NEVER opened. Otherwise ``binary``
    is launched with remote debugging enabled so this window (and not a
    later duplicate) is the one every page tool drives.

    Returns the tool result on success, or ``None`` if the CDP launch or
    connection failed (the caller then falls back to a plain application
    launch).
    """
    with _lock:
        reused = _is_cdp_alive()
        try:
            # Hand off from BiDi if Firefox was active: close the OSIA-owned
            # Firefox (its sessions persist in the profile) so the page tools
            # drive the Chromium window from now on.
            if _active_browser_type == "bidi":
                _disconnect_active_browser()
            if not reused:
                ok, msg = _launch_cdp_browser(binary)
                if not ok:
                    print(f"[OSIA Browser] CDP launch failed: {msg}")
                    return None
            page = _ensure_cdp_browser()
        except Exception as e:
            print(f"[OSIA Browser] CDP connect failed: {e}")
            return None

        label = _canonical_browser_name(binary)
        verb = "Reusing existing" if reused else "Opened"
        try:
            page.bring_to_front()
        except Exception:
            pass
        if not url:
            return _result(f"{verb} {label} window (CDP on port {CDP_PORT})", page)
        try:
            page.goto(_normalize_url(url), wait_until="domcontentloaded", timeout=20000)
            return _result(f"{verb} {label} window at {_page_url(page)}", page)
        except Exception as e:
            return _result(
                f"{verb} {label} window but navigating to {url!r} failed: {e}",
                page,
            )


def open_browser(name: str, url: str | None = None):
    """Open a specific browser by user-facing name (firefox, chrome,
    chromium, opera, edge, brave), optionally at ``url``.

    Firefox is launched through Playwright over WebDriver BiDi and becomes
    the active automated browser — all page tools (navigate, click,
    find_and_click, scroll, …) drive it transparently, exactly as they drive
    a CDP browser. If the BiDi launch fails, Firefox is opened as a plain
    application instead.

    Chromium-based browsers (brave, chrome, chromium, opera, edge) are
    driven over CDP: if a CDP window is already running it is REUSED (never
    a second window); otherwise the browser is launched with remote
    debugging enabled so the page tools can drive it. Only if the CDP path
    fails entirely is the browser opened as a plain application.

    Use this tool when the user asks to open a *specific* browser
    ("open Firefox", "open Chrome").

    Every browser is launched against a dedicated OSIA profile (see
    ``_profile_args``), never the user's personal profile.
    """
    binary = resolve_browser_name(name)
    if not binary:
        known = ", ".join(sorted(_BROWSER_NAME_MAP))
        return (
            f"ERROR: browser {name!r} is not installed "
            f"(known browsers: {known})"
        )

    bidi_note = ""
    if _canonical_browser_name(binary) == "firefox":
        result = _open_firefox_bidi(url)
        if result is not None:
            return result
        # BiDi failed — fall through to the plain application launch below.
        bidi_note = " (WebDriver BiDi unavailable — page tools cannot drive this window)"
    else:
        # CDP-capable browser: reuse the existing CDP window or launch one
        # with remote debugging enabled. Never opens a duplicate window.
        result = _open_cdp_browser(binary, url)
        if result is not None:
            return result
        # CDP failed — fall through to the plain application launch below.
        bidi_note = " (CDP unavailable — page tools cannot drive this window)"

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
    except Exception as e:
        return f"ERROR launching {binary}: {e}"
    suffix = f" at {_normalize_url(url)}" if url else ""
    return f"Opened {os.path.basename(binary)}{suffix}{bidi_note}"


def open_new_tab(url: str) -> str:
    """Open ``url`` as a new tab in the real browser and make it active."""
    global _page
    url = _normalize_url(url)
    with _lock:
        try:
            _ensure_browser()
            new_page = _context.new_page()
            new_page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                new_page.bring_to_front()
            except Exception:
                pass
            _page = new_page  # subsequent tools act on the new tab
            return f"Opened new tab: {new_page.url}"
        except Exception as e:
            return f"ERROR opening new tab {url}: {e}"


# ── New tools ──────────────────────────────────────────────────────────────

def find_and_click(text: str):
    """Find the first visible element whose text matches ``text`` and click it.

    Uses Playwright's text locator, which matches button labels, links,
    list items, etc. — handy when you don't know the CSS selector but
    can see the label on the page. Returns a screenshot so the agent
    can verify the right element was clicked.
    """
    if not text:
        return "ERROR find_and_click: text is required"
    with _lock:
        try:
            page = _ensure_browser()
            locator = page.get_by_text(text, exact=False).first
            locator.click(timeout=5000)
            return _result(f"Clicked element with text: {text!r}", page)
        except Exception as e:
            return f"ERROR clicking element with text {text!r}: {e}"


def wait_for_load() -> str:
    """Wait for the active page's DOM to be ready (domcontentloaded, max 8s).

    Uses ``domcontentloaded`` rather than ``load``/``networkidle`` so the
    wait ends as soon as the DOM is usable, without waiting for every image,
    script, or ad. Never blocks the agent: a timeout is success, not an
    error — the page simply keeps loading resources in the background.

    Mostly unnecessary now that ``navigate()`` waits for the DOM itself;
    kept as a tool for pages that re-render after the initial load.
    """
    with _lock:
        try:
            page = _ensure_browser()
        except Exception as e:
            return f"ERROR waiting for load: {e}"
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
            return f"Page loaded: {_page_url(page)}"
        except _TIMEOUT_ERRORS:
            return (
                f"Page loaded (timeout): {_page_url(page)} — "
                "page may still be loading resources"
            )
        except Exception as e:
            return f"ERROR waiting for load: {e}"


def get_page_text() -> str:
    """Return the full visible text content of the page (truncated to 8000 chars).

    Like ``read_page`` but with a higher cap, intended for full-page reads
    when the agent really needs to inspect long content.
    """
    with _lock:
        try:
            page = _ensure_browser()
            text = page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            if not text:
                return "(empty page)"
            if len(text) > 8000:
                text = text[:8000] + "\n... [TRUNCATED]"
            return text
        except Exception as e:
            return f"ERROR reading page: {e}"


def scroll_down(amount: int | None = None) -> str:
    """Scroll the active page down by one viewport, or by ``amount`` pixels if given."""
    with _lock:
        try:
            page = _ensure_browser()
            if amount is None:
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                return "Scrolled down one viewport"
            try:
                amt = int(amount)
            except (TypeError, ValueError):
                return f"ERROR scroll_down: amount must be an integer, got {amount!r}"
            page.evaluate(f"window.scrollBy(0, {amt})")
            return f"Scrolled down {amt}px"
        except Exception as e:
            return f"ERROR scrolling: {e}"


def type_text(selector: str, text: str) -> str:
    """Focus the element matching ``selector`` and type ``text`` character
    by character via the keyboard. Use this instead of ``fill`` when the
    target field needs real keystroke events (e.g. search-as-you-type
    UIs, JS-heavy forms).
    """
    with _lock:
        try:
            page = _ensure_browser()
            page.click(selector, timeout=5000)  # focus the field
            page.keyboard.type(text)
            return f"Typed into {selector}: {text}"
        except Exception as e:
            return f"ERROR typing into {selector}: {e}"


def press_key(key: str) -> str:
    """Press a single keyboard key on the active page.

    Examples: ``Enter``, ``Escape``, ``Tab``, ``ArrowDown``, ``Backspace``,
    ``Control+A``. Useful after ``type_text`` to submit a search.
    """
    if not key:
        return "ERROR press_key: key is required"
    with _lock:
        try:
            page = _ensure_browser()
            page.keyboard.press(key)
            return f"Pressed key: {key}"
        except Exception as e:
            return f"ERROR pressing key {key}: {e}"


# ── Shutdown ───────────────────────────────────────────────────────────────

def close_browser():
    """Disconnect Playwright from the automated browser.

    For CDP (Chromium) this just disconnects — the user's real Brave window
    keeps running. For BiDi (Firefox) the browser process is owned by OSIA,
    so this quits Firefox; its cookies / sessions persist in the OSIA
    Firefox profile.
    """
    global _playwright
    with _lock:
        _disconnect_active_browser()
        try:
            if _playwright is not None:
                _playwright.stop()
        except Exception:
            pass
        finally:
            _playwright = None
