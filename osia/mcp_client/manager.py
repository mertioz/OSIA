#!/usr/bin/env python3
"""OSIA MCP client core — WOR-44 Phase 1.

Standalone, self-contained MCP CLIENT. Lets OSIA connect to MCP servers,
discover their tools, and call a tool — returning the result as a plain string
or an ``ERROR: ...`` string, mirroring the native-tool contract in
``osia.agent`` (a tool result is treated as a failure when its first non-blank
token begins with ``ERROR``/``ERREUR``; see ``osia.agent._is_tool_error``).

NO agent or server wiring lives here. This module imports neither
``osia.agent`` nor ``osia.server`` and owns its OWN asyncio event loop in a
daemon thread, so it works identically inside the desktop GUI process and the
mobile server process.

The async-loop-thread ↔ sync-call bridge
----------------------------------------
* One dedicated ``asyncio`` event loop runs forever in a daemon thread,
  started lazily on first use (``_ensure_loop``).
* Each connected server is served by ONE long-lived coroutine (``_serve``)
  that opens the transport + ``ClientSession`` inside ``async with`` blocks and
  keeps them alive until disconnect. Entering AND exiting those async context
  managers happens on a SINGLE task — this is required for anyio cancel-scope /
  task-group safety. Per-call requests (``session.call_tool``) are dispatched
  from other tasks via the stored session, which is safe because the MCP SDK's
  session multiplexes requests over anyio memory streams.
* ``call_tool`` is SYNCHRONOUS: it hands the coroutine to the loop with
  ``asyncio.run_coroutine_threadsafe`` and blocks on the returned
  ``concurrent.futures.Future`` with a bounded timeout. EVERY failure path
  (not connected / timeout / transport error / ``isError``) returns an
  ``ERROR: ...`` string — it NEVER raises. This is the exact function Phase 2
  will wrap into a native-style ``TOOL_MAP`` callable, so it already honours the
  string / ``ERROR:`` contract.

The manager supports the ``stdio`` transport (local subprocess servers such as
the reference ``@modelcontextprotocol/server-filesystem``) and, since WOR-44
Phase 5, the ``streamable_http`` transport (remote HTTP servers addressed by a
URL with optional auth headers, e.g. Notion). ``sse`` is recognised but reports
a clear ``failed`` status until a later phase wires it — an unknown /
unsupported transport never crashes the manager.

Thread-safety
-------------
A single re-entrant lock (``self._lock``) guards the servers map and each
server's mutable status/tools/session fields. A separate lock
(``self._lifecycle_lock``) guards loop/thread start-up and shutdown. One bad or
offline server isolates to a ``failed`` status + ``ERROR:`` call results — it
never crashes the manager or the shared loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Bounded wall-clock budgets (seconds) ────────────────────────────────────
# Generous connect budget: a first-run ``npx``/``uvx`` stdio server may fetch
# its package from the network before it can speak the protocol.
DEFAULT_CONNECT_TIMEOUT = 45.0
DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_DISCONNECT_TIMEOUT = 10.0
_LOOP_START_TIMEOUT = 5.0
# WOR-44 Phase 5 (5-oauth-a): an interactive OAuth connect blocks on the user
# completing a browser login. connect() bounds the WHOLE handshake with one
# budget, but session.initialize() runs the SDK flow serially: PRM+ASM discovery
# and RFC 7591 DCR (each an httpx leg at the ~30s streamablehttp default) run
# BEFORE the loopback callback wait (osia.mcp.oauth.OAUTH_CALLBACK_TIMEOUT=300s),
# and the token-exchange POST (~30s) runs AFTER it. So this ceiling must exceed
# 300s + those surrounding network legs (~2×30 discovery + 30 DCR + 30 token)
# or a login finishing near the 300s mark could be cancelled mid-exchange and a
# SUCCESSFUL auth wrongly marked FAILED — hence 420s, not just 300+slack.
# connect() applies it to the ready-future timeout for oauth servers only; the
# wait is on an asyncio Event off the loop, so it never blocks the loop.
OAUTH_CONNECT_TIMEOUT = 420.0

# ── Per-server connection status values ─────────────────────────────────────
STATUS_DISCONNECTED = "disconnected"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_FAILED = "failed"

# Transports recognised by the config schema vs. those actually wired. Phase 1
# wired stdio; WOR-44 Phase 5 wired streamable_http (remote HTTP). A config
# naming a known-but-unsupported transport (e.g. sse) fails cleanly.
SUPPORTED_TRANSPORTS = ("stdio", "streamable_http")
KNOWN_TRANSPORTS = ("stdio", "streamable_http", "sse")

# Non-error sentinel for a successful-but-empty tool result. Matches the native
# convention (``osia.agent._is_tool_error`` does NOT treat ``(no output)`` as an
# error) so Phase 2 won't misread an empty success as a failure.
_EMPTY_OK = "(no output)"


# ---------------------------------------------------------------------------
# CallToolResult → string / ERROR conversion
# ---------------------------------------------------------------------------

def _calltoolresult_to_text(res: Any, server_id: str = "", tool_name: str = "") -> str:
    """Convert an MCP ``CallToolResult`` into a single string.

    * Text content blocks are joined with newlines.
    * Image / audio / embedded-resource blocks become a short textual
      placeholder (Phase 1 is text-focused).
    * ``res.isError`` → an ``ERROR: mcp/<server>/<tool>: ...`` string.
    * A successful-but-empty result → ``(no output)`` (a non-error sentinel).

    Defensive by design (duck-typed ``getattr``) so it tolerates minor MCP SDK
    schema differences across versions; ``call_tool`` still wraps the call in
    try/except as a final backstop, so a truly pathological shape degrades to an
    ``ERROR:`` string rather than escaping.
    """
    prefix = f"ERROR: mcp/{server_id}/{tool_name}:"
    parts: list[str] = []
    content = getattr(res, "content", None) or []
    # Tolerate a malformed/unexpected SDK shape where ``content`` is a single
    # non-sequence object instead of a list (iterating it would raise).
    if not isinstance(content, (list, tuple)):
        content = [content]
    for block in content:
        btype = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        if btype == "image":
            mime = getattr(block, "mimeType", None) or "image"
            data = getattr(block, "data", "") or ""
            parts.append(f"[image content: {mime}, {len(data)} base64 chars]")
            continue
        if btype == "audio":
            mime = getattr(block, "mimeType", None) or "audio"
            data = getattr(block, "data", "") or ""
            parts.append(f"[audio content: {mime}, {len(data)} base64 chars]")
            continue
        resource = getattr(block, "resource", None)
        if resource is not None:
            rtext = getattr(resource, "text", None)
            if rtext is not None:
                parts.append(str(rtext))
            else:
                uri = getattr(resource, "uri", None) or "resource"
                blob = getattr(resource, "blob", "") or ""
                parts.append(f"[embedded resource: {uri}, {len(blob)} base64 chars]")
            continue
        parts.append(f"[unsupported content block: {btype}]")

    text_out = "\n".join(p for p in parts if p != "")

    # Structured-content fallback when there is no textual content at all.
    if not text_out:
        structured = getattr(res, "structuredContent", None)
        if structured:
            try:
                text_out = json.dumps(structured, ensure_ascii=False)
            except Exception:
                text_out = str(structured)

    if bool(getattr(res, "isError", False)):
        return f"{prefix} {text_out or 'tool reported an error'}"
    if not text_out:
        return _EMPTY_OK
    return text_out


def _sanitize_error(text: str, cfg: Optional[dict]) -> str:
    """Redact any secret VALUES the caller injected into a server's config from
    an error/diagnostic string before it is stored or surfaced.

    The manager is secret-free by design, but the caller may inject a token into
    a stdio server's ``env`` (or an HTTP auth header) at connect time. A
    subprocess-spawn / SDK exception is unlikely to embed those values, but
    ``status()`` / ``error()`` / ``servers()`` surface the stored error text, so
    we mask every config ``env`` (and any ``headers``/``token``) value as a
    belt-and-suspenders guarantee that no secret leaks via diagnostics.
    """
    if not text or not cfg:
        return text
    try:
        values: list[str] = []
        env = cfg.get("env")
        if isinstance(env, dict):
            values += [str(v) for v in env.values() if v]
        headers = cfg.get("headers")
        if isinstance(headers, dict):
            values += [str(v) for v in headers.values() if v]
        tok = cfg.get("token")
        if tok:
            values.append(str(tok))
        for v in values:
            if v and v in text:
                text = text.replace(v, "***")
    except Exception:
        pass
    return text


# ``BaseExceptionGroup`` is a builtin on Python 3.11+ (PEP 654). Resolve it once
# so the unwrap below degrades gracefully (to "no groups") on older runtimes.
try:  # pragma: no cover - trivial version probe
    _EXC_GROUP = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover - Python < 3.11
    _EXC_GROUP = None


def _leaf_exception(exc: BaseException) -> "tuple[BaseException, int]":
    """Unwrap a PEP-654 ExceptionGroup down to its leaf causes and return
    ``(first_leaf, leaf_count)``.

    anyio wraps a transport/handshake failure (httpx.ConnectError, TLS, etc.)
    inside a ``BaseExceptionGroup`` ("unhandled errors in a TaskGroup"), whose
    own ``str()`` hides the real cause. This recurses through nested groups and
    returns the FIRST leaf plus the total leaf count. A plain exception (or any
    runtime without ExceptionGroups) returns ``(exc, 1)`` unchanged.
    """
    if _EXC_GROUP is None or not isinstance(exc, _EXC_GROUP):
        return exc, 1
    leaves: list = []

    def _walk(e: BaseException) -> None:
        if _EXC_GROUP is not None and isinstance(e, _EXC_GROUP):
            for sub in e.exceptions:
                _walk(sub)
        else:
            leaves.append(e)

    _walk(exc)
    if not leaves:
        return exc, 1
    return leaves[0], len(leaves)


def _all_leaves_cancelled(exc: BaseException) -> bool:
    """True iff ``exc`` is a ``CancelledError``, or a PEP-654 ExceptionGroup whose
    EVERY leaf is a ``CancelledError``.

    WOR-97: when ``cancel_pending`` / ``disconnect`` / ``shutdown`` cancel a
    ``_serve`` task that is parked DEEP inside the SDK's anyio TaskGroup (e.g. the
    OAuth ``callback_handler`` wait), the injected ``CancelledError`` propagates
    OUT wrapped in a ``BaseExceptionGroup`` ("unhandled errors in a TaskGroup").
    A bare ``isinstance(e, CancelledError)`` check would then misread a PURE
    cancel as a connect FAILURE and record STATUS_FAILED. Classifying an
    all-cancelled group as a cancel keeps a cancelled connector at DISCONNECTED
    regardless of how anyio wrapped it. A group that ALSO carries a real error
    (httpx ``ConnectError``, TLS, a ``TimeoutError`` from the 300s callback
    backstop, …) is NOT a pure cancel — it has a non-cancel leaf — so it still
    classifies as a failure (FAILED), exactly as before."""
    if isinstance(exc, asyncio.CancelledError):
        return True
    if _EXC_GROUP is None or not isinstance(exc, _EXC_GROUP):
        return False
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return False
    return all(_all_leaves_cancelled(s) for s in subs)


def _print_connect_failure(
    server_id: str, leaf: BaseException, count: int, cfg: Optional[dict]
) -> None:
    """Print the LEAF cause of a failed connect to stderr, grep-prefixed with the
    connector id. The traceback text is run through ``_sanitize_error`` first, so
    any secret value the caller injected into the config (stdio ``env`` / HTTP
    ``headers`` / ``token``) is redacted exactly as it is in ``conn.error`` —
    diagnostics never leak a token. Best-effort: any formatting/I-O error is
    swallowed so this can never disturb the failure-handling path.
    """
    try:
        suffix = f" (1 of {count} leaf errors)" if count > 1 else ""
        tb = "".join(
            traceback.format_exception(type(leaf), leaf, leaf.__traceback__)
        )
        sys.stderr.write(
            f"[MCP {server_id}] connect failed{suffix}:\n"
            + _sanitize_error(tb, cfg)
        )
        sys.stderr.flush()
    except Exception:
        pass


def _tool_annotations(t: Any) -> dict:
    """Extract an MCP Tool's behavioural hints (``readOnlyHint`` /
    ``destructiveHint`` / ``idempotentHint`` / ``openWorldHint``) into a PLAIN
    dict, or ``{}`` when absent.

    These optional, server-supplied hints (MCP ``ToolAnnotations``) let a caller
    classify a tool as read-only vs write/destructive (WOR-44 Phase 5 confirm
    gate). We flatten them to a plain dict at the SDK boundary so downstream
    consumers (``osia.mcp.registry``) never touch SDK types. Duck-typed and
    defensive: tolerates a missing field, a ``None`` annotations object, and the
    (older-SDK) case where ``annotations`` is already a dict. Only non-``None``
    hints are copied, so an unset hint is simply absent.
    """
    ann = getattr(t, "annotations", None)
    if ann is None:
        return {}
    out: dict = {}
    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        try:
            val = ann.get(key) if isinstance(ann, dict) else getattr(ann, key, None)
        except Exception:
            val = None
        if val is not None:
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# Credential validation — connect-time auth probe + demote-on-auth-error
# ---------------------------------------------------------------------------
# A token/Bearer connector's handshake (``initialize`` + ``list_tools``) is LOCAL
# protocol negotiation — for stdio it never leaves the subprocess, and many
# remote servers serve ``tools/list`` unauthenticated — so it does NOT exercise
# the credential. A garbage/expired token would therefore show CONNECTED and only
# fail on the first real tool call. These helpers let ``connect`` run a strictly
# read-only probe that DOES exercise the credential, and let ``call_tool`` demote
# a connector the moment a real call proves the token invalid.

# Curated read-only probes per catalog connector id (lower-cased): an ORDERED
# list of (tool_name, args) candidates, tried first, the first whose tool the
# connected server actually exposes AND whose required args are all supplied. A
# curated entry MAY carry args because some servers expose no no-arg identity
# tool: the archived npm ``@modelcontextprotocol/server-github`` has no ``get_me``
# and every read tool needs a required arg, so without an args-bearing probe a
# garbage PAT would still reach CONNECTED there. Every curated tool MUST be
# strictly read-only (search/identity), and the args MUST be innocuous.
_AUTH_PROBES = {
    "github": [
        ("get_me", {}),                                   # newer Go / hosted server
        ("search_repositories", {"query": "octocat"}),    # npm reference server
        ("search_users", {"q": "octocat"}),               # npm reference fallback
    ],
}

# Generic identity/"whoami" read tools to try when no per-server probe matches.
# Each is a credential-exercising read with (typically) no required arguments.
_IDENTITY_PROBE_CANDIDATES = (
    "get_me", "whoami", "get_current_user", "get_authenticated_user",
    "get_viewer", "get_user_info", "current_user", "me",
)

# Bounded probe budget, comfortably under the 45s connect budget: a slow probe
# must NEVER fail an otherwise-good connect — on timeout we fall through to
# CONNECTED and let the demote-on-auth-error fallback catch a bad token on use.
PROBE_CALL_TIMEOUT = 15.0

# Clear authentication/authorization failure phrases. DELIBERATELY conservative:
# only unambiguous auth verdicts, so a 404 / bad-arg / generic tool error never
# trips it. Rate-limiting frequently rides a 403 but is NOT an auth failure (the
# credential is valid, just throttled) and is excluded below. The 401/403 status
# codes are matched separately with word boundaries (``_AUTH_CODE_RE``) so a digit
# embedded in unrelated data can't false-positive.
_AUTH_FAIL_KEYWORDS = (
    "unauthorized", "forbidden", "bad credentials", "invalid credentials",
    "invalid token", "invalid access token", "invalid authentication",
    "authentication failed", "authentication required", "requires authentication",
    "not authenticated", "permission denied", "access denied",
    "invalid api key", "invalid_grant", "invalid_token", "token expired",
    "expired token",
)
_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "ratelimit", "too many requests")
_AUTH_CODE_RE = re.compile(r"\b40[13]\b")


def _looks_like_error(text: str) -> bool:
    """True iff a tool-result string carries the native ``ERROR``/``ERREUR``
    failure marker (same convention as ``osia.agent._is_tool_error``), checked
    locally so this module stays standalone (no agent import)."""
    s = (text or "").lstrip()
    up = s[:6].upper()
    return up.startswith("ERROR") or up.startswith("ERREUR")


def _auth_failure_signal(text: str) -> bool:
    """Conservatively decide whether an error string means the CREDENTIAL was
    rejected (401/403/unauthorized/bad-credentials/…). Returns False for
    rate-limiting (a throttled but valid credential) and for any non-auth error,
    so a healthy connector is never demoted by a 404 / bad-arg / transient fault."""
    if not text:
        return False
    low = text.lower()
    if any(h in low for h in _RATE_LIMIT_HINTS):
        return False  # throttled, not an auth failure — never demote
    if _AUTH_CODE_RE.search(low):
        return True
    return any(kw in low for kw in _AUTH_FAIL_KEYWORDS)


def _strip_mcp_prefix(text: str, server_id: str, tool_name: str) -> str:
    """Drop the leading ``ERROR: mcp/<server>/<tool>:`` namespace wrapper that
    ``_calltoolresult_to_text`` / ``call_tool`` prepend, so an auth-signal scan
    sees only the tool's actual error TEXT — never the user-chosen server/tool id
    (which could itself contain '401'/'403'/an auth word and false-demote)."""
    prefix = f"ERROR: mcp/{server_id}/{tool_name}:"
    s = text or ""
    return s[len(prefix):] if s.startswith(prefix) else s


def _probe_no_required_args(tool: dict) -> bool:
    """True iff a tool's inputSchema declares NO required arguments, so the probe
    can safely call it with ``{}``."""
    return _probe_args_satisfy_required(tool, {})


def _probe_args_satisfy_required(tool: dict, args: dict) -> bool:
    """True iff every REQUIRED arg the tool declares is present in ``args`` — so a
    curated probe call exercises the CREDENTIAL rather than failing on a missing
    required-arg error (which would look like a non-auth error and let a bad token
    pass). With ``args={}`` this is exactly the no-required-args check."""
    schema = tool.get("inputSchema") if isinstance(tool, dict) else None
    if not isinstance(schema, dict):
        return True
    req = schema.get("required")
    if not isinstance(req, list):
        return True
    args = args or {}
    return all((not isinstance(x, str)) or (not x) or (x in args) for x in req)


def _resolve_probe(server_id: str, tools: list):
    """Pick a strictly read-only probe for a token-auth connector as a
    ``(tool_name, args)`` pair, or ``("", {})`` when none is safe. Priority:
    (1) a curated per-server probe whose tool the server exposes AND whose required
    args are all supplied; (2) a known identity/whoami read with no required args;
    (3) any server-declared ``readOnlyHint`` tool with no required args (called
    with ``{}``)."""
    by_name: dict = {}
    for t in tools or []:
        try:
            n = (t.get("name") or "").strip()
        except Exception:
            continue
        if n and n not in by_name:
            by_name[n] = t

    for name, args in _AUTH_PROBES.get(str(server_id or "").strip().lower(), []):
        t = by_name.get(name)
        if t is not None and _probe_args_satisfy_required(t, args):
            return name, dict(args)
    for cand in _IDENTITY_PROBE_CANDIDATES:
        t = by_name.get(cand)
        if t is not None and _probe_no_required_args(t):
            return cand, {}
    for name, t in by_name.items():
        ann = t.get("annotations") if isinstance(t, dict) else None
        if (
            isinstance(ann, dict)
            and ann.get("readOnlyHint") is True
            and _probe_no_required_args(t)
        ):
            return name, {}
    return "", {}


# ---------------------------------------------------------------------------
# Optional process-wide status-change listener (demote-on-auth-error broadcast)
# ---------------------------------------------------------------------------
# The demote path flips a connector to FAILED from a TOOL-CALL thread — outside
# any ``connect``/``disconnect`` the host (mobile server) drives — so the host
# cannot otherwise learn it changed. A host MAY register ONE listener
# ``fn(server_id, status)`` to fan the new status out to its clients. The desktop
# GUI does not need it (it polls ``servers()``); ``None`` = no listener. The
# manager stays standalone: it only ever CALLS an opaque callback, never imports a
# host.
_status_listener = None
_status_listener_lock = threading.Lock()


def set_status_listener(fn) -> None:
    """Register (or clear, with ``None``) the process-wide MCP status-change
    listener. Idempotent; last writer wins. The callback ``fn(server_id, status)``
    must not raise (it is wrapped) and must not block (hop onto your own loop)."""
    global _status_listener
    with _status_listener_lock:
        _status_listener = fn


def _emit_status_change(server_id: str, status: str) -> None:
    """Best-effort notify the registered status listener; never raises."""
    with _status_listener_lock:
        fn = _status_listener
    if fn is None:
        return
    try:
        fn(server_id, status)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-server connection record
# ---------------------------------------------------------------------------

@dataclass
class _ServerConn:
    """Mutable per-server state. All fields are guarded by ``MCPManager._lock``
    except where a method documents otherwise."""

    server_id: str
    cfg: dict
    status: str = STATUS_DISCONNECTED
    error: str = ""
    tools: list = field(default_factory=list)  # [{"name","description","inputSchema"}]

    # Loop-side / cross-thread coordination objects.
    session: Any = None
    stop_event: Optional[asyncio.Event] = None          # set (on the loop) to end _serve
    serve_future: Any = None                            # concurrent.futures.Future of _serve
    done_event: threading.Event = field(default_factory=threading.Event)  # _serve finished


# ---------------------------------------------------------------------------
# MCPManager
# ---------------------------------------------------------------------------

class MCPManager:
    """Connect to MCP servers and call their tools from synchronous code.

    Typical use::

        mgr = MCPManager()
        status = mgr.connect({"id": "fs", "transport": "stdio",
                              "command": "npx",
                              "args": ["-y", "@modelcontextprotocol/server-filesystem", "<allowed-dir>"]})
        if status == STATUS_CONNECTED:
            tools = mgr.list_tools("fs")
            out = mgr.call_tool("fs", "read_text_file", {"path": "<allowed-dir>/x.txt"})
        mgr.shutdown()
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.Lock()   # guards loop/thread lifecycle
        self._lock = threading.RLock()             # guards _servers + conn fields
        self._servers: dict[str, _ServerConn] = {}
        self._shutting_down = False

    # ── Event-loop lifecycle ────────────────────────────────────────────────

    def _ensure_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Start (lazily) the dedicated event loop in a daemon thread and return
        it. Idempotent: a second call while the loop is alive is a no-op.

        Returns ``None`` while a ``shutdown`` is in progress so callers refuse to
        start new work and resurrect a loop that is being torn down. Once a
        shutdown has fully completed (``_shutting_down`` reset to False) a fresh
        call starts a new loop, so the manager / singleton is reusable."""
        with self._lifecycle_lock:
            if self._shutting_down:
                return None
            if (
                self._loop is not None
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return self._loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    # Drain/cancel anything still pending so the subprocess
                    # transports (stdio) are torn down before the loop closes.
                    try:
                        self._cancel_all_tasks(loop)
                    except Exception:
                        pass
                    loop.close()

            thread = threading.Thread(
                target=_run, name="osia-mcp-loop", daemon=True
            )
            thread.start()
            ready.wait(timeout=_LOOP_START_TIMEOUT)
            self._loop = loop
            self._thread = thread
            return loop

    @staticmethod
    def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
        """Cancel and await every task still pending on ``loop`` (called from the
        loop thread's ``finally``, after ``run_forever`` has returned)."""
        try:
            pending = [t for t in asyncio.all_tasks(loop=loop) if not t.done()]
        except RuntimeError:
            pending = []
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get(self, server_id: str) -> Optional[_ServerConn]:
        with self._lock:
            return self._servers.get(server_id)

    def _set_status(self, server_id: str, status: str, error: str = "") -> None:
        with self._lock:
            conn = self._servers.get(server_id)
            if conn is not None:
                conn.status = status
                conn.error = error

    def _fail_unserved(self, server_id: str, error: str) -> None:
        """Mark a server FAILED *before* any ``_serve`` task was scheduled, and
        set its ``done_event`` — there is no coroutine that will ever set it, so
        ``disconnect``/``shutdown`` must not block waiting on it."""
        with self._lock:
            conn = self._servers.get(server_id)
            if conn is not None:
                conn.status = STATUS_FAILED
                conn.error = error
                conn.done_event.set()

    @staticmethod
    def _safe_set_future(fut: "concurrent.futures.Future", value: Any) -> None:
        try:
            if not fut.done():
                fut.set_result(value)
        except Exception:
            pass

    def _signal_stop(self, server_id: str) -> None:
        """Ask a server's ``_serve`` coroutine to wind down gracefully by setting
        its stop event ON the loop thread (asyncio.Event is not thread-safe)."""
        conn = self._get(server_id)
        loop = self._loop
        if conn is None or loop is None:
            return
        ev = conn.stop_event
        if ev is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            pass

    # ── Agent-tool bridge (WOR-44 Phase 2) ────────────────────────────────────
    # Soft, lazy coupling to the agent. The actual mutation of TOOL_MAP /
    # KNOWN_TOOL_NAMES and the lazy ``osia.agent`` import live in
    # ``osia.mcp.registry`` — imported here lazily and wrapped in try/except so
    # this manager stays standalone-importable (no agent, no circular import):
    # if the agent is absent, registration is simply a no-op.

    def _register_tools(self, server_id: str, tools: list) -> None:
        try:
            from osia.mcp_client import registry
            registry.register_server_tools(self, server_id, tools)
        except Exception:
            pass

    def _unregister_tools(self, server_id: str) -> None:
        try:
            from osia.mcp_client import registry
            registry.unregister_server_tools(server_id)
        except Exception:
            pass

    # ── Credential validation (auth probe + demote-on-auth-error) ─────────────

    async def _auth_probe(
        self, session: Any, server_id: str, tools: list, auth_type: str
    ) -> str:
        """Exercise a token-auth connector's CREDENTIAL after a successful
        handshake, so a garbage/expired token is rejected AT CONNECT instead of
        deferred to the first real tool call. Runs ON the MCP loop using the
        just-opened ``session``. Returns ``""`` to ALLOW the connect (probe
        passed, none is known, or it merely timed out / errored non-auth) or a
        short reason to FAIL it. Strictly read-only and bounded by
        ``PROBE_CALL_TIMEOUT`` — a slow network never fails an otherwise-good
        connect (the demote-on-auth-error fallback still guards first use)."""
        if auth_type != "token":
            return ""
        probe, probe_args = _resolve_probe(server_id, tools)
        if not probe:
            return ""
        try:
            res = await asyncio.wait_for(
                session.call_tool(probe, probe_args), timeout=PROBE_CALL_TIMEOUT
            )
            text = _calltoolresult_to_text(res, server_id, probe)
        except asyncio.TimeoutError:
            return ""  # network slowness — don't hard-fail; demote-on-call covers it
        except asyncio.CancelledError:
            raise  # disconnect/shutdown teardown — propagate to the serve handler
        except Exception as e:  # noqa: BLE001 - a transport error here is data
            text = f"ERROR: {type(e).__name__}: {e}"
        # Scan ONLY the tool's error TEXT, not the ``ERROR: mcp/<server>/<tool>:``
        # namespace wrapper — the user-chosen server/tool id could itself contain
        # '401'/'403'/an auth word and cause a false "token rejected".
        if _looks_like_error(text) and _auth_failure_signal(
            _strip_mcp_prefix(text, server_id, probe)
        ):
            return "token rejected"
        return ""

    def _demote_auth_failed(self, server_id: str) -> None:
        """The moment a real tool call proves a connector's credential invalid
        (401/403/auth), flip it from the optimistic CONNECTED to FAILED, drop its
        tools, wind the now-dead session down, and notify the status listener so
        the PC + mobile pills self-correct. Only demotes a currently-CONNECTED
        server; idempotent; never raises."""
        with self._lock:
            conn = self._servers.get(server_id)
            if conn is None or conn.status != STATUS_CONNECTED:
                return
            conn.status = STATUS_FAILED
            conn.error = "token rejected (auth failed)"
        # Outside the lock: unadvertise the tools, ask the serve coroutine to wind
        # the session/subprocess down gracefully, and emit the new status.
        self._unregister_tools(server_id)
        self._signal_stop(server_id)
        _emit_status_change(server_id, STATUS_FAILED)

    # ── The long-lived per-server coroutine ──────────────────────────────────

    async def _serve(
        self, server_id: str, ready: "concurrent.futures.Future"
    ) -> None:
        """Open the transport + session, discover tools, then hold the session
        open until disconnect/shutdown. Runs entirely on the MCP loop. Resolves
        ``ready`` with the final status string so ``connect`` can return."""
        conn = self._get(server_id)
        if conn is None:
            self._safe_set_future(ready, STATUS_FAILED)
            return

        stop_event = asyncio.Event()
        with self._lock:
            conn.stop_event = stop_event

        try:
            # WOR-44 Phase 5: pick the transport. ``streamable_http`` opens a
            # remote HTTP session (URL + optional in-memory auth headers);
            # everything else takes the original Phase-1 ``stdio`` path (local
            # subprocess), byte-for-byte unchanged in the ``else`` branch.
            # Everything downstream of the ``read``/``write`` streams — the
            # ClientSession handshake, tool discovery, registration into the
            # agent maps, and the hold-open until disconnect — is transport-
            # agnostic and DUPLICATED verbatim in each branch (no shared-block
            # refactor) so the stdio bytes never move.
            transport = str(conn.cfg.get("transport") or "stdio").strip().lower()
            if transport == "streamable_http":
                # ``streamablehttp_client`` yields a 3-TUPLE (read, write,
                # get_session_id) — note the extra element vs stdio's 2-tuple.
                # ``headers`` carries the in-memory Authorization header the
                # caller injected for the handshake (None when absent); it is
                # never persisted nor echoed in a frame.
                from mcp import ClientSession
                from mcp.client.streamable_http import streamablehttp_client

                cfg = conn.cfg
                url = str(cfg.get("url") or "")
                auth_type = str(cfg.get("auth_type") or "").strip().lower()

                if auth_type == "oauth" and str(
                    cfg.get("oauth_callback_mode") or ""
                ).strip().lower() == "tunnel":
                    # WOR-96 PART 1: OAuth 2.1 via the PUBLIC HTTPS tunnel callback
                    # (osia.space → THIS server process's GET /oauth/callback).
                    # Identical to the loopback branch below EXCEPT the callback
                    # transport is the cross-loop TunnelOAuthCallback (no socket —
                    # the redirect lands on the server's public route and is
                    # bridged here via a concurrent.futures.Future) and the provider
                    # registers/uses the TUNNEL redirect URI at index 0. Runs ONLY
                    # when the server's trigger route sets cfg
                    # oauth_callback_mode="tunnel"; every other caller (desktop GUI,
                    # boot sweep, mobile mcp_connect, /internal/mcp_sync) omits the
                    # flag and takes the loopback elif below, byte-for-byte
                    # unchanged. The PKCE verifier/state and the token exchange stay
                    # in THIS process/instance — required, since the SDK never
                    # persists the verifier.
                    from osia.mcp_client.oauth import (
                        TunnelOAuthCallback,
                        build_oauth_provider,
                        OAUTH_REDIRECT_URI,
                        OAUTH_TUNNEL_REDIRECT_URI,
                    )

                    _cb = TunnelOAuthCallback(str(cfg.get("oauth_flow_id") or ""))
                    # WOR-98: when the trigger supplied a rendezvous-Worker redirect
                    # URI (any-user quick-tunnel flow), use it at index 0 — the SDK
                    # uses redirect_uris[0] for BOTH authorize and token exchange —
                    # while loopback + osia.space stay in the registered set (DCR
                    # registers all three). When absent (no mobile token), fall back
                    # to today's [osia.space, loopback], byte-for-byte unchanged.
                    worker_uri = str(cfg.get("oauth_redirect_uri") or "").strip()
                    ruris = (
                        [worker_uri, OAUTH_REDIRECT_URI, OAUTH_TUNNEL_REDIRECT_URI]
                        if worker_uri
                        else [OAUTH_TUNNEL_REDIRECT_URI, OAUTH_REDIRECT_URI]
                    )
                    provider = build_oauth_provider(
                        server_id, url, _cb, redirect_uris=ruris,
                    )
                    async with streamablehttp_client(url, auth=provider) as (read, write, _sid):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            tools = [
                                {
                                    "name": getattr(t, "name", ""),
                                    "description": getattr(t, "description", "") or "",
                                    "inputSchema": getattr(t, "inputSchema", None) or {},
                                    "annotations": _tool_annotations(t),
                                }
                                for t in getattr(listed, "tools", []) or []
                            ]
                            with self._lock:
                                conn.session = session
                                conn.tools = tools
                                conn.status = STATUS_CONNECTED
                                conn.error = ""
                            self._register_tools(server_id, tools)
                            self._safe_set_future(ready, STATUS_CONNECTED)
                            await stop_event.wait()
                elif auth_type == "oauth":
                    # WOR-44 Phase 5 (5-oauth-a): OAuth 2.1 branch. The SDK's
                    # OAuthClientProvider (an httpx.Auth) drives the ENTIRE flow —
                    # PKCE + RFC 9728/8414 discovery + RFC 7591 DCR + RFC 8707
                    # resource-binding + automatic refresh + Bearer injection;
                    # OSIA supplies only token storage, a fixed-port loopback
                    # callback server, and the browser launch (osia.mcp.oauth).
                    # OAuthCallbackServer is an async context manager so its
                    # listener is ALWAYS closed (no leak) and the interactive
                    # login is bounded. The session body below is byte-for-byte
                    # IDENTICAL to the token/no-auth branch — only how the client
                    # is opened differs (auth=provider vs headers=). Refresh is
                    # fully SDK-internal in this long-lived session (no OSIA loop).
                    from osia.mcp_client.oauth import (
                        OAuthCallbackServer,
                        build_oauth_provider,
                        force_oauth_login,
                    )

                    # WOR-100: a PRE-REGISTERED (non-DCR) connector (e.g. Google)
                    # pre-seeds a fixed client_id so the SDK skips DCR. Assemble it
                    # from the per-user stored credential (gitignored
                    # oauth_tokens.json) + the catalog's scope/auth-method hints,
                    # and pin the redirect to the single registered loopback URI.
                    # ``static_client``/``scope`` stay None for ordinary DCR
                    # connectors → build_oauth_provider is called EXACTLY as before
                    # (loopback+tunnel redirect, DCR, no seeding).
                    static_client = None
                    oauth_scope = None
                    if cfg.get("oauth_no_dcr"):
                        from osia.config import get_mcp_oauth
                        _sc = (get_mcp_oauth(server_id) or {}).get("static_client") or {}
                        _cid = str(_sc.get("client_id") or "").strip()
                        if _cid:
                            static_client = {
                                "client_id": _cid,
                                "client_secret": _sc.get("client_secret") or None,
                                "token_endpoint_auth_method": str(
                                    cfg.get("oauth_token_endpoint_auth_method") or "none"
                                ),
                            }
                            oauth_scope = str(cfg.get("oauth_scopes") or "").strip() or None

                    async with OAuthCallbackServer() as _cb:
                        provider = build_oauth_provider(
                            server_id, url, _cb,
                            static_client=static_client, scope=oauth_scope,
                        )
                        # WOR-100 Phase 2: a non-DCR connector's server 200s on
                        # initialize/list_tools (no 401), so the SDK's 401-driven
                        # auth never fires — FORCE the authorization-code flow here,
                        # on the connect path (OAUTH_CONNECT_TIMEOUT=420 s), BEFORE
                        # opening the session. Gated on ``static_client is not None``
                        # (i.e. oauth_no_dcr + a stored client_id) so DCR connectors
                        # (Notion/Linear/…) skip it entirely and keep their native
                        # 401 flow byte-for-byte. Runs INSIDE the OAuthCallbackServer
                        # context and BEFORE ``stop_event.wait()`` so WOR-97
                        # cancel_pending unwinds a parked login → DISCONNECTED. A
                        # stored token → headless (no browser). Failure raises →
                        # _serve records FAILED (no false CONNECTED). Least-privilege
                        # ``oauth_scope`` survives (the direct flow bypasses the SDK's
                        # 401-only scope override).
                        if static_client is not None:
                            await force_oauth_login(
                                provider,
                                server_id,
                                issuer=str(cfg.get("oauth_issuer") or ""),
                                authorization_endpoint=str(
                                    cfg.get("oauth_authorization_endpoint") or ""
                                ),
                                token_endpoint=str(
                                    cfg.get("oauth_token_endpoint") or ""
                                ),
                            )
                        async with streamablehttp_client(url, auth=provider) as (read, write, _sid):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                listed = await session.list_tools()
                                tools = [
                                    {
                                        "name": getattr(t, "name", ""),
                                        "description": getattr(t, "description", "") or "",
                                        "inputSchema": getattr(t, "inputSchema", None) or {},
                                        "annotations": _tool_annotations(t),
                                    }
                                    for t in getattr(listed, "tools", []) or []
                                ]
                                with self._lock:
                                    conn.session = session
                                    conn.tools = tools
                                    conn.status = STATUS_CONNECTED
                                    conn.error = ""
                                self._register_tools(server_id, tools)
                                self._safe_set_future(ready, STATUS_CONNECTED)
                                await stop_event.wait()
                else:
                    headers = cfg.get("headers") or None

                    async with streamablehttp_client(url, headers=headers) as (read, write, _sid):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            tools = [
                                {
                                    "name": getattr(t, "name", ""),
                                    "description": getattr(t, "description", "") or "",
                                    "inputSchema": getattr(t, "inputSchema", None) or {},
                                    "annotations": _tool_annotations(t),
                                }
                                for t in getattr(listed, "tools", []) or []
                            ]
                            # FIX 1(a/b): EXERCISE the credential before declaring
                            # CONNECTED. A remote server may serve initialize/
                            # list_tools unauthenticated (the existing 401-at-
                            # initialize guard only catches servers that enforce
                            # auth there), so a strictly read-only probe confirms a
                            # token-auth credential. Bounded; "" = pass/skip.
                            probe_err = await self._auth_probe(
                                session, server_id, tools, auth_type
                            )
                            if probe_err:
                                with self._lock:
                                    conn.session = None
                                    conn.status = STATUS_FAILED
                                    conn.error = _sanitize_error(probe_err, conn.cfg)
                                self._safe_set_future(ready, STATUS_FAILED)
                                return
                            with self._lock:
                                conn.session = session
                                conn.tools = tools
                                conn.status = STATUS_CONNECTED
                                conn.error = ""
                            # WOR-44 Phase 2: register the discovered tools into the
                            # agent's TOOL_MAP / KNOWN_TOOL_NAMES BEFORE signalling
                            # connected, so they are callable the moment connect()
                            # returns. Best-effort: a missing/closed agent never breaks
                            # the connection (the manager stays a usable MCP client).
                            self._register_tools(server_id, tools)
                            self._safe_set_future(ready, STATUS_CONNECTED)
                            # Keep the session alive until asked to stop. Enter AND
                            # exit of the async-with blocks both happen on THIS task —
                            # required for anyio cancel-scope safety.
                            await stop_event.wait()
            else:
                # Lazy import so this module imports cleanly even when the `mcp` SDK
                # is not installed (keeps the package import-safe and standalone).
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                cfg = conn.cfg
                params = StdioServerParameters(
                    command=str(cfg.get("command") or ""),
                    args=[str(a) for a in (cfg.get("args") or [])],
                    env=(dict(cfg["env"]) if cfg.get("env") else None),
                )

                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        tools = [
                            {
                                "name": getattr(t, "name", ""),
                                "description": getattr(t, "description", "") or "",
                                "inputSchema": getattr(t, "inputSchema", None) or {},
                                "annotations": _tool_annotations(t),
                            }
                            for t in getattr(listed, "tools", []) or []
                        ]
                        # FIX 1(a/b): EXERCISE the credential before declaring
                        # CONNECTED. initialize()/list_tools() are local protocol
                        # only (the stdio subprocess never hit the auth boundary),
                        # so a garbage/expired token would otherwise show CONNECTED
                        # and fail only on first use. Bounded, strictly read-only;
                        # "" = pass/skip, else fail the connect cleanly.
                        auth_type = str(cfg.get("auth_type") or "").strip().lower()
                        probe_err = await self._auth_probe(
                            session, server_id, tools, auth_type
                        )
                        if probe_err:
                            with self._lock:
                                conn.session = None
                                conn.status = STATUS_FAILED
                                conn.error = _sanitize_error(probe_err, conn.cfg)
                            self._safe_set_future(ready, STATUS_FAILED)
                            return
                        with self._lock:
                            conn.session = session
                            conn.tools = tools
                            conn.status = STATUS_CONNECTED
                            conn.error = ""
                        # WOR-44 Phase 2: register the discovered tools into the
                        # agent's TOOL_MAP / KNOWN_TOOL_NAMES BEFORE signalling
                        # connected, so they are callable the moment connect()
                        # returns. Best-effort: a missing/closed agent never breaks
                        # the connection (the manager stays a usable MCP client).
                        self._register_tools(server_id, tools)
                        self._safe_set_future(ready, STATUS_CONNECTED)
                        # Keep the session (and its subprocess) alive until asked to
                        # stop. Enter AND exit of the async-with blocks both happen
                        # on THIS task — required for anyio cancel-scope safety.
                        await stop_event.wait()
        except BaseException as e:  # noqa: BLE001 - isolate every failure
            # Includes CancelledError (disconnect/shutdown/cancel_pending
            # teardown): record a status, resolve `ready`, then re-raise a bare
            # CancelledError so the task ends cancelled; swallow ordinary
            # exceptions as a `failed` status.
            # WOR-97: classify a cancel even when anyio has WRAPPED the injected
            # CancelledError in a BaseExceptionGroup — a _serve task cancelled
            # while parked deep in the SDK's TaskGroup (e.g. cancel_pending
            # aborting a stuck OAuth login) surfaces an all-cancelled group, and a
            # pure cancel must record DISCONNECTED, never the FAILED a bare
            # isinstance check would have produced. A group carrying a real error
            # leaf is still a failure (see _all_leaves_cancelled).
            is_cancel = _all_leaves_cancelled(e)
            # Unwrap anyio's ExceptionGroup to the real leaf cause so both the
            # stored status AND the console show what actually failed (httpx
            # ConnectError, TLS, etc.) instead of the useless "unhandled errors
            # in a TaskGroup" wrapper. Only needed on the failure path.
            leaf, leaf_count = (e, 1) if is_cancel else _leaf_exception(e)
            with self._lock:
                conn.session = None
                if is_cancel:
                    if conn.status != STATUS_FAILED:
                        conn.status = STATUS_DISCONNECTED
                else:
                    conn.status = STATUS_FAILED
                    conn.error = _sanitize_error(
                        f"{type(leaf).__name__}: {leaf}", conn.cfg
                    )
            # Surface the leaf traceback on stderr (redacted) — outside the lock,
            # never for a normal cancel/teardown. Best-effort, never raises.
            if not is_cancel:
                _print_connect_failure(server_id, leaf, leaf_count, conn.cfg)
            self._safe_set_future(ready, conn.status)
            # Re-raise ONLY a BARE CancelledError so the task ends cancelled (anyio
            # cancel-scope safety). A wrapped all-cancelled ExceptionGroup has
            # already unwound its scopes by the time it reaches this top-level
            # except, so it is swallowed (its status was classified as a cancel
            # above) — byte-identical to the prior behaviour for the wrapped case,
            # which the old `isinstance` check also left unre-raised.
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            with self._lock:
                conn.session = None
                if conn.status == STATUS_CONNECTED:
                    # Normal stop_event path (no exception): mark disconnected.
                    conn.status = STATUS_DISCONNECTED
            # WOR-44 Phase 2: the server is going away by ANY path (graceful
            # stop, cancel, or error after connect) — drop its tools from the
            # agent maps so the model can no longer call a dead server.
            self._unregister_tools(server_id)
            self._safe_set_future(ready, conn.status)
            conn.done_event.set()

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(
        self, server_cfg: dict, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    ) -> str:
        """Connect to one MCP server and discover its tools. Blocks (bounded by
        ``connect_timeout``) until the handshake + ``list_tools`` complete or
        fail, then returns the resulting status string. Idempotent for a server
        already connecting/connected. Never raises."""
        server_id = str(server_cfg.get("id") or "").strip()
        if not server_id:
            return STATUS_FAILED
        transport = str(server_cfg.get("transport") or "stdio").strip().lower()
        auth_type = str(server_cfg.get("auth_type") or "").strip().lower()

        # WOR-44 Phase 5 (5-oauth-a): an OAuth connect blocks on an interactive
        # browser login, so extend the ready-future budget past the loopback
        # callback wait. Only oauth servers are affected; the default (45s) is
        # unchanged for stdio / token / no-auth. ``max`` preserves any larger
        # caller-supplied timeout.
        effective_connect_timeout = connect_timeout
        if transport == "streamable_http" and auth_type == "oauth":
            effective_connect_timeout = max(connect_timeout, OAUTH_CONNECT_TIMEOUT)

        loop = self._ensure_loop()
        if loop is None:
            # A shutdown is in progress — refuse to start new work (and do not
            # register the server, so nothing is left behind to clean up).
            return STATUS_FAILED

        with self._lock:
            existing = self._servers.get(server_id)
            if existing is not None and existing.status in (
                STATUS_CONNECTING,
                STATUS_CONNECTED,
            ):
                # WOR-97: a re-tap on an OAuth connector stuck CONNECTING (an
                # abandoned interactive login parks ``_serve`` in the provider's
                # ``callback_handler``) must CANCEL the zombie flow and restart —
                # not refuse for the full callback timeout. Gated to OAuth +
                # CONNECTING: a CONNECTED connector, and EVERY non-OAuth
                # (token/stdio) CONNECTING, still return the current status, so
                # the fast connect path and the happy path stay byte-identical.
                if not (auth_type == "oauth"
                        and existing.status == STATUS_CONNECTING):
                    return existing.status
                cancel_first = True
            else:
                cancel_first = False
                conn = _ServerConn(server_id=server_id, cfg=dict(server_cfg))
                conn.status = STATUS_CONNECTING
                self._servers[server_id] = conn

        if cancel_first:
            # Unwind the parked flow OUTSIDE the lock (``cancel_pending`` waits on
            # ``done_event`` up to ~10s — it must never block the lock). If the
            # login actually completed in the gap it returns CONNECTED; honour
            # that and do not start a redundant second flow.
            if self.cancel_pending(server_id) == STATUS_CONNECTED:
                return STATUS_CONNECTED
            with self._lock:
                conn = _ServerConn(server_id=server_id, cfg=dict(server_cfg))
                conn.status = STATUS_CONNECTING
                self._servers[server_id] = conn

        # Validate transport / required fields BEFORE scheduling the coroutine.
        # These branches never schedule _serve, so they mark the server FAILED
        # *and set its done_event* (via _fail_unserved) — otherwise a later
        # disconnect/shutdown would block waiting on an event nothing sets.
        if transport not in KNOWN_TRANSPORTS:
            self._fail_unserved(server_id, f"unknown transport {transport!r}")
            return STATUS_FAILED
        if transport not in SUPPORTED_TRANSPORTS:
            self._fail_unserved(
                server_id,
                f"transport {transport!r} not supported in Phase 1 (stdio only)",
            )
            return STATUS_FAILED
        if transport == "stdio" and not str(server_cfg.get("command") or "").strip():
            self._fail_unserved(server_id, "stdio transport requires a 'command'")
            return STATUS_FAILED
        if transport == "streamable_http" and not str(server_cfg.get("url") or "").strip():
            self._fail_unserved(
                server_id, "streamable_http transport requires a 'url'"
            )
            return STATUS_FAILED
        # Gap #2 (credential hardening): a token-auth connector MUST carry a
        # non-empty credential to even ATTEMPT a connect — otherwise the local
        # handshake succeeds and shows a misleading CONNECTED with no auth
        # exercised. The caller injects the token into env[token_env] (stdio) or an
        # ``Authorization: Bearer`` header (streamable_http); an empty/missing one
        # fails fast. (OAuth carries its bearer via the SDK provider, not here.)
        if auth_type == "token":
            env = server_cfg.get("env") if isinstance(server_cfg.get("env"), dict) else {}
            headers = (
                server_cfg.get("headers")
                if isinstance(server_cfg.get("headers"), dict) else {}
            )
            token_env = str(server_cfg.get("token_env") or "").strip()
            has_token = False
            if transport == "stdio":
                has_token = bool(token_env and str(env.get(token_env) or "").strip())
            elif transport == "streamable_http":
                auth_h = str((headers or {}).get("Authorization") or "").strip()
                has_token = bool(auth_h) and auth_h.lower() != "bearer"
            if not has_token:
                self._fail_unserved(server_id, "token required")
                return STATUS_FAILED

        # WOR-100: a non-DCR OAuth connector (e.g. Google Gmail/Drive) needs a
        # PRE-REGISTERED client_id — there is no DCR to obtain one. Without a
        # stored client_id the SDK would fall back to DCR and the provider would
        # 400, OR (for servers that 200 unauthenticated, like Google) the connect
        # would show a misleading CONNECTED with no usable auth. Fail fast with a
        # clear message instead. Gated on the ``oauth_no_dcr`` flag → ordinary DCR
        # OAuth connectors (Notion/Linear/Sentry/...) are NOT affected.
        if auth_type == "oauth" and server_cfg.get("oauth_no_dcr"):
            cid = ""
            try:
                from osia.config import get_mcp_oauth
                sc = (get_mcp_oauth(server_id) or {}).get("static_client") or {}
                cid = str(sc.get("client_id") or "").strip()
            except Exception:
                cid = ""
            if not cid:
                self._fail_unserved(server_id, "OAuth client ID required")
                return STATUS_FAILED

        ready: concurrent.futures.Future = concurrent.futures.Future()
        try:
            serve_future = asyncio.run_coroutine_threadsafe(
                self._serve(server_id, ready), loop
            )
        except RuntimeError as e:
            self._fail_unserved(
                server_id, f"could not schedule connect ({e})"
            )
            return STATUS_FAILED
        with self._lock:
            c = self._servers.get(server_id)
            if c is not None:
                c.serve_future = serve_future

        try:
            ready.result(timeout=effective_connect_timeout)
        except concurrent.futures.TimeoutError:
            self._set_status(
                server_id,
                STATUS_FAILED,
                f"connect timed out after {effective_connect_timeout:.0f}s",
            )
            # Abort the (possibly hung) handshake so its subprocess is reaped,
            # then briefly wait for _serve's async-with blocks to finish
            # unwinding so we don't leave a transport tearing down behind us.
            try:
                serve_future.cancel()
            except Exception:
                pass
            c = self._get(server_id)
            if c is not None:
                c.done_event.wait(timeout=DEFAULT_DISCONNECT_TIMEOUT)
            return STATUS_FAILED
        except Exception as e:  # noqa: BLE001
            self._set_status(server_id, STATUS_FAILED, f"{type(e).__name__}: {e}")
            return STATUS_FAILED

        return self.status(server_id)

    def disconnect(
        self, server_id: str, timeout: float = DEFAULT_DISCONNECT_TIMEOUT
    ) -> str:
        """Close a server's session cleanly and return the resulting status.
        Graceful stop first; if it doesn't finish within ``timeout`` the serve
        task is cancelled. Never raises."""
        conn = self._get(server_id)
        if conn is None:
            return STATUS_DISCONNECTED

        # Never served (validation / scheduling failure): there is no _serve
        # task or subprocess to wind down, and nothing will ever set done_event
        # — return at once instead of blocking on it (twice).
        if conn.serve_future is None:
            with self._lock:
                conn.session = None
                conn.done_event.set()
                if conn.status != STATUS_FAILED:
                    conn.status = STATUS_DISCONNECTED
            return conn.status

        self._signal_stop(server_id)
        finished = conn.done_event.wait(timeout=timeout)
        if not finished:
            sf = conn.serve_future
            if sf is not None:
                try:
                    sf.cancel()
                except Exception:
                    pass
            conn.done_event.wait(timeout=timeout)

        with self._lock:
            conn.session = None
            if conn.status not in (STATUS_FAILED,):
                conn.status = STATUS_DISCONNECTED
        return conn.status

    def cancel_pending(self, server_id: str) -> str:
        """WOR-97: cancel an in-flight (CONNECTING) connect and reset the
        connector to DISCONNECTED. Returns the resulting status.

        An abandoned interactive OAuth login parks ``_serve`` in the provider's
        ``callback_handler`` (awaiting the browser redirect) for up to
        ``OAUTH_CALLBACK_TIMEOUT`` — during which ``conn.status`` stays CONNECTING
        and the same-connector guard refuses any retry. This unwinds that parked
        flow AT ONCE by cancelling its serve task (no graceful ``stop_event``
        wait: the coroutine is not at ``await stop_event.wait()`` yet, so signalling
        the event would do nothing), then forces the status to DISCONNECTED. The
        cancel lands DISCONNECTED (never FAILED) whether anyio surfaces it as a
        bare ``CancelledError`` or a wrapped all-cancelled ``ExceptionGroup`` —
        ``_serve``'s except classifies the latter as a cancel too (see
        ``_all_leaves_cancelled``), so the forced write here is belt-and-braces,
        not the sole guarantee.

        Real-login-wins (the achievable guarantee): a login that has ALREADY
        reached CONNECTED when this is called is left untouched — the CONNECTING
        guard below returns its status early and the live session survives. A
        login that completes DURING the cancel's async teardown CANNOT be
        preserved: cancelling the serve task unwinds ``_serve``'s ``async with``
        session/transport blocks, so the session is torn down and the connector
        lands DISCONNECTED — but the SDK already persisted the freshly-minted
        token, so the next connect reconnects headlessly (no browser). That
        outcome is correct for all three callers (re-tap wants a fresh flow;
        watchdog / explicit-cancel asked for the abort).

        Idempotent and a no-op unless the connector is CONNECTING; safe to call
        from any worker thread; never raises; NEVER sets ``enabled`` (a cancelled
        flow never connected)."""
        # Snapshot + idempotent guard: only a genuinely in-flight (CONNECTING)
        # connect is cancellable. A connector that already reached CONNECTED (the
        # real login won before we were called) — or any terminal state — is left
        # untouched: its status is returned and the live session survives.
        with self._lock:
            conn = self._servers.get(server_id)
            if conn is None:
                return STATUS_DISCONNECTED
            if conn.status != STATUS_CONNECTING:
                return conn.status
            sf = conn.serve_future

        # Cancel OUTSIDE the lock (matches ``disconnect``): cancelling the serve
        # future propagates a CancelledError into the parked ``callback_handler``
        # await and unwinds ``_serve``'s async-with blocks on the loop thread
        # (closing the loopback listener / freeing the cross-loop registry entry
        # via its own ``finally``). A no-op (returns False) only if the flow
        # already finished.
        if sf is not None:
            try:
                sf.cancel()
            except Exception:
                pass
        # Bounded wait for the unwind so we don't leave a transport tearing down
        # behind us (``done_event`` is a threading.Event — safe off the lock).
        conn.done_event.wait(timeout=DEFAULT_DISCONNECT_TIMEOUT)

        with self._lock:
            conn = self._servers.get(server_id)
            if conn is None:
                return STATUS_DISCONNECTED
            if conn.status == STATUS_CONNECTED:
                # Defensive backstop: if a CONNECTED status somehow survived the
                # cancel (it normally cannot — sf.cancel() tears the session down),
                # never clobber a live session. The early CONNECTING guard above is
                # the real real-login-wins path.
                return STATUS_CONNECTED
            conn.session = None
            conn.status = STATUS_DISCONNECTED

        # Outside the lock (avoid re-entrancy through the listener): push the
        # corrected status to the registered status listener so the server's
        # mirror repaints the PC + mobile pills. Best-effort (already wrapped).
        _emit_status_change(server_id, STATUS_DISCONNECTED)
        return STATUS_DISCONNECTED

    def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Optional[dict] = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> str:
        """Call a tool on a connected server and return its result as a string.

        ALWAYS returns a string — every failure path (server not registered /
        not connected / loop down / scheduling error / timeout / transport
        exception / ``isError`` / conversion error) returns an
        ``ERROR: mcp/<server>/<tool>: ...`` string. It NEVER raises.
        """
        arguments = arguments or {}
        prefix = f"ERROR: mcp/{server_id}/{tool_name}:"

        conn = self._get(server_id)
        if conn is None:
            return f"{prefix} server not registered"

        with self._lock:
            status = conn.status
            session = conn.session
            err = conn.error
        if status != STATUS_CONNECTED or session is None:
            detail = f" ({err})" if err else ""
            return f"{prefix} not connected (status={status}){detail}"

        loop = self._loop
        if loop is None or not loop.is_running():
            return f"{prefix} MCP loop not running"

        try:
            fut = asyncio.run_coroutine_threadsafe(
                session.call_tool(tool_name, arguments), loop
            )
        except RuntimeError as e:
            return f"{prefix} could not schedule call ({e})"
        except Exception as e:  # noqa: BLE001
            return f"{prefix} {type(e).__name__}: {e}"

        try:
            res = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            try:
                fut.cancel()
            except Exception:
                pass
            return f"{prefix} timed out after {timeout:.0f}s"
        except Exception as e:  # noqa: BLE001
            detail = f"{type(e).__name__}: {e}"
            msg = f"{prefix} {detail}"
            # FIX 1(c): a transport-level auth rejection (e.g. a streamable_http
            # 401 raised by the SDK) demotes the connector so it stops showing a
            # false CONNECTED. Scan only ``detail`` (not the server/tool-id prefix).
            if _auth_failure_signal(detail):
                self._demote_auth_failed(server_id)
            return msg

        try:
            out = _calltoolresult_to_text(res, server_id, tool_name)
        except Exception as e:  # noqa: BLE001 - conversion backstop
            return f"{prefix} result conversion failed: {type(e).__name__}: {e}"
        # FIX 1(c): demote-on-auth-error. If the credential was rejected at CALL
        # time (the handshake could not catch it — e.g. an unknown connector with
        # no safe probe), flip the connector to FAILED so the PC + mobile pills
        # self-correct. Conservative: only an ERROR-shaped result carrying a clear
        # auth signal — never a 404 / bad-arg / rate-limit / generic tool error,
        # and scanning only the post-prefix detail so the server/tool id can't trip.
        if _looks_like_error(out) and _auth_failure_signal(
            _strip_mcp_prefix(out, server_id, tool_name)
        ):
            self._demote_auth_failed(server_id)
        return out

    def list_tools(self, server_id: str) -> list:
        """Return the cached discovered tools for a connected server (a list of
        ``{"name", "description", "inputSchema"}`` dicts), or ``[]``."""
        conn = self._get(server_id)
        if conn is None or conn.status != STATUS_CONNECTED:
            return []
        with self._lock:
            return [dict(t) for t in conn.tools]

    def status(self, server_id: str) -> str:
        conn = self._get(server_id)
        return conn.status if conn is not None else STATUS_DISCONNECTED

    def error(self, server_id: str) -> str:
        conn = self._get(server_id)
        return conn.error if conn is not None else ""

    def servers(self) -> dict:
        """Snapshot of all known servers: ``{id: {status, error, tools}}``.
        Never leaks session objects or secrets."""
        with self._lock:
            return {
                sid: {
                    "status": c.status,
                    "error": c.error,
                    "tools": len(c.tools),
                }
                for sid, c in self._servers.items()
            }

    def shutdown(self, timeout: float = DEFAULT_DISCONNECT_TIMEOUT) -> None:
        """Disconnect every server, stop the loop, and join the daemon thread.
        Idempotent, never raises, and leaves the manager reusable: a later
        ``connect`` starts a fresh loop. While a shutdown is in progress,
        ``_ensure_loop`` returns ``None`` so a concurrent ``connect`` cannot
        resurrect the loop or leave a dangling thread."""
        # Read the loop/thread under the lifecycle lock for a consistent
        # snapshot; mark the shutdown so concurrent connects bow out.
        with self._lifecycle_lock:
            self._shutting_down = True
            loop = self._loop
            thread = self._thread

        # Snapshot server ids under the map lock (the documented guard).
        with self._lock:
            sids = list(self._servers.keys())
        for sid in sids:
            try:
                self.disconnect(sid, timeout=timeout)
            except Exception:
                pass

        # Stop the loop and join the thread OUTSIDE the locks (the join can
        # block up to the timeout; the loop thread never takes these locks, but
        # holding them across a blocking join is still avoided on principle).
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=timeout + _LOOP_START_TIMEOUT)

        with self._lock:
            self._servers.clear()
        with self._lifecycle_lock:
            self._loop = None
            self._thread = None
            self._shutting_down = False

    # Context-manager sugar.
    def __enter__(self) -> "MCPManager":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()


# ---------------------------------------------------------------------------
# Process-wide singleton (optional convenience for later phases)
# ---------------------------------------------------------------------------

_manager_singleton: Optional[MCPManager] = None
_singleton_lock = threading.Lock()


def get_manager() -> MCPManager:
    """Return a lazily-created process-wide ``MCPManager``. Later phases (agent
    wiring / server connect-disconnect handlers) share one manager via this."""
    global _manager_singleton
    with _singleton_lock:
        if _manager_singleton is None:
            _manager_singleton = MCPManager()
        return _manager_singleton
