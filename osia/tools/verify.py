#!/usr/bin/env python3
"""Deterministic outcome-verification helpers for OSIA (WOR-87).

OSIA must confirm what actually happened WITHOUT a screenshot wherever possible —
vision is the last resort, not the default. "Firing" a deep link or "spawning" a
process is an ATTEMPT, not a confirmation; this module turns the deterministic
primitives OSIA already has into honest, model-callable confirmation tools:

  * :func:`app_installed`  — is an app resolvable to a real executable?
    (Windows: ``desktop_control.resolve_windows_app``.)
  * :func:`app_running`    — is the app's PROCESS alive? (psutil, via
    ``windows_app_candidates`` -> ``is_app_running``; reliable cross-platform for
    single-token executable names, best-effort for multi-word names off-Windows.)
  * :func:`app_window`     — does the app have a visible WINDOW right now?
    (Windows: ``find_app_window_multi``.)
  * :func:`web_state`      — the live browser page's URL / text / element state,
    read from the real DOM and gated on OSIA actually driving its own browser
    (``browser._dom_gate_ok``).
  * :func:`steam_game_state` — re-exported from :mod:`osia.tools.steam`: a Steam
    game's install/run state from Steam's own manifests.

These COMPOSE existing helpers; they add no new OS access of their own. Every
function is defensive (never raises) and returns a plain, deterministic answer.
The thin string-formatting wrappers the model actually calls live in
``osia.agent`` (``tool_is_installed`` & co.).
"""

from __future__ import annotations

import sys

# Re-export so callers can reach the Steam reader via either module.
from osia.tools.steam import steam_game_state  # noqa: F401


def app_installed(app):
    """Tri-state: is ``app`` installed (resolvable to a concrete executable)?

    ``True``/``False`` on Windows (``resolve_windows_app`` across App Paths,
    Start-menu shortcuts, common install roots and the Store). ``None`` on
    platforms where this can't be determined deterministically (non-Windows), so
    the caller can say "unknown" rather than a misleading "no". Never raises.
    """
    if sys.platform != "win32":
        return None
    try:
        from osia.tools import desktop_control as _dc
        return _dc.resolve_windows_app(app) is not None
    except Exception:
        return None


def _app_candidates(app):
    """Process bases that identify ``app`` (resolved exe base + name token).
    Cross-platform string work; an empty set means "can't identify it".

    When OSIA has NOT confidently resolved ``app`` to a real executable (no
    Windows resolution, or off-Windows), the only candidate is the raw name
    token, so a 1-3 char query could exact-match an unrelated short-named
    process and yield a false "running: yes". We drop <4-char tokens in that
    case — mirroring ``steam._game_proc_candidates`` — so the honest direction
    is "running: no" rather than a wrong positive. A confidently-resolved exe
    base is trusted even when short.
    """
    from osia.tools import desktop_control as _dc
    resolved = None
    if sys.platform == "win32":
        try:
            resolved = _dc.resolve_windows_app(app)
        except Exception:
            resolved = None
    try:
        cands = _dc.windows_app_candidates(app, resolved)
    except Exception:
        return set()
    if resolved is None:
        cands = {c for c in cands if len(c) >= 4}
    return cands


def app_running(app) -> bool:
    """Is a PROCESS belonging to ``app`` currently alive? Deterministic
    (psutil process table), no vision. Reliable cross-platform for single-token
    executable names ('discord', 'steam', 'firefox'); for a multi-word display
    name ('vs code') it depends on Windows app resolution, so off-Windows it may
    miss the real process and answer "no". Never raises."""
    try:
        from osia.tools import desktop_control as _dc
        cands = _app_candidates(app)
        if not cands:
            return False
        return bool(_dc.is_app_running(cands))
    except Exception:
        return False


def app_window(app):
    """The app's visible window as ``(hwnd, title, pid)``, or ``None``.

    Windows-only (``find_app_window_multi``); returns ``None`` on Linux/Mac —
    the caller distinguishes "no window" from "can't check here" via
    ``sys.platform``. Never raises.
    """
    if sys.platform != "win32":
        return None
    try:
        from osia.tools import desktop_control as _dc
        return _dc.find_app_window_multi(_app_candidates(app))
    except Exception:
        return None


def web_state(expect_url=None, expect_text=None, expect_selector=None) -> dict:
    """Deterministically inspect the live browser page WITHOUT vision.

    Gated on OSIA actually driving its OWN browser and that browser being the
    foreground window (``browser._dom_gate_ok``) — so it reads the page the user
    is really looking at, never the user's personal window, and never LAUNCHES a
    browser. Returns a dict:

        {"available": bool, "reason": str,            # when unavailable
         "url": str|None, "checks": {<name>: bool}}   # when available

    ``checks`` carries one entry per expectation supplied:
      * ``url_contains``   — ``expect_url`` is a substring of the current URL.
      * ``text_contains``  — ``expect_text`` appears in the page's visible text.
      * ``selector_found`` — ``expect_selector`` (a CSS selector, or a visible
        label as a fallback) matches an element on the page.
    Never raises.
    """
    try:
        from osia.tools import browser_controller as _b
    except Exception:
        return {"available": False, "reason": "browser module unavailable"}

    try:
        return _b.get_web_state(expect_url, expect_text, expect_selector)
    except Exception as e:
        return {"available": False, "reason": f"page read failed: {e}"}
