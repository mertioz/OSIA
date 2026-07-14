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

On top of cooldowns, a **per-task sticky key** avoids re-walking the chain from
key1 on every call inside one task (one agent run / one tool sequence). The first
key that succeeds is remembered as that provider's *active* key for the rest of
the task; subsequent calls start straight from it, and if it later fails they
advance FORWARD along the chain (active+1, wrapping) rather than restarting at
key1. This state lives in a :class:`contextvars.ContextVar` scoped by
:func:`task_scope`, so it never leaks across unrelated tasks and — crucially —
**when no task scope is active the behavior is byte-for-byte identical to before**
(every existing caller is unaffected until the run loop opts in). The sticky key
composes with cooldowns: it only chooses the *starting* point; cooling keys are
still skipped. See :func:`task_scope`.

It depends only on :mod:`osia.config` (never :mod:`osia.agent`) so any module can
import it without a circular import.
"""
from __future__ import annotations

import contextlib
import contextvars
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
_MAX_RETRY_AFTER = 5.0         # one bounded same-key retry for a short 429


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


def _live_indices(provider: str, chain: list, order=None):
    """Split ``chain``'s indices into ``(live, cooling)``.

    ``order`` is the sequence of indices to consider, in priority order; it
    defaults to natural chain order ``[0, 1, …]`` (so the result is identical to
    the pre-sticky behavior) and the per-task sticky key passes a rotated order
    so the active key — and the keys after it — come first. ``live`` are the
    ``order`` indices whose cooldown has lapsed (or never started), keeping
    ``order``'s sequence; ``cooling`` is ``[(index, seconds_left), …]`` for the
    parked ones. If *every* key is still cooling down they are all treated as
    live (``cooling`` returned empty) so a fully rate-limited provider is still
    attempted instead of failing without trying.
    """
    now = time.time()
    order = list(range(len(chain))) if order is None else list(order)
    live, cooling = [], []
    with _cooldowns_lock:
        for idx in order:
            until = _key_cooldowns.get((provider, idx), 0.0)
            if until > now:
                cooling.append((idx, until - now))
            else:
                live.append(idx)
    if not live:
        return order, []
    return live, cooling


def _park_key(provider: str, idx: int, status) -> None:
    """Start (or refresh) one key's cooldown after a technical failure."""
    with _cooldowns_lock:
        _key_cooldowns[(provider, idx)] = time.time() + _cooldown_seconds(status)


def _clear_key(provider: str, idx: int) -> None:
    """Drop a key's cooldown once it succeeds, so it is preferred again."""
    with _cooldowns_lock:
        _key_cooldowns.pop((provider, idx), None)


# ── Per-task sticky "active key" ───────────────────────────────────────────
# Once a key succeeds inside a task we remember it as that provider's *active*
# key and start subsequent calls FROM it, instead of re-testing key1, key2, …
# from scratch every call. The active key is the *starting point* only — cooling
# keys are still skipped (it composes with the cooldown cache above) — and on a
# technical failure we advance FORWARD along the chain (active+1, wrapping),
# never restarting at key1.
#
# The state is a ContextVar holding ``{provider: raw_chain_index}`` (the same
# stable index identity the cooldown cache uses). It is established/cleared by
# :func:`task_scope`, so it is naturally scoped to one task and isolated per
# thread / asyncio task. The default is ``None`` ("no task scope"): when no scope
# is active, :func:`_get_active` returns ``None``, ordering falls back to natural
# chain order, and :func:`_set_active` is a no-op — i.e. every existing caller
# behaves EXACTLY as before until the run loop opts in via ``task_scope()``.
_active_keys: "contextvars.ContextVar[dict[str, int] | None]" = contextvars.ContextVar(
    "osia_key_chain_active_keys", default=None
)


@contextlib.contextmanager
def task_scope():
    """Scope the per-task sticky "active key" to a single task.

    Wrap one task (one agent run / one tool sequence) in ``with task_scope():``
    so the keys that work during it are remembered and reused, then forgotten
    when it ends:

        with key_chain.task_scope():
            agent.run(user_msg)        # all provider calls share active keys here
        # scope cleared — the next task starts fresh from key1 (cooldowns persist)

    This is the intended integration point for ``OSIAAgent.run`` and the mobile
    server's request handler, but adopting it is OPTIONAL and additive: every
    call site keeps working unchanged, and outside any ``task_scope`` the chain
    behaves byte-for-byte as it did before stickiness existed. Scopes nest
    safely (a nested scope is its own fresh sub-task) and, being a ContextVar,
    each thread / asyncio task gets its own isolated state.
    """
    token = _active_keys.set({})
    try:
        yield
    finally:
        _active_keys.reset(token)


