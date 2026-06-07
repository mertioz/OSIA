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

To avoid re-testing keys we already know are down, a key that fails technically
is parked in a short per-process *cooldown* (see :data:`_key_cooldowns`):
:func:`iter_keys` and :func:`with_key_rotation` skip cooling keys, so back-to-back
calls (e.g. three vision requests per desktop action) jump straight to a working
key instead of re-burning a failed round-trip on each dead one. Cooldowns are
best-effort and self-healing — if every key for a provider is cooling down they
are all retried anyway, so a fully rate-limited provider never fails unattempted.

It depends only on :mod:`osia.config` (never :mod:`osia.agent`) so any module can
import it without a circular import.
"""
from __future__ import annotations

import threading
import time


class KeyChainEmpty(RuntimeError):
    """Raised when a provider has no configured API key at all — no primary and
    no fallbacks. Callers translate this into their own 'no key' handling
    (return False, raise a clear message, skip the cloud provider, …)."""

    def __init__(self, provider: str):
        super().__init__(f"{provider}: no API key configured")
        self.provider = provider


# ── Per-process key cooldown cache ─────────────────────────────────────────
# When a key fails with a *technical* error we park it briefly so the next call
# stops re-testing a key we already know is down. Without this, every chain walk
# restarts at key[0] and pays a full failed round-trip per dead key before it
# reaches a working one — e.g. three vision calls per desktop action would each
# re-burn the same rate-limited keys. The cache is per-process and best-effort:
# cooldowns are short and self-healing, and if a whole provider is cooling down
# we try every key anyway rather than fail without a single request.
#
# Keyed by (provider, key_index) where key_index is the position in the *raw*
# chain from _resolve_keys() — a stable identity that does not shift as keys are
# hidden. Guarded by a lock because the agent and the mobile server may rotate
# the same provider concurrently.
_key_cooldowns: "dict[tuple[str, int], float]" = {}
_cooldowns_lock = threading.Lock()

# Cooldown length by failure class. A rate limit clears on its own fairly soon;
# a 5xx is usually a brief server blip; an auth failure (bad/expired key) will
# not fix itself, so park it long enough to stop hammering it this session.
# Other technical errors (408, timeout, dropped connection) are transient.
_COOLDOWN_RATE_LIMIT = 60.0    # 429
_COOLDOWN_SERVER = 30.0        # 5xx
_COOLDOWN_AUTH = 600.0         # 401 / 403
_COOLDOWN_TRANSIENT = 30.0     # 408 / timeout / connection drop


def _cooldown_seconds(status) -> float:
    """How long to park a key that just failed with ``status`` (an HTTP code, or
    ``None`` for a timeout/connection error). Mirrors the classes recognised by
    :func:`is_technical_error` — only technical failures ever reach here."""
    if status == 429:
        return _COOLDOWN_RATE_LIMIT
    if isinstance(status, int) and status >= 500:
        return _COOLDOWN_SERVER
    if status in (401, 403):
        return _COOLDOWN_AUTH
    return _COOLDOWN_TRANSIENT


def _live_indices(provider: str, chain: list):
    """Split ``chain``'s indices into ``(live, cooling)``.

    ``live`` are the indices whose cooldown has lapsed (or never started), in
    chain order; ``cooling`` is ``[(index, seconds_left), …]`` for the parked
    ones. If *every* key is still cooling down they are all treated as live
    (``cooling`` returned empty) so a fully rate-limited provider is still
    attempted instead of failing without trying.
    """
    now = time.time()
    live, cooling = [], []
    with _cooldowns_lock:
        for idx in range(len(chain)):
            until = _key_cooldowns.get((provider, idx), 0.0)
            if until > now:
                cooling.append((idx, until - now))
            else:
                live.append(idx)
    if not live:
        return list(range(len(chain))), []
    return live, cooling


def _park_key(provider: str, idx: int, status) -> None:
    """Start (or refresh) one key's cooldown after a technical failure."""
    with _cooldowns_lock:
        _key_cooldowns[(provider, idx)] = time.time() + _cooldown_seconds(status)


def _clear_key(provider: str, idx: int) -> None:
    """Drop a key's cooldown once it succeeds, so it is preferred again."""
    with _cooldowns_lock:
        _key_cooldowns.pop((provider, idx), None)


def _resolve_keys(provider: str) -> list:
    """Raw ordered key chain for ``provider`` — primary first, fallbacks after,
    with **no** cooldown filtering. This is the authoritative reading of the
    user's configuration; :func:`iter_keys` layers cooldown skipping on top. A
    key's *index* in this list is the stable identity used by the cooldown
    cache, so the order must depend only on config, never on runtime state.

    The user's ``api_key_fallbacks`` list is authoritative when present
    (key[0] = primary, the rest = fallbacks); otherwise the single saved key
    from ``api_keys.json``. Ollama needs no key. Blank entries are dropped so an
    empty slot never shadows a real key.
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


def iter_keys(provider: str) -> list:
    """Ordered list of API keys to try for ``provider``, **skipping keys still
    in cooldown**.

    Built on :func:`_resolve_keys` (the canonical config resolution that
    ``OSIAAgent._provider_key_chain`` delegates to — keep them identical), then
    a key that recently failed technically is hidden until its cooldown lapses
    so repeated calls stop re-testing dead keys. If every key is cooling down the
    full chain is returned anyway (never fail without trying), which also keeps
    :func:`has_key` and the single-key convenience callers correct.
    """
    chain = _resolve_keys(provider)
    if not chain:
        return []
    live, _cooling = _live_indices(provider, chain)
    return [chain[i] for i in live]


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

    Keys still in cooldown from a recent technical failure are skipped (and
    logged as such) before any request, so a known-down key is not re-tested;
    the failing key here is parked and a succeeding key has its cooldown cleared.
    If every key is cooling down they are all attempted anyway. Key numbers in
    the log (``key1`` …) are positions in the configured chain, stable across
    calls regardless of which keys are currently skipped.
    """
    chain = _resolve_keys(provider)
    if not chain:
        raise KeyChainEmpty(provider)
    log = log or _default_log
    tag = label or provider
    live, cooling = _live_indices(provider, chain)
    for idx, left in cooling:
        log(f"[key-chain {tag}] skipping key{idx + 1} (…{mask_key(chain[idx])}) "
            f"— cooldown {int(left)}s left")
    attempts = len(live)
    last_exc = None
    for pos, idx in enumerate(live):
        key = chain[idx]
        try:
            result = call_fn(key)
        except KeyChainEmpty:
            raise
        except Exception as e:  # classify, then rotate or re-raise
            status = error_status(e)
            if not is_technical_error(e, status):
                # Client/content error — a different key won't help; surface now.
                raise
            last_exc = e
            reason = technical_error_reason(e, status)
            _park_key(provider, idx, status)
            if pos + 1 < attempts:
                nxt = live[pos + 1]
                log(f"[key-chain {tag}] key{idx + 1} (…{mask_key(key)}) failed "
                    f"[{reason}] — rotating to key{nxt + 1} "
                    f"(…{mask_key(chain[nxt])})")
            else:
                log(f"[key-chain {tag}] key{idx + 1} (…{mask_key(key)}) failed "
                    f"[{reason}] — chain exhausted "
                    f"({attempts} key{'s' if attempts != 1 else ''})")
        else:
            _clear_key(provider, idx)
            return result
    raise last_exc
