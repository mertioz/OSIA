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
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_endpoint": "https://openrouter.ai/api/v1/models/user",
    },
    "ollama": {
        "name": "Local (Ollama)",
        "base_url": "http://localhost:11434/v1",
        "models_endpoint": None,
    },
    "lmstudio": {
        "name": "LM Studio",
        "base_url": "http://localhost:1234/v1",
        "models_endpoint": None,
    },
}

# Providers that take an API key (ollama doesn't).
API_PROVIDERS = ("anthropic", "openai", "deepseek", "google", "xai", "groq", "mistral", "openrouter")
LOCAL_PROVIDERS = ("ollama", "lmstudio")

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
    # DeepSeek updates the served revision behind this stable API ID.
    {"id": "deepseek-flash", "label": "DeepSeek V4.1 Flash 1017", "provider": "deepseek",
     "tier": "light", "vision": True, "inputModalities": ["text", "image"], "context_window": 1_000_000,
     "tool_calls": True, "supportedReasoningEfforts": ["auto", "off", "low", "high", "max"]},
    # Google — all Gemini models are multimodal.
    {"id": "gemini-3.5-flash",        "label": "Gemini 3.5 Flash",      "provider": "google", "tier": "mid",   "vision": True, "lifecycle": "deprecated"},
    {"id": "gemini-3.1-pro-preview",  "label": "Gemini 3.1 Pro",        "provider": "google", "tier": "best",  "vision": True},
    {"id": "gemini-3-flash-preview",  "label": "Gemini 3 Flash",        "provider": "google", "tier": "mid",   "vision": True},
    {"id": "gemini-3.1-flash-lite",   "label": "Gemini 3.1 Flash Lite", "provider": "google", "tier": "light", "vision": True},
    {"id": "gemini-2.5-flash",        "label": "Gemini 2.5 Flash",      "provider": "google", "tier": "light", "vision": True, "lifecycle": "deprecated"},
    {"id": "gemini-2.5-flash-lite",   "label": "Gemini 2.5 Flash Lite", "provider": "google", "tier": "light", "vision": True},
    # xAI's May 2026 retirement removed the old Grok 3 API IDs.
    {"id": "grok-4.7", "label": "Grok 4.7", "provider": "xai", "tier": "best",
     "vision": True, "tool_calls": True, "api_protocol": "responses", "context_window": 500_000,
     "public_reasoning_text": True},
    # Retain the existing Mini contract if the key still lists it. It is
    # text-only, unlike the erroneous old generic Grok vision declaration.
    {"id": "grok-3-mini", "label": "Grok 3 Mini", "provider": "xai", "tier": "light",
     "vision": False, "tool_calls": True, "api_protocol": "chat_completions"},
    # Groq — the older Llamas remain available for eligible Enterprise accounts.
    {"id": "llama-3.3-70b-versatile",                   "label": "Llama 3.3 70B Versatile", "provider": "groq", "tier": "mid",   "vision": False, "discovery_only": True},
    {"id": "llama-3.1-8b-instant",                      "label": "Llama 3.1 8B Instant",    "provider": "groq", "tier": "light", "vision": False, "discovery_only": True},
    {"id": "openai/gpt-oss-120b",                       "label": "GPT-OSS 120B",            "provider": "groq", "tier": "best",  "vision": False},
    {"id": "openai/gpt-oss-20b",                        "label": "GPT-OSS 20B",             "provider": "groq", "tier": "light", "vision": False},
    # Mistral — OpenAI-compatible chat API (cf. DeepSeek/Groq). The Large/Medium/
    # Small lines are multimodal; Codestral is a text-only coding model.
    {"id": "mistral-large-2512",  "label": "Mistral Large 3",    "provider": "mistral", "tier": "best",  "vision": True, "lifecycle": "deprecated"},
    {"id": "mistral-medium-3-5",  "label": "Mistral Medium 3.5", "provider": "mistral", "tier": "mid",   "vision": True, "lifecycle": "deprecated"},
    {"id": "mistral-small-2603",  "label": "Mistral Small 4",    "provider": "mistral", "tier": "light", "vision": True, "tool_calls": True, "context_window": 256_000},
    {"id": "codestral-2508",      "label": "Codestral",          "provider": "mistral", "tier": "mid",   "vision": False},
]