def _get_active(provider: str):
    """The current task's remembered raw-chain index for ``provider``, or
    ``None`` when no task scope is active or no key has succeeded yet."""
    state = _active_keys.get()
    return None if state is None else state.get(provider)


def _set_active(provider: str, idx: int) -> None:
    """Remember ``idx`` as ``provider``'s active key for the rest of this task.
    A no-op when no :func:`task_scope` is active (preserves legacy behavior)."""
    state = _active_keys.get()
    if state is not None:
        state[provider] = idx


def _ordered_indices(provider: str, chain: list) -> list:
    """Indices of ``chain`` in the order to try them for the current task.

    With a remembered active key, start from it and walk FORWARD, wrapping:
    ``[active, active+1, …, n-1, 0, …, active-1]`` — so a task that already found
    a working key never re-tests the keys before it. Without an active key (no
    task scope, or none recorded yet, or a stale out-of-range index after the
    configured chain shrank) this is natural order ``[0, 1, …, n-1]``.
    """
    n = len(chain)
    active = _get_active(provider)
    if active is None or not (0 <= active < n):
        return list(range(n))
    return [(active + off) % n for off in range(n)]


def _format_skipped(active_idx: int) -> str:
    """Render the keys jumped over by a sticky start as ``key1`` / ``key1-3``
    (1-based, the keys before ``active_idx``); ``""`` when nothing is skipped."""
    if active_idx <= 0:
        return ""
    if active_idx == 1:
        return "key1"
    return f"key1-{active_idx}"


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

    Inside a :func:`task_scope` the list is rotated to start from this provider's
    remembered active key (then forward, wrapping) so the caller's own rotation
    begins where the task last succeeded; outside any scope the order is the
    natural chain, identical to before. ``iter_keys`` only *consumes* the active
    key — it cannot observe which key the caller ends up using, so it never
    records one; that is :func:`with_key_rotation`'s job.
    """
    chain = _resolve_keys(provider)
    if not chain:
        return []
    live, _cooling = _live_indices(provider, chain, _ordered_indices(provider, chain))
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


def provider_failure_category(e, status=None) -> str:
    """Return a sanitized category without exposing provider payloads."""
    if status is None:
        status = error_status(e)
    if status in (401, 403):
        return "authentication"
    if status == 402:
        return "billing_restriction"
    if status == 429:
        body = getattr(e, "body", None)
        if not isinstance(body, dict):
            response = getattr(e, "response", None)
            try:
                candidate = response.json() if response is not None else None
            except Exception:
                candidate = None
            body = candidate if isinstance(candidate, dict) else {}

        def _safe_fields(value) -> list[str]:
            if not isinstance(value, dict):
                return []
            out = []
            for key, item in value.items():
                if key.lower() in {"code", "type", "status", "message", "reason"}:
                    out.append(str(item).lower())
                elif isinstance(item, dict):
                    out.extend(_safe_fields(item))
                elif isinstance(item, list):
                    for child in item:
                        if isinstance(child, dict):
                            out.extend(_safe_fields(child))
            return out

        detail = " ".join(_safe_fields(body))
        if any(marker in detail for marker in (
                "billing", "payment", "credit balance", "spending limit")):
            return "billing_restriction"
        if any(marker in detail for marker in (
                "insufficient_quota", "quota_exceeded", "quota exceeded",
                "exceeded your current quota", "usage limit")):
            return "quota_exhausted"
        return "transient_rate_limit"
    if isinstance(status, int) and status >= 500:
        return "provider_unavailable"
    if status in (400, 404, 422):
        return "content_or_model_error"
    if status == 408:
        return "transient_timeout"
    if status is None and is_technical_error(e, status):
        return "transient_transport"
    return f"http_{status}" if isinstance(status, int) else "non_technical"


def bounded_retry_after_seconds(e, *, maximum: float = _MAX_RETRY_AFTER):
    """Return a safe ``Retry-After`` delay, or ``None`` if absent/too long."""
    response = getattr(e, "response", None)
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(raw).strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if 0.0 <= seconds <= max(0.0, float(maximum)):
        return seconds
    return None


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def record_active_key(provider: str, key: str) -> None:
    """Publish ``key`` as ``provider``'s per-task active key, resolving it back to
    its raw-chain index so subsequent calls in this task start straight from it.

    This is the recording hook for callers that drive their OWN key rotation and
    therefore never reach :func:`with_key_rotation` (which records the winning key
    itself). The main chat loop is exactly such a caller: ``OSIAAgent.run`` selects
    and rotates keys via :func:`iter_keys` + ``_activate_next_api_key``, and
    :func:`iter_keys` can only *consume* the active key, never set one. After the
    chat completion succeeds the run loop calls this so the rest of the turn's
    provider calls (vision / TTS / STT via :func:`with_key_rotation`, and any
    :func:`iter_keys` consumer) start from the key that just worked.

    Resolves ``key`` against the *raw* chain (:func:`_resolve_keys`) so the
    recorded index is the same stable identity the cooldown cache and
    :func:`_set_active` use. It is a deliberate no-op — leaving behavior
    byte-for-byte unchanged — when any of the following hold:

      * no :func:`task_scope` is active (checked first, so the legacy path never
        even resolves the chain);
      * ``key`` is empty; or
      * ``key`` is not in the provider's configured chain (e.g. the ``"ollama"``
        placeholder, or a key edited out of config mid-session) — there is no
        stable index to pin, so stickiness simply stays off for it, as before.
    """
    if not key:
        return
    # Short-circuit before touching config: outside a task_scope there is no
    # active-key state to write, so the no-scope path stays allocation-free and
    # identical to pre-stickiness behavior.
    if _active_keys.get() is None:
        return
    chain = _resolve_keys(provider)
    try:
        idx = chain.index(key)
    except ValueError:
        return
    _set_active(provider, idx)


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

    Inside a :func:`task_scope` this is also where the per-task sticky key is
    *recorded*: the chain is walked starting from the provider's remembered
    active key (forward, wrapping — never restarting at key1), and the key that
    succeeds becomes the active key for the rest of the task. Outside any scope
    these steps are inert (start at key1, record nothing), so behavior is
    unchanged. The sticky key only picks the starting point — cooling keys are
    still skipped on top of it.
    """
    chain = _resolve_keys(provider)
    if not chain:
        raise KeyChainEmpty(provider)
    log = log or _default_log
    tag = label or provider
    active = _get_active(provider)
    live, cooling = _live_indices(provider, chain, _ordered_indices(provider, chain))
    # Sticky start: announce the remembered active key as the starting point, but
    # only when it really is the live start (``live[0]``). If the active key is
    # itself cooling it is skipped to the next live key, and the cooldown line
    # below explains that — so don't print a "starting from key{N}" line for a
    # key we immediately skip. (``live[0] == active`` also implies active is a
    # valid in-range index, so chain[active] below is safe.)
    if active is not None and active > 0 and live and live[0] == active:
        log(f"[key-chain {tag}] task: starting from active key{active + 1} "
            f"(…{mask_key(chain[active])}) — skipping {_format_skipped(active)}")
    for idx, left in cooling:
        log(f"[key-chain {tag}] skipping key{idx + 1} (…{mask_key(chain[idx])}) "
            f"— cooldown {int(left)}s left")
    attempts = len(live)
    last_exc = None

    def _call_with_bounded_retry_after(key):
        try:
            return call_fn(key)
        except Exception as exc:
            status = error_status(exc)
            category = provider_failure_category(exc, status)
            delay = bounded_retry_after_seconds(exc)
            if (status != 429
                    or category != "transient_rate_limit"
                    or delay is None):
                raise
            log(f"[key-chain {tag}] transient 429 - honoring bounded "
                f"Retry-After ({delay:.1f}s) once")
            if delay:
                time.sleep(delay)
            return call_fn(key)

    for pos, idx in enumerate(live):
        key = chain[idx]
        try:
            result = _call_with_bounded_retry_after(key)
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
                if active is not None and idx == active:
                    # The remembered active key just died — advance FORWARD along
                    # the chain (key+1, wrapping), never restarting at key1.
                    log(f"[key-chain {tag}] task: active key{idx + 1} "
                        f"(…{mask_key(key)}) failed [{reason}] — advancing to "
                        f"key{nxt + 1} (…{mask_key(chain[nxt])})")
                else:
                    log(f"[key-chain {tag}] key{idx + 1} (…{mask_key(key)}) failed "
                        f"[{reason}] — rotating to key{nxt + 1} "
                        f"(…{mask_key(chain[nxt])})")
            else:
                log(f"[key-chain {tag}] key{idx + 1} (…{mask_key(key)}) failed "
                    f"[{reason}] — chain exhausted "
                    f"({attempts} key{'s' if attempts != 1 else ''})")
        else:
            _clear_key(provider, idx)
            # Remember the winning key as this task's active key (no-op outside a
            # task_scope), so the next call in the task starts straight from it.
            _set_active(provider, idx)
            return result
    raise last_exc
