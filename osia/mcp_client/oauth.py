#!/usr/bin/env python3
"""OSIA OAuth 2.1 substrate for MCP connectors — WOR-44 Phase 5 (5-oauth-a).

The MCP SDK (``mcp>=1.28``) ships ``OAuthClientProvider`` — an ``httpx.Auth``
that drives the ENTIRE OAuth 2.1 flow for us: PKCE, RFC 9728/8414 metadata
discovery, RFC 7591 Dynamic Client Registration, RFC 8707 resource-binding,
automatic token refresh, and ``Authorization: Bearer`` injection. OSIA supplies
only the three pieces the SDK leaves to the host application:

1. ``OsiaTokenStorage`` — the SDK's ``TokenStorage`` protocol, persisting the
   token + DCR client_info blob to the gitignored ``oauth_tokens.json`` (via
   :mod:`osia.config`). Writes are IMMEDIATE so a rotated refresh token is never
   lost across a restart (Linear rotates ~every 24h and invalidates the old one).
2. ``OAuthCallbackServer`` — a fixed-port (``127.0.0.1:8975``) asyncio loopback
   HTTP server that captures the ``?code=&state=`` redirect, shows a "you can
   close this tab" page, and resolves the SDK's ``callback_handler()``. The port
   is FIXED (not ephemeral) because some providers (Notion) reject dynamic
   redirect ports. The listener is ALWAYS closed (no leak / no hang) and the
   wait is bounded.
3. ``redirect_handler`` — opens the system browser at the authorize URL.

This module is import-safe without the ``mcp`` SDK installed: every ``mcp``
import is lazy (inside a method/function), mirroring ``osia.mcp.manager``. It
imports ``osia.config`` (no circular import — ``osia.config`` imports nothing
from ``osia.mcp``) and NOTHING from ``osia.agent`` / ``osia.server``.

OAuth tokens are SDK-internal secrets: they NEVER go through ``key_chain`` (that
rotates OSIA API keys, not OAuth bearer tokens), NEVER reach ``settings.json``,
NEVER ride a WebSocket frame, and are NEVER logged (token values are masked or
simply not printed).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import webbrowser
from typing import Any, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from osia.config import get_mcp_oauth, set_mcp_oauth
from osia.oauth_relay import OAUTH_RELAY_REDIRECT_URI


# ── Fixed loopback callback + flow constants ────────────────────────────────
# RFC 8252 §7.3 recommends the literal loopback IP (127.0.0.1) for native-app
# redirect URIs. The port is a STABLE module constant (not ephemeral): Notion
# and some other providers reject dynamic redirect ports, and a fixed value is
# what gets registered via DCR / pre-registered with the provider.
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 8975
OAUTH_REDIRECT_PATH = "/callback"
OAUTH_REDIRECT_URI = f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}{OAUTH_REDIRECT_PATH}"

# WOR-96 PART 1: the PUBLIC HTTPS callback for the fixed app-domain fallback
# (app.osia.space -> the SERVER process on :8765, which serves GET
# /oauth/callback). Mobile normally uses the rendezvous Worker redirect so it can
# follow the current quick tunnel; this URI stays registered for non-rendezvous
# tunnel flows. The flow that USES this redirect MUST run in the server process:
# that is where the redirect lands AND where the SDK's in-memory PKCE verifier
# and state live (the SDK never persists them), so the same process/instance
# that started the flow must perform the token exchange. See the
# ``oauth_callback_mode == "tunnel"`` branch
# in ``osia.mcp_client.manager._serve`` and the server's ``/oauth/callback`` route
# + cross-loop registry below.
OAUTH_TUNNEL_REDIRECT_URI = "https://app.osia.space/oauth/callback"
# Transitional overlap: keep the former root-domain callback registered while
# DNS and existing clients migrate. Do not use this as the primary redirect.
OAUTH_LEGACY_TUNNEL_REDIRECT_URI = "https://osia.space/oauth/callback"

OAUTH_CLIENT_NAME = "OSIA"

# How long we wait for the user to finish the interactive browser login before
# giving up. Bounded so a never-completed login can NEVER hang the asyncio loop.
# This is the REAL guard on the callback wait (the SDK does not enforce its own
# — see OAUTH_FLOW_TIMEOUT). The manager's OAUTH_CONNECT_TIMEOUT sits ABOVE this
# by enough slack to also cover the discovery + DCR (before this wait) and
# token-exchange (after it) network legs, so a never-completed login surfaces as
# this (clearer) callback timeout, and a near-deadline SUCCESS is not cancelled.
OAUTH_CALLBACK_TIMEOUT = 300.0
# Passed to OAuthClientProvider(timeout=...). NOTE: in mcp 1.28.0 the SDK stores
# this on its OAuthContext but NEVER enforces it (no wait_for / cancel scope in
# the auth flow), so it is inert defense-in-depth only — the REAL bounds are
# OAUTH_CALLBACK_TIMEOUT (the loopback wait) and the manager's
# OAUTH_CONNECT_TIMEOUT (the outer ready-future timeout). Kept so it activates
# automatically should a future SDK version begin honouring it.
OAUTH_FLOW_TIMEOUT = 300.0

# Minimal HTML returned to the browser after the redirect is captured.
_CLOSE_TAB_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>OSIA</title></head><body style='font-family:sans-serif;"
    "background:#15151a;color:#e8e8ea;text-align:center;padding-top:18vh'>"
    "<h2>OSIA connected ✓</h2>"
    "<p>Authorization complete — you can close this tab and return to OSIA.</p>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# TokenStorage — persists the SDK's token + client_info to oauth_tokens.json
# ---------------------------------------------------------------------------

class OsiaTokenStorage:
    """Implements the MCP SDK ``TokenStorage`` protocol for one server id.

    All four methods are ``async`` (the SDK awaits them). They persist to / read
    from ``oauth_tokens.json`` through :mod:`osia.config`. ``set_tokens`` /
    ``set_client_info`` write to disk IMMEDIATELY — so a refresh-token rotation
    is durable the instant it happens, and a restart re-reads the latest token +
    DCR client_info (so registration is NOT repeated). Honest boundary: the SDK
    does not pre-flight a refresh on restart (``_initialize`` restores the token
    without seeding its expiry, so ``is_token_valid()`` treats it as valid), so
    the stored access token is reused as-is — no re-auth while it is still
    accepted; if it expired between sessions the server's 401 triggers the SDK's
    normal re-auth (a fresh browser login — the SDK does NOT attempt a
    refresh_token grant on the 401 path). This is a pure-SDK limitation, not
    fixable from the TokenStorage layer.

    Within a single ``OAuthClientProvider`` these calls are serialized by the
    SDK's ``context.lock`` (see ``async_auth_flow``), so the read-modify-write of
    the per-server blob below never races token vs. client_info; the store file
    itself is guarded by ``osia.config._oauth_lock`` across servers/threads.
    """

    def __init__(self, server_id: str) -> None:
        self._sid = server_id

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken
        blob = get_mcp_oauth(self._sid)
        tok = blob.get("token") if isinstance(blob, dict) else None
        if not isinstance(tok, dict):
            return None
        try:
            return OAuthToken(**tok)
        except Exception:
            return None

    async def set_tokens(self, tokens: Any) -> None:
        blob = get_mcp_oauth(self._sid)
        if not isinstance(blob, dict):
            blob = {}
        data = tokens.model_dump(mode="json", exclude_none=True)
        blob["token"] = data
        # Bookkeeping only: an absolute expiry for our own inspection. The SDK
        # reconstructs validity from the token's own ``expires_in`` at runtime —
        # this field is never read back by the flow.
        exp = data.get("expires_in")
        if isinstance(exp, (int, float)):
            blob["expires_at"] = time.time() + int(exp)
        else:
            blob.pop("expires_at", None)
        set_mcp_oauth(self._sid, blob)

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull
        blob = get_mcp_oauth(self._sid)
        ci = blob.get("client") if isinstance(blob, dict) else None
        if not isinstance(ci, dict):
            return None
        try:
            return OAuthClientInformationFull(**ci)
        except Exception:
            return None

    async def set_client_info(self, client_info: Any) -> None:
        blob = get_mcp_oauth(self._sid)
        if not isinstance(blob, dict):
            blob = {}
        blob["client"] = client_info.model_dump(mode="json", exclude_none=True)
        set_mcp_oauth(self._sid, blob)


# ---------------------------------------------------------------------------
# Fixed-port loopback callback server
# ---------------------------------------------------------------------------

class OAuthCallbackServer:
    """Captures the OAuth redirect on a fixed loopback port.

    Use as an async context manager around the OAuth connect so the listener is
    ALWAYS closed (``__aexit__`` -> ``_stop`` is idempotent and is the backstop
    even if the flow errors before the callback fires)::

        async with OAuthCallbackServer() as cb:
            provider = build_oauth_provider(sid, url, cb)
            async with streamablehttp_client(url, auth=provider) as (...):
                ...

    The SDK calls ``redirect_handler(url)`` (opens the browser) then awaits
    ``callback_handler()`` (returns ``(code, state)``). We start the listener in
    ``redirect_handler`` — BEFORE the browser opens — so there is no race with
    the redirect, and ``callback_handler`` stops the listener as soon as the code
    is captured (freeing the fixed port for the rest of the session, and for any
    re-auth, which restarts it). Everything runs on the MCP manager's asyncio
    loop; the bounded wait means a never-finished login cannot hang the loop.
    """

    def __init__(self) -> None:
        self._server: Optional[asyncio.AbstractServer] = None
        self._future: Optional[asyncio.Future] = None

    async def __aenter__(self) -> "OAuthCallbackServer":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self._stop()
        return False

    async def _ensure_started(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle, OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT
        )

    async def _stop(self) -> None:
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.close()
                await srv.wait_closed()
            except Exception:
                pass

    async def redirect_handler(self, url: str) -> None:
        """Start the loopback listener, then open the system browser."""
        await self._ensure_started()
        try:
            webbrowser.open(url)
        except Exception:
            # Headless / no browser: the user can still paste the URL manually;
            # the listener stays up and the bounded wait still applies.
            pass

    async def callback_handler(self) -> tuple:
        """Await the captured ``(code, state)`` (bounded), then free the port."""
        await self._ensure_started()
        try:
            return await asyncio.wait_for(self._future, timeout=OAUTH_CALLBACK_TIMEOUT)
        finally:
            await self._stop()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        code: Optional[str] = None
        state: Optional[str] = None
        error: Optional[str] = None
        is_callback = False
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            request_line = line.decode("latin-1", "replace").strip()
            # "GET /callback?code=...&state=... HTTP/1.1"
            parts = request_line.split(" ")
            target = parts[1] if len(parts) >= 2 else ""
            split = urlsplit(target)
            if split.path == OAUTH_REDIRECT_PATH:
                is_callback = True
                qs = parse_qs(split.query)
                code = (qs.get("code") or [None])[0]
                state = (qs.get("state") or [None])[0]
                error = (qs.get("error") or [None])[0]
            # Respond (200 for the callback, 404 otherwise) so the browser shows
            # a clean page rather than a connection-reset.
            if is_callback:
                body = _CLOSE_TAB_HTML.encode("utf-8")
                head = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("utf-8")
            else:
                body = b"Not found"
                head = (
                    "HTTP/1.1 404 Not Found\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("utf-8")
            writer.write(head + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
        # Resolve the waiter exactly once, and only for the real callback path
        # (ignore favicon.ico and other stray hits).
        if not is_callback:
            return
        fut = self._future
        if fut is None or fut.done():
            return
        if error:
            fut.set_exception(RuntimeError(f"OAuth authorization failed: {error}"))
        else:
            fut.set_result((code, state))


# ---------------------------------------------------------------------------
# Cross-loop OAuth flow registry (server process) + tunnel callback transport
# ---------------------------------------------------------------------------
#
# WOR-96 PART 1. The HTTPS tunnel callback (OAUTH_TUNNEL_REDIRECT_URI) lands on
# the SERVER process's FastAPI event loop (GET /oauth/callback), but the OAuth
# provider that issued the request — and therefore the PKCE verifier and the
# awaiting ``callback_handler`` — runs on the MCPManager's OWN asyncio loop (a
# different thread in the SAME process). So delivery crosses TWO event loops
# within one process. The bridge is a ``concurrent.futures.Future`` per in-flight
# ``state``:
#   * GET /oauth/callback (FastAPI loop) calls ``resolve_oauth_flow`` →
#     ``future.set_result(...)`` — thread-safe, needs no loop reference.
#   * the MCPManager-loop ``callback_handler`` does ``await
#     asyncio.wrap_future(future, loop=<mcp loop>)`` — adopting the concurrent
#     future onto ITS loop (the done-callback hops back via call_soon_threadsafe).
# A bare ``asyncio.Future`` would be bound to one loop and could not be resolved
# safely from the other; that is why this is a ``concurrent.futures.Future``.
#
# These registries are MODULE singletons, so each live in whichever process
# imported this module. Only the server process ever runs a tunnel flow (the
# manager branch is gated on cfg ``oauth_callback_mode == "tunnel"``, set ONLY by
# the server's trigger route), so the desktop process's copies stay empty and
# unused. No secret is held at rest: only the single-use ``code`` transits a
# future to the waiting handler; entries auto-expire (OAUTH_CALLBACK_TIMEOUT) and
# are removed on resolve / timeout / connect completion.

_OAUTH_REG_LOCK = threading.Lock()
# state -> {"future": concurrent.futures.Future, "created_at": float}
_OAUTH_STATE_FLOWS: dict = {}
# flow_id -> {"auth_url": Optional[str], "error": Optional[str],
#             "event": threading.Event, "created_at": float}
_OAUTH_TRIGGER_FLOWS: dict = {}


def _prune_oauth_flows(now: Optional[float] = None) -> None:
    """Drop registry entries older than OAUTH_CALLBACK_TIMEOUT. Caller MUST hold
    ``_OAUTH_REG_LOCK``. Bounds memory if a flow is abandoned (user never finishes
    the login, or a stray /oauth/callback arrives for an expired state)."""
    now = time.time() if now is None else now
    horizon = now - OAUTH_CALLBACK_TIMEOUT
    for st in [k for k, v in _OAUTH_STATE_FLOWS.items()
               if v.get("created_at", 0.0) < horizon]:
        _OAUTH_STATE_FLOWS.pop(st, None)
    for fid in [k for k, v in _OAUTH_TRIGGER_FLOWS.items()
                if v.get("created_at", 0.0) < horizon]:
        _OAUTH_TRIGGER_FLOWS.pop(fid, None)


def register_oauth_trigger_flow(flow_id: str) -> None:
    """Trigger-side: open a pending flow keyed by the OSIA-generated ``flow_id``
    BEFORE starting connect(), so the authorize URL the provider's
    ``redirect_handler`` surfaces later has somewhere to land and a waiter to
    wake. No-op on an empty id."""
    if not flow_id:
        return
    with _OAUTH_REG_LOCK:
        _prune_oauth_flows()
        _OAUTH_TRIGGER_FLOWS[flow_id] = {
            "auth_url": None, "error": None,
            "event": threading.Event(), "created_at": time.time(),
        }


def publish_oauth_auth_url(flow_id: str, auth_url: str) -> None:
    """``redirect_handler``-side: surface the authorize URL for ``flow_id`` and
    wake the trigger waiting on it. No-op if the flow was never registered or has
    expired (the connect still proceeds; only the URL hand-off is lost)."""
    if not flow_id:
        return
    ev: Optional[threading.Event] = None
    with _OAUTH_REG_LOCK:
        entry = _OAUTH_TRIGGER_FLOWS.get(flow_id)
        if entry is None:
            return
        entry["auth_url"] = auth_url
        ev = entry["event"]
    if ev is not None:
        ev.set()


def fail_oauth_trigger_flow(flow_id: str, error: str) -> None:
    """Worker-side: mark a flow failed BEFORE the authorize URL was surfaced (e.g.
    discovery/DCR failed, or connect resolved non-CONNECTED) and wake the trigger
    so it returns promptly instead of blocking the full wait. Never clobbers an
    already-surfaced auth_url."""
    if not flow_id:
        return
    ev: Optional[threading.Event] = None
    with _OAUTH_REG_LOCK:
        entry = _OAUTH_TRIGGER_FLOWS.get(flow_id)
        if entry is None:
            return
        if entry.get("auth_url") is None and not entry.get("error"):
            entry["error"] = error or "oauth flow failed"
        ev = entry["event"]
    if ev is not None:
        ev.set()


def wait_oauth_auth_url(flow_id: str, timeout: float):
    """Trigger-side: BLOCK (off any event loop — call via run_in_executor) until
    the authorize URL is ready or the flow errors / times out. Returns
    ``(auth_url, error)``; both ``None`` on timeout."""
    with _OAUTH_REG_LOCK:
        entry = _OAUTH_TRIGGER_FLOWS.get(flow_id)
    if entry is None:
        return None, "flow not registered"
    entry["event"].wait(timeout=timeout)
    with _OAUTH_REG_LOCK:
        entry = _OAUTH_TRIGGER_FLOWS.get(flow_id)
        if entry is None:
            return None, "flow expired"
        return entry.get("auth_url"), entry.get("error")


def cleanup_oauth_trigger_flow(flow_id: str) -> None:
    """Worker-side: drop the trigger entry once connect() has resolved (the
    authorize URL has been consumed by then; the state→future entry is freed
    separately by ``TunnelOAuthCallback.callback_handler``)."""
    if not flow_id:
        return
    with _OAUTH_REG_LOCK:
        _OAUTH_TRIGGER_FLOWS.pop(flow_id, None)


def register_oauth_state(state: str) -> "concurrent.futures.Future":
    """``redirect_handler``-side: register a ``state`` → cross-loop future the
    public callback route will resolve. Returns the ``concurrent.futures.Future``
    (unstored if ``state`` is empty — it then simply times out in the handler)."""
    fut: concurrent.futures.Future = concurrent.futures.Future()
    if not state:
        return fut
    with _OAUTH_REG_LOCK:
        _prune_oauth_flows()
        _OAUTH_STATE_FLOWS[state] = {"future": fut, "created_at": time.time()}
    return fut


def resolve_oauth_flow(state: str, code: Optional[str],
                       error: Optional[str] = None) -> bool:
    """Public-callback-side: deliver ``(code, state)`` (or an error) to the
    waiting future for ``state``. Returns True iff a live, unresolved flow matched
    — False for an unknown / expired / already-resolved state (the route then
    404s). Thread-safe: resolves a ``concurrent.futures.Future``, so it may be
    called from the FastAPI loop while the awaiter runs on the MCP loop."""
    if not state:
        return False
    with _OAUTH_REG_LOCK:
        entry = _OAUTH_STATE_FLOWS.get(state)
        fut = entry.get("future") if entry else None
        if fut is None or fut.done():
            return False
    try:
        if error:
            fut.set_exception(RuntimeError("OAuth authorization was refused."))
        else:
            fut.set_result((code, state))
        return True
    except Exception:
        return False


def discard_oauth_state(state: str) -> None:
    """``callback_handler``-side: remove the state→future entry once consumed."""
    if not state:
        return
    with _OAUTH_REG_LOCK:
        _OAUTH_STATE_FLOWS.pop(state, None)


# ---------------------------------------------------------------------------
# RFC 7009 token revocation (WOR-96 PART 2a) — best-effort "truly disconnect"
# ---------------------------------------------------------------------------
#
# The MCP SDK (1.28) drives discovery + refresh + Bearer injection but exposes NO
# client-side token-revocation helper, and the AS metadata it discovers lives only
# in the provider's IN-MEMORY context (gone once the session closes). So to TRULY
# disconnect an OAuth connector — invalidate the grant at the authorization server
# so a later reconnect / account-switch is a clean re-auth — we re-discover the
# ``revocation_endpoint`` from the well-knowns and POST per RFC 7009 ourselves.
#
# Best-effort BY CONTRACT: these helpers NEVER raise and NEVER log token values.
# If the endpoint can't be discovered (the provider advertises none) revocation is
# a no-op — the disconnect handler still clears the local token, so OSIA forgets
# it regardless. httpx is imported lazily (kept off the module's import-safe-
# without-mcp path; it is always present wherever the OAuth SDK runs).

async def _discover_revocation_endpoint(server_url: str) -> Optional[str]:
    """Best-effort: return the AS ``revocation_endpoint`` for ``server_url`` (RFC
    9728 protected-resource metadata → RFC 8414 / OpenID AS metadata), or None.
    Never raises."""
    try:
        import httpx
    except Exception:
        return None
    if not server_url:
        return None
    try:
        parts = urlsplit(server_url)
        origin = f"{parts.scheme}://{parts.netloc}"
    except Exception:
        return None
    if not parts.scheme or not parts.netloc:
        return None

    async def _get_json(client, url):
        try:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
        except Exception:
            return None
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            # RFC 9728: the resource may name its authorization server(s). Probe
            # that first, then fall back to the resource origin acting as its own
            # AS (common for MCP servers).
            as_issuers: list = []
            # RFC 9728 §3.1: the PRM well-known may carry the resource's path
            # AFTER the well-known segment. Google's MCP servers serve PRM ONLY at
            # the path-suffixed URL (e.g. /.well-known/oauth-protected-resource/mcp/v1
            # → authorization_servers:[accounts.google.com]) and 404 at the bare
            # path — so probe BOTH, else a path-based resource's AS (and thus its
            # revocation_endpoint) is undiscoverable and revoke silently no-ops.
            prm_urls = [f"{origin}/.well-known/oauth-protected-resource"]
            res_path = parts.path.rstrip("/")
            if res_path:
                prm_urls.append(
                    f"{origin}/.well-known/oauth-protected-resource{res_path}"
                )
            for prm_url in prm_urls:
                prm = await _get_json(client, prm_url)
                if isinstance(prm, dict):
                    servers = prm.get("authorization_servers")
                    if isinstance(servers, list):
                        as_issuers.extend(
                            s for s in servers if isinstance(s, str) and s
                        )
            as_issuers.append(origin)

            seen: set = set()
            for issuer in as_issuers:
                iss = issuer.rstrip("/")
                if not iss or iss in seen:
                    continue
                seen.add(iss)
                ip = urlsplit(iss)
                if not ip.scheme or not ip.netloc:
                    continue
                iss_origin = f"{ip.scheme}://{ip.netloc}"
                iss_path = ip.path.rstrip("/")
                # RFC 8414 inserts the well-known segment BEFORE any issuer path;
                # also try the OpenID layout and the bare issuer. All best-effort.
                candidates = [
                    f"{iss_origin}/.well-known/oauth-authorization-server{iss_path}",
                    f"{iss}/.well-known/oauth-authorization-server",
                    f"{iss_origin}/.well-known/openid-configuration{iss_path}",
                    f"{iss}/.well-known/openid-configuration",
                ]
                for url in candidates:
                    md = await _get_json(client, url)
                    if isinstance(md, dict):
                        rev = md.get("revocation_endpoint")
                        if isinstance(rev, str) and rev:
                            return rev
    except Exception:
        return None
    return None


async def revoke_oauth_token(server_id: str, server_url: str) -> bool:
    """RFC 7009 best-effort revocation of a connector's OAuth grant.

    Reads the stored refresh token (preferred — revoking it SHOULD cascade to its
    access tokens at the AS per RFC 7009 §2.1) or, failing that, the access token,
    plus the DCR ``client_id`` / optional ``client_secret`` from
    ``oauth_tokens.json``, then re-discovers the ``revocation_endpoint`` and POSTs
    the standard ``application/x-www-form-urlencoded`` revocation request. Returns
    True iff a revocation request was sent and accepted (2xx), else False.

    NEVER raises and NEVER logs token values. A missing token or an undiscoverable
    revocation endpoint is a benign no-op (returns False) — the caller still drops
    the local token, so OSIA forgets the connector either way."""
    try:
        import httpx
    except Exception:
        return False
    try:
        blob = get_mcp_oauth(server_id)
    except Exception:
        blob = {}
    if not isinstance(blob, dict):
        return False
    tok = blob.get("token") if isinstance(blob.get("token"), dict) else {}
    refresh = tok.get("refresh_token")
    access = tok.get("access_token")
    token = refresh or access
    if not isinstance(token, str) or not token:
        return False
    hint = "refresh_token" if refresh else "access_token"

    client = blob.get("client") if isinstance(blob.get("client"), dict) else {}
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    # RFC 7009 inherits the connector's TOKEN-ENDPOINT client-auth method, so we
    # must authenticate revocation the SAME way the SDK authenticated the token
    # exchange. The DCR response persisted that method into the client blob (via
    # OsiaTokenStorage.set_client_info); mirror the SDK's three-way branch (mcp
    # 1.28 client/auth/oauth2.py) instead of inferring it from secret presence —
    # a "client_secret_post" client sends the secret in the BODY, and a strict AS
    # rejects HTTP Basic for it (which would leave the grant alive at the AS).
    auth_method = client.get("token_endpoint_auth_method")

    endpoint = await _discover_revocation_endpoint(server_url)
    if not endpoint:
        return False

    data = {"token": token, "token_type_hint": hint}
    auth = None
    has_secret = isinstance(client_secret, str) and bool(client_secret)
    if auth_method == "client_secret_post" and has_secret:
        # Confidential, secret in the BODY (what the AS registered this client for).
        if isinstance(client_id, str) and client_id:
            data["client_id"] = client_id
        data["client_secret"] = client_secret
    elif has_secret:
        # client_secret_basic (or unspecified-with-secret) → HTTP Basic. A single
        # auth method only: do NOT also put client_id in the body.
        auth = (str(client_id or ""), client_secret)
    elif isinstance(client_id, str) and client_id:
        # Public / PKCE client ("none") → identify with client_id in the body.
        data["client_id"] = client_id

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as http:
            r = await http.post(
                endpoint, data=data, auth=auth,
                headers={"Accept": "application/json"},
            )
        # RFC 7009 §2.2: the AS returns 200 for success AND for an already-invalid
        # token. Treat any 2xx as done; never inspect or log the response body.
        return 200 <= r.status_code < 300
    except Exception:
        return False


class TunnelOAuthCallback:
    """Server-process OAuth callback transport for the HTTPS tunnel redirect.

    Drop-in alternative to :class:`OAuthCallbackServer` exposing the SAME two SDK
    hooks (``redirect_handler``/``callback_handler``) but, instead of a loopback
    socket, bridging the provider (on the MCPManager loop) to the public
    ``GET /oauth/callback`` route (on the FastAPI loop) via the cross-loop registry
    above. It owns NO socket, so — unlike OAuthCallbackServer — it is NOT a context
    manager (nothing to open/close).

    One instance per connect, carrying the OSIA-generated ``flow_id`` so the
    trigger route can retrieve the authorize URL surfaced in ``redirect_handler``.
    """

    def __init__(self, flow_id: str) -> None:
        self._flow_id = str(flow_id or "")
        self._state: Optional[str] = None
        self._future: Optional[concurrent.futures.Future] = None

    async def redirect_handler(self, url: str) -> None:
        """Register the SDK-generated ``state`` (parsed out of the authorize URL —
        the SDK embeds it there), then surface the URL to the waiting trigger.
        Does NOT open a browser: the server process neutralises
        ``webbrowser.open`` and, in PART 1, the desktop opens the URL on the PC
        (PART 2 will push it to the phone)."""
        state: Optional[str] = None
        try:
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            states = query.get("state") or []
            redirects = query.get("redirect_uri") or []
            if len(states) == 1 and redirects == [OAUTH_RELAY_REDIRECT_URI]:
                state = states[0]
        except Exception:
            state = None
        self._state = state
        self._future = register_oauth_state(state) if state else None
        if not state:
            raise RuntimeError("OAuth relay callback: invalid state")
        try:
            from osia.oauth_relay import coordinator

            coordinator.register_flow(
                state=state,
                flow_kind="mcp",
                owner=self._flow_id,
                claim_callback=resolve_oauth_flow,
            )
        except Exception as exc:
            discard_oauth_state(state)
            self._future = None
            safe_code = getattr(exc, "code", "relay_registration_rejected")
            fail_oauth_trigger_flow(self._flow_id, str(safe_code))
            raise
        publish_oauth_auth_url(self._flow_id, url)

    async def callback_handler(self) -> tuple:
        """Await the ``(code, state)`` delivered by the public callback route,
        bounded by OAUTH_CALLBACK_TIMEOUT, then free the registry entry. Adopts the
        cross-loop ``concurrent.futures.Future`` onto THIS (MCP) loop via
        ``asyncio.wrap_future``."""
        fut = self._future
        if fut is None:
            # No state was parsed from the authorize URL — fail fast rather than
            # hang; the SDK turns this into an OAuthFlowError → connect FAILED.
            raise RuntimeError("OAuth tunnel callback: no state to await")
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(fut, loop=loop),
                timeout=OAUTH_CALLBACK_TIMEOUT,
            )
        finally:
            if self._state:
                from osia.oauth_relay import coordinator

                coordinator.cancel_flow(state=self._state)
            discard_oauth_state(self._state or "")


def prepare_dcr_registration_for_redirect(
    server_id: str,
    redirect_uri: str = OAUTH_RELAY_REDIRECT_URI,
) -> str:
    """Prepare one connector's saved DCR metadata for the stable redirect.

    MCP 1.28 exposes no registration-management update API.  A saved DCR client
    that lacks the new URI is therefore invalidated narrowly by removing only
    that connector's ``client`` metadata; tokens and every other connector are
    preserved so the SDK can perform targeted DCR on the next interactive auth.
    """
    sid = str(server_id or "").strip()
    uri = str(redirect_uri or "").strip()
    if not sid or uri != OAUTH_RELAY_REDIRECT_URI:
        return "dcr_reconnect_required"
    blob = get_mcp_oauth(sid)
    if not isinstance(blob, dict):
        return "registration_missing"
    client = blob.get("client")
    if not isinstance(client, dict):
        return "registration_missing"
    redirects = client.get("redirect_uris")
    if isinstance(redirects, list) and uri in redirects:
        return "registration_reused"
    updated = dict(blob)
    updated.pop("client", None)
    set_mcp_oauth(sid, updated)
    return "registration_invalidated"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def _seed_static_client_info(
    server_id: str, static_client: dict, redirect_uris: list, scope: Optional[str]
) -> None:
    """WOR-100: persist a PRE-REGISTERED (non-DCR) OAuth client into
    ``oauth_tokens.json`` so :meth:`OsiaTokenStorage.get_client_info` returns it
    and the SDK SKIPS Dynamic Client Registration.

    The MCP SDK only registers a client when ``self.context.client_info`` is
    falsy (mcp 1.28 ``client/auth/oauth2.py``: Step 4 is guarded by
    ``if not self.context.client_info``). ``_initialize`` populates that field
    from ``storage.get_client_info()``, so pre-seeding the stored ``client`` blob
    makes the SDK reuse OUR ``client_id`` (and optional secret) instead of
    attempting DCR — which providers like Google reject with a 400.

    Touches ONLY the per-server ``client`` sub-blob; the SDK ``token`` and the
    user-entered ``static_client`` credential record (under ``static_client``)
    are preserved. Idempotent and AUTHORITATIVE: a changed ``client_id`` replaces
    the prior one. No-op without a non-empty ``client_id``."""
    cid = str(static_client.get("client_id") or "").strip()
    if not cid:
        return
    blob = get_mcp_oauth(server_id)
    if not isinstance(blob, dict):
        blob = {}
    ci = {
        "client_id": cid,
        "redirect_uris": [str(u) for u in redirect_uris],
        "token_endpoint_auth_method": str(
            static_client.get("token_endpoint_auth_method") or "none"
        ),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    sec = static_client.get("client_secret")
    if isinstance(sec, str) and sec:
        ci["client_secret"] = sec
    if scope:
        ci["scope"] = scope
    blob["client"] = ci
    set_mcp_oauth(server_id, blob)


# WOR-100: Google reuses a prior consent grant ("OSIA already has access") and
# re-issues a token with the OLD scopes when the authorize request omits
# ``prompt=consent`` — so broadening ``oauth_scopes`` alone does NOT take effect
# until a genuine re-consent. We therefore force ``prompt=consent`` (Google
# re-displays the consent screen and mints a token carrying the CURRENT scopes)
# and ``access_type=offline`` (ensures a refresh_token). Applied ONLY to non-DCR
# (``static_client``) connectors — i.e. Google Gmail / Drive — so DCR connectors
# (Notion / Linear / …) are untouched and their users are never re-prompted.
_FORCED_CONSENT_AUTH_PARAMS = (("prompt", "consent"), ("access_type", "offline"))


def _with_forced_consent(redirect_handler: Any) -> Any:
    """Wrap an SDK ``redirect_handler`` so the authorize URL it receives gains
    ``prompt=consent`` + ``access_type=offline`` before the browser opens / the
    URL is published.

    The MCP SDK (mcp 1.28 ``client/auth/oauth2.py`` ``_perform_authorization_code_grant``)
    builds the authorize URL from a FIXED ``auth_params`` dict and hands it to
    ``redirect_handler`` with NO extra-auth-param hook — so mutating the URL in
    the handler is the cleanest injection point AND stays off the SDK-internal
    surface (the handler is a public callback OSIA already supplies). Idempotent
    (overwrites only those two keys); preserves every existing query param —
    notably ``state``, which the tunnel handler parses back out of the URL. A
    URL-rewrite error never breaks the login (falls back to the unmodified URL)."""
    async def _wrapped(url: str) -> None:
        try:
            parts = urlsplit(url)
            q = dict(parse_qsl(parts.query, keep_blank_values=True))
            for _k, _v in _FORCED_CONSENT_AUTH_PARAMS:
                q[_k] = _v
            url = urlunsplit(parts._replace(query=urlencode(q)))
        except Exception:
            pass  # never let URL rewriting break the login
        await redirect_handler(url)

    return _wrapped


def build_oauth_provider(
    server_id: str,
    server_url: str,
    callback: Any,
    *,
    redirect_uris: Optional[list] = None,
    static_client: Optional[dict] = None,
    scope: Optional[str] = None,
) -> Any:
    """Return a configured ``OAuthClientProvider`` for ``server_url``.

    ``callback`` supplies the SDK's two hooks (``redirect_handler`` /
    ``callback_handler``); it is an :class:`OAuthCallbackServer` for the loopback
    PC flow or a :class:`TunnelOAuthCallback` for the HTTPS-tunnel flow — both
    expose the same interface, so this factory is transport-agnostic.

    ``redirect_uris`` selects the registered/used redirect URIs. Default
    (``None``) →
    ``[OAUTH_REDIRECT_URI, OAUTH_TUNNEL_REDIRECT_URI, OAUTH_LEGACY_TUNNEL_REDIRECT_URI]``:
    the loopback URI stays index 0, and the SDK uses ``redirect_uris[0]`` for
    BOTH the authorize request and the token exchange (mcp 1.28 ``oauth2.py``),
    so the existing PC loopback flow is byte-for-byte unchanged — while DCR
    (RFC 7591) now ALSO registers the app tunnel URI plus the legacy root URI
    for the migration overlap. The tunnel flow passes the app tunnel URI at
    index 0 (used) while loopback and legacy root stay registered.

    WOR-100 — ``static_client`` / ``scope`` (both default ``None`` → the DCR
    path above is byte-for-byte unchanged): a PRE-REGISTERED (non-DCR) connector
    such as Google Gmail/Drive supplies ``static_client``
    (``{client_id, client_secret?, token_endpoint_auth_method}``). The factory
    then (a) defaults ``redirect_uris`` to ``[OAUTH_REDIRECT_URI]`` ONLY — a
    pre-registered client must use exactly the ONE redirect URI registered with
    the provider (Google requires exact match and has no DCR to register extras),
    and (b) seeds the SDK client so DCR is skipped (see
    :func:`_seed_static_client_info`).

    ``scope`` sets ``OAuthClientMetadata.scope`` as a best-effort/forward-looking
    hint. NOTE (mcp 1.28): on the SDK's 401-driven flow this is OVERWRITTEN at its
    Step 3 — ``async_auth_flow`` sets ``client_metadata.scope`` from
    ``get_client_metadata_scopes(WWW-Authenticate, PRM, OASM)`` BEFORE building the
    authorize URL — so the value actually sent is discovery-derived, not this one.
    Honouring an explicit least-privilege scope therefore requires driving the
    flow WITHOUT relying on the server 401 (a WOR-100 Phase 2 concern). DCR
    connectors leave it ``None`` (unchanged behaviour).
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    if redirect_uris is None:
        # A pre-registered (non-DCR) client uses the single registered loopback
        # URI; a DCR client keeps the original loopback+tunnel pair.
        redirect_uris = (
            [OAUTH_REDIRECT_URI]
            if static_client is not None
            else [
                OAUTH_REDIRECT_URI,
                OAUTH_TUNNEL_REDIRECT_URI,
                OAUTH_LEGACY_TUNNEL_REDIRECT_URI,
            ]
        )

    metadata = OAuthClientMetadata(
        redirect_uris=list(redirect_uris),
        client_name=OAUTH_CLIENT_NAME,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope or None,
    )

    if static_client is not None:
        _seed_static_client_info(server_id, static_client, redirect_uris, scope)

    # WOR-100: a non-DCR (Google) connector forces a real re-consent so a
    # broadened ``oauth_scopes`` actually takes effect on the next login (see
    # _with_forced_consent). DCR connectors (static_client is None) keep the raw
    # handler — byte-for-byte unchanged, no prompt=consent forced on them.
    redirect_handler = callback.redirect_handler
    if static_client is not None:
        redirect_handler = _with_forced_consent(redirect_handler)

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=OsiaTokenStorage(server_id),
        redirect_handler=redirect_handler,
        callback_handler=callback.callback_handler,
        timeout=OAUTH_FLOW_TIMEOUT,
    )


