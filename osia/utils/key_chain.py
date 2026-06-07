#!/usr/bin/env python3
"""Universal API-key fallback chains with technical-error rotation.

OSIA lets users configure an ordered list of API keys per provider
(``api_key_fallbacks`` in settings): key[0] is the primary, the rest are
fallbacks tried in turn when a request fails with a *technical* error — a rate
limit (429), server error (5xx), auth error (401/403/408), request timeout or a
connection drop. A content/client error (400/404/422 …) never rotates, because
another key cannot fix a malformed request.

This module is the **single source of truth** for that behavior so every
provider call site rotates keys identically before falling back to a local
engine:

  * the main chat loop — ``OSIAAgent._provider_key_chain`` delegates to
    :func:`iter_keys` here (its rotation logic in ``run()`` is unchanged);
  * screenshot vision (``agent._describe_screenshot`` + ``vision_router``);
  * TTS — OpenAI and Gemini (``voice.tts`` and the mobile server);
  * STT — Groq Whisper (``voice.stt``).

It depends only on :mod:`osia.config` (never :mod:`osia.agent`) so any module can
import it without a circular import.
"""
from __future__ import annotations


class KeyChainEmpty(RuntimeError):
    """Raised when a provider has no configured API key at all — no primary and
    no fallbacks. Callers translate this into their own 'no key' handling
    (return False, raise a clear message, skip the cloud provider, …)."""

    def __init__(self, provider: str):
        super().__init__(f"{provider}: no API key configured")
        self.provider = provider


def iter_keys(provider: str) -> list:
    """Ordered list of API keys to try for ``provider``.

    The user's ``api_key_fallbacks`` list is authoritative when present
    (key[0] = primary, the rest = fallbacks); otherwise the single saved key
    from ``api_keys.json``. Ollama needs no key. Blank entries are dropped so an
    empty slot never shadows a real key. This is the canonical implementation
    that ``OSIAAgent._provider_key_chain`` delegates to — keep them identical.
    """
    if provider == "ollama":
        return ["ollama"]
    from osia.config import get_setting, get_api_key
    fallbacks = get_setting("api_key_fallbacks", {}) or {}
    chain = [k.strip() for k in (fallbacks.get(provider) or [])
             if isinstance(k, str) and k.strip()]
    if chain:
        return chain
    key = get_api_key(provider)
    return [key] if key else []


def has_key(provider: str) -> bool:
    """True when ``provider`` has at least one usable key (primary or fallback).

    Use this instead of ``bool(get_api_key(provider))`` for dispatch guards so a
    provider configured with *only* fallback keys still gets selected.
    """
    return bool(iter_keys(provider))


def mask_key(key: str) -> str:
    """Render a key for logs as its last 4 chars only — never the full secret."""
    if not key:
        return "????"
    return key[-4:] if len(key) >= 4 else "*" * (4 - len(key)) + key


def error_status(e) -> "int | None":
    """HTTP status from an OpenAI-SDK or ``requests`` exception, or ``None``.

    Mirrors ``OSIAAgent._error_status`` and additionally reads ``requests``
    ``HTTPError.response.status_code`` (raised by ``Response.raise_for_status``).
    """
    return (
        getattr(e, "status_code", None)
        or getattr(getattr(e, "response", None), "status_code", None)
    )


def is_technical_error(e, status=None) -> bool:
    """True for transient/technical failures worth rotating the key for: rate
    limits (429), server errors (5xx), auth errors (401/403), request timeout
    (408), timeouts and connection drops.

    Returns ``False`` for client/content errors (400/404/422 …) and anything
    that is not an API/transport failure. Identical semantics to
    ``OSIAAgent._is_technical_error`` so every call site classifies the same way.
    """
    if status is None:
        status = error_status(e)
    if isinstance(status, int):
        return status == 429 or status >= 500 or status in (401, 403, 408)
    # No HTTP status (timeouts, connection resets): classify by type/name.
    name = type(e).__name__.lower()
    if any(s in name for s in ("timeout", "connection", "serviceunavailable")):
        return True
    msg = str(e).lower()
    return any(
        s in msg
        for s in ("timed out", "timeout", "connection error",
                  "connection reset", "temporarily unavailable", "rate limit")
    )


def technical_error_reason(e, status=None) -> str:
    """Short label for the rotation log line ('429', '503', a type name…)."""
    if status is None:
        status = error_status(e)
    if isinstance(status, int):
        return str(status)
    return type(e).__name__


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def with_key_rotation(provider, call_fn, *, label=None, log=None):
    """Call ``call_fn(key)`` for each key in ``provider``'s chain, rotating to
    the next key when the call raises a *technical* error (429/5xx/auth/timeout)
    and returning the first successful result.

    A non-technical error (400/404/content) is re-raised immediately — another
    key will not fix it. When every key fails technically the *last* error is
    re-raised so the caller can fall back (e.g. to a local engine). Raises
    :class:`KeyChainEmpty` when the provider has no configured key.

    ``call_fn`` MUST contain only the network request — build the client /
    headers from ``key`` inside it — and keep decoding/playback OUTSIDE, so a
    local failure never burns API keys or triggers a pointless rotation.
    ``label`` tags the log lines (defaults to ``provider``); each rotation logs
    the failed key index + reason and the next key, masking all but the last 4
    characters of every key.
    """
    keys = iter_keys(provider)
    if not keys:
        raise KeyChainEmpty(provider)
    log = log or _default_log
    tag = label or provider
    total = len(keys)
    last_exc = None
    for i, key in enumerate(keys):
        try:
            return call_fn(key)
        except KeyChainEmpty:
            raise
        except Exception as e:  # classify, then rotate or re-raise
            status = error_status(e)
            if not is_technical_error(e, status):
                # Client/content error — a different key won't help; surface now.
                raise
            last_exc = e
            reason = technical_error_reason(e, status)
            if i + 1 < total:
                log(f"[key-chain {tag}] key{i + 1} (…{mask_key(key)}) failed "
                    f"[{reason}] — rotating to key{i + 2} "
                    f"(…{mask_key(keys[i + 1])})")
            else:
                log(f"[key-chain {tag}] key{i + 1} (…{mask_key(key)}) failed "
                    f"[{reason}] — chain exhausted "
                    f"({total} key{'s' if total != 1 else ''})")
    raise last_exc
