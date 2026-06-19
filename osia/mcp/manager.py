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
            from osia.mcp import registry
            registry.register_server_tools(self, server_id, tools)
        except Exception:
            pass

    def _unregister_tools(self, server_id: str) -> None:
        try:
            from osia.mcp import registry
            registry.unregister_server_tools(server_id)
        except Exception:
            pass

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

                if auth_type == "oauth":
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
                    from osia.mcp.oauth import (
                        OAuthCallbackServer,
                        build_oauth_provider,
                    )

                    async with OAuthCallbackServer() as _cb:
                        provider = build_oauth_provider(server_id, url, _cb)
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
            # Includes CancelledError (disconnect/shutdown teardown): record a
            # status, resolve `ready`, then re-raise CancelledError so the task
            # ends cancelled; swallow ordinary exceptions as a `failed` status.
            is_cancel = isinstance(e, asyncio.CancelledError)
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
            if is_cancel:
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
                return existing.status
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
            return f"{prefix} {type(e).__name__}: {e}"

        try:
            return _calltoolresult_to_text(res, server_id, tool_name)
        except Exception as e:  # noqa: BLE001 - conversion backstop
            return f"{prefix} result conversion failed: {type(e).__name__}: {e}"

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
