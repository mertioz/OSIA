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
from enum import Enum
from typing import Any, Optional

from osia.permission_outcome import (
    PermissionOutcome,
    PermissionResult,
    normalize_permission_result,
    permission_error_text,
)

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


# ── Write/destructive classification + confirmation gate (WOR-44/WOR-178) ───
# Third-party names and descriptions never lower risk. Only structurally valid,
# non-conflicting MCP annotations may prove read-only semantics; otherwise the
# tool is gated as a write/unknown capability.


def classify_tool(name: str, annotations: Optional[dict] = None) -> bool:
    """Return ``True`` if a tool is WRITE/destructive (gate it), ``False`` if
    READ-only (run it freely).

    Only an exact, structurally valid ``readOnlyHint: true`` with no conflicting
    destructive hint can lower risk. Names and natural-language descriptions
    are never proof of read-only semantics; everything else is write-biased.
    """
    ann = annotations if isinstance(annotations, dict) else {}
    ro = ann.get("readOnlyHint")
    dh = ann.get("destructiveHint")
    hints_are_structural = all(
        key not in ann or isinstance(ann.get(key), bool)
        for key in ("readOnlyHint", "destructiveHint")
    )
    if hints_are_structural and ro is True and dh is not True:
        return False
    if dh is True or ro is False:
        return True
    return True


# Process-wide confirmation provider. A UI (desktop Qt dialog / mobile WS round-
# trip — later sub-steps) registers ONE provider; the gate calls it for write
# tools when confirmation is enabled. Signature:
# ``provider(server_id, tool_name, summary) -> PermissionResult | bool | None``
# Typed results preserve delivery failures.  Legacy True/False remain explicit
# synchronous approve/deny decisions; None means presentation failed.
# A missing provider also means no provider is registered (an unattended
# cron/autonomous run).
_confirmation_provider = None


class ConfirmationDomain(str, Enum):
    """Fixed origin of a sensitive call presented to the shared approver."""

    NATIVE = "action"
    GMAIL = "gmail"
    MCP = "mcp"


def set_confirmation_provider(fn) -> None:
    """Register (or clear, with ``None``) the process-wide confirmation provider.
    Idempotent; last writer wins. Assignment is atomic under the GIL."""
    global _confirmation_provider
    _confirmation_provider = fn


def get_confirmation_provider():
    """Return the registered confirmation provider, or ``None`` if unset."""
    return _confirmation_provider


def _non_security_setting_bool(key: str, default: bool) -> bool:
    """Read a non-authorization boolean setting with a safe fallback."""
    try:
        from osia.config import get_setting
        return bool(get_setting(key, default))
    except Exception:
        return default


def _confirm_summary(server_id: str, tool_name: str, kwargs: dict) -> str:
    """A short, bounded, human-facing description of a pending write call for the
    confirmation UI. It is categorical and never includes argument names or
    values, server identifiers, payloads or provider descriptions."""
    del server_id, tool_name, kwargs
    return "Run one connected write capability"


