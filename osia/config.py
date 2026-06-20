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

import copy
import json
import os
import secrets
import stat
import sys
import threading
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
    # MCP / Connectors (WOR-44): list of Model-Context-Protocol servers OSIA can
    # use as ADDITIONAL agent tools. Each entry is non-secret config only —
    # ``{"id", "name", "transport", "url", "command", "args", "env",
    # "auth_type", "enabled"}``. ``transport`` is one of "stdio" |
    # "streamable_http" | "sse" (Phase 1 wires stdio). ``auth_type`` is "none" |
    # "token" | "oauth". Any token/secret lives in the gitignored api_keys.json
    # under an ``mcp:<id>`` key (see get_mcp_token/set_mcp_token) — NEVER here,
    # because settings.json is git-tracked. See osia.mcp.MCPManager.
    "mcp_servers": [],
    # MCP write-tool confirmation gate (WOR-44 Phase 5, 5-conf-a). Both are
    # NON-secret global toggles. ``mcp_confirm_writes`` (default True): a
    # write/destructive MCP tool must be confirmed before it runs (read-only MCP
    # tools and ALL native tools are unaffected; turning this OFF runs writes
    # unchanged). ``mcp_allow_unattended_writes`` (default False): governs
    # UNATTENDED runs (cron/autonomous, no confirmation UI registered) — False
    # hard-denies the write, True lets it run without confirmation. See
    # osia.mcp.registry (classify_tool / _confirm_gate).
    "mcp_confirm_writes": True,
    "mcp_allow_unattended_writes": False,
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


# ── MCP / Connectors secret storage (WOR-44) ───────────────────────────────
# MCP server tokens are secrets, so they live in the gitignored api_keys.json
# (0600 on Unix) — NEVER in settings.json (git-tracked). They are namespaced
# under ``mcp:<server_id>`` so they share the existing api-keys plumbing and
# masking discipline (callers must never log the token itself). The MCPManager
# stays secret-free: the caller fetches a token here and injects it into the
# server config's ``env`` (stdio) or auth header (HTTP) at connect time.
_MCP_TOKEN_PREFIX = "mcp:"


def get_mcp_token(server_id: str) -> str:
    """Return the stored token for an MCP server id, or '' if unset."""
    if not server_id:
        return ""
    return load_api_keys().get(f"{_MCP_TOKEN_PREFIX}{server_id}", "") or ""


def set_mcp_token(server_id: str, token: str) -> None:
    """Set or clear an MCP server's token (empty string clears it)."""
    if not server_id:
        return
    keys = load_api_keys()
    name = f"{_MCP_TOKEN_PREFIX}{server_id}"
    if token:
        keys[name] = token
    else:
        keys.pop(name, None)
    save_api_keys(keys)


def delete_mcp_token(server_id: str) -> None:
    """Remove an MCP server's stored token, if any."""
    set_mcp_token(server_id, "")


# ── MCP / Connectors OAuth token storage (WOR-44 Phase 5, 5-oauth-a) ─────────
# OAuth 2.1 tokens for MCP connectors (e.g. Linear) live in their OWN gitignored
# file ``oauth_tokens.json`` (already listed in .gitignore as
# ``config/oauth_tokens.json``) — NEVER in settings.json (git-tracked) and NEVER
# routed through api_keys.json / key_chain (these are SDK-managed OAuth blobs,
# not OSIA-rotated API keys). Each entry is a per-server blob
# ``{"token": {access_token, refresh_token, expires_in, scope, token_type},
# "expires_at": <unix>, "client": {client_id, client_secret?, redirect_uris, …}}``
# written by ``osia.mcp.oauth.OsiaTokenStorage`` (the MCP SDK's TokenStorage).
#
# CRITICAL: writes are IMMEDIATE and ATOMIC. Linear rotates the refresh token
# (~24h) and invalidates the old one, so a stale or half-written on-disk token
# after a restart/crash forces a full re-auth. ``_save_oauth_store`` writes to a
# temp file, fsyncs, chmods 0600, then ``os.replace``s it into place (atomic on
# POSIX and Windows). Token VALUES are never logged here (only the Windows ACL
# notice, matching ``save_api_keys``).
OAUTH_TOKENS_FILE = os.path.join(CONFIG_DIR, "oauth_tokens.json")
_oauth_lock = threading.Lock()


