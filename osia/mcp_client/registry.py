#!/usr/bin/env python3
"""WOR-44 Phase 2 — bridge connected MCP tools into the agent.

This is the ONLY place ``osia.mcp`` reaches into ``osia.agent``, and it does so
LAZILY (imports inside functions, guarded by try/except) so that:

  * ``osia.mcp`` stays standalone-importable — the Phase 1 self-test runs with
    no agent (and none of the agent's heavy deps) present; registration then
    simply skips the agent-map mutation.
  * ``agent.py``'s import graph is unchanged: ``get_system_prompt`` lazily
    imports THIS module, which imports neither ``osia.agent`` nor
    ``osia.mcp.manager`` at module load — so there is no circular import.

What it does, all WITHOUT touching any protected logic:

  * Registration mutates only the CONTENTS of ``agent.TOOL_MAP`` /
    ``agent.KNOWN_TOOL_NAMES`` (add/remove entries) under a lock, batched per
    server so a half-registered server is never visible to the parser/dispatch.
    Each MCP tool is registered under the namespaced key
    ``mcp__<server_id>__<tool_name>`` bound to a SYNC wrapper that delegates to
    ``MCPManager.call_tool`` (the Phase 1 bridge — returns a str / ``ERROR:``
    string, never raises), matching the native ``tool_*(...) -> str`` contract.

  * ``mcp_tool_prompt_block`` renders the ``## Connected MCP tools`` section that
    ``get_system_prompt`` appends, listing each currently-registered tool with a
    ``Usage:`` line generated from its JSON Schema.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

# Namespacing: mcp__<server_id>__<tool_name>. Matches the convention used for
# MCP tools elsewhere (e.g. mcp__<server>__<tool>) and cannot collide with the
# 48 native tool names (none contain "__").
_PREFIX = "mcp__"
_SEP = "__"

# Keep the per-call MCP timeout comfortably UNDER the agent tool executor's
# default wall-clock budget (osia.agent.TOOL_TIMEOUT_SECONDS == 30 s) so the
# wrapper returns its OWN ``ERROR: ... timed out`` string before the executor
# abandons the worker thread. We do not read or modify that constant — 25 s is a
# deliberately conservative margin.
WRAPPER_CALL_TIMEOUT = 25.0

# Identifier hygiene (WOR-44 Phase 2 review). MCP tool names and server ids are
# THIRD-PARTY data that ends up (a) interpolated verbatim into the system prompt
# and (b) used to form TOOL_MAP keys. We constrain both to a strict charset that
# excludes whitespace / newlines / markdown so a malicious server cannot inject
# prompt structure (SEC-01), and bound their length to prevent prompt bloat
# (SEC-02). Server ids additionally may not contain the "__" separator, which
# keeps the mcp__<server>__<tool> key unambiguous even when a tool name itself
# contains "__" (SEC-03). A tool / server failing these checks is skipped (its
# tools are simply not exposed to the model); the MCP client connection itself
# is unaffected.
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_MAX_ID_LEN = 128
# Cap how many MCP tools are LISTED in the system prompt (the agent maps stay
# complete, so every connected tool remains callable — only the prompt listing
# is trimmed, with a "(+N more)" note).
_MAX_PROMPT_TOOLS = 50


def _valid_tool_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name) <= _MAX_ID_LEN
        and bool(_ID_RE.match(name))
    )


def _valid_server_id(server_id: str) -> bool:
    return _valid_tool_name(server_id) and _SEP not in server_id


# ── Write/destructive classification + confirmation gate (WOR-44 Phase 5) ────
# A write/destructive MCP tool (create/update/delete/send/…) can have real side
# effects, and MCP tool metadata is third-party text. Before such a tool runs we
# may require a user confirmation. Classification prefers the server-supplied MCP
# annotations (readOnlyHint / destructiveHint); when absent it falls back to a
# name-verb heuristic. The heuristic is WRITE-BIASED: any write verb present wins
# over a read verb (so e.g. "list_and_delete" is treated as write), and a name
# matching neither is treated as write — the SAFE default, since a false "write"
# only costs an extra confirmation while a false "read" would skip the gate on a
# destructive call.
# The write list is deliberately BROAD (mutation + execution + destructive
# verbs): a false "write" only costs an extra confirmation, while a missing write
# verb is a dangerous false-READ that skips the gate on a destructive call. It
# covers state-change, lifecycle, sharing/permission, and EXECUTION verbs
# (run/execute/invoke/trigger/dispatch) — a tool that "runs" something can mutate.
_WRITE_VERBS = frozenset({
    # core CRUD / messaging (original set)
    "create", "update", "delete", "remove", "send", "post", "put", "patch",
    "write", "merge", "archive", "move", "add", "edit", "insert", "upload",
    "set", "cancel", "close", "comment", "append",
    # execution (can mutate — e.g. an SQL "run"/"query" runner)
    "run", "execute", "exec", "invoke", "trigger", "dispatch", "import",
    # destructive / lifecycle
    "purge", "drop", "truncate", "reset", "revoke", "disable", "enable",
    "kill", "terminate", "destroy", "wipe", "clear", "flush", "prune",
    "deactivate", "activate", "deploy", "rollback", "restore", "overwrite",
    "replace", "rename", "duplicate", "clone", "sync", "push", "commit",
    "revert", "format", "install", "uninstall", "start", "stop", "restart",
    "reboot", "schedule", "transfer", "publish", "unpublish",
    # approval / membership / sharing / metadata mutation
    "approve", "reject", "grant", "assign", "unassign", "charge", "pay",
    "refund", "share", "unshare", "lock", "unlock", "mute", "unmute", "ban",
    "unban", "invite", "subscribe", "unsubscribe", "mark", "flag", "pin",
    "unpin", "tag", "untag", "complete", "reopen", "resolve", "register",
    "unregister",
    # additional destructive / lifecycle / infra verbs (red-team residual
    # closure — a false write here only costs a confirmation, so erring broad).
    "void", "invalidate", "escalate", "rotate", "decommission", "deprecate",
    "settle", "dismiss", "quarantine", "scale", "expire", "provision",
    "acknowledge", "evict", "redeem", "detach", "drain", "suspend", "resume",
    "pause", "abort", "halt", "regenerate", "rebuild", "reindex", "block",
    "unblock", "deny", "withdraw", "deposit", "reverse",
})
# NOTE: "query" is intentionally NOT a read verb — an SQL/database "query" can
# INSERT/UPDATE/DELETE/DROP (e.g. d1_database_query), so a query-named tool falls
# through to the safe default (write) unless its name also carries a read verb.
_READ_VERBS = frozenset({
    "get", "list", "search", "read", "fetch", "find", "describe",
    "view", "export",
})


def classify_tool(name: str, annotations: Optional[dict] = None) -> bool:
    """Return ``True`` if a tool is WRITE/destructive (gate it), ``False`` if
    READ-only (run it freely).

    Order of evidence (annotations are authoritative when present):
      1. ``readOnlyHint is True``                                  -> read.
      2. ``destructiveHint`` truthy OR ``readOnlyHint is False``   -> write.
         (``bool(dh)`` coerces a truthy non-bool hint toward write — a dropped
         destructive signal would be unsafe.)
      3. Name-verb heuristic, WRITE-biased: any write verb (token OR name
         prefix) -> write; else any read verb -> read; else -> write (the safe
         default for an ambiguous name).
    """
    ann = annotations if isinstance(annotations, dict) else {}
    ro = ann.get("readOnlyHint")
    dh = ann.get("destructiveHint")
    if ro is True:
        return False
    if (dh is not None and bool(dh)) or ro is False:
        return True
    low = str(name or "").lower()
    tokens = {tok for tok in re.split(r"[^a-z0-9]+", low) if tok}
    # WRITE wins: an exact write-verb token, or a name starting with one. (Write
    # is checked BEFORE read so a name pairing both — e.g. "list_and_delete" —
    # is gated.)
    if tokens & _WRITE_VERBS or any(low.startswith(v) for v in _WRITE_VERBS):
        return True
    if tokens & _READ_VERBS or any(low.startswith(v) for v in _READ_VERBS):
        return False
    return True  # ambiguous -> write (safe)


# Process-wide confirmation provider. A UI (desktop Qt dialog / mobile WS round-
# trip — later sub-steps) registers ONE provider; the gate calls it for write
# tools when confirmation is enabled. Signature:
# ``provider(server_id, tool_name, summary) -> bool`` (True = approved).
# ``None`` means no provider is registered (an unattended cron/autonomous run).
_confirmation_provider = None


def set_confirmation_provider(fn) -> None:
    """Register (or clear, with ``None``) the process-wide confirmation provider.
    Idempotent; last writer wins. Assignment is atomic under the GIL."""
    global _confirmation_provider
    _confirmation_provider = fn


def get_confirmation_provider():
    """Return the registered confirmation provider, or ``None`` if unset."""
    return _confirmation_provider


def _setting_bool(key: str, default: bool) -> bool:
    """Read a boolean setting LIVE (so a toggle takes effect without
    reconnecting). Falls back to ``default`` if config is unavailable."""
    try:
        from osia.config import get_setting
        return bool(get_setting(key, default))
    except Exception:
        return default


def _confirm_summary(server_id: str, tool_name: str, kwargs: dict) -> str:
    """A short, bounded, human-facing description of a pending write call for the
    confirmation UI. Best-effort; never raises (and never inlines arg VALUES,
    which may be large or sensitive — only the arg names)."""
    try:
        keys = ", ".join(list(kwargs.keys())[:8]) if isinstance(kwargs, dict) else ""
    except Exception:
        keys = ""
    base = f"{tool_name} on {server_id}"
    return f"{base} (args: {keys})" if keys else base


def _confirm_gate(server_id: str, tool_name: str, kwargs: dict) -> Optional[str]:
    """Decide whether a WRITE tool may run. Returns ``None`` to ALLOW the call,
    or an ``ERROR: ...`` string to BLOCK it (declined / no approver). Reads the
    two gate settings LIVE. Never raises.

    Decision table (only ever reached for is_write tools):
      * mcp_confirm_writes False                -> allow.
      * confirm True + provider registered      -> ask; approved -> allow,
        declined/raised -> block ("user declined").
      * confirm True + NO provider (unattended) -> mcp_allow_unattended_writes
        True -> allow, else block ("no approver available").
    """
    if not _setting_bool("mcp_confirm_writes", True):
        return None
    provider = get_confirmation_provider()
    if provider is not None:
        try:
            approved = bool(provider(
                server_id, tool_name,
                _confirm_summary(server_id, tool_name, kwargs),
            ))
        except Exception:
            approved = False  # a provider failure is treated as a decline (safe)
        if approved:
            return None
        return f"ERROR: mcp/{server_id}/{tool_name}: user declined"
    # Unattended: no UI/provider present.
    if _setting_bool("mcp_allow_unattended_writes", False):
        return None
    return (
        f"ERROR: mcp/{server_id}/{tool_name}: write tool blocked "
        "(confirmation required, no approver available)"
    )


# Guards the registry's own bookkeeping AND the batched mutation of the agent
# maps. The MCP loop thread writes here (on connect/disconnect); agent threads
# only READ agent.TOOL_MAP / KNOWN_TOOL_NAMES (atomic dict/set ops under the
# GIL), so they never need this lock.
#
# NOTE: this registry state is process-GLOBAL and shared across all MCPManager
# instances; the design assumes a SINGLE manager per process (the app's
# get_manager() singleton). Two managers registering the same server_id would
# stomp each other's entries (last writer wins) — not a concern in the app, but
# noted for tests that spin up multiple managers.
_lock = threading.Lock()

# full_name -> {"server", "tool", "description", "inputSchema"} — the metadata
# the prompt block renders from (TOOL_MAP only stores the callable).
_tool_meta: dict[str, dict] = {}
# server_id -> [full_name, ...] registered for that server (for clean removal).
_by_server: dict[str, list] = {}


def tool_full_name(server_id: str, tool_name: str) -> str:
    """Namespaced TOOL_MAP key for an MCP tool."""
    return f"{_PREFIX}{server_id}{_SEP}{tool_name}"


def _agent_maps():
    """Lazily fetch ``(TOOL_MAP, KNOWN_TOOL_NAMES)`` from ``osia.agent``.

    Returns ``(None, None)`` when the agent module is not importable in the
    current context (e.g. the standalone Phase 1 self-test, where the agent's
    runtime deps are absent) — registration then skips the agent-map mutation
    but still records prompt metadata, so nothing crashes.
    """
    try:
        from osia.agent import TOOL_MAP, KNOWN_TOOL_NAMES
        return TOOL_MAP, KNOWN_TOOL_NAMES
    except Exception:
        return None, None


def _make_wrapper(manager: Any, server_id: str, tool_name: str, is_write: bool = False):
    """Build the SYNC tool callable registered into TOOL_MAP.

    Matches the native ``tool_*`` contract: accepts ``**kwargs`` (the parser
    passes parsed args as kwargs), returns a ``str`` (or ``ERROR: ...``), and
    never raises (``MCPManager.call_tool`` guarantees this from Phase 1).

    ``is_write`` is baked in at registration. When set, the call passes through
    the confirmation gate (``_confirm_gate``) BEFORE ``manager.call_tool``: a
    block returns an ``ERROR: ...`` string (honouring the tool contract) and the
    real call never happens. Read tools skip the gate entirely (one bool check),
    so their hot path is unchanged. The gate reads its settings LIVE, so toggling
    confirmation takes effect without re-registering tools.
    """
    def _mcp_tool(**kwargs) -> str:
        if is_write:
            blocked = _confirm_gate(server_id, tool_name, kwargs)
            if blocked is not None:
                return blocked
        return manager.call_tool(
            server_id, tool_name, kwargs, timeout=WRAPPER_CALL_TIMEOUT
        )

    _mcp_tool.__name__ = "tool_" + tool_full_name(server_id, tool_name)
    _mcp_tool.__qualname__ = _mcp_tool.__name__
    _mcp_tool.__doc__ = f"MCP tool {tool_name!r} on server {server_id!r}."
    return _mcp_tool


def _make_disconnected_stub(server_id: str, tool_name: str):
    """A stub callable left in TOOL_MAP for a tool whose server has gone away.

    WOR-44 Phase 2 review (TS-1): the protected dispatch checks
    ``name in TOOL_MAP`` and only LATER reads ``TOOL_MAP[name]`` (outside a
    try/except). If unregister popped the key in that window, the index would
    raise an uncaught ``KeyError`` and abort the turn. So unregister never
    DELETES a key — it REBINDS it to this stub, which keeps ``TOOL_MAP[name]``
    always resolvable and returns a clean ``ERROR:`` string (honouring the tool
    contract). The name is still removed from KNOWN_TOOL_NAMES + the prompt, so
    the tool is no longer advertised or accepted via the bare-JSON path.
    """
    def _mcp_disconnected(**kwargs) -> str:
        return (
            f"ERROR: mcp/{server_id}/{tool_name}: server disconnected "
            "(tool no longer available)"
        )

    _mcp_disconnected.__name__ = (
        "tool_" + tool_full_name(server_id, tool_name) + "__disconnected"
    )
    _mcp_disconnected.__qualname__ = _mcp_disconnected.__name__
    return _mcp_disconnected


def _unregister_locked(server_id: str, tool_map, known) -> None:
    """Stop advertising a server's tools and neutralise them. Caller holds
    ``_lock``. Removes the names from KNOWN_TOOL_NAMES first (so the bare-JSON
    parser stops accepting them) and from the prompt metadata, then REBINDS the
    TOOL_MAP entries to a disconnected-stub rather than deleting them — see
    ``_make_disconnected_stub`` for why we never pop (TS-1)."""
    names = _by_server.pop(server_id, [])
    if known is not None and names:
        try:
            known.difference_update(names)
        except Exception:
            for f in names:
                known.discard(f)
    for full in names:
        meta = _tool_meta.pop(full, None)
        if tool_map is not None:
            tool_name = (meta or {}).get("tool") or full
            tool_map[full] = _make_disconnected_stub(server_id, tool_name)


def register_server_tools(manager: Any, server_id: str, tools) -> list:
    """Register every discovered tool of a connected server as
    ``mcp__<server_id>__<tool>``. Batched + lock-guarded so a half-registered
    server is never visible. Re-registers cleanly on reconnect (drops the
    server's previous tools first). Returns the list of registered full names
    (empty if the agent maps are unavailable — prompt metadata is still stored).
    """
    # SEC-03 / SEC-01: a server id with a bad charset or the "__" separator
    # would make every mcp__<srv>__<tool> key ambiguous (collision) and could
    # inject prompt structure — refuse the whole server.
    if not _valid_server_id(str(server_id or "")):
        return []

    tool_map, known = _agent_maps()
    new_callables: dict[str, Any] = {}
    new_meta: dict[str, dict] = {}
    for t in tools or []:
        try:
            name = (t.get("name") or "").strip()
        except Exception:
            continue
        # SEC-01 / SEC-02: skip tool names with whitespace/newlines/markdown or
        # absurd length — they would be interpolated raw into the system prompt.
        if not _valid_tool_name(name):
            continue
        full = tool_full_name(server_id, name)
        raw_ann = t.get("annotations")
        annotations = raw_ann if isinstance(raw_ann, dict) else {}
        # Classify ONCE at registration; the result is baked into the wrapper
        # closure (the hot path never re-classifies).
        is_write = classify_tool(name, annotations)
        new_callables[full] = _make_wrapper(manager, server_id, name, is_write)
        new_meta[full] = {
            "server": server_id,
            "tool": name,
            "description": t.get("description") or "",
            "inputSchema": t.get("inputSchema") or {},
            "annotations": annotations,
            "is_write": is_write,
        }

    with _lock:
        # Reconnect hygiene: drop any prior registration for this server.
        _unregister_locked(server_id, tool_map, known)
        # Record prompt metadata (used regardless of agent availability).
        _tool_meta.update(new_meta)
        _by_server[server_id] = list(new_callables.keys())
        # Mutate the agent maps as a BATCH: add to TOOL_MAP first (so dispatch
        # can find every callable), then add the names to KNOWN_TOOL_NAMES (the
        # bare-JSON parser's allowlist) — both are atomic C-level ops under the
        # GIL, so no half-registered server is ever observable.
        if tool_map is not None and known is not None:
            tool_map.update(new_callables)
            try:
                known.update(new_callables.keys())
            except Exception:
                for f in new_callables:
                    known.add(f)
    return list(new_callables.keys())


def unregister_server_tools(server_id: str) -> None:
    """Remove a server's tools from TOOL_MAP / KNOWN_TOOL_NAMES + prompt meta.
    Safe to call for an unknown / never-registered server (no-op)."""
    tool_map, known = _agent_maps()
    with _lock:
        _unregister_locked(server_id, tool_map, known)


def registered_names(server_id: Optional[str] = None) -> list:
    """Snapshot of registered full names (all, or just one server's). For tests
    / introspection."""
    with _lock:
        if server_id is not None:
            return list(_by_server.get(server_id, []))
        return list(_tool_meta.keys())


# ---------------------------------------------------------------------------
# System-prompt block
# ---------------------------------------------------------------------------

def _one_line(text: str, limit: int = 220) -> str:
    """Collapse whitespace and truncate a tool description to one line."""
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _placeholder(prop_schema) -> str:
    """A short illustrative JSON placeholder for one argument, by JSON-Schema
    type."""
    t = None
    if isinstance(prop_schema, dict):
        t = prop_schema.get("type")
        if isinstance(t, list):  # e.g. ["string", "null"]
            t = next((x for x in t if x != "null"), None)
    if t in ("integer", "number"):
        return "<number>"
    if t == "boolean":
        return "<true|false>"
    if t == "array":
        return "[...]"
    if t == "object":
        return "{...}"
    return '"<value>"'


def _usage_from_schema(full: str, schema) -> str:
    """Build a ``Usage:`` JSON example from a tool's inputSchema. Required
    properties are shown inline; optional ones are listed by name after, to keep
    the line concise."""
    props: dict = {}
    required: list = []
    if isinstance(schema, dict):
        p = schema.get("properties")
        if isinstance(p, dict):
            props = p
        r = schema.get("required")
        if isinstance(r, list):
            required = [x for x in r if isinstance(x, str)]
    req = [k for k in required if k in props]
    opt = [k for k in props if k not in req]
    parts = [f'"tool": "{full}"']
    for k in req:
        parts.append(f'"{k}": {_placeholder(props.get(k))}')
    usage = "{" + ", ".join(parts) + "}"
    if opt:
        usage += f"  (optional args: {', '.join(opt)})"
    return usage


def mcp_tool_prompt_block(lang: str = "en") -> str:
    """Render the ``## Connected MCP tools`` system-prompt section from the
    currently-registered MCP tools, or ``""`` when none are connected (so no
    empty section is ever emitted).

    Called fresh by ``get_system_prompt`` each time the system prompt is built.
    On the SERVER path ``run(history=...)`` rebuilds the system message every
    turn, so newly-connected/disconnected MCP tools are reflected without
    restarting the conversation. On the DESKTOP in-process path (``run()`` with
    no history) the system message is refreshed only on ``reset()`` /
    ``set_model()`` / a new conversation, so a mid-conversation MCP change shows
    up on the next such refresh (a known Phase-2 boundary).

    At most ``_MAX_PROMPT_TOOLS`` tools are listed (with a "(+N more)" note);
    the agent maps stay COMPLETE, so every connected tool remains callable even
    when the listing is trimmed (SEC-02).
    """
    with _lock:
        items = sorted(_tool_meta.items())
    if not items:
        return ""
    if lang == "fr":
        lines = [
            "## Outils MCP connectés",
            "Ces outils proviennent de serveurs MCP connectés (Model Context "
            "Protocol). Appelle-les EXACTEMENT comme les outils natifs, avec le "
            "même format JSON {\"tool\": ..., ...}.",
        ]
    else:
        lines = [
            "## Connected MCP tools",
            "These tools come from connected MCP servers (Model Context "
            "Protocol). Call them EXACTLY like native tools, using the same "
            "JSON format {\"tool\": ..., ...}.",
        ]
    shown = items[:_MAX_PROMPT_TOOLS]
    for full, meta in shown:
        desc = _one_line(meta.get("description") or "")
        usage = _usage_from_schema(full, meta.get("inputSchema") or {})
        sep = " " if desc else ""
        lines.append(f"- {full}: {desc}{sep}Usage: {usage}")
    extra = len(items) - len(shown)
    if extra > 0:
        if lang == "fr":
            lines.append(
                f"- (+{extra} autres outils MCP connectés, non listés ici — "
                "appelle-les par leur nom mcp__<serveur>__<outil>.)"
            )
        else:
            lines.append(
                f"- (+{extra} more connected MCP tools not listed here — call "
                "them by their mcp__<server>__<tool> name.)"
            )
    return "\n".join(lines)