def confirm_sensitive_action(
    domain: ConfirmationDomain,
    origin_id: str,
    tool_name: str,
    summary: str,
    *,
    call_arguments: Optional[dict] = None,
    policy_tool_name: str = "",
) -> Optional[str]:
    """Obtain one exact approval for a known sensitive-action domain.

    Returns ``None`` to ALLOW the call, or an ``ERROR: ...`` string to BLOCK it.
    Authorization never depends on the legacy MCP compatibility settings.  A
    missing provider or a provider without a live target always fails closed.
    """
    if not isinstance(domain, ConfirmationDomain):
        return "ERROR: confirmation domain unavailable; the action was not executed"
    error_namespace = domain.value
    normalized = dict(call_arguments) if isinstance(call_arguments, dict) else {}
    try:
        from osia.action_policy import (
            AuthorizationSource,
            DecisionType,
            build_permission_request,
            clear_pending_execution_permit,
            evaluate_permission,
            get_default_permission_policy,
            hold_execution_permit,
            permission_error_for_decision,
        )

        clear_pending_execution_permit()
        policy = get_default_permission_policy()
        request = build_permission_request(
            policy_tool_name or tool_name,
            normalized,
            confirmation_domain=domain.value,
            origin_id=origin_id,
            force_sensitive=True,
            policy=policy,
        )
        permission_decision = evaluate_permission(request, policy=policy)
    except Exception:
        return (
            f"ERROR: {error_namespace}/{origin_id}/{tool_name}: "
            "confirmation scope unavailable"
        )

    if permission_decision.decision_type == DecisionType.ALLOW:
        if permission_decision.authorization_source == AuthorizationSource.NONE:
            return (
                f"ERROR: {error_namespace}/{origin_id}/{tool_name}: "
                "confirmation scope unavailable"
            )
        permit = policy.mint_permit(request, permission_decision)
        if permit is None:
            return (
                f"ERROR: {error_namespace}/{origin_id}/{tool_name}: "
                "confirmation scope unavailable"
            )
        hold_execution_permit(permit, request, policy=policy)
        return None
    if permission_decision.decision_type != DecisionType.REQUIRE_CONFIRMATION:
        return permission_error_for_decision(permission_decision)

    provider = get_confirmation_provider()
    if provider is not None:
        try:
            from osia.tool_contracts import CONFIRMATION_LEDGER

            grant = CONFIRMATION_LEDGER.issue(tool_name, normalized)
        except Exception:
            return (
                f"ERROR: {error_namespace}/{origin_id}/{tool_name}: "
                "confirmation scope unavailable"
            )
        try:
            raw_decision = provider(origin_id, tool_name, str(summary or ""))
            decision = normalize_permission_result(
                raw_decision,
                correlation_id=grant.token,
            )
        except Exception:
            decision = PermissionResult(
                PermissionOutcome.INTERNAL_FAILURE,
                grant.token,
            )
        if decision.approved:
            # Turn the UI decision into one exact, opaque, expiring and
            # single-use grant. Raw values never leave the hash boundary.
            if CONFIRMATION_LEDGER.consume(
                grant.token, tool_name, normalized,
            ):
                permit = policy.mint_permit(
                    request, permission_decision, confirmed=True,
                )
                if permit is not None:
                    hold_execution_permit(permit, request, policy=policy)
                    return None
            return (
                f"ERROR: {error_namespace}/{origin_id}/{tool_name}: "
                "confirmation scope unavailable"
            )
        CONFIRMATION_LEDGER.consume(grant.token, tool_name, normalized)
        return permission_error_text(
            error_namespace, origin_id, tool_name, decision,
        )
    return permission_error_text(
        error_namespace,
        origin_id,
        tool_name,
        PermissionResult(PermissionOutcome.PRESENTATION_FAILED),
    )


def confirm_connector_write(
    server_id: str,
    tool_name: str,
    summary: str,
    *,
    error_namespace: str = "connector",
    call_arguments: Optional[dict] = None,
    policy_tool_name: str = "",
) -> Optional[str]:
    """MCP-specific compatibility wrapper around the shared exact gate.

    ``error_namespace`` remains accepted for call compatibility but cannot
    change the immutable MCP confirmation domain.
    """
    del error_namespace
    return confirm_sensitive_action(
        ConfirmationDomain.MCP,
        server_id,
        tool_name,
        summary,
        call_arguments=call_arguments,
        policy_tool_name=policy_tool_name,
    )


