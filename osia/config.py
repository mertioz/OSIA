#!/usr/bin/env python3
"""OSIA configuration — API keys and settings persistence.

API keys are stored at ``~/.osia/api_keys.json`` with file mode 0600 (owner
read/write only). Settings are stored at ``~/.osia/settings.json``. The mobile
pairing token lives at ``~/.osia/mobile_token.txt``.

The config directory is resolved by ``_resolve_config_dir`` below: in dev/repo
mode — a cloned repo whose ``<repo>/config/settings.json`` exists — it is the
in-repo ``<repo>/config/`` dir; otherwise (installed app / Windows) it is the
per-user ``Path.home() / ".osia"`` dir, which works identically on Windows
(``C:\\Users\\<user>\\.osia``), Mac and Linux. See WOR-11 (Windows port).
"""

import json
import os
import secrets
import stat
import sys
from pathlib import Path

# <repo>/osia/config.py → <repo>. Dynamic so the package works from any
# install location / drive (no hardcoded /home/OSIA/code/... path).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_config_dir() -> str:
    """Resolve the config/data dir.

    Dev/repo mode: when the in-repo ``<repo>/config/settings.json`` exists
    (i.e. you cloned the repo and have a populated ``config/`` folder), use
    ``<repo>/config`` — even on Linux, where ``~/.osia`` may also exist to hold
    conversations/profiles. Otherwise (installed app / Windows) use the
    cross-platform per-user dir ``Path.home() / ".osia"`` (WOR-11).
    """
    repo_config = PROJECT_ROOT / "config"
    if (repo_config / "settings.json").exists():
        return str(repo_config)
    return str(Path.home() / ".osia")