# How long to wait for the (post-redirect) token-exchange HTTP leg. The browser
# login itself is bounded separately by OAUTH_CALLBACK_TIMEOUT (the callback
# wait); this only guards the short authorization_code → token POST so a hung
# token endpoint can never park the connect indefinitely (the outer
# OAUTH_CONNECT_TIMEOUT still sits above both).
OAUTH_TOKEN_EXCHANGE_TIMEOUT = 30.0


def _stored_token_usable(server_id: str) -> bool:
    """True iff the stored OAuth token for ``server_id`` is present and NOT past
    its recorded absolute expiry — i.e. safe to reuse headlessly.

    The SDK's ``_initialize`` restores ``current_tokens`` but does NOT reconstruct
    ``token_expiry_time`` (and the token's own ``expires_in`` is relative to its
    long-gone issuance), so ``is_token_valid()`` would wrongly report a stale
    token as valid. We therefore consult OSIA's own absolute ``expires_at``
    bookkeeping (the same signal the reconnect sweeps use). This matters because a
    non-DCR server like Google 200s UNAUTHENTICATED on initialize/list_tools, so
    reusing a DEAD token would yield a false CONNECTED (the Phase-1 blocker) — it
    must instead trigger a fresh login.

    Trust-present semantics (matches reconnect._oauth_token_fresh, margin 0):
    present + future expiry → usable; present + NO numeric expiry recorded (some
    providers issue no ``expires_in``) → usable; present + PAST expiry → NOT
    usable. A read error → treat as usable (never gratuitously force a browser)."""
    try:
        blob = get_mcp_oauth(server_id)
    except Exception:
        return True
    if not isinstance(blob, dict) or not blob.get("token"):
        return False
    exp = blob.get("expires_at")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return True
    return float(exp) > time.time()


