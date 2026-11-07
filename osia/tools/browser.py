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
the per-user OSIA data directory, so cookies / logins survive between
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
import hashlib
import inspect
import json
import os
import math
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from osia.tools.browser_forms import (
    FORM_MAX_FIELDS_DEFAULT,
    FormInspectionTimeout,
    SemanticFormSession,
    form_fill_error_receipt,
)
from osia.tools.browser_artifacts import (
    BrowserArtifactError,
    MAX_DOWNLOAD_BYTES,
    MAX_SESSION_TEMP_BYTES,
    cleanup_operation_temp,
    get_browser_artifact_store,
    operation_temp_dir,
    revalidate_upload,
    validate_upload_path,
)
from osia.tools.browser_state import (
    BROWSER_PAGE_TEXT_CHUNK_CHARS,
    BoundReferenceStore,
    BrowserReferenceError,
    BrowserStateRef,
    BrowserStateTracker,
    DispatchState,
    ReceiptOutcome,
    WaitConditionType,
    WaitSpec,
    action_result,
    content_fingerprint,
    make_receipt,
    opaque_ref,
    safe_origin,
    safe_url_display,
    scrub_browser_error,
)

# Timeout exceptions that mean "the page is just slow" — never a hard failure.
# Playwright's TimeoutError is caught alongside the builtin for safety across
# Playwright versions (newer versions subclass the builtin, older don't).
_TIMEOUT_ERRORS = (PlaywrightTimeoutError, TimeoutError)

_FIXED_DOM_PRIMITIVES = {
    "main": "main",
    "article": "article",
    "dialog": "dialog, [role=dialog], [aria-modal=true]",
    "form": "form",
    "table": "table",
    "list": "ol, ul",
}


