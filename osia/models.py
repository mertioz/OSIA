#!/usr/bin/env python3
"""OSIA model registry.

PC-owned cloud discovery, documented capability fallbacks and local models.
"""

import re
import copy
import threading
import time

import requests

from osia.perf import perf_timer
from osia.utils.subprocess_policy import run_background_probe

# ── Provider configurations ───────────────────────────────────────────────

PROVIDERS = {
    # Logical cloud provider backed by official Codex managed authentication.
    # It is intentionally absent from API_PROVIDERS and API-key discovery.
    "chatgpt_account": {
        "name": "ChatGPT account",
        "base_url": "",
        "models_endpoint": None,
    },
    "anthropic": {
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models_endpoint": "https://api.anthropic.com/v1/models",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models_endpoint": "https://api.openai.com/v1/models",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models_endpoint": "https://api.deepseek.com/v1/models",
    },
    "google": {
        "name": "Google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
    },
    "xai": {
        "name": "Grok / xAI",
        "base_url": "https://api.x.ai/v1",
        "models_endpoint": "https://api.x.ai/v1/models",
    },
    "groq": {
        "name": "GroqCloud",
        "base_url": "https://api.groq.com/openai/v1",
        "models_endpoint": "https://api.groq.com/openai/v1/models",
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "models_endpoint": "https://api.mistral.ai/v1/models",
    },
    "ollama": {
        "name": "Local (Ollama)",
        "base_url": "http://localhost:11434/v1",
        "models_endpoint": None,
    },
}

# Providers that take an API key (ollama doesn't).
API_PROVIDERS = ("anthropic", "openai", "deepseek", "google", "xai", "groq", "mistral")

# ── Hardcoded models (2026) ───────────────────────────────────────────────