async def force_oauth_login(
    provider: Any,
    server_id: str,
    *,
    issuer: str,
    authorization_endpoint: str,
    token_endpoint: str,
) -> None:
    """WOR-100 Phase 2: FORCE the OAuth 2.1 authorization-code flow at CONNECT,
    WITHOUT relying on a server 401 — for a PRE-REGISTERED (non-DCR) connector
    such as Google Gmail / Drive.

    WHY this exists. The MCP SDK's ``OAuthClientProvider`` is an ``httpx.Auth``
    whose full flow fires ONLY on a 401 (mcp 1.28 ``client/auth/oauth2.py``
    ``async_auth_flow`` → ``if response.status_code == 401``). Google's MCP
    servers return 200 (unauthenticated) on ``initialize`` AND ``tools/list`` —
    they only 401 on a real tool CALL — so the SDK's auth never fires at connect:
    the handshake "succeeds" with NO token (a false CONNECTED), and a login would
    only trigger on the first tool call, bounded by the 25 s call wrapper and
    cancelled by ``manager.call_tool``'s timeout. This drives the SDK's OWN
    authorization-code grant DIRECTLY so the interactive login completes inside
    the connect window (manager's OAUTH_CONNECT_TIMEOUT=420 s) BEFORE status goes
    CONNECTED.

    Driving ``_perform_authorization`` directly (instead of through the 401 path)
    ALSO preserves least-privilege scope: the SDK overwrites
    ``client_metadata.scope`` with the full PRM/OASM scope set at its Step 3
    (``oauth2.py`` ``get_client_metadata_scopes``), but that line lives INSIDE the
    401 branch — never reached here — so the ``scope=`` supplied to
    :func:`build_oauth_provider` (the catalog's ``oauth_scopes``) is what lands in
    the authorize URL.

    ⚠️ SDK-INTERNAL. This calls "private" ``OAuthClientProvider`` methods
    (``_initialize`` / ``_perform_authorization`` / ``_handle_token_response``)
    and writes ``context.oauth_metadata`` — there is NO public on-demand-auth API
    in mcp 1.28. It is therefore PINNED to ``mcp==1.28.0`` and every internal
    access is guarded (``getattr`` + ``try/except``) so a future SDK rename
    degrades to a clear ``OAuthFlowError`` → connect FAILED, never a crash. Do NOT
    "refactor" this onto a public method without re-checking the SDK surface.

    Behaviour:
    * Loads the seeded ``client_info`` (so the SDK skips DCR) + any stored token
      via ``_initialize``.
    * HEADLESS RECONNECT: if a STILL-USABLE token is stored (present and not past
      its recorded expiry — see :func:`_stored_token_usable`), returns immediately
      — NO browser. The SDK then injects the Bearer for the live session.
    * STALE / FIRST CONNECT (no token, or a present-but-EXPIRED one — which would
      false-CONNECT against a server that 200s unauthenticated): seeds the AS
      metadata (Google's well-known endpoints — ``OAuthMetadata`` needs only
      issuer / authorization / token endpoints), runs the authorization-code grant
      (loopback + browser via the callback's
      ``redirect_handler``/``callback_handler``), sends the token-exchange POST
      itself (the 401 flow would ``yield`` it), then persists the token via
      ``OsiaTokenStorage``. (The automated reconnect sweeps already SKIP stale
      tokens, so the only path that reaches here with an expired token is an
      explicit manual re-tap, where a fresh login is the right response.)

    NO RFC 8707 resource param: ``protected_resource_metadata`` is left None and
    ``protocol_version`` stays None, so the SDK's ``should_include_resource_param``
    stays False — matching the verified manual Google flow.

    Raises (→ caller's ``_serve`` records STATUS_FAILED, never a false CONNECTED):
    ``OAuthFlowError`` — incompatible SDK, bad metadata, state mismatch, a non-200
    token exchange (message carries the HTTP status + error body, e.g. a 400
    ``invalid_client`` telling you the Desktop client needs its secret at the token
    endpoint), or a malformed 200 token response (re-raised token-free) — plus
    ``httpx`` errors or ``asyncio.TimeoutError`` (login never completed). A teardown
    ``asyncio.CancelledError`` (WOR-97 ``cancel_pending`` during a parked login)
    propagates cleanly so the connect lands DISCONNECTED.
    """
    # Lazy imports keep this module import-safe without the mcp SDK / httpx
    # (mirrors build_oauth_provider / revoke_oauth_token).
    import httpx
    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.shared.auth import OAuthMetadata

    ctx = getattr(provider, "context", None)
    _initialize = getattr(provider, "_initialize", None)
    _perform_authorization = getattr(provider, "_perform_authorization", None)
    _handle_token_response = getattr(provider, "_handle_token_response", None)
    if (
        ctx is None
        or not callable(_initialize)
        or not callable(_perform_authorization)
        or not callable(_handle_token_response)
    ):
        raise OAuthFlowError(
            "forced OAuth unavailable: incompatible mcp SDK "
            "(expected the 1.28 OAuthClientProvider internal API)"
        )

    # Load the seeded client_info (→ SDK skips DCR) + any stored token.
    await _initialize()

    # Headless reconnect: reuse a stored token WITHOUT a browser ONLY if it is
    # still usable. A present-but-EXPIRED token must NOT short-circuit — the
    # server 200s unauthenticated, so reusing a dead token would false-CONNECT
    # (the Phase-1 blocker). In that case drop it and fall through to a fresh
    # interactive login. (is_token_valid() can't be trusted post-load; see
    # _stored_token_usable.)
    if getattr(ctx, "current_tokens", None) is not None:
        if _stored_token_usable(server_id):
            return
        try:
            ctx.clear_tokens()
        except Exception:
            pass

    # First connect: seed the AS metadata so _perform_authorization targets the
    # right endpoints (the server 200s, so the SDK never discovers them).
    # Deliberately set ONLY oauth_metadata — NOT protected_resource_metadata — so
    # no RFC 8707 resource param is added.
    try:
        ctx.oauth_metadata = OAuthMetadata(
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
        )
    except Exception as e:  # noqa: BLE001 - bad/missing endpoints → clean FAILED
        raise OAuthFlowError(f"forced OAuth: invalid authorization-server metadata: {e}")

    # Drive the authorization-code grant. _perform_authorization opens the
    # loopback listener + browser (redirect_handler), awaits (code, state)
    # (callback_handler, bounded by OAUTH_CALLBACK_TIMEOUT), verifies state, and
    # BUILDS the token-exchange request — it does NOT send it (the 401 flow
    # normally ``yield``s it), so we send it ourselves.
    token_request = await _perform_authorization()

    async with httpx.AsyncClient(timeout=OAUTH_TOKEN_EXCHANGE_TIMEOUT) as client:
        token_response = await client.send(token_request)

    # Token-exchange result handling, kept token-SAFE for conn.error / stderr
    # (_sanitize_error is not OAuth-token-aware):
    #  * non-200 → the body is an OAuth ERROR (not a token), safe AND useful to
    #    surface verbatim — e.g. "invalid_client" means a Desktop client whose
    #    token endpoint needs its secret (switch oauth_token_endpoint_auth_method
    #    to "client_secret_post" + store the secret). Raise with a short excerpt.
    #  * 200-but-malformed → the SDK's pydantic parse error can embed the offending
    #    field's value, i.e. the access_token itself. Re-raise WITHOUT the original
    #    message so no secret leaks into the stored error / logs.
    status = getattr(token_response, "status_code", 0)
    if status != 200:
        try:
            body = (await token_response.aread()).decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        raise OAuthFlowError(
            f"forced OAuth: token exchange failed (HTTP {status}): {body}"
        )
    try:
        # Validates + persists via OsiaTokenStorage; sets context.current_tokens so
        # the live streamablehttp_client session sends a real Bearer.
        await _handle_token_response(token_response)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - token-safe: never echo the parse value
        raise OAuthFlowError(
            f"forced OAuth: invalid token response ({type(e).__name__})"
        )
