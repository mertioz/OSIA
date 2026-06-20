#!/usr/bin/env python3
"""OSIA MCP startup / on-demand reconnect — WOR-44.

ONE transport-agnostic routine, ``reconnect_enabled_connectors``, that reconnects
every MCP connector the user has marked ``enabled`` in ``settings.json`` —
reusing the EXACT same ``MCPManager.connect()`` path the Settings "Connect"
button and the mobile WS ``mcp_connect`` handler already drive. It adds NO new
connect logic: it only decides WHICH saved connectors to (re)connect at boot and
builds each one's headless connect cfg.

WHY this exists: ``enabled: true`` on an ``mcp_servers`` entry means "the user
connected this and hasn't disconnected it", but the ``MCPManager`` runtime status
is transient and starts EMPTY every process launch — so nothing reconnects a
previously-connected connector until the user reopens Settings and clicks
Connect. This routine closes that gap on BOTH the desktop GUI process and the
mobile-server process.

STANDALONE (like the rest of ``osia.mcp_client``): imports only ``osia.config``
and ``osia.mcp_client`` (NO Qt, NO ``osia.agent``, NO ``osia.server``), all
lazily, so the package stays import-safe without the ``mcp`` SDK and both
processes call this identically. Each process owns its OWN ``get_manager()``
singleton — this routine never crosses the process boundary.

SECRETS: ``build_headless_connect_cfg`` pulls a server's token from the
gitignored ``api_keys.json`` (``mcp:<id>``) into an IN-MEMORY ``env``/header for
the handshake only — never persisted, never logged. OAuth blobs live in
``oauth_tokens.json`` and are driven entirely by the SDK provider inside
``MCPManager._serve``; this module only READS the bookkeeping ``expires_at`` to
decide whether a SILENT reconnect is safe.

THREADING: ``MCPManager.connect()`` blocks (bounded) and is idempotent under the
manager's lock (an already-CONNECTING/CONNECTED id returns its existing status,
so a boot reconnect racing a manual Connect can't double-connect). Run this OFF
the Qt main thread / the server asyncio loop — a daemon/worker thread.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


# Default safety margin (seconds) for an OAuth SILENT reconnect: the stored
# access token must be valid for at least this long for a boot reconnect to be
# attempted. The MCP SDK does NOT do a refresh_token grant on a 401 at restart —
# it falls back to a fresh BROWSER login (see osia.mcp_client.oauth's
# OsiaTokenStorage note). So if the token has expired (or is about to) we SKIP
# the connector rather than risk popping a browser at startup. 60s absorbs clock
# skew plus the handshake's own network legs.
OAUTH_FRESH_MARGIN_S = 60.0


def build_headless_connect_cfg(saved: dict) -> dict:
    """Build the IN-MEMORY connect cfg for one saved server with NO Qt/UI input.

    A copy of the non-secret saved cfg with the per-server token (from the
    gitignored ``api_keys.json`` ``mcp:<id>`` store) injected into
    ``env[token_env]`` (stdio) or an ``Authorization: Bearer`` header
    (streamable_http), plus any non-secret ``extra_env`` values. The env/headers
    exist ONLY for the manager handshake — they are never persisted nor echoed in
    a frame. OAuth servers carry no injected secret here: the SDK provider inside
    ``MCPManager._serve`` drives the bearer from ``oauth_tokens.json``.

    This is the headless builder shared by the mobile-server ``mcp_connect`` WS
    handler (``osia.server.server._mcp_build_connect_cfg`` delegates here) and the
    boot auto-reconnect routine below — ONE source of truth, byte-for-byte the
    behaviour the server used before extraction.
    """
    cfg = dict(saved)
    sid = str(saved.get("id") or "")
    transport = str(saved.get("transport") or "stdio").strip().lower()
    env: dict = {}
    headers: dict = {}
    if saved.get("auth_type") == "token":
        try:
            from osia.config import get_mcp_token
            token = get_mcp_token(sid)
        except Exception:
            token = ""
        token_env = saved.get("token_env")
        if token and token_env:
            env[str(token_env)] = token
        # WOR-44 Phase 5: a remote (streamable-HTTP) server has no subprocess
        # env — its token rides an in-memory Authorization header instead. Built
        # here for the manager handshake ONLY; never persisted (the mcp_servers
        # write path strips any 'headers' key) nor echoed in a frame.
        if token and transport == "streamable_http":
            headers["Authorization"] = f"Bearer {token}"
    extra = saved.get("extra_env")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                env[k] = v
    cfg["env"] = env
    if headers:
        cfg["headers"] = headers
    return cfg


def _connectable(saved: dict) -> bool:
    """True iff the manager can connect this saved server now: a supported
    transport AND, for OAuth, a non-empty URL. Mirrors the GUI's / server's
    ``_mcp_connectable`` so a boot sweep never schedules an sse / url-less-OAuth
    server and leaves a noisy FAILED entry behind."""
    try:
        from osia.mcp_client import SUPPORTED_TRANSPORTS
    except Exception:
        SUPPORTED_TRANSPORTS = ("stdio",)
    transport = str(saved.get("transport") or "stdio").strip().lower()
    if transport not in SUPPORTED_TRANSPORTS:
        return False
    if saved.get("auth_type") == "oauth" and not str(saved.get("url") or "").strip():
        return False
    return True


def _oauth_token_fresh(
    server_id: str, margin_s: float, trust_present_token: bool = False
) -> bool:
    """True iff a stored OAuth blob for ``server_id`` has a token AND (strict
    path) its bookkeeping ``expires_at`` is comfortably (> ``margin_s``) in the
    future.

    Conservative by design on the BOOT-SWEEP path (``trust_present_token`` False,
    the default): a MISSING blob, a blob with no ``token``, or an ``expires_at``
    that is absent / non-numeric / not far enough ahead all return False — so the
    connector is SKIPPED rather than risk a boot-time browser. A future-dated
    token can still have been server-revoked (the SDK would then pop a browser on
    a 401); that residual case is the SDK's honest limitation and is accepted here
    — the bounded, off-thread connect keeps it from blocking boot.

    ``trust_present_token`` — set ONLY by the EXPLICIT direction-A mirror path
    (the ``/internal/mcp_sync`` connect, i.e. ``only_id`` is set). On that path the
    desktop JUST proved the token works (its ``connect()`` returned CONNECTED) and
    the server process has a hard ``webbrowser.open`` no-op guard, so the boot-
    browser risk the strict gate exists to avoid cannot apply. With it True the
    gate relaxes to: a present token with an ABSENT / non-numeric ``expires_at``
    (e.g. a provider like Linear that issues no ``expires_in``) is TRUSTED instead
    of skipped, and a present token with a numeric ``expires_at`` is trusted as
    long as it is not already PAST (margin 0 — the token was just minted on the
    PC). A present-but-EXPIRED token (numeric ``expires_at`` in the past) and an
    ABSENT token are STILL skipped, exactly as on the strict path. Summary:
    present+no-expiry ⇒ trust; present+future ⇒ trust; present+past ⇒ skip;
    absent token ⇒ skip.
    """
    try:
        from osia.config import get_mcp_oauth
        blob = get_mcp_oauth(server_id)
    except Exception:
        return False
    if not isinstance(blob, dict) or not blob.get("token"):
        return False  # absent token -> skip (both paths)
    exp = blob.get("expires_at")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        # No numeric expiry recorded (provider issued no expires_in). Strict
        # boot path SKIPS; the explicit-mirror path TRUSTS the just-proved token.
        return bool(trust_present_token)
    if trust_present_token:
        # Present + numeric expiry on the mirror path: trust unless genuinely PAST
        # (margin 0 — the desktop just minted/validated this token).
        return float(exp) > time.time()
    return float(exp) > time.time() + float(margin_s)


def reconnect_enabled_connectors(
    allow_oauth: bool = True,
    on_connected: Optional[Callable[[str, str], None]] = None,
    oauth_margin_s: float = OAUTH_FRESH_MARGIN_S,
    only_id: Optional[str] = None,
    trust_present_token: bool = False,
) -> list:
    """Reconnect every ``enabled`` MCP connector via ``MCPManager.connect()``.

    Parameters
    ----------
    allow_oauth:
        Per-process OAuth policy. ``True`` (desktop) attempts OAuth connectors
        whose stored token is still fresh; ``False`` (mobile server) SKIPS every
        OAuth connector outright — the browser login opens on the PC with nobody
        there.
    on_connected:
        Optional ``(server_id, status)`` callback invoked after each ATTEMPTED
        connector resolves (connected OR failed) — used by the server to
        broadcast a status frame. Never called for SKIPPED connectors. A raising
        callback is swallowed.
    oauth_margin_s:
        Freshness margin passed to the OAuth token check.
    only_id:
        When set, process ONLY the saved connector whose id matches — used by the
        desktop→server MCP state mirror (Phase 1, direction A) to reconcile a
        SINGLE connector the PC just connected. Every existing gate still applies
        (``enabled is True``, ``_connectable``, and ``_oauth_token_fresh`` for
        OAuth — so a genuinely EXPIRED or absent token is SKIPPED, never a
        browser). ``None`` (default) preserves the boot-sweep behaviour
        byte-for-byte: every enabled connector is considered.
    trust_present_token:
        OAuth freshness policy for the EXPLICIT mirror path ONLY (paired with
        ``only_id``). ``False`` (default — every boot-sweep caller) keeps the
        strict gate: an OAuth token with no numeric ``expires_at`` is SKIPPED. The
        ``/internal/mcp_sync`` connect passes ``True`` so a connector the desktop
        JUST connected (whose provider issued no ``expires_in``, e.g. Linear) is
        TRUSTED and reconnected headlessly instead of being skipped — the server's
        ``webbrowser`` no-op guard makes a browser pop impossible regardless. A
        present-but-EXPIRED token stays skipped on both paths. See
        ``_oauth_token_fresh``.

    Returns a list of per-connector result dicts
    ``{"id", "status", "reason"}`` (status is the ``connect()`` result, or
    ``"skipped"``). NEVER raises. MUST be called off the UI/event-loop thread.
    """
    results: list = []
    try:
        from osia.config import get_setting
        servers = get_setting("mcp_servers", []) or []
    except Exception:
        return results
    if not isinstance(servers, list):
        return results

    want_id = str(only_id).strip() if only_id is not None else None
    for saved in servers:
        if not isinstance(saved, dict) or saved.get("enabled") is not True:
            continue
        sid = str(saved.get("id") or "").strip()
        if not sid:
            continue
        if want_id is not None and sid != want_id:
            continue
        auth_type = str(saved.get("auth_type") or "").strip().lower()

        if not _connectable(saved):
            results.append({"id": sid, "status": "skipped",
                            "reason": "not connectable"})
            continue

        if auth_type == "oauth":
            if not allow_oauth:
                results.append({"id": sid, "status": "skipped",
                                "reason": "oauth disabled for this process"})
                continue
            if not _oauth_token_fresh(sid, oauth_margin_s, trust_present_token):
                # Leave the connector untouched in a quiet "needs reconnect"
                # state — NEVER pop a browser at startup.
                results.append({"id": sid, "status": "skipped",
                                "reason": "oauth token absent/expired"})
                continue

        cfg = build_headless_connect_cfg(saved)
        try:
            from osia.mcp_client import get_manager
            status = get_manager().connect(cfg)
        except Exception as e:  # noqa: BLE001 - connect() shouldn't raise, but be safe
            status = "failed"
            print(f"[MCP reconnect] {sid}: connect raised "
                  f"{type(e).__name__}: {e}")
        results.append({"id": sid, "status": status, "reason": ""})
        if on_connected is not None:
            try:
                on_connected(sid, status)
            except Exception:
                pass

    if results:
        attempted = [r for r in results if r["status"] != "skipped"]
        connected = [r for r in attempted if r["status"] == "connected"]
        print(f"[MCP reconnect] {len(connected)}/{len(attempted)} connector(s) "
              f"reconnected, {len(results) - len(attempted)} skipped")
    return results
