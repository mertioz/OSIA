#!/usr/bin/env python3
"""Deterministic deep-link / URL-scheme registry for OSIA (WOR-85).

OSIA should reach a destination the FAST, reliable way before ever falling back
to mouse/vision. Many native apps expose a URI scheme — Steam (``steam://``),
Spotify (``spotify:``), VS Code (``vscode://``) — that jumps straight to a view
(library, console, a file) without a single click. This module is the small,
extensible registry behind the ``open_uri`` agent tool:

  * ``DEEP_LINKS``   — app -> action -> URI template (seeded, easy to extend).
  * ``resolve()``    — build a concrete URI from an app/action + params.
  * ``is_allowed()`` — validate an arbitrary URI against a NARROW allow-set,
                       explicitly blocking dangerous schemes (file:, javascript:,
                       data:, vbscript:) and bare paths so nothing unsafe ever
                       reaches the per-OS launcher.

The actual launch (``os.startfile`` / ``open`` / ``xdg-open``) lives in
``agent.tool_open_uri``; this module only resolves and validates strings. It is
pure string work with no OS-specific branches, so it is cross-platform by
construction.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Registry: app -> action -> URI template. Small but structured — add a new app
# or route by dropping an entry in here; no other code has to change. Templates
# use ``{param}`` placeholders filled by ``resolve()`` (URL-encoded + length
# capped). Steam ``store``/``run`` take an ``{appid}``; name->appid resolution is
# a deliberate FOLLOW-UP (WOR-85), but a literal appid already works today.
# ---------------------------------------------------------------------------
DEEP_LINKS = {
    "steam": {
        "library":   "steam://open/games",
        "friends":   "steam://open/friends",
        "console":   "steam://open/console",
        "downloads": "steam://open/downloads",
        "settings":  "steam://settings",
        "store":     "steam://store/{appid}",
        "run":       "steam://run/{appid}",
    },
    "spotify": {
        "app":    "spotify:",
        "search": "spotify:search:{query}",
    },
    "vscode": {
        "open": "vscode://file/{path}",
    },
}


def _schemes_from_registry() -> set:
    """Collect the scheme (text before the first ``:``) of every template."""
    out = set()
    for actions in DEEP_LINKS.values():
        for template in actions.values():
            scheme = template.split(":", 1)[0].strip().lower()
            if scheme:
                out.add(scheme)
    return out


# Narrow allow-set of URI schemes OSIA may launch: every scheme present in the
# registry, plus a couple of safe siblings (vscode-insiders:, obs:). Start
# narrow — widen DELIBERATELY as new deterministic routes are added.
ALLOWED_SCHEMES = _schemes_from_registry() | {"vscode-insiders", "obs"}

# Schemes that are NEVER allowed, even if a future template typo'd one in — an
# explicit deny-list defends against local-file / script-execution vectors.
_BLOCKED_SCHEMES = {"file", "javascript", "data", "vbscript", "about", "blob"}

# Hard caps so a crafted URI can't blow past sane limits.
_MAX_URI_LEN = 2048
_MAX_PARAM_LEN = 512

# Control chars (incl. NUL / newline / CR / tab) must never appear in a launched
# URI — they enable header/argument smuggling on some handlers.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# RFC-3986-ish scheme: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) then ":".
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def resolve(app, action, **params):
    """Look up ``DEEP_LINKS[app][action]`` and fill its ``{param}`` placeholders.

    Param values are URL-encoded and length-capped so a spaced / Unicode /
    special-char ``{query}`` or ``{path}`` can't corrupt or break out of the
    URI. Returns the concrete URI string, or ``None`` when the app/action is
    unknown or a required placeholder wasn't supplied (the caller then falls
    through to another mechanism).
    """
    try:
        template = DEEP_LINKS[str(app).strip().lower()][str(action).strip().lower()]
    except (KeyError, AttributeError, TypeError):
        return None
    safe = {key: quote(str(value)[:_MAX_PARAM_LEN], safe="") for key, value in params.items()}
    try:
        return template.format(**safe)
    except (KeyError, IndexError):
        # Template needs a placeholder the caller didn't provide -> can't resolve.
        return None


def is_allowed(uri) -> bool:
    """True iff ``uri`` is a safe, whitelisted deep link OSIA may launch.

    Requires an explicit ``scheme:`` prefix whose scheme is in
    ``ALLOWED_SCHEMES`` and NOT in the dangerous deny-list (file:, javascript:,
    data:, vbscript:, …). Bare paths (no scheme), embedded control chars /
    newlines, and over-long strings are all rejected. This is validation only —
    it never launches anything.
    """
    if not isinstance(uri, str):
        return False
    if not uri or len(uri) > _MAX_URI_LEN:
        return False
    if _CONTROL_CHARS_RE.search(uri):
        return False
    m = _SCHEME_RE.match(uri)
    if not m:
        return False  # bare path or no scheme prefix
    scheme = m.group(0)[:-1].lower()
    if scheme in _BLOCKED_SCHEMES:
        return False
    return scheme in ALLOWED_SCHEMES