def _semantic_browser_binding(kind, frame, metadata) -> str:
    """Private stable identity for one rendered target in one document."""

    material = json.dumps(
        {
            "frame": id(frame),
            "kind": str(kind),
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()

_NODE_BINDINGS_JS = r"""
  const binding = globalThis[bindingKey] || (
    globalThis[bindingKey] = {
      ids: new WeakMap(), next: 0,
      documentId: crypto.getRandomValues(new Uint32Array(4)).join('-')
    }
  );
  const nodeId = (el) => {
    if (!binding.ids.has(el)) binding.ids.set(el, binding.documentId + '_' + ++binding.next);
    return binding.ids.get(el);
  };
"""

_STRUCTURED_COLLECTION_JS = r"""
  const clean = (v, n=600) => String(v || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const bounded = (items) => items.slice(0, maxItems);
  const readTable = (table) => {
    const headers = Array.from(table.querySelectorAll('thead th'));
    const fallbackHeaders = headers.length ? headers : Array.from(
      table.rows[0] ? table.rows[0].querySelectorAll('th') : []
    );
    const rows = Array.from(table.rows);
    return {
      caption: clean(table.caption && table.caption.innerText, 300),
      headers: bounded(fallbackHeaders).map(cell => clean(cell.innerText, 500)),
      rows: bounded(rows).map(row =>
        bounded(Array.from(row.cells)).map(cell => clean(cell.innerText, 500))
      ),
      truncated: fallbackHeaders.length > maxItems || rows.length > maxItems ||
        bounded(rows).some(row => row.cells.length > maxItems),
    };
  };
  const readList = (list) => {
    const items = Array.from(list.children).filter(el => el.tagName === 'LI');
    return {
      ordered: list.tagName === 'OL',
      items: bounded(items).map(el => clean(el.innerText, 600)),
      truncated: items.length > maxItems,
    };
  };
"""

_STRUCTURED_INSPECTION_JS = (
    """({mode, maxItems, textOffset, textLimit, bindingKey, expectedFrameUrl}) => {
      if (expectedFrameUrl !== null && location.href !== expectedFrameUrl) return null;
    """
    + _NODE_BINDINGS_JS + _STRUCTURED_COLLECTION_JS + r"""
  const visible = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const all = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible);
  const indexed = (selector) => Array.from(document.querySelectorAll(selector))
    .map((el, index) => ({el, index})).filter(item => visible(item.el));
  const result = {
    title: clean(document.title, 300),
    language: clean(document.documentElement.lang, 32),
    headings: [], sections: [], links: [], buttons: [], controls: [],
    forms: [], tables: [], lists: [], scroll_regions: [], metadata: {},
  };
  const bodyText = document.body ? String(document.body.innerText || '') : '';
  let textEnd = Math.min(bodyText.length, textOffset + textLimit);
  // A cursor is measured in UTF-16 units. Do not split a surrogate pair.
  if (textEnd < bodyText.length &&
      bodyText.charCodeAt(textEnd - 1) >= 0xd800 && bodyText.charCodeAt(textEnd - 1) <= 0xdbff &&
      bodyText.charCodeAt(textEnd) >= 0xdc00 && bodyText.charCodeAt(textEnd) <= 0xdfff) textEnd--;
  result.page_text = bodyText.slice(textOffset, textEnd);
  result.page_text_length = bodyText.length;
  result.page_text_next_offset = textEnd;
  result.content_truncated = textEnd < bodyText.length;
  const hasCaptcha = Boolean(document.querySelector(
    'iframe[src*="captcha" i],iframe[src*="recaptcha" i],[class*="captcha" i],[id*="captcha" i]'
  ));
  const hasOtp = Boolean(document.querySelector(
    'input[autocomplete="one-time-code"],input[name*="otp" i],input[name*="verification" i]'
  ));
  const hasPassword = Boolean(document.querySelector('input[type="password"]'));
  result.manual_intervention = hasCaptcha ? 'captcha_required' :
    hasOtp ? 'two_factor_required' : hasPassword ? 'manual_login_required' : '';
  if (mode === 'page_text_chunk') return result;
  const headings = all('h1,h2,h3,h4,h5,h6');
  result.headings = bounded(headings).map((el, index) => ({
    level: Number(el.tagName.slice(1)), text: clean(el.innerText), index,
  }));
  const sections = all('main,article');
  result.sections = bounded(sections).map((el, index) => ({
    kind: el.tagName.toLowerCase(), text: clean(el.innerText, 4000), index,
  }));
  const links = indexed('a[href]');
  result.links = bounded(links).map(({el, index}) => ({
    text: clean(el.innerText || el.getAttribute('aria-label'), 300),
    destination: el.href, index, node_id: nodeId(el),
  }));
  const buttons = indexed('button,input[type=button],input[type=submit],[role=button]');
  result.buttons = bounded(buttons).map(({el, index}) => ({
    text: clean(el.innerText || el.value || el.getAttribute('aria-label'), 300),
    disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    expanded: el.getAttribute('aria-expanded'), index, node_id: nodeId(el),
  }));
  const controls = indexed('input:not([type=hidden]),textarea,select');
  const safeControls = controls.filter(({el}) =>
    String(el.type || '').toLowerCase() !== 'password' &&
    String(el.autocomplete || '').toLowerCase() !== 'one-time-code'
  );
  result.controls = bounded(safeControls).map(({el, index}) => ({
    kind: el.tagName.toLowerCase(), type: clean(el.type, 40),
    label: clean(el.labels && el.labels[0] ? el.labels[0].innerText : el.getAttribute('aria-label'), 300),
    disabled: Boolean(el.disabled), index, node_id: nodeId(el),
  }));
  const forms = indexed('form');
  result.forms = bounded(forms).map(({el, index}) => ({
    label: clean(el.getAttribute('aria-label') || el.getAttribute('name'), 300),
    field_count: el.querySelectorAll('input:not([type=hidden]),textarea,select').length,
    index, node_id: nodeId(el),
  }));
  const tables = indexed('table');
  result.tables = bounded(tables).map(({el, index}) => ({
    ...readTable(el), index, node_id: nodeId(el),
  }));
  const lists = indexed('ol,ul');
  result.lists = bounded(lists).map(({el, index}) => ({
    ...readList(el), index, node_id: nodeId(el),
  }));
  const scrollRegions = [];
  if (mode === 'snapshot') {
    const elements = document.querySelectorAll('*');
    for (let index = 0; index < elements.length && scrollRegions.length <= maxItems; index++) {
      const el = elements[index];
      if (el !== document.scrollingElement && el.clientHeight > 0 &&
          el.scrollHeight > el.clientHeight + 1 &&
          /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY) && visible(el)) {
        scrollRegions.push({el, index});
      }
    }
  }
  result.scroll_regions = bounded(scrollRegions).map(({el, index}) => ({
    label: clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText, 160),
    scroll_top: el.scrollTop, scroll_height: el.scrollHeight,
    viewport_height: el.clientHeight, index, node_id: nodeId(el),
  }));
  const canonical = document.querySelector('link[rel=canonical]');
  const description = document.querySelector('meta[name=description],meta[property="og:description"]');
  result.metadata = {
    canonical: canonical ? canonical.href : '',
    description: description ? clean(description.content, 600) : '',
  };
  result.content_truncated = [headings, sections, links, buttons, safeControls, forms, tables, lists, scrollRegions]
    .some(items => items.length > maxItems) || result.content_truncated ||
    result.tables.some(item => item.truncated) || result.lists.some(item => item.truncated);
  if (mode === 'article') result.sections = result.sections.filter(s => s.kind === 'article');
  if (mode === 'main_content') result.sections = result.sections.filter(s => s.kind === 'main' || s.kind === 'article');
  return result;
}
"""
)

_TARGETED_COLLECTION_JS = (
    "(el, maxItems) => {" + _STRUCTURED_COLLECTION_JS + r"""
      if (el.tagName === 'TABLE') return readTable(el);
      if (el.tagName === 'OL' || el.tagName === 'UL') return readList(el);
      return null;
    }
"""
)

_SEARCH_RESULT_JS = (
    "(el, bindingKey) => {" + _NODE_BINDINGS_JS + r"""
      return {
        nodeId: nodeId(el),
        href: el.getAttribute('href'),
        title: el.innerText,
        summary: el.parentElement?.innerText,
      };
    }
"""
)

_SCROLL_BOUND_JS = r"""
(el, delta) => {
  if (!el.isConnected) return null;
  const root = el.ownerDocument.scrollingElement;
  let scroller = el;
  while (scroller && scroller !== root) {
    if (scroller.clientHeight > 0 && scroller.scrollHeight > scroller.clientHeight + 1 &&
        /^(auto|scroll|overlay)$/.test(getComputedStyle(scroller).overflowY)) break;
    scroller = scroller.parentElement;
  }
  scroller ||= root;
  if (!scroller) return null;
  const before = scroller.scrollTop;
  scroller.scrollBy({top: delta, behavior: 'instant'});
  return {movement: scroller.scrollTop - before, nested: scroller !== root};
}
"""


class _SnapshotElement:
    """Resolve the inspected node before acting, even when a selector can move."""

    def __init__(self, locator, binding_key: str, node_id: str):
        self.locator = locator
        self.binding_key = binding_key
        self.node_id = node_id

    @asynccontextmanager
    async def _element(self):
        handles = await self.locator.element_handles()
        try:
            if len(handles) != 1 or not await handles[0].evaluate(
                "(el, args) => el.isConnected && globalThis[args.key]?.ids.get(el) === args.id",
                {"key": self.binding_key, "id": self.node_id},
            ):
                raise BrowserReferenceError("stale_element_reference")
            yield handles[0]
        finally:
            for handle in handles:
                await handle.dispose()

    async def count(self) -> int:
        try:
            async with self._element():
                return 1
        except BrowserReferenceError:
            return 0

    async def is_visible(self) -> bool:
        async with self._element() as element:
            return await element.is_visible()

    async def evaluate(self, expression, arg=None):
        async with self._element() as element:
            return await element.evaluate(expression, arg)

    async def click(self, *, timeout):
        async with self._element() as element:
            await element.click(timeout=timeout)

    async def set_input_files(self, files):
        async with self._element() as element:
            await element.set_input_files(files)

_ELEMENT_STATE_JS = r"""
(el) => ({
  disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
  checked: typeof el.checked === 'boolean' ? el.checked : null,
  selected: typeof el.selected === 'boolean' ? el.selected : null,
  expanded: el.getAttribute('aria-expanded'),
  pressed: el.getAttribute('aria-pressed'),
  hidden: Boolean(el.hidden || getComputedStyle(el).display === 'none' || getComputedStyle(el).visibility === 'hidden'),
  fileCount: el.files ? el.files.length : null,
})
"""

_CONTENT_FINGERPRINT_JS = r"""
() => {
  const body = document.body;
  if (!body) return '';
  return [document.title, location.pathname, body.innerText.slice(0, 200000),
    body.querySelectorAll('*').length].join('\n');
}
"""

_SEARCH_TEMPLATE = "https://www.google.com/search?q={query}"

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
elif sys.platform == "darwin":
    _BROWSER_CANDIDATES = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        "/Applications/Opera.app/Contents/MacOS/Opera",
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

_MACOS_NAME_PATHS = {
    "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "firefox": ["/Applications/Firefox.app/Contents/MacOS/firefox"],
    "opera": ["/Applications/Opera.app/Contents/MacOS/Opera"],
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
elif sys.platform == "darwin":
    for _key, _paths in _MACOS_NAME_PATHS.items():
        _BROWSER_NAME_MAP[_key] = list(_paths) + _BROWSER_NAME_MAP[_key]

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
    against OSIA's dedicated per-user profile.

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
_PAGINATION_POSTCONDITION_TIMEOUT_S = 15.0


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
        self._state_tracker = BrowserStateTracker()
        self._bound_refs = BoundReferenceStore()
        self._element_binding_key = opaque_ref("osia_browser_nodes")
        self._pending_dialogs = {}
        self._dialog_sequence = 0
        self._network_activity = {}
        self._page_downloads = {}
        self._download_sequence = 0
        self._download_session_bytes = 0
        self._prepared_uploads = {}
        self._browser_progress_callback = None
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
        try:
            browser_state = (
                self._state_tracker.current(page).to_dict()
                if page is not None and not self._page_is_closed(page)
                else None
            )
        except BrowserReferenceError:
            browser_state = None
        with self._snapshot_lock:
            self._published = {
                "state": self._state.value,
                "threadId": self._owner_thread_id,
                "nativeThreadId": self._owner_native_thread_id,
                "threadAlive": bool(self._thread and self._thread.is_alive()),
                "backend": self._active_browser_type,
                # Published snapshots are diagnostics/mobile-adjacent state.
                # Keep query, fragment, userinfo and private file paths out.
                "activeUrl": safe_url_display(active_url),
                "registryCount": len(self._registry),
                "generation": self._registry_generation,
                "browserState": browser_state,
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
        from osia.tool_attempts import (
            current_tool_attempt_id,
            publish_tool_attempt_event,
        )

        tool_attempt_id = current_tool_attempt_id()
        if tool_attempt_id:
            publish_tool_attempt_event("browser_owner_accepted")
        loop = self._ensure_started()
        acknowledgement = threading.Event()
        future = asyncio.run_coroutine_threadsafe(
            self._execute(operation, args, kwargs, acknowledgement), loop
        )
        outer_timeout = _OWNER_CALL_TIMEOUT_S if timeout is None else timeout
        try:
            if tool_attempt_id:
                publish_tool_attempt_event("browser_owner_running")
            result = future.result(timeout=outer_timeout)
            if tool_attempt_id:
                publish_tool_attempt_event("browser_owner_terminal_receipt")
            return result
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            settled = acknowledgement.wait(_OWNER_CANCEL_TIMEOUT_S)
            if operation == "inspect_form_fields" and settled:
                # An acknowledged read cancellation leaves the tab usable.
                publish_tool_attempt_event(
                    "browser_owner_terminal_receipt", "inspection_timeout",
                )
                return FormInspectionTimeout()
            self.close()
            if tool_attempt_id:
                publish_tool_attempt_event(
                    "browser_owner_cancellation_requested",
                    "owner_timeout_effect_uncertain",
                )
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
                self._state_tracker.set_opener(page, opener)
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
        self._state_tracker.retire_all(reason="context_retired")
        self._bound_refs.clear()
        self._pending_dialogs.clear()
        self._network_activity.clear()
        self._page_downloads.clear()
        self._prepared_uploads.clear()
        self._download_session_bytes = 0
        self._form_session.reset()
        self._active_page = None
        self._registry_context = None
        self._context_handlers = None
        self._registry_generation += 1
        self._publish()

    def _remove_page(self, page, generation, reason="page_closed"):
        self._assert_owner_thread()
        if generation != self._registry_generation:
            return
        key = id(page)
        item = self._registry.pop(key, None)
        self._page_downloads.pop(key, None)
        self._network_activity.pop(key, None)
        self._page_tombstones.add(key)
        try:
            page_id = self._state_tracker.current(page).page_id
            self._bound_refs.invalidate_page(page_id)
        except BrowserReferenceError:
            pass
        self._state_tracker.retire_page(page, reason=reason)
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

    def _handle_frame_navigated(self, page, generation, frame=None):
        self._assert_owner_thread()
        if generation != self._registry_generation or id(page) not in self._registry:
            return
        try:
            is_main = frame is None or frame is page.main_frame
        except Exception:
            is_main = True
        if is_main:
            try:
                previous = self._state_tracker.current(page)
                self._bound_refs.invalidate_page(previous.page_id)
            except BrowserReferenceError:
                pass
            self._state_tracker.note_navigation(page)
            self._form_session.invalidate_page(page)
        self._publish()

    def _handle_dialog(self, page, generation, dialog):
        self._assert_owner_thread()
        if generation != self._registry_generation or id(page) not in self._registry:
            return
        try:
            state = self._state_tracker.current(page)
        except BrowserReferenceError:
            return
        self._dialog_sequence += 1
        dialog_ref = self._bound_refs.issue("dialog", state, dialog)
        self._pending_dialogs[dialog_ref] = {
            "dialog": dialog,
            "page": page,
            "state": state,
            "sequence": self._dialog_sequence,
        }
        self._publish()

    def _handle_network_activity(self, page, generation, request, *, started):
        if generation != self._registry_generation or id(page) not in self._registry:
            return
        state = self._network_activity[id(page)]
        inflight = state["inflight"]
        if started:
            try:
                is_web_request = urlsplit(request.url).scheme in {"http", "https"}
                is_web_document = request.is_navigation_request() or (
                    urlsplit(request.frame.url).scheme in {"http", "https"}
                )
            except Exception:
                return
            if not is_web_request or not is_web_document or request in inflight:
                return
            inflight.add(request)
        elif request in inflight:
            inflight.remove(request)
        else:
            return
        state["last_change"] = time.monotonic()

    def _handle_page_download(self, page, generation, download):
        if generation == self._registry_generation and id(page) in self._registry:
            self._download_sequence += 1
            self._page_downloads[id(page)] = (self._download_sequence, download)

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
                "close": lambda *_args: self._remove_page(
                    page, generation, "page_closed"
                ),
                "crash": lambda *_args: self._remove_page(
                    page, generation, "page_crashed"
                ),
                "framenavigated": lambda frame=None, *_args: self._handle_frame_navigated(
                    page, generation, frame
                ),
                "dialog": lambda dialog: self._handle_dialog(
                    page, generation, dialog
                ),
                "download": lambda download: self._handle_page_download(
                    page, generation, download
                ),
                "request": lambda request: self._handle_network_activity(
                    page, generation, request, started=True,
                ),
                "requestfinished": lambda request: self._handle_network_activity(
                    page, generation, request, started=False,
                ),
                "requestfailed": lambda request: self._handle_network_activity(
                    page, generation, request, started=False,
                ),
            }
            item = {
                "page": page,
                "order": self._registry_sequence,
                "opener": opener if opener is not None else self._page_opener(page),
                "handlers": handlers,
            }
            self._registry[key] = item
            self._state_tracker.register_page(
                page, opener=item["opener"], popup=item["opener"] is not None,
            )
            self._network_activity[key] = {
                "inflight": set(), "last_change": time.monotonic(),
            }
            if item["opener"] is None:
                self._schedule_opener_resolution(page, context, generation)
            for event, handler in handlers.items():
                try:
                    page.on(event, handler)
                except Exception:
                    pass
        elif opener is not None:
            item["opener"] = opener
            self._state_tracker.set_opener(page, opener)
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
        self._state_tracker.start_context(context)
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
            print(
                "[OSIA Browser] screenshot capture failed: "
                f"{scrub_browser_error(exc)}"
            )
            return None

    async def _result(self, text, page):
        screenshot = await self._screenshot_b64(page)
        try:
            state = self._current_state(page).to_dict()
        except Exception:
            state = {}
        return {
            "text": text,
            "screenshot": screenshot,
            "vision_target": {
                "state": state,
                "frame_id": "main",
                "capture_method": "browser_exact_page",
            },
        }

    async def _page_url(self, page):
        if self._active_browser_type == "bidi":
            try:
                return await page.evaluate("location.href")
            except Exception:
                pass
        return page.url

    async def _after_interaction_page(self, fallback):
        # Yield only so already-queued lifecycle events can run.  Correctness
        # is proven by BrowserStateRef/WaitSpec evidence, never this yield.
        await asyncio.sleep(0)
        if self._context is not None:
            return self._reconcile_pages(self._context) or fallback
        return self._active_page or fallback

    def _current_state(self, page):
        return self._state_tracker.current(page)

    async def _resolve_state_page(self, state_ref=None, *, activate=False):
        page = await self._ensure_browser()
        if state_ref is not None:
            page = self._state_tracker.validate(state_ref)
        if self._page_is_closed(page):
            raise BrowserReferenceError()
        if activate:
            self._active_page = page
            try:
                await page.bring_to_front()
            except Exception:
                pass
            self._publish()
        return page, self._current_state(page)

    @staticmethod
    def _receipt_failure(
        *, before=None, after=None, started_at=None, code="browser_operation_failed",
        dispatched=False, evidence=(), unknown=False,
    ):
        receipt = make_receipt(
            before_state=before,
            after_state=after if after is not None else before,
            dispatch_state=(
                DispatchState.EFFECT_UNKNOWN if unknown else
                DispatchState.DISPATCHED if dispatched else DispatchState.NOT_DISPATCHED
            ),
            outcome=(
                ReceiptOutcome.OUTCOME_UNKNOWN if unknown else
                ReceiptOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
            ),
            evidence=list(evidence),
            started_at=started_at,
        )
        return action_result(ok=False, receipt=receipt, error_code=code)

    @staticmethod
    def _receipt_success(
        *, before, after, started_at, evidence=(), data=None, text="",
    ):
        receipt = make_receipt(
            before_state=before,
            after_state=after,
            dispatch_state=DispatchState.EFFECT_CONFIRMED,
            outcome=ReceiptOutcome.CONFIRMED_SUCCESS,
            evidence=list(evidence),
            started_at=started_at,
        )
        return action_result(ok=True, receipt=receipt, data=data, text=text)

    async def _page_fingerprint(self, page):
        try:
            raw = await page.evaluate(_CONTENT_FINGERPRINT_JS)
        except Exception:
            raw = ""
        return content_fingerprint(raw)

    async def _locator_state(self, locator):
        try:
            value = await locator.evaluate(_ELEMENT_STATE_JS)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    async def _target_manual_intervention(self, locator):
        try:
            reason = await locator.evaluate(r"""
                el => {
                  if (String(el.type || '').toLowerCase() === 'password') return 'manual_login_required';
                  if (String(el.autocomplete || '').toLowerCase() === 'one-time-code') return 'two_factor_required';
                  if (el.closest('[class*="captcha" i],[id*="captcha" i]')) return 'captcha_required';
                  return '';
                }
            """)
        except Exception:
            return ""
        return reason if reason in {
            "manual_login_required", "captcha_required", "two_factor_required",
        } else ""

    async def _wait_condition(self, page, spec, baseline=None):
        wait = WaitSpec.parse(spec)
        baseline = dict(baseline or {})
        deadline = asyncio.get_running_loop().time() + wait.timeout_seconds
        stable_value = None
        stable_since = None
        while True:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                return False, {"condition": wait.condition_type.value}
            condition = wait.condition_type
            try:
                state = self._current_state(page)
                raw_url = await self._page_url(page)
            except Exception:
                return False, {"condition": condition.value, "state": "closed"}
            matched = False
            evidence = {"condition": condition.value}
            if condition in {
                WaitConditionType.NAVIGATION_COMMIT,
                WaitConditionType.DOCUMENT_EPOCH_CHANGE,
            }:
                matched = state.document_epoch != baseline.get(
                    "document_epoch", state.document_epoch
                )
                evidence["document_epoch_advanced"] = matched
            elif condition == WaitConditionType.URL_CHANGE:
                matched = raw_url != baseline.get("url", raw_url)
                evidence["url_changed"] = matched
            elif condition == WaitConditionType.URL_MATCH:
                expected = str(wait.expected or "")
                matched = bool(expected and expected.casefold() in raw_url.casefold())
                evidence["url_matched"] = matched
            elif condition == WaitConditionType.DOM_SELECTOR_STATE:
                expected = wait.expected if isinstance(wait.expected, dict) else {}
                selector = _FIXED_DOM_PRIMITIVES.get(str(expected.get("primitive") or ""))
                desired = str(expected.get("state") or "visible")
                if selector:
                    locator = page.locator(selector).first
                    count = await locator.count()
                    visible = bool(count and await locator.is_visible())
                    matched = (
                        (desired == "attached" and count > 0)
                        or (desired == "visible" and visible)
                        or (desired == "hidden" and not visible)
                    )
                evidence["element_state_matched"] = matched
            elif condition == WaitConditionType.ELEMENT_STATE:
                expected = wait.expected if isinstance(wait.expected, dict) else {}
                ref_id = str(expected.get("element_ref") or "")
                desired = str(expected.get("state") or "visible")
                try:
                    locator = self._bound_refs.resolve(ref_id, state, kinds={
                        "link", "button", "control", "form", "table", "list",
                        "search_result", "pagination",
                    })
                    count = await locator.count()
                    element_state = await self._locator_state(locator) if count else {}
                    visible = bool(count and await locator.is_visible())
                    matched = {
                        "visible": visible,
                        "hidden": not visible,
                        "enabled": visible and not element_state.get("disabled", False),
                        "disabled": bool(element_state.get("disabled")),
                        "checked": element_state.get("checked") is True,
                        "selected": element_state.get("selected") is True,
                        "expanded": element_state.get("expanded") == "true",
                        "collapsed": element_state.get("expanded") == "false",
                    }.get(desired, False)
                except BrowserReferenceError:
                    matched = False
                evidence["element_state_matched"] = matched
            elif condition in {
                WaitConditionType.TEXT_APPEARANCE,
                WaitConditionType.TEXT_DISAPPEARANCE,
            }:
                expected = str(wait.expected or "")
                body = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                present = expected.casefold() in str(body or "").casefold()
                matched = present if condition == WaitConditionType.TEXT_APPEARANCE else not present
                evidence["text_state_matched"] = matched
            elif condition == WaitConditionType.POPUP_CREATION:
                known = set(baseline.get("page_ids") or ())
                current = {ref.page_id for _item, ref in self._state_tracker.tab_refs()}
                matched = bool(current - known)
                evidence["popup_created"] = matched
            elif condition == WaitConditionType.DIALOG_APPEARANCE:
                matched = self._dialog_sequence > int(baseline.get("dialog_sequence", 0))
                evidence["dialog_appeared"] = matched
            elif condition == WaitConditionType.DIALOG_CLOSURE:
                matched = str(wait.expected or "") not in self._pending_dialogs
                evidence["dialog_closed"] = matched
            elif condition == WaitConditionType.NETWORK_QUIET:
                activity = self._network_activity.get(id(page), {})
                quiet_for = time.monotonic() - float(activity.get("last_change", 0))
                matched = not activity.get("inflight") and quiet_for >= wait.quiet_period_seconds
                evidence["network_quiet"] = matched
            elif condition == WaitConditionType.STABLE_CONTENT:
                current = await self._page_fingerprint(page)
                if current != stable_value:
                    stable_value = current
                    stable_since = now
                matched = stable_since is not None and now - stable_since >= wait.quiet_period_seconds
                evidence["content_stable"] = matched
                if matched:
                    evidence["content_fingerprint"] = current
            elif condition == WaitConditionType.CUSTOM_POSTCONDITION:
                nested_evidence = []
                matched = True
                for nested in wait.expected:
                    nested_ok, nested_item = await self._wait_condition(
                        page, nested, baseline=baseline,
                    )
                    nested_evidence.append(nested_item)
                    if not nested_ok:
                        matched = False
                        break
                evidence["all_conditions_matched"] = matched
                evidence["condition_count"] = len(nested_evidence)
            elif condition in {
                WaitConditionType.DOWNLOAD_START,
                WaitConditionType.DOWNLOAD_COMPLETION,
            }:
                sequence, download = self._page_downloads.get(id(page), (0, None))
                fresh = sequence > baseline.get("download_sequence", self._download_sequence)
                evidence["fresh_download_observed"] = fresh
                if fresh and condition == WaitConditionType.DOWNLOAD_START:
                    matched = True
                elif fresh:
                    try:
                        failure = await asyncio.wait_for(
                            download.failure(), timeout=max(0.001, deadline - now),
                        )
                        matched = failure is None
                    except asyncio.TimeoutError:
                        matched = False
                    evidence["download_completed"] = matched
            if matched:
                return True, evidence
            await asyncio.sleep(min(0.05, max(0.001, deadline - now)))

    async def _wait_after_dispatch(self, page, wait_spec, baseline):
        try:
            return await self._wait_condition(page, wait_spec, baseline=baseline)
        except Exception:
            return False, {"condition": "wait_failed"}

    async def _op_browser_state(self):
        started = time.time()
        try:
            page, state = await self._resolve_state_page()
            display = safe_url_display(await self._page_url(page))
        except Exception as exc:
            return self._receipt_failure(
                started_at=started, code=scrub_browser_error(exc),
            )
        return self._receipt_success(
            before=state, after=state, started_at=started,
            evidence=[{"state_observed": True}],
            data={
                "state": state.to_dict(),
                "safe_url": display,
                "lifecycle_events": self._state_tracker.events(
                    after=max(0, len(self._state_tracker.events()) - 16)
                ),
            },
        )

    async def _inspect_frame_summaries(self, page, state):
        summaries = []
        try:
            frames = list(page.frames)
            main = page.main_frame
            main_origin = safe_origin(await self._page_url(page))
        except Exception:
            return summaries
        for frame in frames[:32]:
            if frame is main:
                continue
            try:
                frame_url = frame.url
            except Exception:
                frame_url = ""
            origin = safe_origin(frame_url)
            same_origin = bool(
                origin and (origin == main_origin or frame_url.startswith("about:blank"))
            )
            item = {
                "safe_url": safe_url_display(frame_url),
                "same_origin": same_origin,
                "accessible": same_origin,
            }
            if same_origin:
                item["frame_ref"] = self._bound_refs.issue(
                    "frame",
                    state,
                    (frame, frame_url),
                    binding_key=_semantic_browser_binding(
                        "frame", frame, {"url": frame_url},
                    ),
                )
            else:
                item["status"] = "cross_origin_inaccessible"
            summaries.append(item)
        return summaries

    def _snapshot_element(self, locator, node_id: str):
        if not isinstance(node_id, str) or not node_id:
            raise BrowserReferenceError("stale_element_reference")
        return _SnapshotElement(locator, self._element_binding_key, node_id)

    def _issue_snapshot_element(self, kind, frame, state, selector, item):
        # The snapshot uses document.querySelectorAll, which does not pierce
        # shadow roots. Resolve with the same selector scope and index.
        locator = frame.locator("css:light=" + selector).nth(item["index"])
        return self._bound_refs.issue(
            kind, state, self._snapshot_element(locator, item["node_id"]),
            binding_key=_semantic_browser_binding(kind, frame, item),
        )

    async def _structured_frame_data(
        self, frame, state, *, mode, max_items, text_offset, text_limit,
        expected_frame_url=None,
    ):
        raw = await frame.evaluate(_STRUCTURED_INSPECTION_JS, {
            "mode": mode,
            "maxItems": max_items,
            "textOffset": text_offset,
            "textLimit": text_limit,
            "bindingKey": self._element_binding_key,
            "expectedFrameUrl": expected_frame_url,
        })
        if raw is None and expected_frame_url is not None:
            raise BrowserReferenceError("stale_element_reference")
        if not isinstance(raw, dict):
            raise RuntimeError("invalid_structured_snapshot")
        links = []
        for item in raw.get("links") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            links.append({
                "element_ref": self._issue_snapshot_element(
                    "link", frame, state, "a[href]", item,
                ),
                "text": str(item.get("text") or "")[:300],
                "safe_destination": safe_url_display(item.get("destination")),
            })
        buttons = []
        button_selector = "button,input[type=button],input[type=submit],[role=button]"
        for item in raw.get("buttons") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            buttons.append({
                "element_ref": self._issue_snapshot_element(
                    "button", frame, state, button_selector, item,
                ),
                "text": str(item.get("text") or "")[:300],
                "disabled": item.get("disabled") is True,
                "expanded": item.get("expanded") if item.get("expanded") in {"true", "false"} else None,
            })
        controls = []
        for item in raw.get("controls") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            controls.append({
                "element_ref": self._issue_snapshot_element(
                    "control", frame, state, "input:not([type=hidden]),textarea,select", item,
                ),
                "kind": str(item.get("kind") or "")[:40],
                "type": str(item.get("type") or "")[:40],
                "label": str(item.get("label") or "")[:300],
                "disabled": item.get("disabled") is True,
            })
        tables = []
        for item in raw.get("tables") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            rows = []
            for row in (item.get("rows") or [])[:max_items]:
                if isinstance(row, list):
                    rows.append([str(cell)[:500] for cell in row[:max_items]])
            tables.append({
                "element_ref": self._issue_snapshot_element(
                    "table", frame, state, "table", item,
                ),
                "caption": str(item.get("caption") or "")[:300],
                "headers": [
                    str(value)[:500]
                    for value in (item.get("headers") or [])[:max_items]
                ],
                "rows": rows,
            })
        lists = []
        for item in raw.get("lists") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            lists.append({
                "element_ref": self._issue_snapshot_element(
                    "list", frame, state, "ol,ul", item,
                ),
                "ordered": item.get("ordered") is True,
                "items": [str(value)[:600] for value in (item.get("items") or [])[:max_items]],
            })
        forms = []
        for item in raw.get("forms") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            forms.append({
                "element_ref": self._issue_snapshot_element(
                    "form", frame, state, "form", item,
                ),
                "label": str(item.get("label") or "")[:300],
                "field_count": max(0, min(int(item.get("field_count") or 0), 10_000)),
            })
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        scroll_regions = []
        for item in raw.get("scroll_regions") or []:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                continue
            scroll_regions.append({
                "element_ref": self._issue_snapshot_element(
                    "scroll_region", frame, state, "*", item,
                ),
                "label": str(item.get("label") or "")[:160],
                "scroll_top": item.get("scroll_top"),
                "scroll_height": item.get("scroll_height"),
                "viewport_height": item.get("viewport_height"),
            })
        payload = {
            "title": str(raw.get("title") or "")[:300],
            "language": str(raw.get("language") or "")[:32],
            "headings": [
                {
                    "level": max(1, min(int(item.get("level") or 1), 6)),
                    "text": str(item.get("text") or "")[:600],
                }
                for item in (raw.get("headings") or [])[:max_items]
                if isinstance(item, dict)
            ],
            "sections": [
                {
                    "kind": str(item.get("kind") or "")[:32],
                    "text": str(item.get("text") or "")[:4_000],
                }
                for item in (raw.get("sections") or [])[:max_items]
                if isinstance(item, dict)
            ],
            "links": links,
            "buttons": buttons,
            "controls": controls,
            "forms": forms,
            "tables": tables,
            "lists": lists,
            "scroll_regions": scroll_regions,
            "metadata": {
                "canonical": safe_url_display(metadata.get("canonical")),
                "description": str(metadata.get("description") or "")[:600],
            },
            "manual_intervention": (
                str(raw.get("manual_intervention") or "")
                if str(raw.get("manual_intervention") or "") in {
                    "manual_login_required", "captcha_required", "two_factor_required",
                } else ""
            ),
            "page_text": str(raw.get("page_text") or "")[:text_limit],
            "content_truncated": raw.get("content_truncated") is True,
            "page_text_length": max(0, int(raw.get("page_text_length") or 0)),
            "page_text_next_offset": max(0, int(raw.get("page_text_next_offset") or 0)),
        }
        return payload

    async def _inspect_collection(self, state, mode, target_ref, max_items, started):
        locator = self._bound_refs.resolve(target_ref, state, kinds={"table", "list"})
        collection = await locator.evaluate(_TARGETED_COLLECTION_JS, max_items)
        if not isinstance(collection, dict):
            raise BrowserReferenceError("stale_element_reference")
        kind = "table" if "rows" in collection else "list"
        if mode not in {"snapshot", kind}:
            return self._receipt_failure(
                before=state, started_at=started, code="target_does_not_match_extraction_mode",
            )
        truncated = collection.pop("truncated")
        collection["element_ref"] = target_ref
        return self._receipt_success(
            before=state, after=state, started_at=started,
            evidence=[{"targeted_extraction": True, "truncated": truncated}],
            data={
                "browser_state": state.to_dict(),
                "tables": [collection] if kind == "table" else [],
                "lists": [collection] if kind == "list" else [],
                "content_truncated": truncated,
                "status": "truncated" if truncated else "ok",
            },
        )

    async def _op_inspect_page(
        self, state_ref=None, mode="snapshot", target_ref=None, cursor=None,
        frame_ref=None, max_items=40,
    ):
        started = time.time()
        allowed_modes = {
            "snapshot", "main_content", "article", "headings", "links",
            "metadata", "table", "list", "page_text_chunk",
        }
        if mode not in allowed_modes:
            return self._receipt_failure(
                started_at=started, code="unsupported_extraction_mode",
            )
        try:
            max_items = max(1, min(int(max_items), 100))
            page, state = await self._resolve_state_page(state_ref)
            if target_ref:
                if cursor or frame_ref:
                    return self._receipt_failure(
                        before=state, started_at=started, code="conflicting_extraction_references",
                    )
                return await self._inspect_collection(
                    state, mode, target_ref, max_items, started,
                )
            frame = page
            expected_frame_url = None
            if frame_ref:
                frame, expected_frame_url = self._bound_refs.resolve(
                    frame_ref, state, kinds={"frame"},
                )
                if frame.url != expected_frame_url:
                    raise BrowserReferenceError("stale_element_reference")
            text_offset = 0
            if cursor:
                text_offset = int(self._bound_refs.resolve(
                    cursor, state, kinds={"cursor"},
                ))
            payload = await self._structured_frame_data(
                frame, state, mode=mode, max_items=max_items,
                text_offset=text_offset, text_limit=BROWSER_PAGE_TEXT_CHUNK_CHARS,
                expected_frame_url=expected_frame_url,
            )
            payload["browser_state"] = state.to_dict()
            payload["safe_url"] = safe_url_display(await self._page_url(page))
            if frame is page:
                payload["frames"] = await self._inspect_frame_summaries(page, state)
            else:
                payload["frames"] = []
            next_offset = payload.pop("page_text_next_offset")
            length = payload.get("page_text_length", 0)
            payload["page_text_offset"] = text_offset
            payload["next_cursor"] = (
                self._bound_refs.issue("cursor", state, next_offset)
                if next_offset < length else None
            )
            if mode in {"snapshot", "main_content", "article"} and frame is page:
                form_result = await self._form_session.inspect(
                    page=page,
                    context_generation=self._registry_generation,
                    include_hidden=False,
                    include_disabled=False,
                    max_fields=min(max_items, FORM_MAX_FIELDS_DEFAULT),
                )
                try:
                    if str(form_result).startswith("ERROR: "):
                        form_result = str(form_result)[7:]
                    semantic = json.loads(str(form_result))
                except (TypeError, ValueError):
                    semantic = {"ok": False, "category": "inspection_failed"}
                payload["semantic_forms"] = semantic
            selection = {
                "headings": payload["headings"],
                "links": payload["links"],
                "metadata": payload["metadata"],
                "table": payload["tables"],
                "list": payload["lists"],
                "article": [s for s in payload["sections"] if s["kind"] == "article"],
                "main_content": payload["sections"],
                "page_text_chunk": payload["page_text"],
            }.get(mode)
            status = "ok"
            if mode != "snapshot" and selection in (None, "", []):
                status = "not_found"
            elif payload["content_truncated"]:
                status = "truncated"
            payload["status"] = status
            fingerprint = await self._page_fingerprint(page)
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(
                started_at=started, code=scrub_browser_error(exc),
            )
        return self._receipt_success(
            before=state, after=state, started_at=started,
            evidence=[{
                "structured_snapshot": True,
                "content_fingerprint": fingerprint,
                "truncated": payload["content_truncated"],
            }],
            data=payload,
        )

    async def _op_wait_spec(self, state_ref, wait_spec):
        started = time.time()
        try:
            page, state = await self._resolve_state_page(state_ref)
            baseline = {
                "url": await self._page_url(page),
                "document_epoch": state.document_epoch,
                "page_ids": [ref.page_id for _item, ref in self._state_tracker.tab_refs()],
                "dialog_sequence": self._dialog_sequence,
                "download_sequence": self._download_sequence,
            }
            ok, evidence = await self._wait_condition(page, wait_spec, baseline)
            after = self._current_state(page)
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except ValueError:
            return self._receipt_failure(started_at=started, code="invalid_wait_spec")
        except Exception as exc:
            return self._receipt_failure(
                started_at=started, code=scrub_browser_error(exc),
            )
        if not ok:
            return self._receipt_failure(
                before=state, started_at=started, code="wait_condition_timeout",
                evidence=[evidence],
            )
        return self._receipt_success(
            before=state, after=after, started_at=started,
            evidence=[evidence], data={"state": after.to_dict()},
        )

    async def _op_navigate_safe(self, url, state_ref=None, wait_spec=None):
        started = time.time()
        try:
            normalized = _normalize_url(url)
            target_display = safe_url_display(normalized)
            if not target_display or target_display == "invalid-url":
                raise ValueError("invalid_url")
            page, before = await self._resolve_state_page(state_ref, activate=True)
            before_url = await self._page_url(page)
            baseline = {
                "url": before_url,
                "document_epoch": before.document_epoch,
                "page_ids": [ref.page_id for _item, ref in self._state_tracker.tab_refs()],
                "dialog_sequence": self._dialog_sequence,
                "download_sequence": self._download_sequence,
            }
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception:
            return self._receipt_failure(started_at=started, code="invalid_navigation_target")
        try:
            await page.goto(normalized, wait_until="commit", timeout=10_000)
        except _TIMEOUT_ERRORS as exc:
            try:
                after = self._current_state(page)
                after_url = await self._page_url(page)
            except Exception:
                after, after_url = before, before_url
            evidence = [{
                "navigation_dispatched": True,
                "url_changed": after_url != before_url,
                "document_epoch_advanced": after.document_epoch != before.document_epoch,
            }]
            return self._receipt_failure(
                before=before, after=after, started_at=started,
                code=scrub_browser_error(exc), dispatched=True,
                unknown=True, evidence=evidence,
            )
        except Exception as exc:
            return self._receipt_failure(
                before=before, started_at=started,
                code=scrub_browser_error(exc), dispatched=True, unknown=True,
                evidence=[{"navigation_dispatched": True}],
            )
        await asyncio.sleep(0)
        after = self._current_state(page)
        if after.document_epoch == before.document_epoch:
            after = self._state_tracker.note_navigation(
                page, reason="navigation_committed",
            )
            self._bound_refs.invalidate_page(before.page_id)
        after_url = await self._page_url(page)
        try:
            # The committed document is already authoritative success evidence.
            # This event-based wait only gives subframes and semantic controls a
            # chance to become available; its timeout never fabricates failure
            # or success and is not a fixed correctness delay.
            await page.wait_for_load_state("domcontentloaded", timeout=3_000)
        except Exception:
            pass
        evidence = [{
            "navigation_commit": True,
            "document_epoch_advanced": after.document_epoch > before.document_epoch,
            "safe_url_before": safe_url_display(before_url),
            "safe_url_after": safe_url_display(after_url),
        }]
        if wait_spec is not None:
            ok, waited = await self._wait_after_dispatch(page, wait_spec, baseline)
            evidence.append(waited)
            if not ok:
                return self._receipt_failure(
                    before=before, after=after, started_at=started,
                    code="postcondition_timeout", dispatched=True, unknown=True,
                    evidence=evidence,
                )
            after = self._current_state(page)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        self._active_page = page
        self._publish()
        return self._receipt_success(
            before=before, after=after, started_at=started, evidence=evidence,
            data={"state": after.to_dict(), "safe_url": safe_url_display(after_url)},
            text=f"Navigated to: {safe_url_display(after_url)}",
        )

    async def _op_activate(
        self, state_ref, element_ref, wait_spec=None, require_wait_spec=False,
    ):
        started = time.time()
        if require_wait_spec or wait_spec is not None:
            try:
                # Validate the observable postcondition before resolving the
                # target or permitting the one mutating click. Agent-side
                # schema repair is the primary gate; this owner-side check
                # keeps direct canonical callers safe while legacy aliases
                # retain their established argument contract.
                wait_spec = WaitSpec.parse(wait_spec).to_dict()
            except ValueError:
                return self._receipt_failure(
                    started_at=started, code="invalid_wait_spec",
                )
        try:
            page, before = await self._resolve_state_page(state_ref, activate=True)
            locator = self._bound_refs.resolve(
                element_ref, before,
                kinds={"link", "button", "control", "search_result", "pagination"},
            )
            if (
                (not isinstance(locator, _SnapshotElement) and await locator.count() != 1)
                or not await locator.is_visible()
            ):
                return self._receipt_failure(
                    before=before, started_at=started, code="target_not_actionable",
                )
            intervention = await self._target_manual_intervention(locator)
            if intervention:
                blocked = self._receipt_failure(
                    before=before, started_at=started, code=intervention,
                )
                blocked["data"] = {"manual_intervention": intervention}
                return blocked
            element_before = await self._locator_state(locator)
            before_url = await self._page_url(page)
            before_fingerprint = await self._page_fingerprint(page)
            baseline = {
                "url": before_url,
                "document_epoch": before.document_epoch,
                "page_ids": [ref.page_id for _item, ref in self._state_tracker.tab_refs()],
                "dialog_sequence": self._dialog_sequence,
                "download_sequence": self._download_sequence,
            }
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(
                started_at=started, code=scrub_browser_error(exc),
            )
        try:
            await locator.click(timeout=5_000)
            click_error = None
        except BrowserReferenceError as exc:
            return self._receipt_failure(before=before, started_at=started, code=exc.code)
        except Exception as exc:
            click_error = exc
        evidence = [{"single_dispatch": True, "click_returned": click_error is None}]
        # A JavaScript dialog deliberately blocks the page's event loop.  With
        # a dialog listener installed, Playwright can therefore time out the
        # click even though the one permitted dispatch demonstrably created
        # the dialog.  That event is authoritative effect evidence; it must
        # not trigger a retry or be downgraded to an unknown outcome.
        if click_error is not None and self._dialog_sequence > baseline["dialog_sequence"]:
            evidence.append({"dialog_appeared": True})
            ok = True
        elif click_error is not None:
            return self._receipt_failure(
                before=before, started_at=started,
                code=scrub_browser_error(click_error), dispatched=True, unknown=True,
                evidence=evidence,
            )
        else:
            ok = False
        if wait_spec is not None:
            ok, waited = await self._wait_after_dispatch(page, wait_spec, baseline)
            evidence.append(waited)
        elif not ok:
            deadline = asyncio.get_running_loop().time() + 3.0
            ok = False
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                active = self._reconcile_pages(self._context) or page
                try:
                    current = self._current_state(page)
                    current_url = await self._page_url(page)
                except Exception:
                    current, current_url = before, before_url
                current_pages = {
                    ref.page_id for _item, ref in self._state_tracker.tab_refs()
                }
                if current.document_epoch != before.document_epoch or current_url != before_url:
                    evidence.append({
                        "navigation_or_url_change": True,
                        "document_epoch_advanced": current.document_epoch != before.document_epoch,
                    })
                    ok = True
                    break
                if current_pages - set(baseline["page_ids"]):
                    evidence.append({"popup_created": True})
                    ok = True
                    break
                if self._dialog_sequence > baseline["dialog_sequence"]:
                    evidence.append({"dialog_appeared": True})
                    ok = True
                    break
                download_sequence, _download = self._page_downloads.get(id(page), (0, None))
                if download_sequence > baseline["download_sequence"]:
                    evidence.append({"download_created": True})
                    ok = True
                    break
                element_after = await self._locator_state(locator)
                if element_after and element_after != element_before:
                    evidence.append({"expected_element_state_changed": True})
                    ok = True
                    break
                if await self._page_fingerprint(active) != before_fingerprint:
                    evidence.append({"content_fingerprint_changed": True})
                    ok = True
                    break
        active = self._reconcile_pages(self._context) or page
        self._active_page = active
        self._publish()
        try:
            after = self._current_state(active)
        except BrowserReferenceError:
            after = before
        if not ok:
            return self._receipt_failure(
                before=before, after=after, started_at=started,
                code="postcondition_not_observed", dispatched=True, unknown=True,
                evidence=[*evidence, {"postcondition_observed": False}],
            )
        return self._receipt_success(
            before=before, after=after, started_at=started, evidence=evidence,
            data={"state": after.to_dict()},
        )

    async def _op_list_tabs(self, state_ref=None):
        started = time.time()
        try:
            _page, before = await self._resolve_state_page(state_ref)
            tabs = []
            for page, _ref in self._state_tracker.tab_refs()[:64]:
                try:
                    title_ref = self._current_state(page)
                    try:
                        title = await page.title()
                    except Exception:
                        title = ""
                    for _ in range(2):
                        before_read = self._current_state(page)
                        safe_url = safe_url_display(await self._page_url(page))
                        ref = self._current_state(page)
                        if (
                            ref.navigation_id == before_read.navigation_id
                            and ref.document_epoch == before_read.document_epoch
                        ):
                            break
                    else:
                        continue
                except BrowserReferenceError:
                    continue
                tabs.append({
                    "state": ref.to_dict(),
                    "safe_url": safe_url,
                    "title": str(title or "")[:300] if ref.navigation_id == title_ref.navigation_id else "",
                    "active": page is self._active_page,
                    "has_opener": self._state_tracker.opener(page) is not None,
                })
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        return self._receipt_success(
            before=before, after=before, started_at=started,
            evidence=[{"tab_count": len(tabs)}], data={"tabs": tabs},
        )

    async def _op_select_tab(self, state_ref):
        started = time.time()
        try:
            page = self._state_tracker.validate(state_ref)
            before = self._current_state(self._active_page) if self._active_page else None
            await page.bring_to_front()
            self._active_page = page
            after = self._current_state(page)
            self._publish()
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{"active_page_selected": True}],
            data={"state": after.to_dict()},
        )

    async def _op_close_tab(self, state_ref):
        started = time.time()
        try:
            page = self._state_tracker.validate(state_ref)
            before = self._current_state(page)
            await page.close()
            await asyncio.sleep(0)
            active = self._reconcile_pages(self._context) if self._context is not None else None
            after = self._current_state(active) if active is not None else None
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(
                before=locals().get("before"), started_at=started,
                code=scrub_browser_error(exc), dispatched=True, unknown=True,
            )
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{"page_closed": True}],
            data={"active_state": after.to_dict() if after else None},
        )

    async def _op_return_to_opener(self, state_ref):
        started = time.time()
        try:
            page = self._state_tracker.validate(state_ref)
            before = self._current_state(page)
            opener = self._state_tracker.opener(page)
            if opener is None:
                item = self._registry.get(id(page)) or {}
                opener = item.get("opener")
            if opener is None or self._page_is_closed(opener):
                return self._receipt_failure(
                    before=before, started_at=started, code="opener_unavailable",
                )
            await opener.bring_to_front()
            self._active_page = opener
            after = self._current_state(opener)
            self._publish()
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{"opener_selected": True}], data={"state": after.to_dict()},
        )

    async def _history_operation(self, action, state_ref=None):
        started = time.time()
        try:
            page, before = await self._resolve_state_page(state_ref, activate=True)
            before_url = await self._page_url(page)
            if action == "back":
                response = (
                    await self._bidi_history_step(page, -1)
                    if self._active_browser_type == "bidi" else
                    await page.go_back(wait_until="commit", timeout=10_000)
                )
            elif action == "forward":
                response = (
                    await self._bidi_history_step(page, 1)
                    if self._active_browser_type == "bidi" else
                    await page.go_forward(wait_until="commit", timeout=10_000)
                )
            else:
                response = await page.reload(wait_until="commit", timeout=10_000)
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except _TIMEOUT_ERRORS as exc:
            return self._receipt_failure(
                before=locals().get("before"), started_at=started,
                code=scrub_browser_error(exc), dispatched=True, unknown=True,
                evidence=[{"history_action": action}],
            )
        except Exception as exc:
            return self._receipt_failure(
                before=locals().get("before"), started_at=started,
                code=scrub_browser_error(exc), dispatched="before" in locals(),
                unknown="before" in locals(),
            )
        await asyncio.sleep(0)
        after = self._current_state(page)
        after_url = await self._page_url(page)
        if response is None and action != "reload" and after_url == before_url:
            return self._receipt_failure(
                before=before, started_at=started, code=f"history_{action}_unavailable",
                evidence=[{"no_history_entry": True}],
            )
        if after.document_epoch == before.document_epoch:
            after = self._state_tracker.note_navigation(page, reason=f"history_{action}")
            self._bound_refs.invalidate_page(before.page_id)
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{
                "history_action": action,
                "document_epoch_advanced": after.document_epoch > before.document_epoch,
                "safe_url_before": safe_url_display(before_url),
                "safe_url_after": safe_url_display(after_url),
            }],
            data={"state": after.to_dict(), "safe_url": safe_url_display(after_url)},
        )

    async def _op_reload(self, state_ref):
        return await self._history_operation("reload", state_ref)

    async def _op_go_back_safe(self, state_ref=None):
        return await self._history_operation("back", state_ref)

    async def _op_go_forward_safe(self, state_ref=None):
        return await self._history_operation("forward", state_ref)

    async def _op_open_new_tab_safe(self, url, state_ref=None, reuse_existing=True):
        started = time.time()
        try:
            normalized = _normalize_url(url)
            opener, before = await self._resolve_state_page(state_ref)
            matching_tab = None
            # BiDi needs a remote URL read. Do not add that wait when a
            # caller explicitly asks for a separate tab.
            if reuse_existing or self._active_browser_type != "bidi":
                tabs = sorted(
                    self._state_tracker.tab_refs(),
                    key=lambda item: item[0] is not self._active_page,
                )
                for existing, ref in tabs:
                    try:
                        if await self._page_url(existing) != normalized:
                            continue
                        if self._current_state(existing) != ref:
                            continue
                    except Exception as exc:
                        if reuse_existing and not isinstance(exc, BrowserReferenceError):
                            raise
                        continue
                    matching_tab = ref
                    break
            if reuse_existing and matching_tab is not None:
                selected = await self._op_select_tab(matching_tab.to_dict())
                if selected["ok"]:
                    selected["data"].update(
                        tab_reused=True, safe_url=safe_url_display(normalized),
                    )
                return selected
            page = await self._context.new_page()
            generation = self._register_context(self._context)
            self._register_page(
                page, context=self._context, generation=generation,
                opener=opener, make_active=True,
            )
            created = self._current_state(page)
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        try:
            await page.goto(normalized, wait_until="commit", timeout=20_000)
        except Exception as exc:
            return self._receipt_failure(
                before=before, started_at=started,
                code=scrub_browser_error(exc), dispatched=True, unknown=True,
                evidence=[{"tab_created": True, "navigation_verified": False}],
            )
        await asyncio.sleep(0)
        after = self._current_state(page)
        if after.document_epoch == created.document_epoch:
            after = self._state_tracker.note_navigation(page, reason="new_tab_navigation")
        try:
            await page.bring_to_front()
        except Exception:
            pass
        self._active_page = page
        self._publish()
        data = {
            "state": after.to_dict(),
            "safe_url": safe_url_display(await self._page_url(page)),
            "tab_reused": False,
        }
        if matching_tab is not None:
            try:
                self._state_tracker.validate(matching_tab)
            except BrowserReferenceError:
                pass
            else:
                data["matching_tab"] = {
                    "state": matching_tab.to_dict(),
                    "safe_url": safe_url_display(normalized),
                }
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{"tab_created": True, "navigation_commit": True}],
            data=data,
        )

    async def _op_search_web(self, query, state_ref=None):
        started = time.time()
        normalized_query = " ".join(str(query or "").split())[:300]
        if not normalized_query:
            return self._receipt_failure(started_at=started, code="empty_search_query")
        target = _SEARCH_TEMPLATE.format(query=quote_plus(normalized_query))
        navigation = await self._op_navigate_safe(target, state_ref)
        if not isinstance(navigation, dict) or navigation.get("ok") is not True:
            if isinstance(navigation, dict):
                navigation.setdefault("error", {"code": "search_engine_unavailable"})
            return navigation
        try:
            search_before = BrowserStateRef.parse(
                (navigation.get("receipt") or {}).get("before_state")
            )
        except BrowserReferenceError:
            search_before = None
        try:
            page = self._active_page
            state = self._current_state(page)
            preferred = page.locator("[data-osia-search-result] a[href]")
            count = await preferred.count()
            locator = preferred if count else page.locator(
                "main a[href], #search a[href], #b_results a[href]"
            )
            count = min(await locator.count(), 20)
            results = []
            seen = set()
            for index in range(count):
                item = locator.nth(index)
                try:
                    metadata = await item.evaluate(_SEARCH_RESULT_JS, self._element_binding_key)
                    bound = self._snapshot_element(item, metadata["nodeId"])
                    display = safe_url_display(metadata["href"])
                    if not display or display in seen:
                        continue
                    title = " ".join((metadata["title"] or "").split())[:300]
                    summary = " ".join((metadata["summary"] or "").split())[:600]
                except Exception:
                    continue
                seen.add(display)
                results.append({
                    "result_ref": self._bound_refs.issue(
                        "search_result", state, bound,
                    ),
                    "title": title,
                    "safe_destination": display,
                    "summary": summary,
                })
            after = self._current_state(page)
        except Exception as exc:
            return self._receipt_failure(
                before=(
                    search_before
                    or BrowserStateRef.parse(navigation["data"]["state"])
                ),
                started_at=started, code=scrub_browser_error(exc),
            )
        if not results:
            return self._receipt_failure(
                before=after, started_at=started, code="search_results_not_found",
                evidence=[{"search_navigation_verified": True}],
            )
        return self._receipt_success(
            before=search_before or after, after=after, started_at=started,
            evidence=[{"search_navigation_verified": True, "result_count": len(results)}],
            data={
                "state": after.to_dict(), "results": results,
                "engine": urlsplit(_SEARCH_TEMPLATE).hostname or "configured",
            },
        )

    async def _op_open_search_result(self, state_ref, result_ref):
        return await self._op_activate(
            state_ref,
            result_ref,
            {
                "condition_type": "document_epoch_change",
                "timeout_seconds": 15.0,
                "quiet_period_seconds": 0.1,
                "failure_policy": "outcome_unknown_if_dispatched",
            },
        )

    async def _op_paginate(self, state_ref, next_ref, max_pages=1):
        started = time.time()
        try:
            page, before = await self._resolve_state_page(state_ref, activate=True)
            locator = self._bound_refs.resolve(
                next_ref, before, kinds={"link", "button", "pagination"},
            )
            maximum = max(1, min(int(max_pages), 20))
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception:
            return self._receipt_failure(started_at=started, code="invalid_pagination_request")
        fingerprints = {await self._page_fingerprint(page)}
        completed = 0
        dispatches = 0
        status = "max_pages"
        evidence = []
        for _index in range(maximum):
            subscribed_page = page
            navigation_seen = asyncio.Event()

            def _navigation_handler(frame=None, *_args):
                try:
                    if frame is None or frame is subscribed_page.main_frame:
                        navigation_seen.set()
                except Exception:
                    navigation_seen.set()

            try:
                if await locator.count() != 1 or not await locator.is_visible():
                    status = "missing_next_control"
                    break
                current = self._current_state(page)
                before_url = await self._page_url(page)
                before_fingerprint = await self._page_fingerprint(page)
                page.on("framenavigated", _navigation_handler)
            except Exception as exc:
                return self._receipt_failure(
                    before=before, started_at=started, code=(
                        "stale_element_reference"
                        if completed == 0 else scrub_browser_error(exc)
                    ),
                    dispatched=completed > 0,
                    unknown=completed > 0,
                    evidence=evidence,
                )
            click_error = None
            try:
                dispatches += 1
                await locator.click(timeout=5_000)
            except Exception as exc:
                # A navigation can retire the exact locator before Playwright's
                # click coroutine returns.  The dispatch has still happened;
                # verify the subscribed navigation/content postcondition before
                # classifying its completion error.
                click_error = exc

            deadline = (
                asyncio.get_running_loop().time()
                + _PAGINATION_POSTCONDITION_TIMEOUT_S
            )
            changed = False
            navigation_changed = False
            content_changed = False
            stable_fingerprint = ""
            stable_since = 0.0
            new_state = current
            fingerprint = before_fingerprint
            try:
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
                    page = self._reconcile_pages(self._context) or page
                    new_state = self._current_state(page)
                    after_url = await self._page_url(page)
                    fingerprint = await self._page_fingerprint(page)
                    navigation_changed = bool(
                        navigation_seen.is_set()
                        or new_state.document_epoch != current.document_epoch
                        or new_state.navigation_id != current.navigation_id
                        or after_url != before_url
                    )
                    if navigation_changed:
                        if new_state.document_epoch == current.document_epoch:
                            self._bound_refs.invalidate_page(current.page_id)
                            new_state = self._state_tracker.note_navigation(
                                page, reason="pagination_navigation_verified",
                            )
                        changed = True
                        break
                    if fingerprint != before_fingerprint:
                        now = asyncio.get_running_loop().time()
                        if fingerprint != stable_fingerprint:
                            stable_fingerprint = fingerprint
                            stable_since = now
                        elif now - stable_since >= 0.2:
                            content_changed = True
                            changed = True
                            break
                    else:
                        stable_fingerprint = ""
                        stable_since = 0.0
            finally:
                self._emitter_off(
                    subscribed_page, "framenavigated", _navigation_handler,
                )
            if not changed:
                return self._receipt_failure(
                    before=before, after=self._current_state(page), started_at=started,
                    code="pagination_postcondition_timeout",
                    dispatched=True, unknown=True,
                    evidence=evidence + [{
                        "single_dispatch": dispatches == 1,
                        "page_count_advanced": completed,
                        "navigation_changed": False,
                        "content_changed": False,
                    }],
                )
            completed += 1
            evidence.append({
                "pagination_step": completed,
                "single_dispatch": True,
                "navigation_changed": navigation_changed,
                "document_epoch_advanced": (
                    new_state.document_epoch > current.document_epoch
                ),
                "content_changed": content_changed,
                "dispatch_completion_error_after_verified_effect": (
                    click_error is not None
                ),
            })
            if fingerprint in fingerprints:
                status = "repeated_document"
                break
            fingerprints.add(fingerprint)
            candidates = page.locator(
                "a[rel=next],button[data-next],a[data-next],"
                "button[aria-label='Next'],a[aria-label='Next']"
            )
            if await candidates.count() != 1:
                status = "missing_next_control"
                break
            locator = candidates.first
        if completed == 0:
            return self._receipt_failure(
                before=before, started_at=started,
                code="pagination_control_missing",
                evidence=[{"single_dispatch": dispatches == 1}],
            )
        after = self._current_state(page)
        return self._receipt_success(
            before=before, after=after, started_at=started, evidence=evidence,
            data={
                "state": after.to_dict(), "pages_advanced": completed,
                "status": status, "dispatch_count": dispatches,
            },
        )

    async def _op_infinite_scroll(self, state_ref, max_steps=8):
        started = time.time()
        try:
            page, before = await self._resolve_state_page(state_ref, activate=True)
            maximum = max(1, min(int(max_steps), 20))
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception:
            return self._receipt_failure(started_at=started, code="invalid_scroll_request")
        previous = await self._page_fingerprint(page)
        stable = 0
        completed = 0
        status = "max_steps"
        for _index in range(maximum):
            try:
                await page.evaluate(
                    "() => window.scrollBy(0, document.documentElement.clientHeight)"
                )
            except Exception as exc:
                return self._receipt_failure(
                    before=before, started_at=started,
                    code=scrub_browser_error(exc), dispatched=True, unknown=True,
                )
            completed += 1
            deadline = asyncio.get_running_loop().time() + 5.0
            changed = False
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
                current = await self._page_fingerprint(page)
                if current != previous:
                    previous = current
                    changed = True
                    stable = 0
                    break
            if not changed:
                stable += 1
                if stable >= 2:
                    try:
                        at_end = await page.evaluate(
                            "() => Math.ceil(scrollY + innerHeight) >= document.documentElement.scrollHeight"
                        )
                    except Exception:
                        at_end = False
                    status = "reached_end" if at_end else "no_progress"
                    break
        after = self._current_state(page)
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{
                "bounded_scroll_steps": completed,
                "stable_observations": stable,
            }],
            data={"state": after.to_dict(), "steps": completed, "status": status},
        )

    async def _op_list_dialogs(self, state_ref):
        started = time.time()
        try:
            _page, state = await self._resolve_state_page(state_ref)
            dialogs = []
            for ref_id, item in list(self._pending_dialogs.items())[:16]:
                if item["state"].page_id != state.page_id:
                    continue
                try:
                    kind = str(item["dialog"].type or "")[:32]
                except Exception:
                    kind = "unknown"
                dialogs.append({"dialog_ref": ref_id, "type": kind})
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        return self._receipt_success(
            before=state, after=state, started_at=started,
            evidence=[{"dialog_count": len(dialogs)}], data={"dialogs": dialogs},
        )

    async def _op_handle_dialog(
        self, state_ref, dialog_ref, action, prompt_text=None,
    ):
        started = time.time()
        if action not in {"accept", "dismiss"}:
            return self._receipt_failure(started_at=started, code="invalid_dialog_action")
        try:
            _page, before = await self._resolve_state_page(state_ref)
            dialog = self._bound_refs.resolve(dialog_ref, before, kinds={"dialog"})
            item = self._pending_dialogs.get(dialog_ref)
            if item is None:
                raise BrowserReferenceError("stale_dialog_reference")
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        try:
            if action == "accept":
                if prompt_text is None:
                    await dialog.accept()
                else:
                    await dialog.accept(str(prompt_text)[:4_000])
            else:
                await dialog.dismiss()
            self._pending_dialogs.pop(dialog_ref, None)
            after = self._current_state(item["page"])
        except Exception as exc:
            return self._receipt_failure(
                before=before, started_at=started,
                code=scrub_browser_error(exc), dispatched=True, unknown=True,
            )
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{"dialog_closed": True, "dialog_action": action}],
            data={"state": after.to_dict()},
        )

    async def _op_prepare_upload(self, arguments):
        values = dict(arguments or {})
        try:
            page, state = await self._resolve_state_page(values.get("state_ref"))
            element_ref = str(values.get("element_ref") or "")
            self._bound_refs.resolve(element_ref, state, kinds={"control", "button"})
            artifact_id = str(values.get("artifact_id") or "")
            local_path = values.get("local_path")
            if bool(artifact_id) == bool(local_path):
                raise BrowserArtifactError("exactly_one_upload_source_required")
            path = (
                get_browser_artifact_store().resolve_private_path(artifact_id)
                if artifact_id else Path(str(local_path))
            )
            validated = validate_upload_path(path)
            origin = safe_origin(await self._page_url(page))
            now = time.time()
            for existing_ref, existing in list(self._prepared_uploads.items()):
                if now >= float(existing.get("expires_at", 0)):
                    self._prepared_uploads.pop(existing_ref, None)
                    continue
                if (
                    existing.get("state") == state
                    and existing.get("element_ref") == element_ref
                    and existing.get("origin") == origin
                    and existing.get("validated") == validated
                ):
                    return {
                        "prepared_upload_ref": existing_ref,
                        "destination_origin": origin,
                        "file_size": validated.size,
                        "detected_mime": validated.mime_type,
                        "operation_expires_at": existing["expires_at"],
                    }
            prepared_ref = opaque_ref("upload")
            expires_at = now + 120.0
            self._prepared_uploads[prepared_ref] = {
                "validated": validated,
                "state": state,
                "element_ref": element_ref,
                "origin": origin,
                "expires_at": expires_at,
            }
            while len(self._prepared_uploads) > 64:
                self._prepared_uploads.pop(next(iter(self._prepared_uploads)))
            return {
                "prepared_upload_ref": prepared_ref,
                "destination_origin": origin,
                "file_size": validated.size,
                "detected_mime": validated.mime_type,
                "operation_expires_at": expires_at,
            }
        except BrowserReferenceError as exc:
            return {"error": exc.code}
        except BrowserArtifactError as exc:
            return {"error": exc.code}
        except Exception as exc:
            return {"error": scrub_browser_error(exc)}

    async def _op_prepare_action(self, arguments):
        values = dict(arguments or {})
        try:
            page, state = await self._resolve_state_page(values.get("state_ref"))
            locator = self._bound_refs.resolve(
                str(values.get("element_ref") or ""), state,
                kinds={"link", "button", "control"},
            )
            intervention = await self._target_manual_intervention(locator)
            if intervention:
                return {"error": intervention}
            metadata = await locator.evaluate(r"""
                el => ({
                  tag: el.tagName.toLowerCase(),
                  type: String(el.type || '').toLowerCase(),
                  role: String(el.getAttribute('role') || '').toLowerCase(),
                  href: el.href || '',
                  markup: String(el.outerHTML || '').slice(0, 12000),
                  inForm: Boolean(el.form || el.closest('form')),
                  formMethod: String((el.form || el.closest('form'))?.method || '').toLowerCase(),
                })
            """)
            if not isinstance(metadata, dict):
                raise RuntimeError("invalid_action_metadata")
            tag = str(metadata.get("tag") or "")
            input_type = str(metadata.get("type") or "")
            final = bool(
                input_type == "submit"
                or (tag == "button" and metadata.get("inForm") is True)
                or str(metadata.get("role") or "") == "submit"
                or str(metadata.get("href") or "").startswith(("mailto:", "tel:"))
            )
            destination = (
                safe_origin(metadata.get("href"))
                or safe_origin(await self._page_url(page))
            )
            target_binding = hashlib.sha256(json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8", errors="replace")).hexdigest()
            return {
                "trusted_final_action": final,
                "destination_origin": destination,
                "trusted_target_binding": target_binding,
            }
        except BrowserReferenceError as exc:
            return {"error": exc.code}
        except Exception as exc:
            return {"error": scrub_browser_error(exc)}

    async def _op_upload_file(
        self, state_ref, element_ref, artifact_id=None, local_path=None,
        prepared_upload_ref=None, destination_origin=None, file_size=None,
        detected_mime=None, operation_expires_at=None,
    ):
        del destination_origin, file_size, detected_mime, operation_expires_at
        started = time.time()
        prepared = self._prepared_uploads.pop(str(prepared_upload_ref or ""), None)
        if prepared is None:
            prepared_values = await self._op_prepare_upload({
                "state_ref": state_ref, "element_ref": element_ref,
                "artifact_id": artifact_id, "local_path": local_path,
            })
            if "error" in prepared_values:
                return self._receipt_failure(
                    started_at=started, code=prepared_values["error"],
                )
            prepared = self._prepared_uploads.pop(
                prepared_values["prepared_upload_ref"], None,
            )
        try:
            if prepared is None or time.time() >= prepared["expires_at"]:
                raise BrowserArtifactError("upload_authorization_expired")
            page, before = await self._resolve_state_page(state_ref, activate=True)
            if before != prepared["state"] or element_ref != prepared["element_ref"]:
                raise BrowserArtifactError("upload_binding_changed")
            if safe_origin(await self._page_url(page)) != prepared["origin"]:
                raise BrowserArtifactError("upload_origin_changed")
            validated = revalidate_upload(prepared["validated"])
            locator = self._bound_refs.resolve(
                element_ref, before, kinds={"control", "button"},
            )
            tag = await locator.evaluate(
                "el => ({tag: el.tagName.toLowerCase(), type: String(el.type || '').toLowerCase()})"
            )
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except BrowserArtifactError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        dispatched = False
        try:
            if tag == {"tag": "input", "type": "file"}:
                dispatched = True
                await locator.set_input_files(str(validated.canonical_path))
                input_locator = locator
            else:
                async with page.expect_file_chooser(timeout=10_000) as chooser_info:
                    dispatched = True
                    await locator.click(timeout=5_000)
                chooser = await chooser_info.value
                await chooser.set_files(str(validated.canonical_path))
                input_locator = chooser.element
            selected = await input_locator.evaluate(
                "el => el.files ? el.files.length : 0"
            )
        except Exception as exc:
            return self._receipt_failure(
                before=before, started_at=started,
                code=scrub_browser_error(exc), dispatched=dispatched,
                unknown=dispatched, evidence=[{"single_dispatch": dispatched}],
            )
        after = self._current_state(page)
        if int(selected or 0) < 1:
            return self._receipt_failure(
                before=before, started_at=started,
                code="upload_selection_unverified", dispatched=True, unknown=True,
                evidence=[{"selected_file_count": 0}],
            )
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{
                "single_dispatch": True,
                "upload_selection_verified": True,
                "safe_file_count": int(selected),
            }],
            data={
                "state": after.to_dict(),
                "selected_file_count": int(selected),
                "size": validated.size,
                "mime_type": validated.mime_type,
                "classification": validated.classification,
            },
        )

    def _emit_browser_progress(self, phase, **safe_values):
        from osia.tool_attempts import (
            current_tool_attempt_id,
            publish_tool_attempt_event,
        )

        tool_attempt_id = current_tool_attempt_id()
        if tool_attempt_id:
            publish_tool_attempt_event("browser_progress", str(phase)[:32])
        callback = self._browser_progress_callback
        if callback is None:
            return
        frame = {"phase": str(phase)[:32]}
        if tool_attempt_id:
            frame["tool_attempt_id"] = tool_attempt_id
        for key, value in safe_values.items():
            if isinstance(value, str):
                frame[str(key)[:32]] = value[:300]
            elif value is None or isinstance(value, (bool, int, float)):
                frame[str(key)[:32]] = value
        try:
            callback(frame)
        except Exception:
            pass

    async def _op_download_file(self, state_ref, element_ref):
        started = time.time()
        operation_id = opaque_ref("download")
        temporary = None
        try:
            page, before = await self._resolve_state_page(state_ref, activate=True)
            locator = self._bound_refs.resolve(
                element_ref, before,
                kinds={"link", "button", "search_result"},
            )
            if await locator.count() != 1 or not await locator.is_visible():
                return self._receipt_failure(
                    before=before, started_at=started, code="target_not_actionable",
                )
            metadata = await locator.evaluate(
                "el => ({tag: el.tagName.toLowerCase(), href: el.href || '', download: el.hasAttribute('download')})"
            )
            if (
                isinstance(metadata, dict)
                and metadata.get("tag") == "a"
                and metadata.get("download") is True
                and safe_origin(metadata.get("href")) == safe_origin(
                    await self._page_url(page)
                )
            ):
                response = None
                try:
                    response = await page.context.request.head(
                        metadata["href"], timeout=5_000,
                        fail_on_status_code=False,
                    )
                    raw_length = response.headers.get("content-length", "")
                    content_length = int(raw_length)
                    if content_length < 0 or content_length > MAX_DOWNLOAD_BYTES:
                        return self._receipt_failure(
                            before=before, started_at=started,
                            code="file_size_limit_exceeded",
                            evidence=[{"content_length_preflight": True}],
                        )
                except (TypeError, ValueError, OverflowError):
                    pass
                except Exception:
                    # Missing/untrustworthy HEAD metadata is not proof of a
                    # failure; the post-download bounded copy remains the cap.
                    pass
                finally:
                    if response is not None:
                        try:
                            await response.dispose()
                        except Exception:
                            pass
            temporary = operation_temp_dir(operation_id)
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except BrowserArtifactError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        self._emit_browser_progress("waiting")
        try:
            async with page.expect_download(timeout=15_000) as download_info:
                await locator.click(timeout=5_000)
            download = await download_info.value
            self._emit_browser_progress("downloading")
            partial = temporary / "download.part"
            await download.save_as(str(partial))
            size = partial.stat().st_size
            if size > MAX_DOWNLOAD_BYTES:
                raise BrowserArtifactError("file_size_limit_exceeded")
            if self._download_session_bytes + size > MAX_SESSION_TEMP_BYTES:
                raise BrowserArtifactError("browser_session_quota_exceeded")
            self._emit_browser_progress("finalizing", bytes=size)
            reference = get_browser_artifact_store().finalize_download(
                partial, suggested_filename=download.suggested_filename,
            )
            self._download_session_bytes += size
            after = self._current_state(page)
        except BrowserArtifactError as exc:
            self._emit_browser_progress("failed")
            if temporary is not None:
                cleanup_operation_temp(temporary)
            return self._receipt_failure(
                before=before, started_at=started, code=exc.code,
                dispatched=True, unknown=False,
                evidence=[{"download_rejected": True, "partial_cleaned": True}],
            )
        except asyncio.CancelledError:
            self._emit_browser_progress("cancelled")
            if temporary is not None:
                cleanup_operation_temp(temporary)
            raise
        except Exception as exc:
            self._emit_browser_progress("failed")
            if temporary is not None:
                cleanup_operation_temp(temporary)
            return self._receipt_failure(
                before=before, started_at=started,
                code=scrub_browser_error(exc), dispatched=True, unknown=True,
                evidence=[{"single_dispatch": True, "partial_cleaned": True}],
            )
        if temporary is not None:
            cleanup_operation_temp(temporary)
        self._emit_browser_progress("complete", bytes=reference["size"])
        return self._receipt_success(
            before=before, after=after, started_at=started,
            evidence=[{
                "single_dispatch": True,
                "download_created": True,
                "download_finalized": True,
                "byte_size": reference["size"],
            }],
            data={"state": after.to_dict(), "artifact": reference},
        )

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
        return await self._op_navigate_safe(url)
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
        started = time.time()
        try:
            page, state = await self._resolve_state_page()
            locator = page.locator(selector)
            if await locator.count() != 1:
                return self._receipt_failure(
                    before=state, started_at=started, code="target_not_unique",
                )
            element_ref = self._bound_refs.issue("button", state, locator)
            return await self._op_activate(state.to_dict(), element_ref)
        except Exception as exc:
            return self._receipt_failure(
                started_at=started, code=scrub_browser_error(exc),
            )
        # Compatibility source below is intentionally unreachable; keeping the
        # surrounding launch-sensitive block stable minimizes regression risk.
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
            locator = page.locator(selector)
            if await locator.count() != 1:
                return "ERROR: fill target is not unique"
            intervention = await self._target_manual_intervention(locator)
            if intervention:
                return f"ERROR: manual intervention required ({intervention})"
            await page.fill(selector, value, timeout=5000)
            return "Field filled; the private value was withheld."
        except Exception as exc:
            return f"ERROR: fill failed ({scrub_browser_error(exc)})"

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
            return f"ERROR: page read failed ({scrub_browser_error(exc)})"

    async def _op_get_current_url(self):
        try:
            page = await self._ensure_browser()
            return f"Current URL: {safe_url_display(await self._page_url(page))}"
        except Exception as exc:
            return f"ERROR: URL read failed ({scrub_browser_error(exc)})"

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
            movement = await page.evaluate(f"""() => {{
                const before = window.scrollY;
                window.scrollBy({{top: {delta}, behavior: 'instant'}});
                return window.scrollY - before;
            }}""")
            if type(movement) not in {int, float} or not math.isfinite(movement):
                return "ERROR scrolling: browser scroll position could not be verified"
            if movement == 0:
                return "The browser page stayed at the same scroll position."
            actual_direction = "up" if movement < 0 else "down"
            return f"Scrolled {actual_direction} by {abs(movement):g}px"
        except Exception as exc:
            return f"ERROR scrolling: {scrub_browser_error(exc)}"

    async def _op_scroll_bound(self, state_ref, element_ref, direction="down", amount=600):
        started = time.time()
        try:
            if state_ref is None or direction not in {"up", "down"} or int(amount) < 1:
                return self._receipt_failure(started_at=started, code="invalid_scroll_request")
            page, state = await self._resolve_state_page(state_ref)
            locator = (
                self._bound_refs.resolve(element_ref, state, kinds={
                    "link", "button", "control", "form", "table", "list", "scroll_region",
                }) if element_ref else page.locator(":root")
            )
            delta = int(amount) * (-1 if direction == "up" else 1)
        except BrowserReferenceError as exc:
            return self._receipt_failure(started_at=started, code=exc.code)
        except (TypeError, ValueError):
            return self._receipt_failure(started_at=started, code="invalid_scroll_request")
        except Exception as exc:
            return self._receipt_failure(started_at=started, code=scrub_browser_error(exc))
        try:
            result = await locator.evaluate(_SCROLL_BOUND_JS, delta)
            if result is None:
                raise BrowserReferenceError("stale_element_reference")
            movement = result["movement"]
            if type(movement) not in {int, float} or not math.isfinite(movement):
                raise ValueError("unverified_scroll_movement")
        except BrowserReferenceError as exc:
            return self._receipt_failure(before=state, started_at=started, code=exc.code)
        except Exception as exc:
            return self._receipt_failure(
                before=state, started_at=started, code=scrub_browser_error(exc),
                dispatched=True, unknown=True,
            )
        return self._receipt_success(
            before=state, after=self._current_state(page), started_at=started,
            evidence=[{"scroll_movement_observed": True, "movement": movement}],
            data={
                "movement": movement, "nested": result["nested"] is True,
                "status": "moved" if movement else "at_boundary",
            },
        )

    async def _op_go_back(self):
        return await self._op_go_back_safe()
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
        return await self._op_go_forward_safe()
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
            print(
                "[OSIA Browser] Firefox BiDi launch failed: "
                f"{scrub_browser_error(exc)}"
            )
            await self._disconnect_active_browser()
            return None
        try:
            await page.bring_to_front()
        except Exception:
            pass
        _raise_browser_window(self._active_browser_type)
        state = self._current_state(page)
        if url:
            return await self._op_navigate_safe(url, state.to_dict())
        if not url:
            return self._receipt_success(
                before=state, after=state, started_at=time.time(),
                evidence=[{"browser_ready": True, "backend": "bidi"}],
                data={"state": state.to_dict(), "backend": "firefox"},
                text="Opened Firefox (driven over WebDriver BiDi)",
            )
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
                    print("[OSIA Browser] CDP launch failed (details withheld)")
                    return None
            page = await self._ensure_cdp_browser(binary)
        except Exception as exc:
            print(
                "[OSIA Browser] CDP connect failed: "
                f"{scrub_browser_error(exc)}"
            )
            return None
        label = _canonical_browser_name(binary)
        verb = "Reusing existing" if reused else "Opened"
        try:
            await page.bring_to_front()
        except Exception:
            pass
        _raise_browser_window(self._active_browser_type)
        state = self._current_state(page)
        if url:
            return await self._op_navigate_safe(url, state.to_dict())
        if not url:
            return self._receipt_success(
                before=state, after=state, started_at=time.time(),
                evidence=[{"browser_ready": True, "backend": "cdp"}],
                data={"state": state.to_dict(), "backend": label},
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
        return await self._op_open_new_tab_safe(url, reuse_existing=False)
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
            return f"ERROR: click preparation failed ({scrub_browser_error(exc)})"
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
        if locator is None:
            return self._receipt_failure(
                before=self._current_state(page), started_at=time.time(),
                code="target_not_found",
            )
        state = self._current_state(page)
        element_ref = self._bound_refs.issue("button", state, locator)
        return await self._op_activate(state.to_dict(), element_ref)
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
            state = self._current_state(page)
            element_ref = self._bound_refs.issue("button", state, locator)
            return await self._op_activate(state.to_dict(), element_ref)
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
        started = time.time()
        try:
            page, state = await self._resolve_state_page()
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
            after = self._current_state(page)
        except _TIMEOUT_ERRORS as exc:
            return self._receipt_failure(
                before=locals().get("state"), started_at=started,
                code=scrub_browser_error(exc),
                evidence=[{"dom_content_loaded": False}],
            )
        except Exception as exc:
            return self._receipt_failure(
                before=locals().get("state"), started_at=started,
                code=scrub_browser_error(exc),
            )
        return self._receipt_success(
            before=state, after=after, started_at=started,
            evidence=[{"dom_content_loaded": True}],
            data={
                "state": after.to_dict(),
                "safe_url": safe_url_display(await self._page_url(page)),
            },
        )
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
            return f"ERROR: page read failed ({scrub_browser_error(exc)})"

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
            return f"ERROR scrolling: {scrub_browser_error(exc)}"

    async def _op_type_text(self, selector, text):
        try:
            page = await self._ensure_browser()
            locator = page.locator(selector)
            if await locator.count() != 1:
                return "ERROR: text target is not unique"
            intervention = await self._target_manual_intervention(locator)
            if intervention:
                return f"ERROR: manual intervention required ({intervention})"
            await page.click(selector, timeout=5000)
            await page.keyboard.type(text)
            return "Text entered; the private value was withheld."
        except Exception as exc:
            return f"ERROR: text entry failed ({scrub_browser_error(exc)})"

    async def _op_press_key(self, key):
        if not key:
            return "ERROR press_key: key is required"
        try:
            page = await self._ensure_browser()
            await page.keyboard.press(key)
            return f"Pressed key: {key}"
        except Exception as exc:
            return f"ERROR pressing browser key: {scrub_browser_error(exc)}"

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
        return {"available": True, "url": safe_url_display(url), "checks": checks}

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
            return form_fill_error_receipt(
                "page_unavailable", str(field_ref or "")[:128],
                dispatched=False,
            )
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
    category = scrub_browser_error(error)
    if prefix == "navigation":
        return f"ERROR: the browser did not open ({category})"
    return f"ERROR {prefix}: {category}"


def navigate(url: str):
    try:
        return _owner.call("navigate", url)
    except Exception as exc:
        return _owner_error("navigation", exc)


def click(selector: str):
    try:
        return _owner.call("click", selector)
    except Exception as exc:
        return _owner_error("clicking page element", exc)


def fill(selector: str, value: str) -> str:
    try:
        return _owner.call("fill", selector, value)
    except Exception as exc:
        return _owner_error("filling page field", exc)


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


def scroll_bound(state_ref, element_ref=None, direction="down", amount=600):
    try:
        return _owner.call("scroll_bound", state_ref, element_ref, direction, amount)
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
        receipt = make_receipt(
            before_state=None, after_state=None,
            dispatch_state=DispatchState.NOT_DISPATCHED,
            outcome=ReceiptOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
            evidence=[{"browser_unavailable": True}],
        )
        return action_result(
            ok=False, receipt=receipt, error_code="browser_unavailable",
            data={
                "supported_installation_options": [
                    "a compatible system Chromium-family browser", "Firefox",
                ],
            },
        )
    note = ""
    try:
        if _canonical_browser_name(binary) == "firefox":
            result = _owner.call("open_firefox_bidi", url)
            note = " (WebDriver BiDi unavailable — page tools cannot drive this window)"
        else:
            result = _owner.call("open_cdp_browser", binary, url)
            note = " (CDP unavailable — page tools cannot drive this window)"
    except Exception as exc:
        print("[OSIA Browser] automated browser launch failed (details withheld)")
        result = None
    if result is not None:
        return result
    receipt = make_receipt(
        before_state=None, after_state=None,
        dispatch_state=DispatchState.NOT_DISPATCHED,
        outcome=ReceiptOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
        evidence=[{"browser_unavailable": True}],
    )
    return action_result(
        ok=False, receipt=receipt, error_code="browser_unavailable",
        data={
            "supported_installation_options": [
                "a compatible system Chromium-family browser", "Firefox",
            ],
        },
    )
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
        return _owner_error(
            f"opening new tab {safe_url_display(_normalize_url(url))}", exc,
        )


def find_and_click(text: str):
    try:
        return _owner.call("find_and_click", text)
    except Exception as exc:
        return _owner_error("clicking named page element", exc)


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
        return _owner_error("typing into page field", exc)


def press_key(key: str) -> str:
    try:
        return _owner.call("press_key", key)
    except Exception as exc:
        return _owner_error("pressing browser key", exc)


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
            "fill_form_field", field_ref, value, timeout=22.0,
        )
    except Exception:
        return form_fill_error_receipt(
            "owner_receipt_unavailable", str(field_ref or "")[:128],
            dispatched=True,
        )


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


