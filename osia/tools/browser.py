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

Note on Firefox: Firefox does NOT support the Chrome DevTools Protocol on
this flag, so CDP requires a Chromium-based browser (Brave, Chromium,
Chrome, Opera, Edge). Firefox can still be launched as a plain application
via ``open_browser``. If no CDP-capable browser is installed we surface a
clear error.
"""

import base64
import os
import shutil
import socket
import subprocess
import threading
import time

from playwright.sync_api import sync_playwright

# ── Configuration ──────────────────────────────────────────────────────────

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
OSIA_PROFILE_DIR = "/home/OSIA/.osia/brave-profile"

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

# Browsers that can NOT be driven over the Chrome DevTools Protocol
# (non-Chromium engines). They can still be launched as plain applications
# via ``open_browser`` but are skipped for CDP automation.
_NON_CDP_BROWSERS = {"firefox", "firefox-esr"}


# ── Module state ───────────────────────────────────────────────────────────

_playwright = None
_browser = None        # Playwright Browser (CDP-connected)
_context = None        # Playwright BrowserContext (the existing one from CDP)
_page = None           # Active Playwright Page
_lock = threading.Lock()


# ── CDP lifecycle ──────────────────────────────────────────────────────────

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


def _launch_cdp_browser() -> tuple[bool, str]:
    """Spawn Brave (or another Chromium browser if Brave isn't installed)
    against OSIA's dedicated profile at ``/home/OSIA/.osia/brave-profile``.

    The dedicated profile keeps OSIA's cookies, sessions, and history
    isolated from — and persistent across restarts independently of —
    the user's personal Brave instance.

    Returns ``(success, message)``.
    """
    browser = _detect_chromium_browser()
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
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"failed to launch {browser}: {e}"

    # Poll for the debugger socket — Chromium typically needs ~0.5-2s.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if _is_cdp_alive():
            return True, f"launched {os.path.basename(browser)} with CDP on port {CDP_PORT}"
        time.sleep(0.2)
    return False, (
        f"launched {browser} but CDP did not come online on port {CDP_PORT} within 10s"
    )


def _ensure_browser():
    """Ensure the CDP browser is running, connect Playwright if needed,
    and return an active Page. Recovers transparently if the previously
    cached page was closed (e.g. the user closed the tab).
    """
    global _playwright, _browser, _context, _page

    # Fast path: existing page is still alive.
    if _page is not None:
        try:
            _ = _page.url  # raises if the page was closed
            return _page
        except Exception:
            _page = None  # fall through to reconnect / re-acquire

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
    return _page


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
    """
    url = _normalize_url(url)
    with _lock:
        try:
            page = _ensure_browser()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            return _result(f"Navigated to: {page.url}", page)
        except Exception as e:
            return f"ERROR navigating to {url}: {e}"


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
            return f"Current URL: {page.url}"
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


def go_back():
    """Navigate back in the active page's history. Returns a screenshot
    of the resulting page state.
    """
    with _lock:
        try:
            page = _ensure_browser()
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
            response = page.go_forward(timeout=5000)
            if response is None:
                return _result("No forward page in history.", page)
            return _result(f"Went forward to: {page.url}", page)
        except Exception as e:
            return f"ERROR going forward: {e}"


def open_browser(name: str, url: str | None = None) -> str:
    """Open a specific browser by user-facing name (firefox, chrome,
    chromium, opera, edge, brave), optionally at ``url``.

    Unlike ``navigate``, this launches the named browser as a regular
    application — it works for any installed browser, including Firefox,
    which cannot be driven over CDP. Use it when the user asks to open a
    *specific* browser ("open Firefox", "open Chrome").
    """
    binary = resolve_browser_name(name)
    if not binary:
        known = ", ".join(sorted(_BROWSER_NAME_MAP))
        return (
            f"ERROR: browser {name!r} is not installed "
            f"(known browsers: {known})"
        )
    cmd = [binary]
    if url:
        cmd.append(_normalize_url(url))
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return f"ERROR launching {binary}: {e}"
    suffix = f" at {_normalize_url(url)}" if url else ""
    return f"Opened {os.path.basename(binary)}{suffix}"


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
    """Wait for the active page to finish loading (network idle, up to 15s)."""
    with _lock:
        try:
            page = _ensure_browser()
            page.wait_for_load_state("networkidle", timeout=15000)
            return f"Page loaded: {page.url}"
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
    """Disconnect Playwright from the real browser. Does NOT kill the
    browser process itself — the user's real Brave window keeps running.
    """
    global _playwright, _browser, _context, _page
    with _lock:
        try:
            if _browser is not None:
                _browser.close()  # for CDP this just disconnects
        except Exception:
            pass
        try:
            if _playwright is not None:
                _playwright.stop()
        except Exception:
            pass
        finally:
            _playwright = None
            _browser = None
            _context = None
            _page = None
