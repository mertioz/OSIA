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
import time
import webbrowser
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from osia.config import get_mcp_oauth, set_mcp_oauth


# ── Fixed loopback callback + flow constants ────────────────────────────────
# RFC 8252 §7.3 recommends the literal loopback IP (127.0.0.1) for native-app
# redirect URIs. The port is a STABLE module constant (not ephemeral): Notion
# and some other providers reject dynamic redirect ports, and a fixed value is
# what gets registered via DCR / pre-registered with the provider.
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 8975
OAUTH_REDIRECT_PATH = "/callback"
OAUTH_REDIRECT_URI = f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}{OAUTH_REDIRECT_PATH}"
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
# Provider factory
# ---------------------------------------------------------------------------

def build_oauth_provider(
    server_id: str, server_url: str, callback: "OAuthCallbackServer"
) -> Any:
    """Return a configured ``OAuthClientProvider`` for ``server_url``.

    Scope is left unset: the SDK negotiates it from the server's protected-
    resource metadata / ``WWW-Authenticate`` header during the flow. The
    token-endpoint auth method is likewise left to DCR (the registered
    ``client_info`` dictates it). ``redirect_uris`` is the fixed loopback URI.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        redirect_uris=[OAUTH_REDIRECT_URI],
        client_name=OAUTH_CLIENT_NAME,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=OsiaTokenStorage(server_id),
        redirect_handler=callback.redirect_handler,
        callback_handler=callback.callback_handler,
        timeout=OAUTH_FLOW_TIMEOUT,
    )