def get_browser_state() -> dict:
    return _owner.call("browser_state", timeout=10.0)


def navigate_browser(
    url: str, state_ref: dict | None = None, wait_spec: dict | None = None,
) -> dict:
    return _owner.call(
        "navigate_safe", url, state_ref, wait_spec, timeout=45.0,
    )


def inspect_browser_page(
    state_ref: dict | None = None,
    mode: str = "snapshot",
    target_ref: str | None = None,
    cursor: str | None = None,
    frame_ref: str | None = None,
    max_items: int = 40,
) -> dict:
    return _owner.call(
        "inspect_page", state_ref, mode, target_ref, cursor, frame_ref,
        max_items, timeout=20.0,
    )


def wait_for_browser_condition(state_ref: dict, wait_spec: dict) -> dict:
    try:
        owner_timeout = max(
            65.0, WaitSpec.parse(wait_spec).timeout_seconds + 5.0,
        )
    except ValueError:
        owner_timeout = 65.0
    return _owner.call("wait_spec", state_ref, wait_spec, timeout=owner_timeout)


def activate_browser_element(
    state_ref: dict, element_ref: str, wait_spec: dict | None = None,
) -> dict:
    try:
        owner_timeout = max(
            65.0, WaitSpec.parse(wait_spec).timeout_seconds + 5.0,
        )
    except ValueError:
        owner_timeout = 65.0
    return _owner.call(
        "activate", state_ref, element_ref, wait_spec, True,
        timeout=owner_timeout,
    )


