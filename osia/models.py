#!/usr/bin/env python3
"""OSIA model registry.

Defines providers, their hardcoded 2026 models, smart routing logic and
remote model discovery.
"""

import re
import subprocess
import threading

import requests

# ── Provider configurations ───────────────────────────────────────────────

PROVIDERS = {
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
        "models_endpoint": None,  # No public list endpoint — hardcoded only
    },
    "google": {
        "name": "Google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
    },
    "xai": {
        "name": "xAI",
        "base_url": "https://api.x.ai/v1",
        "models_endpoint": "https://api.x.ai/v1/models",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_endpoint": "https://api.groq.com/openai/v1/models",
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        # hardcoded only (cf. DeepSeek) — Mistral's /models also lists
        # embed/OCR/moderation models, which don't belong in the chat selector.
        "models_endpoint": None,
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
    # DeepSeek — text-only, no vision.
    {"id": "deepseek-v4-pro",     "label": "DeepSeek V4 Pro",     "provider": "deepseek",  "tier": "best",  "vision": False},
    {"id": "deepseek-v4-flash",   "label": "DeepSeek V4 Flash",   "provider": "deepseek",  "tier": "light", "vision": False},
    # Google — all Gemini models are multimodal.
    {"id": "gemini-3.5-flash",        "label": "Gemini 3.5 Flash",      "provider": "google", "tier": "mid",   "vision": True},
    {"id": "gemini-3.1-pro-preview",  "label": "Gemini 3.1 Pro",        "provider": "google", "tier": "best",  "vision": True},
    {"id": "gemini-3-flash-preview",  "label": "Gemini 3 Flash",        "provider": "google", "tier": "mid",   "vision": True},
    {"id": "gemini-3.1-flash-lite",   "label": "Gemini 3.1 Flash Lite", "provider": "google", "tier": "light", "vision": True},
    {"id": "gemini-2.5-flash",        "label": "Gemini 2.5 Flash",      "provider": "google", "tier": "light", "vision": True},  # GA; shutting down Oct 2026
    {"id": "gemini-2.5-flash-lite",   "label": "Gemini 2.5 Flash Lite", "provider": "google", "tier": "light", "vision": True},
    # xAI — Grok is vision-capable.
    {"id": "grok-3",              "label": "Grok 3",              "provider": "xai",       "tier": "mid",   "vision": True},
    {"id": "grok-3-mini",         "label": "Grok 3 Mini",         "provider": "xai",       "tier": "light", "vision": True},
    # Groq — ids must match Groq's /models API exactly (some are namespaced).
    # These Groq-hosted chat models are text-only (Llama 4 Scout vision lives in
    # VISION_MODELS below); pair them with a vision companion via WOR-25.
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

# Vision-capable models kept OUT of the chat selector — referenced internally
# for multimodal use (screenshot analysis, image understanding) only, and
# offered as a vision companion in settings (WOR-25). Llama 4 Scout is vision-
# capable but unreliable for tool calls, hence kept out of the chat selector.
VISION_MODELS = [
    {"id": "meta-llama/llama-4-scout-17b-16e-instruct", "label": "Llama 4 Scout 17B", "provider": "groq", "tier": "mid", "vision": True},
]

_VISION_ONLY_IDS = frozenset(m["id"] for m in VISION_MODELS)


# ── Vision capability (WOR-25) ─────────────────────────────────────────────

def model_has_vision(provider: str | None, model_id: str) -> bool:
    """Return True if ``(provider, model_id)`` can process images/screenshots.

    Resolution order:
      1. Exact ``(provider, id)`` match against the hardcoded registry
         (``HARDCODED_MODELS`` + the dedicated ``VISION_MODELS`` list).
      2. id-only match (covers a missing/forced provider).
      3. A name-based heuristic for ids not in the registry (e.g. remote-
         discovered models): Anthropic Claude, Google Gemini, xAI Grok and the
         OpenAI GPT-4o/4.1/5.x/o-series families are vision-capable; DeepSeek,
         Groq-hosted text models and Llama/Qwen chat models are not. Mistral
         vision support varies by model version (Large/Medium/Small are
         multimodal, Codestral is not), so it is taken from the registry above
         rather than guessed here; unknown Mistral ids default to non-vision.
    """
    if not model_id:
        return False
    # 1) exact provider + id, then 2) id-only.
    for require_provider in (True, False):
        for m in (*HARDCODED_MODELS, *VISION_MODELS):
            if m["id"] != model_id:
                continue
            if require_provider and provider and m["provider"] != provider:
                continue
            return bool(m.get("vision", False))
    # 3) heuristic for unknown (remote-discovered) ids. provider_for_model
    #    already maps claude/gemini/grok name fragments to anthropic/google/xai,
    #    so a provider-level check is sufficient and — unlike a bare name
    #    substring — never promotes a Groq-hosted text model that happens to
    #    contain "grok"/"gemini"/"claude".
    prov = (provider or provider_for_model(model_id) or "").lower()
    lid = model_id.lower()
    if prov in ("anthropic", "google", "xai"):
        return True
    if prov == "openai":
        return any(tag in lid for tag in
                   ("gpt-4o", "gpt-4.1", "gpt-5", "gpt-4-turbo", "o1", "o3", "o4"))
    return False


def vision_capable_models() -> list:
    """Return the models offerable as a vision companion (WOR-25).

    Every hardcoded model flagged ``vision: True`` plus the dedicated vision-
    only models (``VISION_MODELS``). Used by the settings UI to populate the
    "paired vision model" dropdown. Returns copies so callers can't mutate the
    registry.
    """
    out = [dict(m) for m in HARDCODED_MODELS if m.get("vision")]
    out.extend(dict(m) for m in VISION_MODELS)
    return out


# ── Local Ollama discovery ────────────────────────────────────────────────

def get_ollama_models() -> list:
    """List locally-installed Ollama models."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
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


def fetch_remote_models(provider: str, api_key: str, timeout: float = 5.0) -> list:
    """Query a provider's /models endpoint. Returns [] on any failure."""
    if not api_key:
        return []
    cfg = PROVIDERS.get(provider)
    if not cfg or not cfg.get("models_endpoint"):
        return []

    headers = {}
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        r = requests.get(cfg["models_endpoint"], headers=headers, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    items = data.get("data") or data.get("models") or []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name") or ""
        if not mid:
            continue
        # Google's API prefixes names with "models/" — strip it.
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        # Groq's endpoint mixes chat with non-chat models — keep chat only.
        if provider == "groq" and any(p in mid.lower() for p in GROQ_NON_CHAT_PATTERNS):
            continue
        # Vision-only models (see VISION_MODELS) never appear in the selector.
        if mid in _VISION_ONLY_IDS:
            continue
        out.append({
            "id": mid,
            "label": item.get("display_name") or mid,
            "provider": provider,
            "tier": "mid",
        })
    return out


def discover_all_remote_models(api_keys: dict, timeout: float = 5.0) -> list:
    """Run remote discovery for each provider with a key set. Returns merged list."""
    out = []
    for provider in API_PROVIDERS:
        key = api_keys.get(provider)
        if key:
            out.extend(fetch_remote_models(provider, key, timeout=timeout))
    return out


# ── Combined registry ─────────────────────────────────────────────────────

def get_all_models(api_keys: dict | None = None,
                   include_remote: bool = False,
                   remote_models: list | None = None) -> list:
    """Return combined model list: hardcoded + local Ollama + (optional) remote.

    Models are deduplicated by (provider, id). Pass ``remote_models`` if you
    already discovered them in a background thread.
    """
    if api_keys is None:
        from osia.config import load_api_keys
        api_keys = load_api_keys()

    models = list(HARDCODED_MODELS)
    models.extend(get_ollama_models())

    if remote_models:
        models.extend(remote_models)
    elif include_remote:
        models.extend(discover_all_remote_models(api_keys))

    seen = set()
    deduped = []
    for m in models:
        key = (m["provider"], m["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    return deduped


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
    ("deepseek-v4-pro",     "deepseek"),
]

SIMPLE_API_FALLBACK = [
    ("claude-haiku-4-5-20251001", "anthropic"),
    ("gpt-4.1-mini",              "openai"),
    ("deepseek-v4-flash",         "deepseek"),
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
