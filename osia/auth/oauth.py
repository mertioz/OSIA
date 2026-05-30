#!/usr/bin/env python3
"""OSIA OAuth — Claude Code-style PKCE login for Anthropic.

Only Anthropic is supported. OpenAI and Google have no public OAuth flow
that yields a token usable against their chat APIs, so they stay on
API-key auth (see osia/config.py).

The Anthropic flow reuses Claude Code's first-party client_id. Tokens are
exchanged at console.anthropic.com and work against api.anthropic.com as
``Authorization: Bearer <token>`` with the ``anthropic-beta:
oauth-2025-04-20`` header. The redirect URI is fixed
(console.anthropic.com/oauth/code/callback), so the GUI uses a
paste-the-code flow rather than a local HTTP server (this is what the
``code=true`` authorize-URL parameter requests).

Because the token is used directly for inference, we request only the
``user:*`` subscription scopes; ``org:create_api_key`` is the Console
"mint an API key" scope and must not be sent here, or claude.ai rejects
the authorize request as an invalid/unknown scope.

Tokens are saved to config/oauth_tokens.json with mode 0600.
"""

import base64
import hashlib
import json
import os
import secrets
import stat
import time
import webbrowser

import requests

from osia.config import CONFIG_DIR


OAUTH_TOKENS_FILE = os.path.join(CONFIG_DIR, "oauth_tokens.json")

# Public Claude Code OAuth client. Reusing the first-party client_id is
# how every FOSS Claude Code-compatible tool authenticates today; the
# resulting token works against api.anthropic.com via Bearer auth.
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
# Subscription/inference scopes only. We use the resulting token directly as
# a Bearer token against api.anthropic.com (see osia/agent.py), so we request
# the Claude Code subscription scopes — NOT ``org:create_api_key``, which is
# the Console-only "mint an API key" scope. Sending org:create_api_key on the
# claude.ai subscription authorize flow makes the server reject the request
# with "Invalid OAuth Request: Unknown scope: org:create_api_key".
ANTHROPIC_SCOPES = "user:inference user:profile"

PROVIDERS = ("anthropic",)


# ── PKCE primitives ──────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _new_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ── Token store ──────────────────────────────────────────────────────────

def _load_store() -> dict:
    if not os.path.exists(OAUTH_TOKENS_FILE):
        return {}
    try:
        with open(OAUTH_TOKENS_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_store(store: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(OAUTH_TOKENS_FILE, "w") as f:
        json.dump(store, f, indent=2)
    try:
        os.chmod(OAUTH_TOKENS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _save_provider_tokens(provider: str, tokens: dict) -> None:
    store = _load_store()
    store[provider] = tokens
    _save_store(store)


def _delete_provider_tokens(provider: str) -> None:
    store = _load_store()
    if provider in store:
        store.pop(provider)
        _save_store(store)


# ── Public API ───────────────────────────────────────────────────────────

class OAuthError(RuntimeError):
    pass


def start_oauth_flow(provider: str) -> dict:
    """Begin a PKCE login. Opens the provider's auth page in the browser
    and returns a session dict to be passed to :func:`complete_oauth_flow`.

    Returns: ``{"auth_url", "code_verifier", "state", "provider"}``.
    """
    if provider != "anthropic":
        raise OAuthError(f"OAuth not supported for provider: {provider}")

    verifier, challenge = _new_pkce_pair()
    # CSRF state — random, distinct from the PKCE verifier, matching the
    # 32-byte size Claude Code uses.
    state = _b64url(secrets.token_bytes(32))

    params = {
        "code": "true",
        "client_id": ANTHROPIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_REDIRECT_URI,
        "scope": ANTHROPIC_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    from urllib.parse import urlencode
    auth_url = f"{ANTHROPIC_AUTHORIZE_URL}?{urlencode(params)}"

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    return {
        "provider": provider,
        "auth_url": auth_url,
        "code_verifier": verifier,
        "state": state,
    }


def complete_oauth_flow(session: dict, code_input: str) -> dict:
    """Finish a PKCE login by exchanging the user-pasted code.

    ``code_input`` is what the user copied from the redirect page —
    typically ``<code>#<state>`` or just ``<code>``. We accept both.
    Returns the saved tokens dict on success, raises OAuthError otherwise.
    """
    provider = session.get("provider")
    if provider != "anthropic":
        raise OAuthError(f"OAuth not supported for provider: {provider}")

    raw = (code_input or "").strip()
    if not raw:
        raise OAuthError("Code vide")

    # Claude Code's redirect page renders the value as "<code>#<state>".
    if "#" in raw:
        code, returned_state = raw.split("#", 1)
    else:
        code, returned_state = raw, ""

    payload = {
        "code": code,
        "state": returned_state or session.get("state", ""),
        "grant_type": "authorization_code",
        "client_id": ANTHROPIC_CLIENT_ID,
        "redirect_uri": ANTHROPIC_REDIRECT_URI,
        "code_verifier": session["code_verifier"],
    }
    try:
        r = requests.post(
            ANTHROPIC_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
    except Exception as e:
        raise OAuthError(f"Réseau: {e}") from e

    if r.status_code != 200:
        raise OAuthError(
            f"Échange du code refusé (HTTP {r.status_code}): {r.text[:200]}"
        )

    data = r.json()
    access = data.get("access_token")
    if not access:
        raise OAuthError("Réponse sans access_token")

    expires_in = int(data.get("expires_in") or 0)
    tokens = {
        "access_token": access,
        "refresh_token": data.get("refresh_token", ""),
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + expires_in if expires_in else 0,
        "scope": data.get("scope", ANTHROPIC_SCOPES),
    }
    _save_provider_tokens(provider, tokens)
    return tokens


def _refresh(provider: str, tokens: dict) -> dict | None:
    refresh = tokens.get("refresh_token")
    if not refresh:
        return None
    try:
        r = requests.post(
            ANTHROPIC_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": ANTHROPIC_CLIENT_ID,
            },
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    access = data.get("access_token")
    if not access:
        return None
    expires_in = int(data.get("expires_in") or 0)
    new_tokens = {
        "access_token": access,
        "refresh_token": data.get("refresh_token", refresh),
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + expires_in if expires_in else 0,
        "scope": data.get("scope", tokens.get("scope", ANTHROPIC_SCOPES)),
    }
    _save_provider_tokens(provider, new_tokens)
    return new_tokens


def get_token(provider: str) -> str | None:
    """Return a valid access_token for the provider, refreshing if
    expired. Returns None if not connected or refresh failed."""
    tokens = _load_store().get(provider)
    if not tokens or not tokens.get("access_token"):
        return None
    exp = int(tokens.get("expires_at") or 0)
    # Refresh ~60s before expiry to avoid races.
    if exp and exp - 60 < int(time.time()):
        refreshed = _refresh(provider, tokens)
        if refreshed:
            return refreshed["access_token"]
        return None
    return tokens["access_token"]


def is_connected(provider: str) -> bool:
    return bool(_load_store().get(provider, {}).get("access_token"))


def revoke_token(provider: str) -> None:
    """Delete the saved token for a provider (local only)."""
    _delete_provider_tokens(provider)