def list_browser_tabs(state_ref: dict | None = None) -> dict:
    return _owner.call("list_tabs", state_ref, timeout=10.0)


def select_browser_tab(state_ref: dict) -> dict:
    return _owner.call("select_tab", state_ref, timeout=10.0)


def close_browser_tab(state_ref: dict) -> dict:
    return _owner.call("close_tab", state_ref, timeout=15.0)


def reload_browser_page(state_ref: dict) -> dict:
    return _owner.call("reload", state_ref, timeout=20.0)


def browser_back(state_ref: dict) -> dict:
    return _owner.call("go_back_safe", state_ref, timeout=20.0)


def browser_forward(state_ref: dict) -> dict:
    return _owner.call("go_forward_safe", state_ref, timeout=20.0)


def open_new_browser_tab(
    url: str, state_ref: dict | None = None, reuse_existing: bool = True,
) -> dict:
    return _owner.call("open_new_tab_safe", url, state_ref, reuse_existing, timeout=30.0)


def return_to_browser_opener(state_ref: dict) -> dict:
    return _owner.call("return_to_opener", state_ref, timeout=10.0)


def search_web(query: str, state_ref: dict | None = None) -> dict:
    return _owner.call("search_web", query, state_ref, timeout=45.0)


def open_search_result(state_ref: dict, result_ref: str) -> dict:
    return _owner.call(
        "open_search_result", state_ref, result_ref, timeout=30.0,
    )


