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
    "ollama": {
        "name": "Local (Ollama)",
        "base_url": "http://localhost:11434/v1",
        "models_endpoint": None,
    },
}

# Providers that take an API key (ollama doesn't).
API_PROVIDERS = ("anthropic", "openai", "deepseek", "google", "xai", "groq")

# ── Hardcoded models (2026) ───────────────────────────────────────────────

HARDCODED_MODELS = [
    # Anthropic
    {"id": "claude-opus-4-8",            "label": "Claude Opus 4.8",     "provider": "anthropic", "tier": "best"},
    {"id": "claude-opus-4-7",            "label": "Claude Opus 4.7",     "provider": "anthropic", "tier": "best"},
    {"id": "claude-opus-4-6",            "label": "Claude Opus 4.6",     "provider": "anthropic", "tier": "best"},
    {"id": "claude-sonnet-4-6",          "label": "Claude Sonnet 4.6",   "provider": "anthropic", "tier": "mid"},
    {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5",    "provider": "anthropic", "tier": "light"},
    # OpenAI
    {"id": "gpt-5.5",             "label": "GPT-5.5",             "provider": "openai",    "tier": "best"},
    {"id": "gpt-5.4",             "label": "GPT-5.4",             "provider": "openai",    "tier": "best"},
    {"id": "gpt-5.4-mini",        "label": "GPT-5.4 Mini",        "provider": "openai",    "tier": "mid"},
    {"id": "gpt-4.1",             "label": "GPT-4.1",             "provider": "openai",    "tier": "mid"},
    {"id": "gpt-4.1-mini",        "label": "GPT-4.1 Mini",        "provider": "openai",    "tier": "light"},
    {"id": "gpt-4.1-nano",        "label": "GPT-4.1 Nano",        "provider": "openai",    "tier": "light"},
    # DeepSeek
    {"id": "deepseek-v4-pro",     "label": "DeepSeek V4 Pro",     "provider": "deepseek",  "tier": "best"},
    {"id": "deepseek-v4-flash",   "label": "DeepSeek V4 Flash",   "provider": "deepseek",  "tier": "light"},
    # Google
    {"id": "gemini-3.1-pro",        "label": "Gemini 3.1 Pro",        "provider": "google", "tier": "best"},
    {"id": "gemini-2.5-pro",        "label": "Gemini 2.5 Pro",        "provider": "google", "tier": "mid"},
    {"id": "gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "provider": "google", "tier": "light"},
    {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "provider": "google", "tier": "light"},
    # xAI
    {"id": "grok-3",              "label": "Grok 3",              "provider": "xai",       "tier": "mid"},
    {"id": "grok-3-mini",         "label": "Grok 3 Mini",         "provider": "xai",       "tier": "light"},
    # Groq — ids must match Groq's /models API exactly (some are namespaced).
    {"id": "llama-3.3-70b-versatile",                   "label": "Llama 3.3 70B Versatile", "provider": "groq", "tier": "mid"},
    {"id": "llama-3.1-8b-instant",                      "label": "Llama 3.1 8B Instant",    "provider": "groq", "tier": "light"},
    {"id": "qwen/qwen3-32b",                            "label": "Qwen 3 32B",              "provider": "groq", "tier": "mid"},
    {"id": "openai/gpt-oss-120b",                       "label": "GPT-OSS 120B",            "provider": "groq", "tier": "best"},
    {"id": "openai/gpt-oss-20b",                        "label": "GPT-OSS 20B",             "provider": "groq", "tier": "light"},
    {"id": "moonshotai/kimi-k2-instruct",               "label": "Kimi K2",                 "provider": "groq", "tier": "best"},
]

# Vision-capable models kept OUT of the chat selector — referenced internally
# for future multimodal use (screenshot analysis, image understanding) only.
# Llama 4 Scout: vision-only, unreliable for tool calls.
VISION_MODELS = [
    {"id": "meta-llama/llama-4-scout-17b-16e-instruct", "label": "Llama 4 Scout 17B", "provider": "groq", "tier": "mid"},
]

_VISION_ONLY_IDS = frozenset(m["id"] for m in VISION_MODELS)


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
    if "gemini" in lower:
        return "google"
    if "grok" in lower:
        return "xai"
    return "ollama"
