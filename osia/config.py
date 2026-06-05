#!/usr/bin/env python3
"""OSIA configuration — API keys and settings persistence.

API keys are stored at /home/OSIA/code/osia-project/config/api_keys.json
with file mode 0600 (owner read/write only).
Settings are stored at /home/OSIA/code/osia-project/config/settings.json.
"""

import json
import os
import secrets
import stat

CONFIG_DIR = "/home/OSIA/code/osia-project/config"
API_KEYS_FILE = os.path.join(CONFIG_DIR, "api_keys.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
MOBILE_TOKEN_FILE = os.path.join(CONFIG_DIR, "mobile_token.txt")

DEFAULT_SETTINGS = {
    "selected_model": None,
    "tts_provider": "local",   # one of: "local", "openai", "google"
    "tts_voice": None,         # provider-specific voice id; None = provider default
    # Speech-to-text (Whisper). The active switching logic lands in Bloc 2;
    # for now the GUI only persists the user's choice.
    "stt_provider": "local",   # one of: "local" (faster-whisper), "groq"
    # Provider OSIA falls back to when the primary STT fails (error/no key/timeout).
    "stt_fallback_provider": "local",  # one of: "local", "groq", "none" (no fallback)
    "stt_local_model": "small",                 # faster-whisper size: "small", "medium"
    "stt_groq_model": "whisper-large-v3-turbo", # "whisper-large-v3", "whisper-large-v3-turbo"
    "language": "en",          # UI language; one of osia.i18n.LANGUAGES
    # AI task hierarchy (Bloc 3): ordered "provider:model" fallback chains per
    # task category. Empty chains = no hierarchy → the manually selected model
    # is used unchanged. See osia.task_router and OSIAAgent.run().
    "ai_hierarchy": {"heavy": [], "medium": []},
    # Master switch for the hierarchy: when False, OSIA never classifies/routes
    # and always uses the manually selected model (the chains above are kept so
    # they can still be configured). See OSIAAgent.run().
    "ai_hierarchy_enabled": False,
    # Live API voice-to-voice (Bloc 4): when enabled, OSIA streams audio to a
    # provider's realtime/live endpoint and bypasses the local STT (Whisper)
    # and TTS pipelines — the live endpoint handles both natively. The API key
    # is reused from the matching AI provider (no duplicate storage). See
    # osia.voice.live_api and osia.main.wake_mode.
    "liveapi_enabled": False,                  # master switch
    "liveapi_provider": "google",              # "google", "openai", "claude"
    "liveapi_model": "gemini-3.1-flash-live",  # provider-specific live model id
    "island_x": None,          # last dragged capsule position; None = top-centre
    "island_y": None,
}


def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_api_keys() -> dict:
    """Return the saved API-key dict, or {} if the file is missing/corrupt."""
    if not os.path.exists(API_KEYS_FILE):
        return {}
    try:
        with open(API_KEYS_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_api_keys(keys: dict) -> None:
    """Persist API keys to disk and chmod the file to 0600."""
    _ensure_config_dir()
    with open(API_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)
    try:
        os.chmod(API_KEYS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def get_api_key(provider: str) -> str:
    """Return the API key for a provider, or '' if unset."""
    return load_api_keys().get(provider, "") or ""


def set_api_key(provider: str, key: str) -> None:
    """Set or clear a provider's API key (empty string clears it)."""
    keys = load_api_keys()
    if key:
        keys[provider] = key
    else:
        keys.pop(provider, None)
    save_api_keys(keys)


def load_settings() -> dict:
    """Return the saved settings dict, merging in defaults."""
    base = dict(DEFAULT_SETTINGS)
    if not os.path.exists(SETTINGS_FILE):
        return base
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            base.update(data)
    except Exception:
        pass
    return base


def save_settings(settings: dict) -> None:
    _ensure_config_dir()
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def get_setting(key: str, default=None):
    return load_settings().get(key, default)


def set_setting(key: str, value) -> None:
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


# ── Mobile pairing token ───────────────────────────────────────────────────
# The mobile app authenticates against the token stored in
# ``config/mobile_token.txt`` (the same file the server reads on startup).
# The GUI can reveal it and regenerate it; regenerating invalidates the old
# token so the mobile app must re-pair.

def load_mobile_token() -> str:
    """Return the mobile pairing token, or '' if it hasn't been created yet."""
    try:
        with open(MOBILE_TOKEN_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def regenerate_mobile_token() -> str:
    """Generate a fresh 20-char mobile token, persist it (0600), and return it."""
    _ensure_config_dir()
    token = secrets.token_hex(10)  # 20 hex chars
    with open(MOBILE_TOKEN_FILE, "w") as f:
        f.write(token)
    try:
        os.chmod(MOBILE_TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return token