# Config/data dir: <repo>/config in dev/repo mode, else ~/.osia (see above).
CONFIG_DIR = _resolve_config_dir()
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
    # Floating Dynamic Island capsule (WOR-22): when False the capsule window
    # is hidden entirely. The capsule's adaptive shape (capsule/circle/docked)
    # is driven by its on-screen position, persisted in island_shape /
    # island_dock_side / island_x / island_y. See osia.gui.dynamic_island.
    "show_capsule": True,
    # Clock display format for the wall-clock times OSIA shows (conversation
    # timestamps, cron daily schedules, heartbeat alert logs): "24h" → 14:30,
    # "12h" → 2:30 PM. See osia.timefmt.
    "time_format": "24h",      # one of: "24h", "12h"
    # AI task hierarchy (Bloc 3): ordered "provider:model" fallback chains per
    # task category. Empty chains = no hierarchy → the manually selected model
    # is used unchanged. See osia.task_router and OSIAAgent.run().
    "ai_hierarchy": {"heavy": [], "medium": []},
    # Master switch for the hierarchy: when False, OSIA never classifies/routes
    # and always uses the manually selected model (the chains above are kept so
    # they can still be configured). See OSIAAgent.run().
    "ai_hierarchy_enabled": False,
    # Vision model cooperation (WOR-25): when the selected main model has no
    # vision capability (e.g. DeepSeek text), OSIA pairs it with a vision-capable
    # companion (e.g. Gemini) for image/screenshot tasks. Empty = no companion
    # configured; the settings UI only surfaces these when the main model can't
    # see. See osia.vision_router and osia.models.model_has_vision.
    "vision_provider": "",
    "vision_model": "",
    # Live API voice-to-voice (Bloc 4): when enabled, OSIA streams audio to a
    # provider's realtime/live endpoint and bypasses the local STT (Whisper)
    # and TTS pipelines — the live endpoint handles both natively. The API key
    # is reused from the matching AI provider (no duplicate storage). See
    # osia.voice.live_api and osia.main.wake_mode.
    "liveapi_enabled": False,                  # master switch
    "liveapi_provider": "google",              # "google", "openai", "claude"
    "liveapi_model": "gemini-3.1-flash-live",  # provider-specific live model id
    # Wake-word listener master switch (settings UI toggle): when False OSIA
    # does not start — and stops — the always-on "OSIA" wake-word detector.
    # OSIA stays reachable via the capsule click and the Ctrl+Alt+O window.
    # Wired in osia.main.wake_mode via MainPopup.wake_word_enabled_changed.
    "wake_word_enabled": True,
    # Wake-word greeting: when enabled, OSIA speaks ``wake_greeting_text`` right
    # after the wake word is detected and BEFORE it starts listening for the
    # command (see osia.main.wake_mode.handle_wake). Empty text = no greeting.
    "wake_greeting_enabled": False,
    "wake_greeting_text": "",
    # Scheduled tasks (Bloc 5): list of cron entries. Each entry is
    # ``{"task": <description>, "schedule": <spec>, "enabled": <bool>}``.
    # ``schedule`` is one of "every <N> minutes|hours|days" or "at HH:MM"
    # (daily). When a job fires, OSIA runs ``task`` through the agent exactly
    # like a spoken command. See osia.cron.CronManager.
    "cron_jobs": [],
    # Heartbeat system monitoring (Bloc 5): when enabled, a background thread
    # samples the machine every ``heartbeat_interval_minutes`` and speaks a
    # TTS alert when a monitored metric crosses its threshold. See
    # osia.heartbeat.HeartbeatMonitor.
    "heartbeat_enabled": False,
    "heartbeat_interval_minutes": 5,
    "heartbeat_interval_seconds": 30,
    "heartbeat_checks": {
        # cpu/ram thresholds are "alert when usage % > threshold"; disk is
        # "alert when free % < threshold"; network has no threshold (a failed
        # connectivity probe is the alert).
        "cpu": {"enabled": True, "threshold": 80},
        "ram": {"enabled": True, "threshold": 90},
        "disk": {"enabled": True, "threshold": 10},
        "network": {"enabled": True},
    },
    # API endpoint fallback (Bloc 5): per-provider base_url overrides. This is
    # distinct from the AI Task Hierarchy (which swaps *models*) — it only
    # changes which API endpoint/base_url a provider talks to. Shape:
    # ``{<provider>: {"primary": <url>, "fallback": <url>}}``. When the primary
    # endpoint fails with a connection/timeout error, OSIA retries the same
    # request against the fallback URL. See osia.models.endpoint_for_provider.
    "api_fallbacks": {},
    # API key fallback hierarchy: per-provider ordered list of API keys. The
    # first key is the primary; the rest are tried in order when a request
    # fails with a rate-limit (429) or auth (401/403) error, BEFORE the AI Task
    # Hierarchy advances to a different model. Shape:
    # ``{<provider>: ["key1", "key2", ...]}``. Empty/absent = use the single
    # saved key (api_keys.json) unchanged. See OSIAAgent.set_model/run().
    "api_key_fallbacks": {},
    # Skills (Bloc 6): enabled/disabled flag per skill file. The skill prose
    # itself lives as Markdown under ``~/.osia/skills/*.md``; only the on/off
    # state is stored here. Shape: ``{"<name>.md": true}``. A skill with no
    # recorded flag defaults to enabled. Enabled skills are appended to the
    # system prompt — see osia.personality and osia.agent.get_system_prompt.
    "skills_enabled": {},
    "island_shape": "capsule",  # adaptive shape: "capsule", "circle", "docked"
    "island_dock_side": "right",  # docked screen edge: "left", "right"
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
        with open(API_KEYS_FILE, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_api_keys(keys: dict) -> None:
    """Persist API keys to disk and chmod the file to 0600."""
    _ensure_config_dir()
    with open(API_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    if sys.platform != "win32":
        try:
            os.chmod(API_KEYS_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    else:
        print("[config] ACL hardening (0o600) not applied on Windows")


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
        with open(SETTINGS_FILE, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, dict):
            base.update(data)
    except Exception:
        pass
    return base


def save_settings(settings: dict) -> None:
    _ensure_config_dir()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
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
        with open(MOBILE_TOKEN_FILE, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def regenerate_mobile_token() -> str:
    """Generate a fresh 20-char mobile token, persist it (0600), and return it."""
    _ensure_config_dir()
    token = secrets.token_hex(10)  # 20 hex chars
    with open(MOBILE_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    if sys.platform != "win32":
        try:
            os.chmod(MOBILE_TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    else:
        print("[config] ACL hardening (0o600) not applied on Windows")
    return token


# ── First-launch config bootstrap ──────────────────────────────────────────
# A fresh clone/install ships without the gitignored, security-sensitive config
# files (settings.json, mobile_token.txt) — only settings.example.json is in the
# repo. ``ensure_config_files`` creates whatever is missing on the very first
# launch, on every OS, so first-time setup (and the mobile pairing) works out of
# the box. It is idempotent and best-effort: it only writes files that don't yet
# exist, and never raises (a config-bootstrap failure must not block startup).

def ensure_config_files(create_token: bool = True) -> None:
    """Create any missing config files on first launch (idempotent, best-effort).

    Ensures ``settings.json`` (persisted from ``DEFAULT_SETTINGS``) and — when
    ``create_token`` is True — the mobile pairing token exist on disk, so a fresh
    install on Windows/Mac/Linux works immediately and the mobile WebSocket
    server authenticates against a *persisted* token rather than a memory-only
    one.

    ``create_token`` must be False for callers that do NOT own the mobile-server
    launch (the desktop GUI on Linux, where a separate ``run_server.py`` process
    is the single token creator) — otherwise two processes could race on a fresh
    boot and persist *divergent* tokens, leaving the phone unable to connect
    until the server is restarted. See the call sites in ``osia/main.py`` (GUI)
    and ``osia/server/run_server.py`` (server).
    """
    try:
        _ensure_config_dir()
    except Exception as e:
        print(f"[CONFIG] could not create config dir {CONFIG_DIR!r}: {e}")
        return
    # settings.json — persist the defaults so the file exists for the user to
    # find/edit and so every component reads the same on-disk config.
    if not os.path.exists(SETTINGS_FILE):
        try:
            save_settings(dict(DEFAULT_SETTINGS))
            print("[CONFIG] Created settings.json from defaults (saved to disk)")
        except Exception as e:
            print(f"[CONFIG] could not create settings.json: {e}")
    # Mobile pairing token — generate + persist BEFORE the WebSocket server
    # starts so the server reads the same token the GUI displays. load_mobile_token()
    # returns "" for both a missing and an empty/whitespace-only token file.
    if create_token and not load_mobile_token():
        try:
            token = regenerate_mobile_token()
            masked = (
                f"{token[:4]}...{token[-4:]}" if len(token) >= 8 else "****"
            )
            print(
                f"[CONFIG] Generated new mobile token: {masked} (saved to disk)"
            )
        except Exception as e:
            print(f"[CONFIG] could not generate mobile token: {e}")
