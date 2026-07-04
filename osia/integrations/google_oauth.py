#!/usr/bin/env python3
"""Google OAuth helpers for native REST integrations.

This module is deliberately separate from the MCP OAuth substrate. It reuses
OSIA's local ``oauth_tokens.json`` store and the same fixed Desktop loopback
redirect, but performs a plain Google Authorization Code + PKCE flow with
``requests`` and stdlib only. Token values and client secrets are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit

from osia.config import (
    get_oauth_client,
    get_oauth_token,
    set_oauth_client,
    set_oauth_token,
)


OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 8975
OAUTH_REDIRECT_PATH = "/callback"
OAUTH_REDIRECT_URI = (
    f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}{OAUTH_REDIRECT_PATH}"
)

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"

DEFAULT_TIMEOUT_S = 30.0
CALLBACK_TIMEOUT_S = 300.0
TOKEN_EXPIRY_MARGIN_S = 60.0

_CLIENT_FALLBACK_KEYS = {
    "gmail-rest": ("gmail",),
}

_ENV_CLIENT_KEYS = {
    "gmail-rest": (
        ("OSIA_GMAIL_REST_CLIENT_ID", "OSIA_GMAIL_REST_CLIENT_SECRET"),
        ("OSIA_GOOGLE_CLIENT_ID", "OSIA_GOOGLE_CLIENT_SECRET"),
    ),
}

_CLOSE_TAB_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>OSIA</title></head><body style='font-family:sans-serif;"
    "background:#15151a;color:#e8e8ea;text-align:center;padding-top:18vh'>"
    "<h2>OSIA connected</h2>"
    "<p>Authorization complete. You can close this tab and return to OSIA.</p>"
    "</body></html>"
)


class GoogleOAuthError(RuntimeError):
    """Token-safe OAuth failure."""


def _requests() -> Any:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise GoogleOAuthError(
            "Python package 'requests' is required for Google REST OAuth."
        ) from exc
    return requests


@dataclass(frozen=True)
class GoogleOAuthClient:
    client_id: str
    client_secret: str = ""
    token_endpoint_auth_method: str = "client_secret_post"
    source_key: str = ""

    def as_dict(self) -> dict:
        data = {
            "client_id": self.client_id,
            "redirect_uris": [OAUTH_REDIRECT_URI],
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        return data


class _LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _OAuthCallbackHandler)
        self.result: dict[str, str] = {}
        self.done = threading.Event()


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        split = urlsplit(self.path)
        if split.path != OAUTH_REDIRECT_PATH:
            self._write_response(404, b"Not found", "text/plain")
            return

        qs = parse_qs(split.query)
        result: dict[str, str] = {}
        for name in ("code", "state", "error", "error_description"):
            value = (qs.get(name) or [""])[0]
            if value:
                result[name] = value
        self.server.result = result
        self.server.done.set()

        if result.get("error"):
            body = (
                "<!doctype html><html><body><h2>OSIA authorization failed</h2>"
                "<p>You can close this tab and return to OSIA.</p></body></html>"
            ).encode("utf-8")
            self._write_response(400, body, "text/html; charset=utf-8")
            return
        self._write_response(200, _CLOSE_TAB_HTML.encode("utf-8"),
                             "text/html; charset=utf-8")

    def _write_response(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass


def load_client_config(integration_key: str) -> GoogleOAuthClient:
    """Load static Google Desktop client fields for an integration key.

    ``gmail-rest`` first checks its own local blob, then falls back to the
    legacy ``gmail`` MCP static client so Phase 0 can reuse the existing wizard
    fields without sharing OAuth tokens.
    """
    key = str(integration_key or "").strip()
    if not key:
        raise GoogleOAuthError("OAuth integration key is empty.")

    for source_key in (key, *(_CLIENT_FALLBACK_KEYS.get(key) or ())):
        client = get_oauth_client(source_key)
        loaded = _client_from_blob(client, source_key)
        if loaded:
            if source_key != key:
                set_oauth_client(key, loaded.as_dict())
            return loaded

    loaded = _client_from_environment(key)
    if loaded:
        set_oauth_client(key, loaded.as_dict())
        return loaded

    raise GoogleOAuthError(
        f"OAuth client not configured for {key}. Add a Google Desktop OAuth "
        "client to the local oauth_tokens.json store via the existing Google "
        "setup path, or set OSIA_GMAIL_REST_CLIENT_ID and "
        "OSIA_GMAIL_REST_CLIENT_SECRET before connecting."
    )


def authorize(
    integration_key: str,
    scopes: Iterable[str] | str,
    *,
    force_consent: bool = True,
) -> dict:
    """Run an interactive Google OAuth flow and persist the resulting token."""
    key = str(integration_key or "").strip()
    client = load_client_config(key)
    scope_text = _scope_string(scopes)
    if not scope_text:
        raise GoogleOAuthError("OAuth scope list is empty.")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_challenge(verifier)
    auth_params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": scope_text,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
    }
    if force_consent:
        auth_params["prompt"] = "consent"
    auth_url = f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(auth_params)}"

    code = _capture_authorization_code(auth_url, state)
    token = _exchange_authorization_code(client, code, verifier)
    blob = _store_token(key, token, client)
    return dict(blob)


def ensure_access_token(
    integration_key: str,
    scopes: Iterable[str] | str,
    *,
    interactive: bool = True,
) -> str:
    """Return a usable access token, refreshing or authorizing if needed."""
    key = str(integration_key or "").strip()
    required_scopes = _normalize_scopes(scopes)
    blob = get_oauth_token(key)
    token = blob.get("token") if isinstance(blob, dict) else None
    token = token if isinstance(token, dict) else {}

    access_token = token.get("access_token")
    if (
        isinstance(access_token, str)
        and access_token
        and _token_has_scopes(token, required_scopes)
        and not _token_expired(blob)
    ):
        return access_token

    refresh_token = token.get("refresh_token")
    if (
        isinstance(refresh_token, str)
        and refresh_token
        and _token_has_scopes(token, required_scopes)
    ):
        try:
            return refresh_access_token(key, required_scopes)
        except GoogleOAuthError:
            if not interactive:
                raise

    if interactive:
        blob = authorize(key, required_scopes, force_consent=True)
        token = blob.get("token") if isinstance(blob, dict) else None
        access_token = token.get("access_token") if isinstance(token, dict) else ""
        if isinstance(access_token, str) and access_token:
            return access_token

    raise GoogleOAuthError(
        f"OAuth token missing, expired, or missing required scopes for {key}."
    )


def refresh_access_token(
    integration_key: str,
    scopes: Iterable[str] | str | None = None,
) -> str:
    """Refresh and persist a Google access token using the stored refresh token."""
    key = str(integration_key or "").strip()
    blob = get_oauth_token(key)
    token = blob.get("token") if isinstance(blob, dict) else None
    token = token if isinstance(token, dict) else {}
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GoogleOAuthError(f"No refresh token stored for {key}.")

    required_scopes = _normalize_scopes(scopes or ())
    if required_scopes and not _token_has_scopes(token, required_scopes):
        raise GoogleOAuthError(f"Stored token for {key} is missing required scopes.")

    client = load_client_config(key)
    requests = _requests()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client.client_id,
    }
    if client.client_secret:
        data["client_secret"] = client.client_secret

    try:
        response = requests.post(
            GOOGLE_TOKEN_ENDPOINT, data=data, timeout=DEFAULT_TIMEOUT_S
        )
    except requests.RequestException as exc:
        raise GoogleOAuthError(
            f"OAuth refresh request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise GoogleOAuthError(
            "OAuth refresh failed: " + _oauth_error_summary(response)
        )

    refreshed = _parse_token_response(response)
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = refresh_token
    if "scope" not in refreshed and token.get("scope"):
        refreshed["scope"] = token["scope"]
    merged = dict(token)
    merged.update(refreshed)
    stored = _store_token(key, merged, client)
    stored_token = stored.get("token") if isinstance(stored, dict) else {}
    access_token = stored_token.get("access_token") if isinstance(stored_token, dict) else ""
    if not isinstance(access_token, str) or not access_token:
        raise GoogleOAuthError("OAuth refresh response did not include an access token.")
    return access_token


def revoke_token(integration_key: str) -> bool:
    """Best-effort Google OAuth token revocation for a native REST integration.

    Reads the stored refresh token first, falling back to the access token. Never
    raises and never logs token values; callers should still delete the local
    OAuth blob even when Google revocation is unavailable.
    """
    key = str(integration_key or "").strip()
    if not key:
        return False
    try:
        blob = get_oauth_token(key)
    except Exception:
        return False
    token_blob = blob.get("token") if isinstance(blob, dict) else None
    token_blob = token_blob if isinstance(token_blob, dict) else {}
    token = token_blob.get("refresh_token") or token_blob.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    try:
        requests = _requests()
        response = requests.post(
            GOOGLE_REVOCATION_ENDPOINT,
            data={"token": token},
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT_S,
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


def authorized_request(
    integration_key: str,
    method: str,
    url: str,
    scopes: Iterable[str] | str,
    *,
    interactive: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
    **kwargs: Any,
) -> Any:
    """Perform an authenticated REST request and retry once after a 401."""
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Accept", "application/json")
    token = ensure_access_token(integration_key, scopes, interactive=interactive)
    requests = _requests()
    headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method, url, headers=headers, timeout=timeout, **kwargs
        )
    except requests.RequestException as exc:
        raise GoogleOAuthError(
            f"Authenticated REST request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code != 401:
        return response

    try:
        token = refresh_access_token(integration_key, scopes)
    except GoogleOAuthError:
        if not interactive:
            return response
        token = ensure_access_token(integration_key, scopes, interactive=True)
    headers["Authorization"] = f"Bearer {token}"
    try:
        return requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise GoogleOAuthError(
            f"Authenticated REST retry failed: {type(exc).__name__}"
        ) from exc


def _client_from_blob(blob: dict, source_key: str) -> GoogleOAuthClient | None:
    if not isinstance(blob, dict):
        return None
    client_id = str(blob.get("client_id") or "").strip()
    if not client_id:
        return None
    client_secret = str(blob.get("client_secret") or "").strip()
    auth_method = str(
        blob.get("token_endpoint_auth_method")
        or ("client_secret_post" if client_secret else "none")
    ).strip() or "client_secret_post"
    return GoogleOAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint_auth_method=auth_method,
        source_key=source_key,
    )


def _client_from_environment(integration_key: str) -> GoogleOAuthClient | None:
    for client_id_key, client_secret_key in _ENV_CLIENT_KEYS.get(integration_key, ()):
        client_id = str(os.environ.get(client_id_key) or "").strip()
        if not client_id:
            continue
        client_secret = str(os.environ.get(client_secret_key) or "").strip()
        return GoogleOAuthClient(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint_auth_method="client_secret_post" if client_secret else "none",
            source_key="environment",
        )
    return None


def _capture_authorization_code(auth_url: str, expected_state: str) -> str:
    try:
        server = _LoopbackHTTPServer((OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT))
    except OSError as exc:
        raise GoogleOAuthError(
            f"OAuth callback port {OAUTH_CALLBACK_PORT} is unavailable."
        ) from exc

    thread = threading.Thread(
        target=server.serve_forever,
        name="osia-google-oauth-callback",
        daemon=True,
    )
    thread.start()
    try:
        if not webbrowser.open(auth_url):
            raise GoogleOAuthError("Could not open the browser for Google OAuth.")
        if not server.done.wait(CALLBACK_TIMEOUT_S):
            raise GoogleOAuthError("Timed out waiting for the Google OAuth callback.")
        result = dict(server.result)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        thread.join(timeout=5.0)

    if result.get("error"):
        error = result.get("error") or "unknown_error"
        description = result.get("error_description") or ""
        detail = f"{error}: {description}" if description else error
        raise GoogleOAuthError(f"Google OAuth authorization failed: {detail[:300]}")
    if result.get("state") != expected_state:
        raise GoogleOAuthError("Google OAuth state mismatch.")
    code = result.get("code")
    if not code:
        raise GoogleOAuthError("Google OAuth callback did not include a code.")
    return code


def _exchange_authorization_code(
    client: GoogleOAuthClient, code: str, verifier: str
) -> dict:
    requests = _requests()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "client_id": client.client_id,
        "code_verifier": verifier,
    }
    if client.client_secret:
        data["client_secret"] = client.client_secret
    try:
        response = requests.post(
            GOOGLE_TOKEN_ENDPOINT, data=data, timeout=DEFAULT_TIMEOUT_S
        )
    except requests.RequestException as exc:
        raise GoogleOAuthError(
            f"OAuth token request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise GoogleOAuthError(
            "OAuth token exchange failed: " + _oauth_error_summary(response)
        )
    return _parse_token_response(response)


def _parse_token_response(response: Any) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise GoogleOAuthError("OAuth token response was not JSON.") from exc
    if not isinstance(data, dict):
        raise GoogleOAuthError("OAuth token response had an invalid shape.")
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleOAuthError("OAuth token response did not include an access token.")
    return dict(data)


def _store_token(
    integration_key: str, token: dict, client: GoogleOAuthClient | None = None
) -> dict:
    blob = get_oauth_token(integration_key)
    if not isinstance(blob, dict):
        blob = {}
    blob["token"] = dict(token)
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        blob["expires_at"] = time.time() + float(expires_in)
    else:
        blob.pop("expires_at", None)
    if client is not None:
        blob["client"] = client.as_dict()
    set_oauth_token(integration_key, blob)
    return blob


def _token_expired(blob: dict) -> bool:
    if not isinstance(blob, dict):
        return True
    expires_at = blob.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return True
    return time.time() + TOKEN_EXPIRY_MARGIN_S >= float(expires_at)


def _token_has_scopes(token: dict, required_scopes: tuple[str, ...]) -> bool:
    if not required_scopes:
        return True
    stored_scope = token.get("scope")
    if not isinstance(stored_scope, str) or not stored_scope.strip():
        return False
    granted = set(stored_scope.split())
    return all(scope in granted for scope in required_scopes)


def _normalize_scopes(scopes: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(scopes, str):
        raw = scopes.split()
    else:
        raw = [str(scope or "") for scope in scopes]
    clean: list[str] = []
    seen: set[str] = set()
    for scope in raw:
        value = scope.strip()
        if not value or value in seen:
            continue
        clean.append(value)
        seen.add(value)
    return tuple(clean)


def _scope_string(scopes: Iterable[str] | str) -> str:
    return " ".join(_normalize_scopes(scopes))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _oauth_error_summary(response: Any) -> str:
    try:
        data = response.json()
    except ValueError:
        body = response.text[:300]
        return f"HTTP {response.status_code}: {body}"
    if not isinstance(data, dict):
        return f"HTTP {response.status_code}"
    error = str(data.get("error") or "").strip()
    description = str(data.get("error_description") or "").strip()
    if description:
        description = html.unescape(description)
    detail = ": ".join(part for part in (error, description) if part)
    return f"HTTP {response.status_code}: {detail[:300]}" if detail else (
        f"HTTP {response.status_code}"
    )