# Documented contract fallback, verified 2026-11-04. These records
# describe capabilities; only discovery proves current account availability.
HARDCODED_MODELS.extend([
    {"id": mid, "label": label, "provider": "openai", "tier": tier,
     "vision": True, "tool_calls": True, "api_protocol": "responses", "context_window": 1_050_000}
    for mid, label, tier in (
        ("gpt-6-astra", "GPT-6 Astra", "best"), ("gpt-6.1-sol", "GPT-6.1 Sol", "best"),
        ("gpt-6.1-terra", "GPT-6.1 Terra", "mid"), ("gpt-6-luna", "GPT-6 Luna", "light"),
        ("gpt-6-sol", "GPT-6 Sol", "best"), ("gpt-5.6-sol", "GPT-5.6 Sol", "best"),
        ("gpt-5.6-terra", "GPT-5.6 Terra", "mid"), ("gpt-5.6-luna", "GPT-5.6 Luna", "light"),
    )
])
HARDCODED_MODELS.extend([
    {"id": mid, "label": label, "provider": "anthropic", "tier": tier,
     "vision": True, "tool_calls": True, "context_window": 1_000_000,
     "adaptive_thinking": True, "thinking_disabled": mid == "claude-haiku-5-5",
     "supportedReasoningEfforts": ["low", "medium", "high", "xhigh", "max"],
     "discovery_only": mid == "claude-mythos-5-1"}
    for mid, label, tier in (("claude-fable-5-1", "Claude Fable 5.1", "best"),
                             ("claude-opus-5-5", "Claude Opus 5.5", "best"),
                             ("claude-sonnet-5-5", "Claude Sonnet 5.5", "mid"),
                             ("claude-haiku-5-5", "Claude Haiku 5.5", "light"),
                             ("claude-mythos-5-1", "Claude Mythos 5.1", "best"))
])
HARDCODED_MODELS.extend([
    {"id": mid, "label": label, "provider": "google", "tier": tier,
     "vision": True, "tool_calls": True, "context_window": 1_048_576}
    for mid, label, tier in (("gemini-4-argon", "Gemini 4 Argon", "best"),
                             ("gemini-3.8-flash", "Gemini 3.8 Flash", "mid"),
                             ("gemini-3.6-flash", "Gemini 3.6 Flash", "mid"),
                             ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "light"))
])
HARDCODED_MODELS.extend([
    {"id": "grok-4.6", "label": "Grok 4.6", "provider": "xai", "tier": "best",
     "vision": True, "tool_calls": True, "api_protocol": "responses", "context_window": 500_000},
    {"id": "qwen/qwen3.8-27b", "label": "Qwen 3.8 27B", "provider": "groq",
     "tier": "mid", "vision": True, "tool_calls": True, "context_window": 131_072},
    {"id": "meta-models/muse-spark-open", "label": "Muse Spark Open", "provider": "groq",
     "tier": "best", "vision": True, "tool_calls": True, "context_window": 131_072, "discovery_only": True},
    {"id": "minimaxai/minimax-m2.7", "label": "MiniMax M2.7", "provider": "groq",
     "tier": "best", "vision": False, "tool_calls": True, "context_window": 196_608, "discovery_only": True},
    {"id": "mistral-large-4", "label": "Mistral Large 4", "provider": "mistral",
     "tier": "best", "vision": True, "tool_calls": True, "context_window": 1_000_000},
    {"id": "mistral-large-4-0", "label": "Mistral Large 4", "provider": "mistral",
     "tier": "best", "vision": True, "tool_calls": True, "context_window": 1_000_000, "discovery_only": True},
    {"id": "zai-glm-5-3", "label": "GLM 5.3", "provider": "mistral",
     "tier": "best", "vision": False, "tool_calls": True, "context_window": 1_000_000},
    {"id": "zai-glm-5-3-flash", "label": "GLM 5.3 Flash", "provider": "mistral",
     "tier": "mid", "vision": True, "tool_calls": True, "context_window": 1_000_000},
    *({"id": f"ministral-{size}b-2512", "label": f"Ministral 3 {size}B", "provider": "mistral",
       "tier": "light", "vision": True, "tool_calls": True, "context_window": 256_000}
      for size in (14, 8, 3)),
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


# ── Local model discovery ───────────────────────────────────────────────

# Keep slow runtime probes off the GUI thread and coalesce concurrent refreshes.
_LOCAL_CACHE: list | None = None
_LOCAL_CACHE_AT: float = 0.0
_LOCAL_TTL = 30.0  # seconds
_LOCAL_CACHE_LOCK = threading.Lock()
_LOCAL_CACHE_EPOCH = 0
_LOCAL_REFRESH_IN_FLIGHT = False
_LOCAL_REFRESH_PENDING = False
_LOCAL_REFRESH_CALLBACKS: list = []


def invalidate_local_models_cache(*, discard_snapshot: bool = False) -> None:
    """Force the next local-model lookup to query the PC runtime again."""
    global _LOCAL_CACHE, _LOCAL_CACHE_AT, _LOCAL_CACHE_EPOCH, _LOCAL_REFRESH_PENDING
    with _LOCAL_CACHE_LOCK:
        if discard_snapshot:
            _LOCAL_CACHE = None
        _LOCAL_CACHE_AT = 0.0
        _LOCAL_CACHE_EPOCH += 1
        if _LOCAL_REFRESH_IN_FLIGHT:
            _LOCAL_REFRESH_PENDING = True


def _query_ollama_models() -> list:
    from osia.local_model_discovery import query_ollama_models

    return query_ollama_models(endpoint_for_provider("ollama"))


def _query_lmstudio_models() -> list:
    from osia.local_model_discovery import query_lmstudio_models
    from osia.utils.key_chain import iter_keys

    key = next(iter(iter_keys("lmstudio")), None)
    headers = {"Authorization": f"Bearer {key}"} if key else None
    return query_lmstudio_models(endpoint_for_provider("lmstudio"), headers)


def _query_local_models() -> list:
    with perf_timer("models.get_local_models (runtime discovery)"):
        out = []
        for probe in (_query_ollama_models, _query_lmstudio_models):
            try:
                out.extend(probe())
            except Exception:
                continue
        return out


def cached_local_models() -> list:
    """Return the last known local-model snapshot without starting a process."""
    with _LOCAL_CACHE_LOCK:
        return list(_LOCAL_CACHE or [])


def _local_cache_fresh(now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    return _LOCAL_CACHE is not None and (now - _LOCAL_CACHE_AT) < _LOCAL_TTL


def _finish_local_refresh(out: list, epoch: int) -> None:
    global _LOCAL_CACHE, _LOCAL_CACHE_AT, _LOCAL_REFRESH_IN_FLIGHT
    global _LOCAL_REFRESH_PENDING

    callbacks = []
    follow_up = False
    with _LOCAL_CACHE_LOCK:
        if epoch == _LOCAL_CACHE_EPOCH:
            _LOCAL_CACHE = list(out)
            _LOCAL_CACHE_AT = time.monotonic()
            callbacks = list(_LOCAL_REFRESH_CALLBACKS)
            _LOCAL_REFRESH_CALLBACKS.clear()
        else:
            _LOCAL_REFRESH_PENDING = True
        _LOCAL_REFRESH_IN_FLIGHT = False
        if _LOCAL_REFRESH_PENDING:
            _LOCAL_REFRESH_PENDING = False
            follow_up = True
    for callback in callbacks:
        try:
            callback(list(out))
        except Exception:
            pass
    if follow_up:
        refresh_local_models_async()


def refresh_local_models_async(callback=None, *, force: bool = False):
    """Refresh local models on one daemon thread, coalescing concurrent callers."""
    global _LOCAL_CACHE_AT, _LOCAL_CACHE_EPOCH, _LOCAL_REFRESH_IN_FLIGHT
    global _LOCAL_REFRESH_PENDING

    with _LOCAL_CACHE_LOCK:
        if force:
            _LOCAL_CACHE_AT = 0.0
            _LOCAL_CACHE_EPOCH += 1
            if _LOCAL_REFRESH_IN_FLIGHT:
                _LOCAL_REFRESH_PENDING = True
        if _local_cache_fresh() and not force:
            return None
        if callback is not None and callback not in _LOCAL_REFRESH_CALLBACKS:
            _LOCAL_REFRESH_CALLBACKS.append(callback)
        if _LOCAL_REFRESH_IN_FLIGHT:
            return None
        _LOCAL_REFRESH_IN_FLIGHT = True
        epoch = _LOCAL_CACHE_EPOCH

    def worker():
        _finish_local_refresh(_query_local_models(), epoch)

    thread = threading.Thread(
        target=worker, name="osia-local-models", daemon=True
    )
    thread.start()
    return thread


def get_local_models() -> list:
    """List models from local runtimes (cached for ``_LOCAL_TTL`` seconds).

    Returns a fresh copy each call so callers may mutate the result without
    corrupting the cache.
    """
    global _LOCAL_REFRESH_IN_FLIGHT
    with _LOCAL_CACHE_LOCK:
        if _local_cache_fresh():
            return list(_LOCAL_CACHE or [])
        if _LOCAL_REFRESH_IN_FLIGHT:
            return list(_LOCAL_CACHE or [])
        _LOCAL_REFRESH_IN_FLIGHT = True
        epoch = _LOCAL_CACHE_EPOCH
    out = _query_local_models()
    _finish_local_refresh(out, epoch)
    return cached_local_models()


def get_ollama_models() -> list:
    """Compatibility inventory for callers that specifically use Ollama."""
    return [model for model in get_local_models() if model["provider"] == "ollama"]


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
    if provider in LOCAL_PROVIDERS:
        entry = next((item for item in cached_local_models()
                      if item["provider"] == provider and item["id"] == model_id), None)
        return copy.deepcopy(entry)
    with _DISCOVERY_LOCK:
        remote = _DISCOVERY.get(provider, {}).get("models", [])
        entry = next((m for m in remote if m["id"] == model_id), None)
        if entry is not None:
            return copy.deepcopy(entry)
    return _documented_model_metadata(provider, model_id)


def _documented_model_metadata(provider, model_id):
    known = next((dict(m) for m in HARDCODED_MODELS
                  if m["provider"] == provider and m["id"] == model_id), None)
    if known is None and provider == "openai":
        from osia.openai_model_contracts import DISCOVERY_CONTRACTS, SNAPSHOT_BASES
        known = copy.deepcopy(DISCOVERY_CONTRACTS.get(model_id))
        if known is None and model_id in SNAPSHOT_BASES:
            base = _documented_model_metadata(provider, SNAPSHOT_BASES[model_id])
            if base:
                known = {**base, "id": model_id, "reasoning_model_id": SNAPSHOT_BASES[model_id]}
    return known


def _supported(value):
    return value is True or (isinstance(value, dict) and value.get("supported") is True)


def _discovered_model(provider, item):
    mid = item.get("id") or item.get("name") or ""
    if not isinstance(mid, str) or not mid or any(c.isspace() for c in mid):
        return None
    if provider != "openrouter":
        mid = mid.removeprefix("models/")
    if item.get("active") is False or item.get("archived") is True or item.get("lifecycle") == "retired":
        return None
    known = _documented_model_metadata(provider, mid) or {}
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
    if provider == "openrouter":
        from osia.openrouter import model_capabilities

        known = model_capabilities(mid, item)
        if known is None:
            return None
    result = {**known, "id": mid, "provider": provider,
              "label": item.get("display_name") or item.get("displayName") or known.get("label") or mid,
              "tier": known.get("tier", "mid"), "source": "provider_discovery",
              "availability": "listed", "available": True, "streaming": known.get("streaming", True)}
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
    limit = (item.get("max_input_tokens") or item.get("inputTokenLimit")
             or item.get("context_window") or item.get("max_context_length")
             or item.get("context_length"))
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
    if provider == "openrouter":
        # The catalogue is public even with an invalid bearer token. Verify the
        # key separately so a disconnected account cannot publish usable models.
        key_status = requests.get(cfg["base_url"] + "/key", headers=headers, timeout=timeout)
        if key_status.status_code != 200:
            raise ModelDiscoveryError(key_status.status_code)
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
                          for m in HARDCODED_MODELS
                          if m["provider"] == provider and not m.get("discovery_only"))
    result.extend(local)
    return list({(m["provider"], m["id"]): m for m in result
                 if m["provider"] != "deepseek" or m["id"] == "deepseek-flash"}.values())


def get_all_models(api_keys=None, include_remote=False, remote_models=None):
    if include_remote:
        if api_keys is None:
            from osia.config import load_api_keys
            api_keys = load_api_keys()
        remote_models = discover_all_remote_models(api_keys)
    return _combined_models(get_local_models(), remote_models)


def get_all_models_cached(api_keys=None, remote_models=None):
    return _combined_models(cached_local_models(), remote_models)


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
        local = [model for model in get_ollama_models() if model.get("available", True)]
        if local:
            return local[0]["id"]
    else:
        local = [model for model in get_ollama_models() if model.get("available", True)]
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
    if provider == "ollama":
        from osia.local_model_discovery import ollama_loopback_endpoint

        default = ollama_loopback_endpoint(default)
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
    if (lower.startswith(("meta-llama/", "moonshotai/", "qwen/", "groq/", "playai/", "meta-models/", "minimaxai/"))
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
                         "open-mixtral", "zai-glm-")):
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


def _discovery_key_changed(provider):
    if provider is None or provider in LOCAL_PROVIDERS:
        invalidate_local_models_cache(discard_snapshot=True)
    if provider not in LOCAL_PROVIDERS:
        invalidate_remote_models(provider)


_listen_for_key_changes(_discovery_key_changed)

from osia.config import add_setting_change_listener as _listen_for_setting_changes

def _discovery_setting_changed(key):
    if key in {"api_key_fallbacks", "api_fallbacks"}:
        invalidate_local_models_cache(discard_snapshot=True)
    if key in {"api_key_fallbacks", "anthropic_workspace_id", "api_fallbacks"}:
        invalidate_remote_models()

_listen_for_setting_changes(_discovery_setting_changed)