def _load_oauth_store() -> dict:
    """Return the whole ``oauth_tokens.json`` dict, or {} if missing/corrupt."""
    if not os.path.exists(OAUTH_TOKENS_FILE):
        return {}
    try:
        with open(OAUTH_TOKENS_FILE, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_oauth_store(store: dict) -> None:
    """Persist the OAuth token store atomically and chmod it to 0600.

    Temp-file + fsync + ``os.replace`` so a crash mid-write can never corrupt
    the live file and lose a rotated refresh token (see the note above)."""
    _ensure_config_dir()
    tmp = OAUTH_TOKENS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    if sys.platform != "win32":
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    else:
        print("[config] ACL hardening (0o600) not applied on Windows")
    os.replace(tmp, OAUTH_TOKENS_FILE)


def get_mcp_oauth(server_id: str) -> dict:
    """Return the stored OAuth blob for an MCP server id, or {} if unset."""
    if not server_id:
        return {}
    with _oauth_lock:
        blob = _load_oauth_store().get(server_id)
    return dict(blob) if isinstance(blob, dict) else {}


def set_mcp_oauth(server_id: str, blob: dict) -> None:
    """Set (or clear, when ``blob`` is falsy) an MCP server's OAuth blob.

    Read-modify-write of the shared store is guarded by ``_oauth_lock`` so two
    connectors writing concurrently can't clobber each other's entry."""
    if not server_id:
        return
    with _oauth_lock:
        store = _load_oauth_store()
        if blob:
            store[server_id] = blob
        else:
            store.pop(server_id, None)
        _save_oauth_store(store)


def delete_mcp_oauth(server_id: str) -> None:
    """Remove an MCP server's stored OAuth blob, if any."""
    set_mcp_oauth(server_id, {})


# ── settings.json read cache (mtime+size based) ────────────────────────────
# load_settings() is on the hot per-message server path and was opening
# settings.json on EVERY call (config.py callers: get_setting, iter_keys, the
# per-skill is_skill_enabled loop, …). On Windows each open triggers an
# antivirus scan, so the repeated reads cost real time. This cache parses the
# file at most once per (mtime_ns, size): the fast path only os.stat()s it
# (cheap, no content scan) and reuses the last parse. It is a TRANSPARENT
# read-path optimization — load_settings() still returns the same value and
# still reflects on-disk changes (the stat catches external/cross-process
# edits; save_settings invalidates it for in-process writes).
_settings_cache_lock = threading.Lock()
# (st_mtime_ns, st_size) of the file the cache was built from, or None when
# nothing valid is cached yet (first read, missing file, corrupt parse, write).
_settings_cache_signature: "tuple | None" = None
# Last good RAW parse of the file (defaults NOT merged in). Never handed out
# directly — load_settings() deep-copies it so callers can't mutate the cache.
_settings_cache_data: dict = {}


def _read_settings_file() -> dict:
    """Return the raw dict stored in settings.json (no defaults merged), or
    ``{}`` when the file is missing / empty / corrupt.

    Served from an mtime+size cache: when the file is unchanged since the last
    read it is NOT reopened — only ``os.stat`` runs — and an independent
    deep copy of the cached parse is returned. The copy guarantees callers can
    freely mutate ``load_settings()``'s result without corrupting the cache,
    exactly as when every call re-parsed the file.
    """
    global _settings_cache_signature, _settings_cache_data
    try:
        st = os.stat(SETTINGS_FILE)
    except OSError:
        # Missing/unreadable — match the old "file absent → defaults only"
        # path and forget any stale cache so a later (re)created file is read.
        with _settings_cache_lock:
            _settings_cache_signature = None
            _settings_cache_data = {}
        return {}

    signature = (st.st_mtime_ns, st.st_size)
    with _settings_cache_lock:
        if signature == _settings_cache_signature:
            return copy.deepcopy(_settings_cache_data)

    # Slow path: file changed (or first read). Do the open+parse OUTSIDE the
    # lock so concurrent readers never block on disk I/O; the worst case is two
    # threads parsing the same bytes, which is harmless (same result stored).
    data: dict = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8", errors="replace") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        # Corrupt or mid-write file: behave like before (defaults only) and do
        # NOT poison the cache — drop the signature so the next call retries.
        with _settings_cache_lock:
            _settings_cache_signature = None
            _settings_cache_data = {}
        return {}

    with _settings_cache_lock:
        _settings_cache_signature = signature
        _settings_cache_data = data
        return copy.deepcopy(data)


def load_settings() -> dict:
    """Return the saved settings dict, merging in defaults.

    Reads are served from an mtime+size cache (see :func:`_read_settings_file`)
    so an unchanged ``settings.json`` is opened at most once — a transparent
    optimization that matters on Windows, where every file open triggers an
    antivirus scan. The return value is always a fresh, independent dict, so
    callers may mutate it (e.g. :func:`set_setting`) without touching the cache.
    """
    base = dict(DEFAULT_SETTINGS)
    base.update(_read_settings_file())
    return base


def save_settings(settings: dict) -> None:
    global _settings_cache_signature, _settings_cache_data
    _ensure_config_dir()
    # Atomic write (temp + fsync + os.replace), mirroring _save_oauth_store: a
    # crash mid-write can never corrupt settings.json, and a concurrent reader in
    # the OTHER process never observes a half-written/torn file — the desktop and
    # the mobile server share this file (e.g. the MCP ``mcp_servers[].enabled``
    # mirror, Phase 1). os.replace is atomic on POSIX and Windows; the temp lives
    # in the same CONFIG_DIR as the target so the replace stays a same-filesystem
    # rename. (Cross-process read-modify-write *lost updates* are a separate
    # concern, out of scope here — writes are human-paced and the Phase-2
    # reconcile self-heals any transient divergence.)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, SETTINGS_FILE)
    # Invalidate the read cache so the very next load_settings() re-reads what
    # we just wrote. Hard invalidation (not in-place update) keeps the cache
    # byte-for-byte identical to disk — a populate-from-memory shortcut could
    # diverge via JSON round-tripping (tuples→lists, int keys→strings).
    with _settings_cache_lock:
        _settings_cache_signature = None
        _settings_cache_data = {}


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


def rendezvous_proof(token: str) -> str:
    """HMAC-SHA256(key=token_utf8, msg='osia-rdv-v1') -> 64-hex (WOR-64).

    The PC publishes this ``proof`` (never the raw token) to the rendezvous
    Worker so the phone can locate the current Cloudflare quick-tunnel URL; the
    Worker derives the lookup key from the same HMAC itself. See
    :mod:`osia.rendezvous`.
    """
    import hmac
    import hashlib
    return hmac.new(token.encode("utf-8"), b"osia-rdv-v1", hashlib.sha256).hexdigest()


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