def paginate_browser(state_ref: dict, next_ref: str, max_pages: int = 1) -> dict:
    return _owner.call(
        "paginate", state_ref, next_ref, max_pages,
        timeout=min(45.0 + max(1, min(int(max_pages), 20)) * 20.0, 450.0),
    )


def scroll_infinite(state_ref: dict, max_steps: int = 8) -> dict:
    return _owner.call(
        "infinite_scroll", state_ref, max_steps,
        timeout=min(20.0 + max(1, min(int(max_steps), 20)) * 6.0, 150.0),
    )


def list_browser_dialogs(state_ref: dict) -> dict:
    return _owner.call("list_dialogs", state_ref, timeout=10.0)


def handle_browser_dialog(
    state_ref: dict, dialog_ref: str, action: str,
    prompt_text: str | None = None,
) -> dict:
    return _owner.call(
        "handle_dialog", state_ref, dialog_ref, action, prompt_text,
        timeout=15.0,
    )


def prepare_upload_authorization(arguments: dict) -> dict:
    """Bind one upload to an exact live page, origin, target and file identity."""
    return _owner.call("prepare_upload", dict(arguments), timeout=60.0)


def prepare_browser_action_authorization(arguments: dict) -> dict:
    return _owner.call("prepare_action", dict(arguments), timeout=10.0)


def upload_browser_file(
    state_ref: dict,
    element_ref: str,
    artifact_id: str | None = None,
    local_path: str | None = None,
    prepared_upload_ref: str | None = None,
    destination_origin: str | None = None,
    file_size: int | None = None,
    detected_mime: str | None = None,
    operation_expires_at: float | None = None,
) -> dict:
    return _owner.call(
        "upload_file", state_ref, element_ref, artifact_id, local_path,
        prepared_upload_ref, destination_origin, file_size, detected_mime,
        operation_expires_at, timeout=90.0,
    )


def download_browser_file(state_ref: dict, element_ref: str) -> dict:
    return _owner.call(
        "download_file", state_ref, element_ref, timeout=180.0,
    )


def set_browser_progress_callback(callback) -> None:
    _owner._browser_progress_callback = callback


def _set_search_template_for_testing(template: str) -> str:
    global _SEARCH_TEMPLATE
    previous = _SEARCH_TEMPLATE
    if "{query}" not in str(template):
        raise ValueError("search template must contain {query}")
    _SEARCH_TEMPLATE = str(template)
    return previous


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
        print(
            "[OSIA Browser] owner shutdown failed: "
            f"{scrub_browser_error(exc)}"
        )