HARDCODED_MODELS = [
    # Anthropic — every Claude 3.x+ model accepts images.
    {"id": "claude-opus-4-8",            "label": "Claude Opus 4.8",     "provider": "anthropic", "tier": "best",  "vision": True},
    {"id": "claude-opus-4-7",            "label": "Claude Opus 4.7",     "provider": "anthropic", "tier": "best",  "vision": True},
    {"id": "claude-opus-4-6",            "label": "Claude Opus 4.6",     "provider": "anthropic", "tier": "best",  "vision": True},
    {"id": "claude-sonnet-4-6",          "label": "Claude Sonnet 4.6",   "provider": "anthropic", "tier": "mid",   "vision": True},
    {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5",    "provider": "anthropic", "tier": "light", "vision": True},
    # OpenAI — the GPT-4.1 and GPT-5 families are multimodal (GPT-4o lineage).
    {"id": "gpt-5.5",             "label": "GPT-5.5",             "provider": "openai",    "tier": "best",  "vision": True},
    {"id": "gpt-5.4",             "label": "GPT-5.4",             "provider": "openai",    "tier": "best",  "vision": True},
    {"id": "gpt-5.4-mini",        "label": "GPT-5.4 Mini",        "provider": "openai",    "tier": "mid",   "vision": True},
    {"id": "gpt-4.1",             "label": "GPT-4.1",             "provider": "openai",    "tier": "mid",   "vision": True},
    {"id": "gpt-4.1-mini",        "label": "GPT-4.1 Mini",        "provider": "openai",    "tier": "light", "vision": True},
    {"id": "gpt-4.1-nano",        "label": "GPT-4.1 Nano",        "provider": "openai",    "tier": "light", "vision": True},
    # DeepSeek — one native text/image model (official API, 2026-09-10).
    {"id": "deepseek-flash", "label": "DeepSeek V4.1 Flash", "provider": "deepseek",
     "tier": "light", "vision": True, "inputModalities": ["text", "image"],
     "tool_calls": True, "supportedReasoningEfforts": ["auto", "off", "low", "high", "max"]},
    # Google — all Gemini models are multimodal.
    {"id": "gemini-3.5-flash",        "label": "Gemini 3.5 Flash",      "provider": "google", "tier": "mid",   "vision": True},
    {"id": "gemini-3.1-pro-preview",  "label": "Gemini 3.1 Pro",        "provider": "google", "tier": "best",  "vision": True},
    {"id": "gemini-3-flash-preview",  "label": "Gemini 3 Flash",        "provider": "google", "tier": "mid",   "vision": True},
    {"id": "gemini-3.1-flash-lite",   "label": "Gemini 3.1 Flash Lite", "provider": "google", "tier": "light", "vision": True},
    {"id": "gemini-2.5-flash",        "label": "Gemini 2.5 Flash",      "provider": "google", "tier": "light", "vision": True},  # GA; shutting down Oct 2026
    {"id": "gemini-2.5-flash-lite",   "label": "Gemini 2.5 Flash Lite", "provider": "google", "tier": "light", "vision": True},
    # xAI's May 2026 retirement removed the old Grok 3 API IDs.
    {"id": "grok-4.7", "label": "Grok 4.7", "provider": "xai", "tier": "best",
     "vision": True, "tool_calls": True, "api_protocol": "responses", "context_window": 500_000,
     "public_reasoning_text": True},
    # Retain the existing Mini contract if the key still lists it. It is
    # text-only, unlike the erroneous old generic Grok vision declaration.
    {"id": "grok-3-mini", "label": "Grok 3 Mini", "provider": "xai", "tier": "light",
     "vision": False, "tool_calls": True, "api_protocol": "chat_completions"},
    # Groq — ids must match Groq's /models API exactly (some are namespaced).
    # These Groq-hosted chat models are text-only.
    {"id": "llama-3.3-70b-versatile",                   "label": "Llama 3.3 70B Versatile", "provider": "groq", "tier": "mid",   "vision": False},
    {"id": "llama-3.1-8b-instant",                      "label": "Llama 3.1 8B Instant",    "provider": "groq", "tier": "light", "vision": False},
    {"id": "qwen/qwen3-32b",                            "label": "Qwen 3 32B",              "provider": "groq", "tier": "mid",   "vision": False},
    {"id": "openai/gpt-oss-120b",                       "label": "GPT-OSS 120B",            "provider": "groq", "tier": "best",  "vision": False},
    {"id": "openai/gpt-oss-20b",                        "label": "GPT-OSS 20B",             "provider": "groq", "tier": "light", "vision": False},
    {"id": "moonshotai/kimi-k2-instruct",               "label": "Kimi K2",                 "provider": "groq", "tier": "best",  "vision": False},
    # Mistral — OpenAI-compatible chat API (cf. DeepSeek/Groq). The Large/Medium/
    # Small lines are multimodal; Codestral is a text-only coding model.
    {"id": "mistral-large-2512",  "label": "Mistral Large 3",    "provider": "mistral", "tier": "best",  "vision": True},
    {"id": "mistral-medium-3-5",  "label": "Mistral Medium 3.5", "provider": "mistral", "tier": "mid",   "vision": True},
    {"id": "mistral-small-2603",  "label": "Mistral Small 4",    "provider": "mistral", "tier": "light", "vision": True},
    {"id": "codestral-2508",      "label": "Codestral",          "provider": "mistral", "tier": "mid",   "vision": False},
]

# Small documented contract fallback, verified 2026-10-20. These records
# describe capabilities; only discovery proves current account availability.
HARDCODED_MODELS.extend([
    {"id": mid, "label": label, "provider": "openai", "tier": "best",
     "vision": True, "tool_calls": True, "api_protocol": "responses", "context_window": 1_050_000}
    for mid, label in (
        ("gpt-6.1-sol", "GPT-6.1 Sol"), ("gpt-6-astra", "GPT-6 Astra"),
        ("gpt-6-sol", "GPT-6 Sol"), ("gpt-6-luna", "GPT-6 Luna"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"), ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
    )
])
HARDCODED_MODELS.extend([
    {"id": mid, "label": label, "provider": "anthropic", "tier": "best",
     "vision": True, "tool_calls": True, "context_window": 1_000_000}
    for mid, label in (("claude-fable-5-1", "Claude Fable 5.1"),
                       ("claude-opus-5-5", "Claude Opus 5.5"),
                       ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                       ("claude-haiku-5-5", "Claude Haiku 5.5"))
])
HARDCODED_MODELS.extend([
    {"id": mid, "label": label, "provider": "google", "tier": "best",
     "vision": True, "tool_calls": True, "context_window": 1_048_576}
    for mid, label in (("gemini-4-argon", "Gemini 4 Argon"),
                       ("gemini-3.8-flash", "Gemini 3.8 Flash"))
])
HARDCODED_MODELS.extend([
    {"id": "qwen/qwen3.8-27b", "label": "Qwen 3.8 27B", "provider": "groq",
     "tier": "mid", "vision": True, "tool_calls": True, "context_window": 131_072},
    {"id": "mistral-large-4", "label": "Mistral Large 4", "provider": "mistral",
     "tier": "best", "vision": True, "tool_calls": True, "context_window": 1_000_000},
])

# ── Vision capability (WOR-25) ─────────────────────────────────────────────

def model_has_vision(provider: str | None, model_id: str) -> bool:
    """Return True if ``(provider, model_id)`` can process images/screenshots.

    Only exact authoritative registry metadata may return ``True``. Unknown or
    remote-discovered IDs fail closed instead of inferring capability from a
    provider name or model-name fragment.
    """
    metadata = model_metadata(str(provider or "").lower(), model_id)
    return bool(metadata and metadata.get("vision") is True)


# ── Local Ollama discovery ────────────────────────────────────────────────

# ``ollama list`` spawns a subprocess that is slow on Windows (~30-50ms, and up
# to the timeout if the ollama service is installed-but-hung). The GUI hits it on
# many main-thread paths (model panel grid, AI-hierarchy rows, provider_for_model
# → get_all_models, the settings _load_state pass), so a single settings open or
# tab switch used to fan out into a dozen subprocess spawns. Cache the result for
# a short TTL so those repeated calls are served from memory (WOR perf fix). The
# timeout is also lowered (10s → 2s) so an unresponsive ollama can never stall
# the UI for ten seconds.
_OLLAMA_CACHE: list | None = None
_OLLAMA_CACHE_AT: float = 0.0
_OLLAMA_TTL = 30.0  # seconds
_OLLAMA_CACHE_LOCK = threading.Lock()
_OLLAMA_CACHE_EPOCH = 0
_OLLAMA_REFRESH_IN_FLIGHT = False
_OLLAMA_REFRESH_PENDING = False
_OLLAMA_REFRESH_CALLBACKS: list = []


def invalidate_ollama_models_cache() -> None:
    """Force the next local-model lookup to query the PC runtime again."""
    global _OLLAMA_CACHE_AT, _OLLAMA_CACHE_EPOCH, _OLLAMA_REFRESH_PENDING
    with _OLLAMA_CACHE_LOCK:
        _OLLAMA_CACHE_AT = 0.0
        _OLLAMA_CACHE_EPOCH += 1
        if _OLLAMA_REFRESH_IN_FLIGHT:
            _OLLAMA_REFRESH_PENDING = True


def _query_ollama_models() -> list:
    with perf_timer("models.get_ollama_models (ollama list subprocess)"):
        try:
            result = run_background_probe(
                ["ollama", "list"], capture_output=True, text=True, timeout=2
            )
            out = []
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if parts:
                    mid = parts[0]
                    out.append({
                        "id": mid,
                        "label": mid,
                        "provider": "ollama",
                        "tier": "light",
                    })
            return out
        except Exception:
            return []


def cached_ollama_models() -> list:
    """Return the last known local-model snapshot without starting a process."""
    with _OLLAMA_CACHE_LOCK:
        return list(_OLLAMA_CACHE or [])


def _ollama_cache_fresh(now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    return _OLLAMA_CACHE is not None and (now - _OLLAMA_CACHE_AT) < _OLLAMA_TTL


def _finish_ollama_refresh(out: list, epoch: int) -> None:
    global _OLLAMA_CACHE, _OLLAMA_CACHE_AT, _OLLAMA_REFRESH_IN_FLIGHT
    global _OLLAMA_REFRESH_PENDING

    callbacks = []
    follow_up = False
    with _OLLAMA_CACHE_LOCK:
        if epoch == _OLLAMA_CACHE_EPOCH:
            _OLLAMA_CACHE = list(out)
            _OLLAMA_CACHE_AT = time.monotonic()
            callbacks = list(_OLLAMA_REFRESH_CALLBACKS)
            _OLLAMA_REFRESH_CALLBACKS.clear()
        else:
            _OLLAMA_REFRESH_PENDING = True
        _OLLAMA_REFRESH_IN_FLIGHT = False
        if _OLLAMA_REFRESH_PENDING:
            _OLLAMA_REFRESH_PENDING = False
            follow_up = True
    for callback in callbacks:
        try:
            callback(list(out))
        except Exception:
            pass
    if follow_up:
        refresh_ollama_models_async()


def refresh_ollama_models_async(callback=None, *, force: bool = False):
    """Refresh local models on one daemon thread, coalescing concurrent callers."""
    global _OLLAMA_CACHE_AT, _OLLAMA_CACHE_EPOCH, _OLLAMA_REFRESH_IN_FLIGHT
    global _OLLAMA_REFRESH_PENDING

    with _OLLAMA_CACHE_LOCK:
        if force:
            _OLLAMA_CACHE_AT = 0.0
            _OLLAMA_CACHE_EPOCH += 1
            if _OLLAMA_REFRESH_IN_FLIGHT:
                _OLLAMA_REFRESH_PENDING = True
        if _ollama_cache_fresh() and not force:
            return None
        if callback is not None and callback not in _OLLAMA_REFRESH_CALLBACKS:
            _OLLAMA_REFRESH_CALLBACKS.append(callback)
        if _OLLAMA_REFRESH_IN_FLIGHT:
            return None
        _OLLAMA_REFRESH_IN_FLIGHT = True
        epoch = _OLLAMA_CACHE_EPOCH

    def worker():
        _finish_ollama_refresh(_query_ollama_models(), epoch)

    thread = threading.Thread(
        target=worker, name="osia-ollama-models", daemon=True
    )
    thread.start()
    return thread


def get_ollama_models() -> list:
    """List locally-installed Ollama models (cached for ``_OLLAMA_TTL`` seconds).

    Returns a fresh copy each call so callers may mutate the result without
    corrupting the cache.
    """
    global _OLLAMA_REFRESH_IN_FLIGHT
    with _OLLAMA_CACHE_LOCK:
        if _ollama_cache_fresh():
            return list(_OLLAMA_CACHE or [])
        if _OLLAMA_REFRESH_IN_FLIGHT:
            return list(_OLLAMA_CACHE or [])
        _OLLAMA_REFRESH_IN_FLIGHT = True
        epoch = _OLLAMA_CACHE_EPOCH
    out = _query_ollama_models()
    _finish_ollama_refresh(out, epoch)
    return cached_ollama_models()


# ── Remote model discovery ────────────────────────────────────────────────

# Groq hosts STT/TTS/safety/routing models on the same /models endpoint —
# only chat models belong in the selector.
GROQ_NON_CHAT_PATTERNS = (
    "whisper",    # STT → Whisper Providers section
    "orpheus",    # TTS → Voice Providers section
    "playai",     # TTS
    "-tts",       # TTS
    "guard",      # safety/moderation (prompt-guard, llama-guard, gpt-oss-safeguard)
    "compound",   # Groq routing systems
)


# Discovery data is PC-owned, process-local, and contains no credentials.
_DISCOVERY_LOCK = threading.RLock()
_DISCOVERY: dict[str, dict] = {}
_DISCOVERY_INVALIDATED: set[str] = set()
_DISCOVERY_EPOCH = 0
_DISCOVERY_TTL = 900.0


class ModelDiscoveryError(RuntimeError):
    def __init__(self, status_code=None):
        super().__init__("model_discovery_failed")
        self.status_code = status_code


def invalidate_remote_models(provider=None):
    global _DISCOVERY_EPOCH
    with _DISCOVERY_LOCK:
        _DISCOVERY_EPOCH += 1
        _DISCOVERY_INVALIDATED.update((provider,) if provider else API_PROVIDERS)
        if provider:
            _DISCOVERY.pop(provider, None)
        else:
            _DISCOVERY.clear()


def discovery_status() -> dict:
    with _DISCOVERY_LOCK:
        return {provider: {"status": value["status"], "checked_at": value["checked_at"]}
                for provider, value in _DISCOVERY.items()}


def model_metadata(provider, model_id):
    with _DISCOVERY_LOCK:
        remote = _DISCOVERY.get(provider, {}).get("models", [])
        entry = next((m for m in remote if m["id"] == model_id), None)
        if entry is not None:
            return copy.deepcopy(entry)
    return next((dict(m) for m in HARDCODED_MODELS
                 if m["provider"] == provider and m["id"] == model_id), None)


def _supported(value):
    return value is True or (isinstance(value, dict) and value.get("supported") is True)


def _discovered_model(provider, item):
    mid = item.get("id") or item.get("name") or ""
    if not isinstance(mid, str) or not mid or any(c.isspace() for c in mid):
        return None
    mid = mid.removeprefix("models/")
    if item.get("active") is False or item.get("archived") is True or item.get("lifecycle") == "retired":
        return None
    known = next((m for m in HARDCODED_MODELS if m["provider"] == provider and m["id"] == mid), {})
    caps = item.get("capabilities") or {}
    if not isinstance(caps, dict):
        caps = {}
    if provider == "deepseek" and mid != "deepseek-flash":
        return None
    if provider == "xai" and mid in {
        "grok-3", "grok-4-0709", "grok-4-1-fast-reasoning", "grok-4-1-fast-non-reasoning",
        "grok-4-fast-reasoning", "grok-4-fast-non-reasoning", "grok-code-fast-1",
    }:
        return None
    if provider == "mistral" and not _supported(caps.get("completion_chat")):
        return None
    if provider == "google":
        if "generateContent" not in item.get("supportedGenerationMethods", []):
            return None
        if any(x in mid.lower() for x in ("embedding", "image", "tts", "live", "robotics")):
            return None
    if provider == "groq" and any(p in mid.lower() for p in GROQ_NON_CHAT_PATTERNS):
        return None
    # OpenAI / xAI list endpoints do not carry chat/endpoint capabilities.
    # An exact documented contract is required for tool-capable chat selection.
    if provider == "openai" and not known:
        return None
    if provider == "xai" and not known and not item.get("_language_model"):
        return None
    if provider == "anthropic" and caps.get("tool_use") is not None and not _supported(caps["tool_use"]):
        return None
    result = {**known, "id": mid, "provider": provider,
              "label": item.get("display_name") or item.get("displayName") or known.get("label") or mid,
              "tier": known.get("tier", "mid"), "source": "provider_discovery",
              "availability": "listed", "available": True, "streaming": True}
    if provider == "mistral":
        result.update(vision=_supported(caps.get("vision")), tool_calls=_supported(caps.get("function_calling")))
    elif provider == "xai":
        result["api_protocol"] = known.get("api_protocol", "responses")
        if item.get("_language_model"):
            modalities = item.get("input_modalities") or ["text"]
            result.update(inputModalities=modalities, vision="image" in modalities)
        efforts = caps.get("reasoning_effort")
        if isinstance(efforts, list):
            result["supportedReasoningEfforts"] = [level for level in efforts
                if level in {"none", "low", "medium", "high", "xhigh", "max"}]
    elif provider == "anthropic" and caps:
        if "image_input" in caps:
            result["vision"] = _supported(caps["image_input"])
        thinking = caps.get("thinking") or {}
        effort = caps.get("effort") or {}
        types = thinking.get("types") or {}
        if _supported(thinking) and _supported(types.get("adaptive")):
            result["adaptive_thinking"] = True
            result["thinking_disabled"] = _supported(types.get("disabled"))
            result["supportedReasoningEfforts"] = [k for k in ("low", "medium", "high", "xhigh", "max") if _supported(effort.get(k))]
    limit = item.get("max_input_tokens") or item.get("inputTokenLimit") or item.get("context_window") or item.get("max_context_length")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        result["context_window"] = limit
    if item.get("lifecycle") in {"active", "deprecated"}:
        result["lifecycle"] = item["lifecycle"]
    return result


def _fetch_remote_models(provider, api_key, timeout):
    cfg = PROVIDERS.get(provider, {})
    if not cfg.get("models_endpoint"):
        return []
    if provider == "anthropic":
        from osia.anthropic_api import request_headers
        headers = {**request_headers(api_key), "anthropic-version": "2023-06-01"}
    elif provider == "google":
        headers = {"x-goog-api-key": api_key}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
    items, cursor, seen = [], None, set()
    while True:
        params = {}
        if provider == "anthropic":
            params = {"limit": 1000, **({"after_id": cursor} if cursor else {})}
        elif provider == "google":
            params = {"pageSize": 1000, **({"pageToken": cursor} if cursor else {})}
        response = requests.get(cfg["models_endpoint"], headers=headers, timeout=timeout, **({"params": params} if params else {}))
        if response.status_code != 200:
            raise ModelDiscoveryError(response.status_code)
        data = response.json()
        if not isinstance(data, dict):
            raise ModelDiscoveryError()
        page = data.get("models", []) if provider == "google" else data.get("data", [])
        if not isinstance(page, list):
            raise ModelDiscoveryError()
        items.extend(page)
        next_cursor = (data.get("last_id") if data.get("has_more") is True else None) if provider == "anthropic" else data.get("nextPageToken") if provider == "google" else None
        if not next_cursor and not (provider == "anthropic" and data.get("has_more") is True):
            break
        if not page or not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            raise ModelDiscoveryError()
        seen.add(next_cursor)
        cursor = next_cursor
    out, identities = [], set()
    if provider == "xai":
        # The generic /models list proves identity; language-models supplies
        # modalities and effort metadata without mixing Imagine/Voice services.
        try:
            response = requests.get(cfg["base_url"] + "/language-models", headers=headers, timeout=timeout)
            language_models = response.json().get("models", []) if response.status_code == 200 else []
            language_by_id = {}
            for item in language_models:
                if not isinstance(item, dict) or "text" not in item.get("output_modalities", []):
                    continue
                for identity in [item.get("id"), *(item.get("aliases") or [])]:
                    language_by_id[identity] = item
            items = [{**language_by_id.get(item.get("id"), {}), **item,
                      "_language_model": item.get("id") in language_by_id}
                     for item in items if isinstance(item, dict)]
        except Exception:
            pass  # Exact documented contracts still work with /models alone.
    for item in items:
        model = _discovered_model(provider, item) if isinstance(item, dict) else None
        if model is not None and model["id"] not in identities:
            identities.add(model["id"])
            out.append(model)
    return out


def fetch_remote_models(provider: str, api_key: str, timeout: float = 5.0) -> list:
    """Compatibility entry point; failures never publish a partial catalogue."""
    if not api_key:
        return []
    try:
        return _fetch_remote_models(provider, api_key, timeout)
    except Exception:
        return []


def discover_all_remote_models(api_keys: dict, timeout: float = 5.0) -> list:
    from osia.utils.key_chain import iter_keys, is_technical_error

    out = []
    with _DISCOVERY_LOCK:
        epoch = _DISCOVERY_EPOCH
    for provider in API_PROVIDERS:
        # Shared key-chain owner also supports fallback-only configuration.
        chain = list(iter_keys(provider))
        if not chain and api_keys.get(provider):
            chain = [api_keys[provider]]
        if not chain:
            continue
        models, status = [], "error"
        for key in chain:
            try:
                models = _fetch_remote_models(provider, key, timeout)
                status = "ready" if models else "empty"
                break
            except Exception as exc:
                if not is_technical_error(exc):
                    break
        with _DISCOVERY_LOCK:
            if epoch != _DISCOVERY_EPOCH:
                continue  # Credentials changed while the request was in flight.
            _DISCOVERY[provider] = {"models": copy.deepcopy(models), "status": status, "checked_at": time.time()}
            _DISCOVERY_INVALIDATED.discard(provider)
        out.extend(models)
    return out


def _combined_models(local, remote_models=None):
    with _DISCOVERY_LOCK:
        states = copy.deepcopy(_DISCOVERY)
        invalidated = set(_DISCOVERY_INVALIDATED)
    provided = {}
    for item in remote_models or []:
        provided.setdefault(item["provider"], []).append(item)
    result = []
    for provider in API_PROVIDERS:
        state = states.get(provider)
        if state is not None:
            fresh = time.time() - state["checked_at"] < _DISCOVERY_TTL
            result.extend({**m, "available": fresh and state["status"] == "ready",
                           "availability": "listed" if fresh else "stale"} for m in state["models"])
        elif provider in provided and provider not in invalidated:
            result.extend(provided[provider])
        else:
            result.extend({**m, "source": "static_fallback", "availability": "unverified", "available": False}
                          for m in HARDCODED_MODELS if m["provider"] == provider)
    result.extend(local)
    return list({(m["provider"], m["id"]): m for m in result
                 if m["provider"] != "deepseek" or m["id"] == "deepseek-flash"}.values())


def get_all_models(api_keys=None, include_remote=False, remote_models=None):
    if include_remote:
        if api_keys is None:
            from osia.config import load_api_keys
            api_keys = load_api_keys()
        remote_models = discover_all_remote_models(api_keys)
    return _combined_models(get_ollama_models(), remote_models)


def get_all_models_cached(api_keys=None, remote_models=None):
    return _combined_models(cached_ollama_models(), remote_models)


def discover_models_async(callback, api_keys: dict | None = None) -> threading.Thread:
    """Run remote discovery in a daemon thread; invoke callback(remote_models)."""
    if api_keys is None:
        from osia.config import load_api_keys
        api_keys = load_api_keys()

    def _run():
        try:
            remote = discover_all_remote_models(api_keys)
        except Exception:
            remote = []
        try:
            callback(remote)
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── Smart routing ─────────────────────────────────────────────────────────

COMPLEX_KEYWORDS = (
    "code", "coding", "build", "create", "automate", "automation", "script",
    "analyze", "analysis", "implement", "develop", "design", "refactor",
    "debug", "program", "algorithm", "function",
)


def classify_complexity(prompt: str) -> str:
    """Return 'complex' or 'simple' based on prompt length and keywords."""
    if not prompt:
        return "simple"
    if len(prompt) > 200:
        return "complex"
    lower = prompt.lower()
    for kw in COMPLEX_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\w*\b", lower):
            return "complex"
    return "simple"


# Preference order for complex / simple tasks.
COMPLEX_PREFERENCE = [
    ("claude-opus-4-8",     "anthropic"),
    ("gpt-5.5",             "openai"),
    ("deepseek-flash",      "deepseek"),
]

SIMPLE_API_FALLBACK = [
    ("claude-haiku-4-5-20251001", "anthropic"),
    ("gpt-4.1-mini",              "openai"),
    ("deepseek-flash",            "deepseek"),
    ("gemini-2.5-flash",          "google"),
]


def select_model(prompt: str, fallback_model: str = "llama3") -> str:
    """Pick a model id based on prompt complexity + which API keys are set."""
    from osia.config import load_api_keys
    keys = load_api_keys()
    complexity = classify_complexity(prompt)

    if complexity == "complex":
        for model_id, provider in COMPLEX_PREFERENCE:
            if keys.get(provider):
                return model_id
        local = get_ollama_models()
        if local:
            return local[0]["id"]
    else:
        local = get_ollama_models()
        if local:
            return local[0]["id"]
        for model_id, provider in SIMPLE_API_FALLBACK:
            if keys.get(provider):
                return model_id

    return fallback_model


# ── API endpoint fallback (Bloc 5) ─────────────────────────────────────────
# Distinct from the AI Task Hierarchy (which swaps *models*): these helpers
# resolve which base_url a provider should talk to. The ``api_fallbacks``
# setting lets the user override a provider's primary endpoint and supply a
# secondary one that OSIA retries against when the primary connection
# fails. Shape: ``{<provider>: {"primary": <url>, "fallback": <url>}}``.

def endpoint_for_provider(provider: str) -> str:
    """Return the effective primary base_url for ``provider``.

    Uses the user's ``api_fallbacks[provider]["primary"]`` override when it is
    set and non-empty, otherwise the provider's built-in ``base_url``.
    """
    default = PROVIDERS.get(provider, {}).get("base_url", "")
    try:
        from osia.config import get_setting
        entry = (get_setting("api_fallbacks", {}) or {}).get(provider) or {}
        primary = (entry.get("primary") or "").strip()
        return primary or default
    except Exception:
        return default


def fallback_endpoint_for_provider(provider: str) -> str | None:
    """Return the configured fallback base_url for ``provider``, or ``None``.

    The fallback is used only when a request to the primary endpoint fails
    with a connection/timeout error (see ``OSIAAgent.run``).
    """
    try:
        from osia.config import get_setting
        entry = (get_setting("api_fallbacks", {}) or {}).get(provider) or {}
        fallback = (entry.get("fallback") or "").strip()
        return fallback or None
    except Exception:
        return None


def provider_for_model(model_id: str, models: list | None = None) -> str:
    """Return the provider for a given model id (heuristic fallback if unknown)."""
    if models is None:
        models = get_all_models()
    for m in models:
        if m["id"] == model_id:
            return m["provider"]
    lower = model_id.lower()
    if "claude" in lower:
        return "anthropic"
    # Groq-hosted models — the exact-match loop above resolves the hardcoded
    # entries; these patterns catch remote-discovered Groq ids. Checked before
    # the generic "gpt" match so "openai/gpt-oss-*" routes to Groq, not OpenAI.
    if (lower.startswith(("meta-llama/", "moonshotai/", "qwen/", "groq/", "playai/"))
            or "gpt-oss" in lower
            or "kimi" in lower
            or lower.endswith(("-versatile", "-instant"))):
        return "groq"
    if "gpt" in lower or lower.startswith("o1") or lower.startswith("o3"):
        return "openai"
    if "deepseek" in lower:
        return "deepseek"
    # Mistral families (mistral-*, codestral-*, pixtral-*, …) — mirrors the
    # DeepSeek case so non-hardcoded Mistral ids resolve to the right endpoint.
    if lower.startswith(("mistral", "codestral", "pixtral", "ministral",
                         "magistral", "devstral", "mixtral", "open-mistral",
                         "open-mixtral")):
        return "mistral"
    if "gemini" in lower:
        return "google"
    if "grok" in lower:
        return "xai"
    return "ollama"


def provider_for_model_cached(model_id: str) -> str:
    """Resolve a model for UI rendering without starting a runtime probe."""
    return provider_for_model(model_id, models=get_all_models_cached())


from osia.config import add_api_key_change_listener as _listen_for_key_changes
_listen_for_key_changes(invalidate_remote_models)

from osia.config import add_setting_change_listener as _listen_for_setting_changes

def _discovery_setting_changed(key):
    if key in {"api_key_fallbacks", "anthropic_workspace_id", "api_fallbacks"}:
        invalidate_remote_models()

_listen_for_setting_changes(_discovery_setting_changed)
