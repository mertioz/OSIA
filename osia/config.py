#!/usr/bin/env python3
"""OSIA configuration — API keys and settings persistence.

API keys are stored at /home/OSIA/code/osia-project/config/api_keys.json
with file mode 0600 (owner read/write only).
Settings are stored at /home/OSIA/code/osia-project/config/settings.json.
"""

import json
import os
import stat

CONFIG_DIR = "/home/OSIA/code/osia-project/config"
API_KEYS_FILE = os.path.join(CONFIG_DIR, "api_keys.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "smart_routing": False,
    "selected_model": None,
    "tts_provider": "local",   # one of: "local", "openai", "google"
    "tts_voice": None,         # provider-specific voice id; None = provider default
    "language": "en",          # UI language; one of osia.i18n.LANGUAGES
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