def _confirm_gate(server_id: str, tool_name: str, kwargs: dict) -> Optional[str]:
    """Decide whether a WRITE tool may run. Returns ``None`` to ALLOW the call,
    or an ``ERROR: ...`` string to BLOCK it (declined / no approver). Legacy
    confirmation settings cannot weaken this gate. Never raises.
    """
    return confirm_connector_write(
        server_id,
        tool_name,
        _confirm_summary(server_id, tool_name, kwargs),
        error_namespace="mcp",
        call_arguments=kwargs,
        policy_tool_name=tool_full_name(server_id, tool_name),
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


def _claim_current_tool_attempt_dispatch() -> str | None:
    """Claim the exact durable boundary immediately before ``call_tool``.

    Calls without a common ToolAttempt context retain the legacy wrapper path.
    Once an exact attempt exists, any missing/corrupt boundary fails closed.
    """

    try:
        from osia.tool_attempts import (
            TOOL_DISPATCH_BOUNDARY_UNAVAILABLE_RESULT,
            TOOL_DISPATCH_CANCELLED_RESULT,
            ToolAttemptStatus,
            current_tool_attempt_id,
            current_tool_attempt_store,
        )
    except Exception:
        return None
    attempt_id = str(current_tool_attempt_id() or "")
    store = current_tool_attempt_store()
    if not attempt_id and store is None:
        return None
    if not attempt_id or store is None:
        return TOOL_DISPATCH_BOUNDARY_UNAVAILABLE_RESULT
    try:
        record, claimed = store.claim_owner_dispatch(attempt_id)
        if claimed:
            return None
        if ToolAttemptStatus(record.get("status")) is (
            ToolAttemptStatus.CANCELLED_PROVEN
        ):
            return TOOL_DISPATCH_CANCELLED_RESULT
    except Exception:
        pass
    return TOOL_DISPATCH_BOUNDARY_UNAVAILABLE_RESULT


def _make_wrapper(
    manager: Any,
    server_id: str,
    tool_name: str,
    is_write: bool = False,
    input_schema: Optional[dict] = None,
    is_deletion: bool = False,
):
    """Build the SYNC tool callable registered into TOOL_MAP.

    Matches the native ``tool_*`` contract: accepts ``**kwargs`` (the parser
    passes parsed args as kwargs), returns a ``str`` (or ``ERROR: ...``), and
    never raises (``MCPManager.call_tool`` guarantees this from Phase 1).

    ``is_write`` is baked in at registration. When set, the call passes through
    the confirmation gate (``_confirm_gate``) BEFORE ``manager.call_tool``: a
    block returns an ``ERROR: ...`` string (honouring the tool contract) and the
    real call never happens. Autonomous reads traverse the central permission
    facade at this final connector boundary so durable anti-replay state cannot
    be bypassed after a reconnect changes annotations. The gate reads its
    settings LIVE, so toggling confirmation takes effect without re-registering
    tools.

    Arguments are validated exactly as advertised. Invalid envelopes and
    unknown fields are rejected rather than silently coerced (WOR-178).
    """
    def _mcp_tool(**kwargs) -> str:
        try:
            from osia.tool_contracts import validate_arguments

            kwargs = validate_arguments(input_schema or {}, kwargs)
        except Exception:
            return "ERROR: invalid MCP tool arguments; the tool was not executed"
        deletion_dispatch = None
        if not is_write:
            try:
                from osia.action_policy import (
                    autonomous_permission_gate,
                    scheduled_permission_gate,
                )

                blocked = autonomous_permission_gate(
                    tool_full_name(server_id, tool_name),
                    kwargs,
                    connector_boundary=True,
                )
                if blocked is None:
                    blocked = scheduled_permission_gate(
                        tool_full_name(server_id, tool_name),
                        kwargs,
                        connector_boundary=True,
                    )
            except Exception:
                blocked = "ERROR: MCP permission preflight unavailable"
            if blocked is not None:
                return blocked
        if is_write:
            try:
                from osia.action_policy import system_mutation_barrier

                system_block = system_mutation_barrier(tool_name, kwargs)
            except Exception:
                system_block = "ERROR: BLOCK_SYSTEM_DESTRUCTION"
            if system_block is not None:
                return system_block
        if is_deletion:
            try:
                from osia.action_policy import authorize_mcp_deletion_call

                blocked = authorize_mcp_deletion_call(
                    server_id, tool_name, kwargs,
                )
            except Exception:
                blocked = "ERROR: MCP deletion preflight unavailable"
            if blocked is not None:
                return blocked
        elif is_write:
            blocked = _confirm_gate(server_id, tool_name, kwargs)
            if blocked is not None:
                return blocked
        if is_write:
            try:
                from osia.action_policy import consume_pending_execution_permit

                permitted = consume_pending_execution_permit(
                    tool_full_name(server_id, tool_name), kwargs,
                )
            except Exception:
                permitted = False
            if not permitted:
                return (
                    f"ERROR: mcp/{server_id}/{tool_name}: execution permit unavailable"
                )
        dispatch_block = _claim_current_tool_attempt_dispatch()
        if dispatch_block is not None:
            return dispatch_block
        if is_deletion:
            try:
                from osia.action_policy import begin_external_deletion_dispatch

                deletion_dispatch = begin_external_deletion_dispatch(
                    tool_full_name(server_id, tool_name), kwargs,
                )
            except Exception:
                return (
                    f"ERROR: mcp/{server_id}/{tool_name}: "
                    "deletion plan changed or permit unavailable"
                )
        try:
            result = manager.call_tool(
                server_id, tool_name, kwargs, timeout=WRAPPER_CALL_TIMEOUT
            )
        except Exception:
            if deletion_dispatch is None:
                return f"ERROR: mcp/{server_id}/{tool_name}: dispatch failed"
            result = None
        if deletion_dispatch is not None:
            try:
                from osia.action_policy import external_deletion_result

                return external_deletion_result(deletion_dispatch, result)
            except Exception:
                return "ERROR: OUTCOME_UNKNOWN"
        return result

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
    try:
        from osia.tool_contracts import unregister_dynamic_tools

        unregister_dynamic_tools(names)
    except Exception:
        pass
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
    duplicate_names: set[str] = set()
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
        if full in duplicate_names:
            continue
        if full in new_callables:
            duplicate_names.add(full)
            new_callables.pop(full, None)
            new_meta.pop(full, None)
            continue
        raw_ann = t.get("annotations")
        annotations = raw_ann if isinstance(raw_ann, dict) else {}
        # Classify ONCE at registration; the result is baked into the wrapper
        # closure (the hot path never re-classifies).
        is_write = classify_tool(name, annotations)
        # MCP's destructive hint marks a potentially irreversible write; it
        # does not mean the operation literally deletes an object. Route only
        # deletion-named tools through the reinforced deletion preflight. Other
        # destructive writes still remain write-biased and reach the canonical
        # confirmation gate below.
        is_deletion = bool(
            re.search(r"(?:^|_)(?:delete|remove|erase|destroy)(?:_|$)", name, re.IGNORECASE)
        )
        if is_deletion:
            is_write = True
        new_callables[full] = _make_wrapper(
            manager,
            server_id,
            name,
            is_write,
            t.get("inputSchema") or {},
            is_deletion,
        )
        new_meta[full] = {
            "server": server_id,
            "tool": name,
            "description": t.get("description") or "",
            "inputSchema": t.get("inputSchema") or {},
            "annotations": annotations,
            "is_write": is_write,
            "is_deletion": is_deletion,
        }

    with _lock:
        # Reconnect hygiene: drop any prior registration for this server.
        _unregister_locked(server_id, tool_map, known)
        # Register connected MCP contracts only after the previous generation
        # is removed. Invalid third-party schemas fail closed and are neither
        # advertised nor executable.
        valid_callables: dict[str, Any] = {}
        valid_meta: dict[str, dict] = {}
        for full, dispatcher in new_callables.items():
            meta = new_meta[full]
            annotations = meta.get("annotations") or {}
            structural_semantics = bool(
                isinstance(annotations, dict)
                and all(
                    key not in annotations
                    or isinstance(annotations.get(key), bool)
                    for key in ("readOnlyHint", "destructiveHint")
                )
                and (
                    "readOnlyHint" in annotations
                    or annotations.get("destructiveHint") is True
                )
            )
            try:
                from osia.tool_contracts import register_dynamic_tool

                registered_spec = register_dynamic_tool(
                    name=full,
                    dispatcher=dispatcher,
                    capability=meta.get("description") or "Connected MCP capability",
                    input_schema=meta.get("inputSchema") or {},
                    connector_id=server_id,
                    read_only=not bool(meta.get("is_write")),
                    semantics_known=structural_semantics,
                )
            except Exception:
                continue
            valid_callables[full] = dispatcher
            authoritative_meta = dict(meta)
            authoritative_meta["inputSchema"] = registered_spec.input_schema
            valid_meta[full] = authoritative_meta
        new_callables = valid_callables
        new_meta = valid_meta
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


def mcp_tool_prompt_block(
    lang: str = "en", *, server_id: str | None = None,
) -> str:
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

    ``server_id`` narrows only the rendered per-turn inventory. It never
    mutates registration, connection state, permissions, or dispatch maps.
    """
    with _lock:
        items = sorted(
            (full, meta)
            for full, meta in _tool_meta.items()
            if server_id is None or str(meta.get("server") or "") == server_id
        )
    if not items:
        return ""
    del lang
    lines = [
        "## Connected MCP tools (available now)",
        "Call these with the same textual <tool_call> JSON protocol and top-level "
        "arguments as OSIA tools. Their connector permissions and anti-replay rules apply.",
    ]
    shown = items[:_MAX_PROMPT_TOOLS]
    for full, meta in shown:
        desc = _one_line(meta.get("description") or "")
        usage = _usage_from_schema(full, meta.get("inputSchema") or {})
        sep = " " if desc else ""
        lines.append(f"- {full}: {desc}{sep}Usage: {usage}")
    extra = len(items) - len(shown)
    if extra > 0:
        lines.append(
            f"- (+{extra} more connected MCP tools not listed here; call them "
            "by their mcp__<server>__<tool> name.)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legacy connector-summary compatibility
# ---------------------------------------------------------------------------


def mcp_connectors_prompt_block(lang: str = "en") -> str:
    """Return no duplicate connector catalog; connected tool signatures are canonical."""
    del lang
    return ""
