#!/usr/bin/env python3
"""
OSIA Mobile Server — FastAPI application with a WebSocket endpoint (/ws).

Protocol (JSON over WebSocket, port 8765):
  Client → Server:
    {"type": "auth",                 "token": "..."}
    {"type": "text",                 "content": "..."}
    {"type": "audio",                "format": "wav", "data": "<base64>"}
    {"type": "list_conversations"}
    {"type": "get_conversations"}                                      # compact id+title list (≤50 most recent) for the mobile cron "linked conversation" picker
    {"type": "load_conversation",    "id": "..."}
    {"type": "new_conversation"}
    {"type": "new_chat"}                                               # "Nouveau chat" voice command relayed from mobile; resets server state like new_conversation, then broadcasts new_chat to other devices (WOR-10)
    {"type": "delete_conversation",  "id": "..."}
    {"type": "rename_conversation",  "id": "...", "title": "..."}       # WOR-76: rename a conversation; acks {conversation_renamed, success}, then sends the refreshed list to the caller and broadcasts it to other devices
    {"type": "get_models"}
    {"type": "set_model",            "provider": "...", "model": "..."}
    {"type": "set_model_reasoning",  "provider": "...", "model": "...", "reasoning": "..."}
    {"type": "get_chatgpt_account"}
    {"type": "chatgpt_account_login"}
    {"type": "chatgpt_account_cancel_login"}
    {"type": "chatgpt_account_logout"}
    {"type": "file",                 "filename": "...", "data": "<base64>"}
    {"type": "speak",                "text": "..."}      # synthesize text → audio frame sent back to the phone
    {"type": "set_api_key",          "provider": "...", "key": "..."}  # save a provider key; empty key deletes it entirely (config/api_keys.json)
    {"type": "delete_api_key",       "provider": "..."}                # remove a provider's key entirely from config/api_keys.json
    {"type": "set_api_fallback_key", "provider": "...", "index": int, "key": "..."}  # set/replace the fallback key at index (index=-1 appends) in settings.json api_key_fallbacks
    {"type": "delete_api_fallback_key", "provider": "...", "index": int}             # remove the fallback key at index from settings.json api_key_fallbacks
    {"type": "set_setting",          "key": "...", "value": ...}       # set one whitelisted key in config/settings.json
                                                                       #   (heavy_chain/medium_chain map onto ai_hierarchy.{heavy,medium})
    {"type": "get_config"}                                             # request the safe config snapshot (settings/status metadata, personality, skills)
    {"type": "set_personality",      "text": "..."}                    # save the personality document (~/.osia/personality.md)
    {"type": "set_skill",            "name": "...", "enabled": true, "text": "..."}  # update a skill's enabled flag and/or prose (both optional)
    {"type": "create_skill",         "name": "..."}                    # create a new skill from the template (starts enabled)
    {"type": "delete_skill",         "name": "..."}                    # delete a skill and drop its enabled flag
    {"type": "get_api_keys"}                                           # request configured/not-configured status only
    {"type": "get_heartbeat_config"}                                   # request the live-heartbeat enable flag + push interval (seconds)
    {"type": "restart_server"}                                         # restart the server process in place via os.execv; acked with status "done" + "message": "restarting" first (WOR-28)
    {"type": "cancel_active_work", "schema_version": 1, "scope": "chat", ...correlated target...}  # WOR-231: exact idempotent cancellation control; never a user turn
    {"type": "scheduled_task_disable", "task_id": "..."}                # stop a scheduled task; pure execution edit, no authorization envelope
  Server → Client:
    {"type": "api_keys",             "keys": {provider: "", ...}, "keys_set": {provider: bool}} # legacy shape; no credential values
    {"type": "heartbeat_config",     "config": {"enabled": bool, "interval_seconds": int}}
    {"type": "heartbeat_data",       "cpu_percent": float, "ram_mb": int,
                                     "ram_percent": float, "disk_percent": float,
                                     "cpu_temp": float|null, "gpu_temp": float|null,
                                     "timestamp": "<ISO>"}              # pushed every heartbeat_interval_seconds while heartbeat_enabled
    {"type": "transcript",           "content": "...", "conversation_id": "..."}
    {"type": "response_chunk",       "content": "...", "conversation_id": "..."}  # canonical text compatibility chunk; never provisional tool-round prose
    {"type": "response",             "content": "...", "conversation_id": "..."}  # final assembled reply
    {"type": "audio",                "format": "mp3", "data": "<base64>", "source": "response|speak", "conversation_id": "..."}
    {"type": "status",               "content": "thinking|done|error", "conversation_id": "..."}
                                     # conversation_id (added WOR HOL fix): OPTIONAL routing hint on the per-turn frames
                                     # above so a client can attach a backgrounded stream to the right conversation
                                     # while viewing another. Absent on non-turn frames; old clients may ignore it.
    {"type": "conversations",        "items": [{id,title,created_at,updated_at,message_count}, ...]}
    {"type": "conversations_list",   "conversations": [{id,title}, ...]}  # compact list (≤50 most recent) for the mobile cron "linked conversation" picker
    {"type": "conversation_history", "id": "...", "conversation_id": "...", "title": "...",
                                     "messages": [{"role": "user|assistant", "content": "..."}]}  # conversation_id mirrors id (additive routing field)
    {"type": "conversation_deleted", "id": "...", "success": true|false}
    {"type": "active_conversation",  "id": "...", "title": "..."}
    {"type": "new_chat"}                                               # broadcast to OTHER connected devices when one device starts a new chat, so every UI clears (WOR-10)
    {"type": "models_list",          "catalogue": {"version": 1, "providers": [...]},
                                       "models": [...], "current": "...", "current_provider": "...",
                                       "account": {...}}
    {"type": "chatgpt_account_state", "account": {...}}
    {"type": "file_received",        "filename": "...", "success": true|false}
    {"type": "config",               "config": {...}}                  # safe config snapshot (credential values omitted)
    {"type": "cron_notification",    "id": "...", "task_id": "...", "title": "...",
                                     "status": "completed|failed", "conversation_id": "...", "created_at": "..."}  # in-app fallback for scheduled task completion
    {"type": "status",               "status": "done|error"}           # ack for the config-family setters (create_skill adds "name", restart_server adds "message": "restarting")
"""

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import wave
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request

from osia import chat_routing, conversations, document_sources, i18n
from osia.attachments import (
    ATTACHMENT_EVENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    AttachmentBatchManager,
    AttachmentProtocolError,
    build_agent_input,
    cleanup_orphan_batches,
)
from osia.config import CONFIG_DIR as _CONFIG_DIR, regenerate_mobile_token
from osia.document_progress import document_progress_scope
from osia.runtime import (
    application_dir as _runtime_application_dir,
    is_frozen as _runtime_is_frozen,
    source_tree_fingerprint as _source_tree_fingerprint,
)
from osia.durable_transport import (
    InboxDisposition,
    get_durable_transport_store,
)
from osia.version import current_product_version
from osia.legacy_uploads import (
    LegacyUploadError,
    LegacyUploadManager,
    reconcile_legacy_uploads,
    run_with_legacy_cleanup,
    safe_legacy_display_name,
)
from osia.memory import (
    MemoryError as OsiaMemoryError,
    MemorySecretError,
    MemoryStore,
    MemoryValidationError,
)
from osia.server.text_coalescer import AssistantTextCoalescer
from osia.turn_context import (
    TurnContext,
    ValidatedLiveAssistantChunk,
    fixed_turn_context,
    localized_text,
    log_final,
    normalize_locale,
    persisted_language_drift_diagnostic,
    resolve_turn_context,
)

# Windows consoles default to cp1252, which raises UnicodeEncodeError the moment
# a log line contains a non-ASCII char (e.g. the "▶" in "[OSIA Agent] ▶ tool:")
# — that crash propagates to the mobile client as an error instead of the AI's
# reply and blocks persisting new conversations. Force UTF-8 (replace
# undecodable chars) on the std streams as soon as this module is imported, so
# it applies whether the server is started via run_server.py (Task Scheduler) or
# directly (python -m osia.server.server / the restart re-spawn). Guarded for
# the detached-launch case where stdout/stderr may be None or lack .reconfigure
# — this must never crash startup. Linux/Mac consoles are UTF-8 already, so the
# whole block is win32-only.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# <repo>/osia/server/server.py → <repo>. Resolved dynamically so the server
# runs from any install location / drive on both Linux and Windows (WOR-11),
# never a hardcoded /home/OSIA/code/... path.
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
# Per-user config/data dir — imported from osia.config so the server and the
# desktop GUI always resolve the SAME directory via ``_resolve_config_dir()``
# (``~/.osia``, with the Linux <repo>/config fallback). Wrapped in ``Path`` here
# because osia.config.CONFIG_DIR is a ``str`` while the server uses Path ops.
CONFIG_DIR = Path(_CONFIG_DIR)
TOKEN_FILE = CONFIG_DIR / "mobile_token.txt"
# Cross-platform scratch dir for mobile uploads — ``/tmp`` does not exist on
# Windows, so derive it from the OS temp dir instead.
UPLOAD_DIR = Path(tempfile.gettempdir()) / "osia_uploads"
ATTACHMENT_UPLOAD_DIR = Path(tempfile.gettempdir()) / "osia_attachment_batches"
cleanup_orphan_batches(ATTACHMENT_UPLOAD_DIR)


# ---------------------------------------------------------------------------
# X11 environment for headless browser automation (Linux only)
# ---------------------------------------------------------------------------
def _ensure_x11_env() -> None:
    """Resolve DISPLAY + XAUTHORITY so the CDP/Firefox-BiDi browser tools work
    when the server runs headless under systemd (no graphical login session).

    The per-session X authority cookie lives in the XDG runtime dir
    (``/run/user/<uid>/xauth_XXXXXX``) under a *random* suffix that changes on
    every login/reboot, so it can't be pinned in a systemd override without
    going stale. We resolve it live at startup and stash it in ``os.environ``;
    the browser subprocesses OSIA launches inherit the environment (Popen is
    called without ``env=`` in ``osia/tools/browser.py``), so that's all they
    need.

    Linux-only — a no-op on Windows/macOS, where the desktop login session
    already exports a valid DISPLAY/XAUTHORITY.
    """
    if not sys.platform.startswith("linux"):
        return

    # DISPLAY — default to the primary local display when the unit inherited none.
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    # WAYLAND_DISPLAY — under a Wayland login (KDE Plasma/GNOME on Fedora) the
    # screenshot backend (spectacle/grim, see osia/agent.py) needs the
    # compositor socket, which a headless systemd unit may not inherit. Resolve
    # it from the XDG runtime dir — only when a socket really exists — so we
    # never set a bogus value on a pure X11 host (which would otherwise fool the
    # Wayland detection in take_screenshot into trying Wayland-only tools).
    if not os.environ.get("WAYLAND_DISPLAY"):
        xdg_rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        runtime = Path(xdg_rt)
        if (runtime / "wayland-0").exists():
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        else:
            try:
                sockets = sorted(
                    p.name for p in runtime.glob("wayland-*")
                    if not p.name.endswith(".lock")
                )
            except OSError:
                sockets = []
            if sockets:
                os.environ["WAYLAND_DISPLAY"] = sockets[0]

    # XAUTHORITY — keep an existing value only while it still points at a real
    # file; otherwise hunt for the live per-session cookie.
    xauth = os.environ.get("XAUTHORITY")
    if not (xauth and os.path.isfile(xauth)):
        # Primary location is the XDG runtime dir (/run/user/<uid>); the
        # classic ``~/.Xauthority`` is the fallback.
        search_dirs = [f"/run/user/{os.getuid()}"]
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and xdg not in search_dirs:
            search_dirs.append(xdg)

        candidates: list[Path] = []
        for d in search_dirs:
            try:
                candidates.extend(Path(d).glob("xauth_*"))
            except OSError:
                pass
        home_xauth = Path.home() / ".Xauthority"
        if home_xauth.is_file():
            candidates.append(home_xauth)

        # The most recently modified cookie belongs to the live session.
        best: Path | None = None
        best_mtime = -1.0
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime

        if best is not None:
            os.environ["XAUTHORITY"] = str(best)

    resolved = os.environ.get("XAUTHORITY", "<unresolved>")
    wayland = os.environ.get("WAYLAND_DISPLAY", "<none>")
    print(
        f"[X11] DISPLAY={os.environ['DISPLAY']} XAUTHORITY={resolved} "
        f"WAYLAND_DISPLAY={wayland}"
    )


# Image extensions that should trigger DeepSeek Vision automatically.
_IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


def _image_mime_for(path: Path) -> str | None:
    """Return the image MIME type for ``path`` based on extension, or
    None if the extension isn't a recognised image format.
    """
    return _IMAGE_MIME.get(path.suffix.lower().lstrip("."))


def _sanitize_upload_filename(filename: str) -> str:
    """Return a safe basename for a client-supplied upload filename.

    Applied on all platforms (important on Windows): keep the basename only,
    drop the chars ``<>:"/\\|?*`` and control chars, strip leading/trailing
    dots and spaces, and rename Windows reserved basenames. Always returns a
    non-empty name (falls back to ``upload.bin``).
    """
    return safe_legacy_display_name(filename or "")

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
def load_or_create_token() -> str:
    """Read the mobile token from disk, or generate + persist a new one.

    Creation of a *brand-new* token is delegated to
    ``osia.config.regenerate_mobile_token`` so that module is the SINGLE source
    of truth for token generation: a fresh install gets the same 20-char token
    (``secrets.token_hex(10)``) whoever creates it first — this server,
    ``config.ensure_config_files`` (bootstrap), or the GUI regenerate button.
    WOR-64 key derivation relies on that exact on-disk length. Existing tokens
    are read/compared unchanged; only the no-file creation path is affected.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            # Silently upgrade legacy 16-char tokens to 20 chars.
            if len(token) == 16:
                token = secrets.token_hex(10)  # 20 hex chars
                TOKEN_FILE.write_text(token)
                if sys.platform != "win32":
                    TOKEN_FILE.chmod(0o600)
                else:
                    print("[OSIA Server] ACL hardening (0o600) not applied on Windows")
            return token
    # Fresh box, no token file yet: delegate to config.regenerate_mobile_token
    # (the single source of truth, 20-char token_hex(10)) instead of generating a
    # divergent 64-char token here. It writes the same CONFIG_DIR/mobile_token.txt
    # this function reads, with identical 0600 hardening, and returns the token.
    return regenerate_mobile_token()


MOBILE_TOKEN = load_or_create_token()
_MOBILE_SERVER_ID = hashlib.sha256(
    ("osia-permission-server-v1\0" + MOBILE_TOKEN).encode("utf-8")
).hexdigest()


def _log_active_model() -> None:
    """Resolve the active model from settings and print a banner so it's
    obvious in the server logs which provider/model is actually loaded.
    """
    try:
        from osia.config import load_settings
        from osia.agent import MODEL as DEFAULT_MODEL

        settings = load_settings()
        selected = settings.get("selected_model")
        if selected:
            provider = str(
                settings.get("selected_model_provider") or ""
            ).strip()
            if not provider:
                try:
                    from osia.models import provider_for_model_cached
                    provider = provider_for_model_cached(selected)
                except Exception:
                    provider = "unknown"
            model = selected
            source = "settings.json (selected_model)"
        else:
            model = DEFAULT_MODEL
            provider = "ollama"
            source = "agent default (no selected_model in settings)"

        print("=" * 70)
        print(f"[OSIA Server] Active model: {model}")
        print(f"[OSIA Server] Provider:     {provider}")
        print(f"[OSIA Server] Source:       {source}")
        print("=" * 70)
    except Exception as e:
        print(f"[OSIA Server] Could not resolve active model: {e}")


_log_active_model()


# ---------------------------------------------------------------------------
# TTS → mp3 bytes (synthesis without playback)
# ---------------------------------------------------------------------------
def _kokoro_to_mp3_bytes(text: str, language: str | None = None) -> bytes:
    """Synthesize text with Kokoro and return mp3 bytes."""
    from kokoro import KPipeline
    from osia.voice.tts import (
        detect_language,
        KOKORO_SR,
        KOKORO_SPEED,
        KOKORO_VOICE_EN,
        KOKORO_VOICE_FR,
        local_tts_supports,
    )

    lang = normalize_locale(language) if language is not None else detect_language(text)
    if not local_tts_supports(lang):
        raise RuntimeError(
            f"local TTS unavailable for authoritative language {lang or 'unknown'}"
        )
    lang_code = "f" if lang == "fr" else "a"
    voice = KOKORO_VOICE_FR if lang == "fr" else KOKORO_VOICE_EN
    pipeline = KPipeline(lang_code=lang_code)

    segments = []
    for _, _, audio in pipeline(text, voice=voice, speed=KOKORO_SPEED):
        if audio is not None:
            segments.append(audio)

    if not segments:
        raise RuntimeError("Kokoro produced no audio segments")

    audio = np.concatenate(segments)

    # Write WAV to in-memory buffer
    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio, KOKORO_SR, format="WAV")
    wav_buf.seek(0)

    # Convert WAV → MP3 via ffmpeg
    return _wav_to_mp3(wav_buf.read())


def _openai_to_mp3_bytes(text: str, language: str | None = None) -> bytes:
    """Synthesize text with OpenAI TTS and return mp3 bytes.

    Rotates through the user's OpenAI key chain on a 429/5xx/auth/timeout error
    (shared rotation via :func:`osia.utils.key_chain.with_key_rotation`) before
    the caller falls back to Kokoro. WAV→MP3 conversion stays outside the
    rotation so an ffmpeg failure never burns API keys.
    """
    del language  # OpenAI's multilingual voice reads the authoritative text verbatim.
    import requests
    from osia.config import get_setting
    from osia.voice.tts import OPENAI_VOICES, PROVIDER_DEFAULT_VOICE
    from osia.utils.key_chain import with_key_rotation, has_key

    if not has_key("openai"):
        raise RuntimeError("OpenAI TTS: no API key")

    voice = get_setting("tts_voice") or PROVIDER_DEFAULT_VOICE["openai"]
    if voice not in OPENAI_VOICES:
        voice = PROVIDER_DEFAULT_VOICE["openai"]

    def _synth(key):
        r = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-4o-mini-tts",
                "input": text,
                "voice": voice,
                "response_format": "wav",
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    # OpenAI returns WAV; convert to MP3
    content = with_key_rotation("openai", _synth, label="server-tts:openai")
    return _wav_to_mp3(content)


def _google_to_mp3_bytes(text: str, language: str | None = None) -> bytes:
    """Synthesize text with Gemini TTS (REST generateContent) and return mp3 bytes.

    Mirrors ``osia.voice.tts.speak_google``: Google *Cloud* TTS rejects simple
    Gemini API keys (401, OAuth2 required), so we call the Gemini
    generateContent endpoint, which accepts the ``x-goog-api-key`` header.
    Audio comes back as base64 24kHz 16-bit mono PCM, which we wrap as WAV and
    transcode to MP3 for the phone (the WebSocket protocol still ships mp3).
    """
    del language  # Gemini TTS is instructed to preserve the text's language.
    import requests
    from osia.config import get_setting
    from osia.voice.tts import (
        GEMINI_TTS_MODELS, GEMINI_TTS_SR, GEMINI_VOICES, GEMINI_DEFAULT_VOICE,
        gemini_tts_prompt,
    )
    from osia.utils.key_chain import with_key_rotation, has_key

    if not has_key("google"):
        raise RuntimeError("Google (Gemini) TTS: no API key")

    # A stale Cloud-style voice (e.g. "fr-FR-Neural2-A") isn't a Gemini voice,
    # so validate against the prebuilt list and fall back to the default.
    voice = get_setting("tts_voice") or GEMINI_DEFAULT_VOICE
    if voice not in GEMINI_VOICES:
        voice = GEMINI_DEFAULT_VOICE

    body = {
        "contents": [{"parts": [{"text": gemini_tts_prompt(text)}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }

    def _synth(key):
        """Try the model list (newest first) for one API key; return float32
        samples on the first success, else raise the last error so
        ``with_key_rotation`` can advance to the next key on a 429/5xx."""
        last_err = None
        for model in GEMINI_TTS_MODELS:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json()
                # base64 PCM lives at candidates[0].content.parts[0].inlineData.data
                b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
                pcm = base64.b64decode(b64)
                # 24kHz 16-bit signed PCM mono -> float32 in [-1, 1]
                return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception as e:
                # Model unavailable on this tier / transient error -> try the next.
                last_err = e
                continue
        raise last_err

    # WAV→MP3 conversion stays outside the rotation (matches the kokoro path).
    samples = with_key_rotation("google", _synth, label="server-tts:google")
    wav_buf = io.BytesIO()
    sf.write(wav_buf, samples, GEMINI_TTS_SR, format="WAV")
    wav_buf.seek(0)
    return _wav_to_mp3(wav_buf.read())


def _wav_to_mp3(wav_bytes: bytes, bitrate: str = "128k") -> bytes:
    """Convert WAV audio bytes to MP3 in-process through PyAV/libmp3lame."""
    import av

    try:
        bit_rate = int(bitrate.lower().removesuffix("k")) * 1000
    except (AttributeError, ValueError):
        raise ValueError(f"invalid MP3 bitrate: {bitrate!r}") from None

    output = io.BytesIO()
    source = av.open(io.BytesIO(wav_bytes), mode="r")
    target = av.open(output, mode="w", format="mp3")
    try:
        stream = target.add_stream("libmp3lame", rate=24000)
        stream.bit_rate = bit_rate
        resampler = av.AudioResampler(
            format=stream.codec_context.format.name,
            layout="mono",
            rate=24000,
        )
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                converted.pts = None
                for packet in stream.encode(converted):
                    target.mux(packet)
        for converted in resampler.resample(None):
            converted.pts = None
            for packet in stream.encode(converted):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    finally:
        target.close()
        source.close()
    return output.getvalue()


def synthesize_to_mp3_bytes(text: str, language: str | None = None) -> bytes:
    """Synthesize text to MP3 bytes using the configured TTS provider.

    Follows the same provider chain as ``osia.voice.tts._speak_blocking``:
    openai → google → local (Kokoro). Falls back to Kokoro on failure.
    """
    from osia.config import get_setting
    from osia.utils.key_chain import has_key

    provider = get_setting("tts_provider", "local") or "local"
    print(
        f"[TTS] synthesize_to_mp3_bytes: start "
        f"(provider={provider!r}, {len(text)} chars)",
        flush=True,
    )

    # Try configured provider first, fall back to Kokoro. The per-provider
    # helpers rotate through the user's key chain internally before raising, so
    # we only reach Kokoro once every configured key is exhausted.
    if provider == "openai" and has_key("openai"):
        try:
            mp3 = _openai_to_mp3_bytes(text, language)
            print(
                f"[TTS] synthesize_to_mp3_bytes: done via openai ({len(mp3)} bytes)",
                flush=True,
            )
            return mp3
        except Exception as e:
            print(f"⚠️  OpenAI TTS failed, falling back to Kokoro: {e}")
    elif provider == "google" and has_key("google"):
        try:
            mp3 = _google_to_mp3_bytes(text, language)
            print(
                f"[TTS] synthesize_to_mp3_bytes: done via google ({len(mp3)} bytes)",
                flush=True,
            )
            return mp3
        except Exception as e:
            print(f"⚠️  Google TTS failed, falling back to Kokoro: {e}")

    mp3 = _kokoro_to_mp3_bytes(text, language)
    print(
        f"[TTS] synthesize_to_mp3_bytes: done via kokoro ({len(mp3)} bytes)",
        flush=True,
    )
    return mp3


# ---------------------------------------------------------------------------
# Audio decoding (base64 WAV → numpy array)
# ---------------------------------------------------------------------------
MAX_MICROPHONE_WAV_BYTES = 16 * 1024 * 1024
MAX_MICROPHONE_SECONDS = 60.0
MAX_MICROPHONE_CHANNELS = 2
MAX_MICROPHONE_SAMPLE_RATE = 192_000


def decode_base64_wav(b64_data: str) -> tuple[np.ndarray, int]:
    """Strictly decode one bounded PCM WAV without libsndfile in this process."""

    if not isinstance(b64_data, str):
        raise ValueError("invalid_audio_encoding")
    maximum_b64 = ((MAX_MICROPHONE_WAV_BYTES + 2) // 3) * 4
    if not b64_data or len(b64_data) > maximum_b64:
        raise ValueError("audio_size_limit")
    try:
        raw = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_audio_encoding") from exc
    if not (44 <= len(raw) <= MAX_MICROPHONE_WAV_BYTES):
        raise ValueError("audio_size_limit")
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("invalid_wav")
    if int.from_bytes(raw[4:8], "little") + 8 != len(raw):
        raise ValueError("invalid_wav")
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            channels = int(reader.getnchannels())
            sample_rate = int(reader.getframerate())
            sample_width = int(reader.getsampwidth())
            frame_count = int(reader.getnframes())
            if reader.getcomptype() != "NONE":
                raise ValueError("unsupported_wav_codec")
            if not (1 <= channels <= MAX_MICROPHONE_CHANNELS):
                raise ValueError("audio_channel_limit")
            if not (1 <= sample_rate <= MAX_MICROPHONE_SAMPLE_RATE):
                raise ValueError("audio_sample_rate_limit")
            if sample_width not in {1, 2, 3, 4}:
                raise ValueError("unsupported_wav_codec")
            if frame_count <= 0 or frame_count / sample_rate > MAX_MICROPHONE_SECONDS:
                raise ValueError("audio_duration_limit")
            payload = reader.readframes(frame_count)
            if len(payload) != frame_count * channels * sample_width:
                raise ValueError("invalid_wav")
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid_wav") from exc

    if sample_width == 1:
        samples = (np.frombuffer(payload, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(payload, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        octets = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
        signed = (
            octets[:, 0].astype(np.int32)
            | (octets[:, 1].astype(np.int32) << 8)
            | (octets[:, 2].astype(np.int32) << 16)
        )
        signed = (signed ^ 0x800000) - 0x800000
        samples = signed.astype(np.float32) / 8388608.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return np.ascontiguousarray(samples, dtype=np.float32), sample_rate


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="OSIA Mobile Server", version=current_product_version())

# Immutable identity captured when this source module is imported. The Windows
# startup contract compares this fingerprint with the current checkout; a live
# listener cannot claim freshness merely because it owns port 8765.
_SERVER_RUNTIME_SOURCE_MODE = "frozen" if _runtime_is_frozen() else "source"
_SERVER_RUNTIME_CHECKOUT_ROOT = str(_runtime_application_dir().resolve())
_SERVER_RUNTIME_ENTRYPOINT = str(Path(__file__).resolve())
_SERVER_RUNTIME_SOURCE_FINGERPRINT = _source_tree_fingerprint()
_SERVER_RUNTIME_EXECUTABLE = str(sys.executable or "")
_SERVER_RUNTIME_ID = (
    f"{os.getpid()}:{_SERVER_RUNTIME_SOURCE_FINGERPRINT}"
)


# Registry of authenticated mobile clients (WOR-10). Holds each connection's
# OUTBOUND CHANNEL (``_OutboundChannel``) — NOT the raw WebSocket — so a
# server-initiated fan-out frame (e.g. the "new chat" reset) is funnelled
# through that connection's single writer task instead of being sent from a
# foreign coroutine (which would race the connection's own writer and corrupt
# Starlette's send state). Populated after a successful auth, pruned on
# disconnect.
_WS_CLIENTS: set["_OutboundChannel"] = set()
_MOBILE_CATALOGUE_BROADCAST_LOCK: asyncio.Lock | None = None
_CONVERSATION_BROADCAST_LOCK: asyncio.Lock | None = None
_LAST_BROADCAST_REVISIONS: dict[str, int] = {}

_MOBILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _mobile_safe_id(value) -> str:
    candidate = str(value or "")
    return candidate if _MOBILE_ID_RE.fullmatch(candidate) else ""


def _mobile_document_owner(device_id: str) -> str:
    """Stable private owner identity for one authenticated mobile device."""

    safe_device = _mobile_safe_id(device_id)
    if not safe_device:
        return ""
    return "mobile-document:" + hashlib.sha256(
        f"{_MOBILE_SERVER_ID}\0{safe_device}".encode("utf-8"),
    ).hexdigest()


def _current_policy_epoch() -> int:
    """Canonical epoch used to bind document sources.

    WOR-301: no permission epochs remain; the constant generation keeps the
    binding contract while freshness is proven against the source itself.
    """

    return 0


_CRON_NOTIFICATION_MAX = 30
_CRON_NOTIFICATIONS: "deque[dict]" = deque(maxlen=_CRON_NOTIFICATION_MAX)
_CRON_NOTIFICATIONS_LOCK = threading.Lock()

# WOR-96 PART 2a: strong references to in-flight "drive OAuth" background tasks
# spawned by the mobile mcp_connect handler. asyncio keeps only a WEAK reference
# to a bare create_task() result, so a fire-and-forget task can be garbage
# collected mid-await ("Task was destroyed but it is pending"). Holding the task
# here until it completes prevents that; the done-callback prunes it.
_OAUTH_DRIVE_TASKS: set = set()

# Native Gmail REST mobile OAuth expiry broadcasters. The PKCE attempt itself
# lives in ``osia.integrations.google_oauth``; these tasks only ensure a phone
# receives a retryable timeout snapshot even when no further request arrives.
_GMAIL_OAUTH_WATCHDOG_TASKS: dict[str, "asyncio.Task"] = {}
_GMAIL_OAUTH_ATTEMPT_OWNERS: dict[str, object] = {}


# WOR-62: in-flight turns DETACHED on client disconnect so they run to
# completion and persist (instead of being cancelled, which dropped the reply
# when a phone slept mid-generation). Held here at module scope so the event
# loop keeps a STRONG reference — a task whose only reference is the local
# ``streaming_tasks`` dict would be eligible for GC the moment that dict is
# cleared in the disconnect ``finally``, and could vanish mid-run. Each task
# removes itself via an ``add_done_callback`` so this set can never grow
# unbounded.
_DETACHED_TURNS: set["asyncio.Task"] = set()

# WOR-62 follow-up: conv_id -> its DETACHED in-flight turn. Held in PARALLEL
# with ``_DETACHED_TURNS`` (which only keeps a strong GC ref, keyed by nothing)
# so a reconnecting client's ``load_conversation`` can find and AWAIT the turn
# still finishing for that conversation BEFORE reading disk. Without this the
# mobile woke, reloaded, and read an empty/stale history because a detached turn
# persists the reply only at END of turn (after load_conversation had already
# read disk). Keyed by conversation id and cleaned by the SAME done-callback as
# the set, so it can never grow unbounded nor keep a stale entry.
_DETACHED_BY_CONV: dict[str, "asyncio.Task"] = {}

# Upper bound (seconds) a ``load_conversation`` will wait for an in-flight
# detached turn to finish (and persist) before reading disk anyway. Best-effort:
# on timeout the turn keeps running and its reply shows up on a later reload.
# Bounded so a slow turn can never stall the receive loop indefinitely. Mirrors
# the existing 30s lower bound used elsewhere in this module.
_LOAD_CONV_AWAIT_TIMEOUT = 30.0

# WOR-62 regression fix (restores WOR-53 concurrency): background
# ``load_conversation`` tasks. When a reconnect's load_conversation finds a LIVE
# detached turn for that conversation, the reply isn't on disk yet (a detached
# turn persists only at END of turn), so we must wait for it — but NEVER inline
# on the serial receive loop. Awaiting there parks the loop, so a SUBSEQUENT
# load_conversation (e.g. switching to another chat) sits UNREAD in the socket
# buffer until the first turn finishes (the WOR-53 break). Instead the wait +
# disk read + history send run in a background task held here by a STRONG
# reference (asyncio only weakly tracks tasks, so without this it could be GC'd
# mid-flight). Each task removes itself via a done-callback, so the set stays
# bounded — same pattern as ``_DETACHED_TURNS`` / ``_detached_done``.
_DEFERRED_LOADS: set["asyncio.Task"] = set()

# WOR-103: process-wide in-flight chat turns, keyed by conversation id.
# ``streaming_tasks`` below is per WebSocket connection; this map is shared
# across connected devices so a same-conversation race is rejected instead of
# queued, while different conversations can still run concurrently.
_ACTIVE_TURNS_BY_CONV: dict[str, "asyncio.Future"] = {}
_ACTIVE_TURN_META_BY_CONV: dict[str, dict] = {}
_ACTIVE_CONV_BY_REQUEST: dict[str, str] = {}
_POST_TURN_MEDIA_TASKS: set["asyncio.Task"] = set()
_POST_TURN_MEDIA_BY_CONV: dict[str, "asyncio.Task"] = {}
_POST_TURN_MEDIA_META_BY_CONV: dict[str, dict] = {}
_PENDING_FINAL_PERSIST_TASKS: set["asyncio.Task"] = set()
_PENDING_SOURCE_CLEANUP_TASKS: set["asyncio.Task"] = set()
_TTS_MAX_PENDING_SEGMENTS = 8
_CONVERSATION_BUSY_MESSAGE = "OSIA is still working in this conversation."
_TURN_ID_PREFIX = "turn-"
_EVENT_ID_PREFIX = "event-"
_MESSAGE_ID_PREFIX = "message-"
_CLIENT_REQUEST_ID_MAX = 128
_TURN_PROGRESS_INTERVAL_SECONDS = 10.0
_TURN_EVENT_SEQUENCE_DIR = CONFIG_DIR / "turn_event_sequences"
_TURN_EVENT_SEQUENCE_LOCK = threading.RLock()
_DURABLE_TRANSPORT = get_durable_transport_store()


# Sentinel pushed onto an _OutboundChannel to tell its writer task to stop.
_WRITER_STOP = object()


class _OutboundChannel:
    """Single-writer outbound funnel for ONE WebSocket connection.

    Head-of-line fix: the per-connection handler is split into cooperating
    tasks (the receive loop, one streaming task per turn, the heartbeat). Any
    of them may want to emit a frame, but Starlette's ``WebSocket.send`` has no
    internal lock — two coroutines sending concurrently corrupt the connection.
    So NOBODY calls ``ws.send_*`` directly any more; every outbound frame is
    enqueued here and a single dedicated writer task (``_writer``) performs the
    real sends in FIFO order.

    The public surface mirrors the subset of ``WebSocket`` the handlers use
    (``send_json`` / ``send_text``), so a call site that previously held a
    ``ws`` for sending can hold an ``_OutboundChannel`` instead with no other
    change. Adjacent correlated ``response_chunk`` frames may wait for the
    bounded text-coalescing deadline; every other frame flushes them first.
    Queueing still never waits for the network, which keeps the receive loop
    free while a response streams.
    """

    __slots__ = (
        "_closed", "_durable_buffer", "_durable_client_id",
        "_durable_ready", "_queue", "_text_coalescer",
    )

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._durable_buffer: list[dict] = []
        self._durable_client_id = ""
        self._durable_ready = False
        self._text_coalescer = AssistantTextCoalescer(
            lambda frame: self._queue.put_nowait(("json", frame)),
            loop=asyncio.get_running_loop(),
        )

    async def send_json(self, obj) -> None:
        if self._closed:
            return
        if (
            isinstance(obj, dict)
            and obj.get("event_id")
            and obj.get("stream_id")
            and not self._durable_ready
        ):
            self._durable_buffer.append(dict(obj))
            return
        if self._text_coalescer.offer(obj):
            return
        await self._queue.put(("json", obj))

    def send_json_nowait(self, obj) -> None:
        if self._closed:
            return
        if (
            isinstance(obj, dict)
            and obj.get("event_id")
            and obj.get("stream_id")
            and not self._durable_ready
        ):
            self._durable_buffer.append(dict(obj))
            return
        if self._text_coalescer.offer(obj):
            return
        self._queue.put_nowait(("json", obj))

    def bind_durable_client(self, client_id: str) -> None:
        self._durable_client_id = str(client_id or "")

    async def send_durable_replay(self, frame: dict) -> None:
        """Queue an already-persisted envelope without re-entering buffering."""

        if self._closed:
            return
        self._text_coalescer.flush()
        await self._queue.put(("json", dict(frame)))

    async def activate_durable_delivery(self) -> None:
        """Flush events created while the reconnect suffix was being replayed."""

        if self._durable_ready:
            return
        self._durable_ready = True
        buffered = list(self._durable_buffer)
        self._durable_buffer.clear()
        buffered.sort(key=lambda frame: (
            float(frame.get("created_at") or 0.0),
            str(frame.get("stream_id") or ""),
            int(frame.get("sequence") or 0),
        ))
        for frame in buffered:
            await self.send_durable_replay(frame)

    async def send_text(self, text: str) -> None:
        if self._closed:
            return
        self._text_coalescer.flush()
        await self._queue.put(("text", text))

    async def close(self) -> None:
        """Signal the writer task to drain what's queued, then stop."""
        if self._closed:
            return
        self._text_coalescer.flush()
        self._closed = True
        await self._queue.put((_WRITER_STOP, None))

    def abort(self) -> None:
        """Clear pending coalesced text after the transport has failed."""
        self._closed = True
        self._text_coalescer.clear()


async def _writer(ws: WebSocket, channel: "_OutboundChannel") -> None:
    """Drain ``channel`` and perform the real WebSocket sends — the ONLY
    coroutine that touches ``ws.send_*`` for this connection.

    Exits when it pulls the stop sentinel (graceful close, after draining
    everything queued before it) or when a send raises (the socket is gone;
    the receive loop owns disconnect detection and teardown).
    """
    queue = channel._queue
    while True:
        kind, payload = await queue.get()
        if kind is _WRITER_STOP:
            return
        try:
            if kind == "json":
                await ws.send_json(payload)
            else:
                await ws.send_text(payload)
        except Exception as e:
            print(f"[OSIA Server] writer send failed, stopping writer: {e}")
            channel.abort()
            return


async def _broadcast_new_chat(exclude: "_OutboundChannel | None" = None) -> None:
    """Best-effort fan-out of {"type": "new_chat"} to all connected clients.

    Enqueues onto each connection's outbound channel, so the actual send is
    performed by that connection's own writer task (never from here).
    """
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json({"type": "new_chat"})
        except Exception:
            _WS_CLIENTS.discard(client)


async def _broadcast_conversation_snapshot(
    frame: dict,
    *,
    exclude: "_OutboundChannel | None" = None,
) -> None:
    """Queue one already-read canonical list snapshot to connected clients."""

    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


async def _broadcast_conversations(exclude: "_OutboundChannel | None" = None) -> None:
    """Best-effort fan-out of the refreshed conversation LIST to every connected
    client (WOR-76). Used after a rename so all devices' sidebars update.

    Like ``_broadcast_new_chat``, each frame is enqueued onto the connection's
    outbound channel — the real send is performed by that connection's own
    writer task (never from here). Unlike ``new_chat``, this carries the
    ``conversations`` list and does NOT clear the chat view; it only refreshes
    the sidebar.
    """
    frame = conversations.conversation_snapshot()
    print(
        "[OSIA DeleteTrace] origin=server phase=list_snapshot "
        f"source=broadcast snapshot_revision={int(frame.get('snapshot_revision') or 0)} "
        f"operation_id={str(frame.get('operation_id') or '')!r} "
        f"conversation_count={len(frame.get('items') or [])}",
        flush=True,
    )
    await _broadcast_conversation_snapshot(frame, exclude=exclude)


# ── MCP / Connectors server-side plumbing (WOR-44 Phase 4) ──────────────────
def _cron_notification_frame(payload: dict) -> dict:
    raw_status = str(payload.get("status") or "").strip().lower()
    status = "failed" if raw_status in {"failed", "error"} else "completed"
    created_at = str(payload.get("created_at") or "").strip()
    if not created_at:
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    task_id = str(payload.get("task_id") or "").strip()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    note_id = str(payload.get("id") or "").strip()
    if not note_id:
        note_id = f"cron_note_{secrets.token_urlsafe(12).rstrip('=')}"
    safe_category = str(payload.get("safe_category") or "scheduled_task").strip()
    if not re.fullmatch(r"[a-z0-9_.:-]{1,64}", safe_category):
        safe_category = "scheduled_task"
    correlation_digest = hashlib.sha256(note_id.encode("utf-8")).hexdigest()[:32]
    return {
        "type": "cron_notification",
        "id": note_id,
        "event_id": note_id,
        "event_type": "cron_notification",
        "request_id": f"cron_req_{correlation_digest}",
        "client_request_id": f"cron_req_{correlation_digest}",
        "turn_id": f"cron_turn_{correlation_digest}",
        "message_id": note_id,
        "sequence": 1,
        "terminal": True,
        "task_id": task_id,
        "title": "Scheduled task",
        "status": status,
        "safe_category": safe_category,
        "conversation_id": conversation_id,
        "created_at": created_at,
    }


def _remember_cron_notification(frame: dict) -> None:
    with _CRON_NOTIFICATIONS_LOCK:
        _CRON_NOTIFICATIONS.append(dict(frame))


def _pending_cron_notifications() -> list[dict]:
    with _CRON_NOTIFICATIONS_LOCK:
        return [dict(frame) for frame in _CRON_NOTIFICATIONS]


async def _broadcast_cron_notification(
    frame: dict,
    exclude: "_OutboundChannel | None" = None,
) -> None:
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


async def _send_pending_cron_notifications(channel: "_OutboundChannel") -> None:
    for frame in _pending_cron_notifications():
        try:
            await channel.send_json(frame)
        except Exception:
            return


def _conversation_history_frame(conv: dict) -> dict:
    conv_id = str(conv.get("id") or "")
    return {
        "type": "conversation_history",
        "id": conv_id,
        "conversation_id": conv_id,
        "title": conv.get("title"),
        "revision": int(conv.get("revision") or 0),
        "operation_id": str(conv.get("last_operation_id") or ""),
        "messages": _displayable_history(list(conv.get("messages") or [])),
    }


async def _send_conversation_frame(
    frame: dict,
    *,
    exclude: "_OutboundChannel | None" = None,
) -> None:
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


async def _broadcast_conversation_history(conv_id: str | None) -> None:
    if not conv_id:
        return
    conv = conversations.get_conversation(conv_id)
    if not conv:
        return
    await _send_conversation_frame(_conversation_history_frame(conv))


async def _broadcast_canonical_conversation_change(
    conv_id: str | None,
    *,
    revision: int = 0,
    operation_id: str = "",
    action: str = "upsert",
    exclude: "_OutboundChannel | None" = None,
) -> bool:
    """Broadcast one newest durable canonical state, coalescing stale signals."""

    global _CONVERSATION_BROADCAST_LOCK
    if not conv_id:
        return False
    if _CONVERSATION_BROADCAST_LOCK is None:
        _CONVERSATION_BROADCAST_LOCK = asyncio.Lock()
    async with _CONVERSATION_BROADCAST_LOCK:
        if action == "delete":
            snapshot = conversations.conversation_snapshot()
            if any(
                str(item.get("id") or "") == str(conv_id)
                for item in snapshot.get("items") or []
            ):
                print(
                    "[OSIA DeleteTrace] origin=PC phase=notification_rejected "
                    f"conversation_id={str(conv_id)!r} "
                    f"operation_id={str(operation_id or '')!r} "
                    f"reason=canonical_id_still_present "
                    f"snapshot_revision={int(snapshot.get('snapshot_revision') or 0)}",
                    flush=True,
                )
                return False
            _LAST_BROADCAST_REVISIONS.pop(conv_id, None)
            await _send_conversation_frame(
                {
                    "type": "conversation_deleted",
                    "id": conv_id,
                    "success": True,
                    "operation_id": str(operation_id or ""),
                    "snapshot_revision": int(
                        snapshot.get("snapshot_revision") or 0
                    ),
                },
                exclude=exclude,
            )
            print(
                "[OSIA DeleteTrace] origin=PC phase=ack_queued "
                f"conversation_id={str(conv_id)!r} "
                f"operation_id={str(operation_id or '')!r} "
                f"snapshot_revision={int(snapshot.get('snapshot_revision') or 0)} "
                "success=true",
                flush=True,
            )
            await _broadcast_conversation_snapshot(snapshot, exclude=exclude)
            return True

        conv = conversations.get_conversation(conv_id)
        if conv is None:
            return False
        current_revision = int(conv.get("revision") or 0)
        last_revision = _LAST_BROADCAST_REVISIONS.get(conv_id, -1)
        if current_revision <= last_revision:
            return False
        # A delayed desktop notification must never regress clients: its hint is
        # advisory, while the durable file is authoritative.
        if revision and int(revision) > current_revision:
            return False
        _LAST_BROADCAST_REVISIONS[conv_id] = current_revision
        await _send_conversation_frame(
            _conversation_history_frame(conv),
            exclude=exclude,
        )
        await _broadcast_conversations(exclude=exclude)
        return True


async def _broadcast_committed_receipt(
    conv_id: str | None,
    receipt: dict | None,
) -> bool:
    if not receipt or not receipt.get("committed"):
        return False
    return await _broadcast_canonical_conversation_change(
        conv_id,
        revision=int(receipt.get("revision") or 0),
        operation_id=str(receipt.get("operation_id") or ""),
    )


def _conversation_busy_frame(conv_id: str) -> dict:
    return {
        "type": "conversation_busy",
        "conversation_id": conv_id,
        "message": _CONVERSATION_BUSY_MESSAGE,
    }


def _conversation_busy_changed_frame(
    conv_id: str,
    busy: bool,
    turn_meta: dict | None = None,
) -> dict:
    if isinstance(turn_meta, dict):
        return _turn_event_frame(
            turn_meta,
            "conversation_busy_changed",
            payload={"busy": bool(busy)},
        )
    return {
        "type": "conversation_busy_changed",
        "conversation_id": conv_id,
        "busy": bool(busy),
    }


def _active_conversation_busy_ids() -> list[str]:
    busy: list[str] = []
    for conv_id, task in list(_ACTIVE_TURNS_BY_CONV.items()):
        if task.done():
            _ACTIVE_TURNS_BY_CONV.pop(conv_id, None)
            meta = _ACTIVE_TURN_META_BY_CONV.pop(conv_id, None)
            req_id = str((meta or {}).get("client_request_id") or "")
            if req_id:
                _ACTIVE_CONV_BY_REQUEST.pop(req_id, None)
            continue
        busy.append(conv_id)
    for conv_id, task in list(_POST_TURN_MEDIA_BY_CONV.items()):
        if task.done():
            _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
            _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
            continue
        if conv_id not in busy:
            busy.append(conv_id)
    return busy


def _is_conversation_busy(conv_id: str | None) -> bool:
    if not conv_id:
        return False
    media_task = _POST_TURN_MEDIA_BY_CONV.get(conv_id)
    if media_task is not None:
        if not media_task.done():
            return True
        _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
        _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
    task = _ACTIVE_TURNS_BY_CONV.get(conv_id)
    if task is None:
        return False
    if task.done():
        _ACTIVE_TURNS_BY_CONV.pop(conv_id, None)
        meta = _ACTIVE_TURN_META_BY_CONV.pop(conv_id, None)
        req_id = str((meta or {}).get("client_request_id") or "")
        if req_id:
            _ACTIVE_CONV_BY_REQUEST.pop(req_id, None)
        return False
    return True


def _clean_client_request_id(value) -> str | None:
    req_id = str(value or "").strip()
    if not req_id or len(req_id) > _CLIENT_REQUEST_ID_MAX:
        return None
    if not all(ch.isalnum() or ch in "_.:-" for ch in req_id):
        return None
    return req_id


_DURABLE_COMMAND_TYPES = frozenset({
    "text", "audio", "cancel_active_work",
})


def _durable_command_identity(msg_type: str, msg: dict) -> tuple[str, dict] | None:
    """Return a stable value-free command ID and its semantic binding."""

    if msg_type not in _DURABLE_COMMAND_TYPES or not isinstance(msg, dict):
        return None
    if msg_type in {"text", "audio"}:
        stable = _clean_client_request_id(
            msg.get("request_id") or msg.get("client_request_id")
        )
        if not stable:
            return None
        raw_payload = (
            str(msg.get("content") or "")
            if msg_type == "text" else str(msg.get("data") or "")
        )
        if not raw_payload:
            return None
        payload_hash = hashlib.sha256(
            raw_payload.encode("utf-8", errors="replace")
        ).hexdigest()
        directives = msg.get("turn_directives")
        directives_hash = (
            hashlib.sha256(json.dumps(
                directives,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if directives is not None else ""
        )
        identity = {
            "request_id": stable,
            "conversation_id": str(msg.get("conversation_id") or ""),
            "command_kind": msg_type,
        }
        binding = {
            **identity,
            "payload_hash": payload_hash,
            "directives_hash": directives_hash,
            "attachment_batch_id": str(
                msg.get("attachment_batch_id") or ""
            ),
            "format": str(msg.get("format") or "") if msg_type == "audio" else "",
        }
    elif msg_type == "cancel_active_work":
        if msg.get("schema_version") != 1:
            return None
        binding = {
            key: str(msg.get(key) or "")
            for key in (
                "scope", "conversation_id", "request_id", "turn_id", "run_id",
                "tool_attempt_id", "task_id", "operation_id",
            )
        }
        if not binding.get("scope") or not binding.get("run_id"):
            return None
        identity = dict(binding)
    else:
        binding = {
            key: msg.get(key)
            for key in (
                "request_id", "task_id", "run_id", "plan_revision",
                "authorization_revision", "operation_id", "action",
            )
            if key in msg
        }
        if not binding:
            return None
        identity = {
            key: binding[key]
            for key in (
                "request_id", "task_id", "run_id", "plan_revision",
                "authorization_revision", "operation_id",
            )
            if key in binding
        }
        if not identity:
            return None
    material = json.dumps(
        {"type": msg_type, "identity": identity},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "command_" + hashlib.sha256(material).hexdigest()[:48], binding


def _complete_durable_command_now(msg: dict, result: dict):
    command_id = str(msg.get("_durable_command_id") or "")
    execution_epoch = str(msg.get("_durable_command_epoch") or "")
    if not command_id or not execution_epoch:
        return None
    if result.get("state") == "turn_accepted":
        # Acceptance transfers authority to the canonical turn; it is not
        # command completion. The pre-write fence was persisted below.
        return None
    return _DURABLE_TRANSPORT.complete_command(
        command_id,
        execution_epoch=execution_epoch,
        result=result,
    )


async def _complete_durable_command(msg: dict, result: dict) -> None:
    try:
        await asyncio.to_thread(
            _complete_durable_command_now,
            msg,
            result,
        )
    except (RuntimeError, TypeError, ValueError):
        pass


def _cancelled_command_result(
    turn_binding: dict,
    terminal_result: dict,
    *,
    reason_code: str,
    superseded_by_turn_id: str = "",
    tool_attempt: dict | None = None,
) -> dict:
    """Build a factual durable cancellation result without assistant prose."""

    result = {
        "accepted": True,
        "state": "superseded" if superseded_by_turn_id else "cancelled",
        "reason_code": str(reason_code or "turn_cancelled")[:128],
        "terminal_state": "user_cancelled",
        "reservation_id": str(terminal_result.get("reservation_id") or ""),
        "assistant_final_persisted": False,
        **{
            key: str(turn_binding.get(key) or "")
            for key in (
                "conversation_id", "request_id", "turn_id", "run_id",
            )
        },
    }
    if superseded_by_turn_id:
        result["superseded_by_turn_id"] = str(superseded_by_turn_id)
    if isinstance(tool_attempt, dict):
        result["tool_attempt_id"] = str(
            tool_attempt.get("tool_attempt_id") or ""
        )
        result["tool_attempt_status"] = str(tool_attempt.get("status") or "")
        result["tool_attempt_outcome"] = str(
            tool_attempt.get("terminal_outcome") or ""
        )
        result["tool_dispatch_state"] = str(
            tool_attempt.get("dispatch_state") or ""
        )
    return result


def _reconcile_cancelled_websocket_commands(
    *, conversation_id: str = "",
) -> list[str]:
    """Settle accepted commands whose exact turn already has a cancel tombstone.

    The canonical TurnTerminality reservation is the authority. This path
    never resumes provider work and never manufactures an assistant final.
    """

    from osia.turn_terminality import load_persisted_terminal_result

    recovered: list[str] = []
    for record in _DURABLE_TRANSPORT.list_commands(state="running"):
        if record.get("command_type") not in {"text", "audio"}:
            continue
        command_id = str(record.get("command_id") or "")
        execution_epoch = str(record.get("execution_epoch") or "")
        execution_binding = record.get("execution_binding")
        command_binding = record.get("binding")
        if (
            not command_id
            or not execution_epoch
            or not isinstance(execution_binding, dict)
            or not isinstance(command_binding, dict)
        ):
            continue
        conv_id = str(execution_binding.get("conversation_id") or "")
        request_id = str(execution_binding.get("request_id") or "")
        turn_id = str(execution_binding.get("turn_id") or "")
        if (
            not conv_id
            or not request_id
            or not turn_id
            or (conversation_id and conversation_id != conv_id)
            or str(command_binding.get("conversation_id") or "") != conv_id
            or str(command_binding.get("request_id") or "") != request_id
        ):
            continue
        terminal = load_persisted_terminal_result(turn_id)
        if (
            not isinstance(terminal, dict)
            or terminal.get("terminal_state") != "user_cancelled"
            or terminal.get("final_text") != ""
        ):
            continue
        turn_binding = terminal.get("execution_binding")
        if (
            not isinstance(turn_binding, dict)
            or any(
                str(turn_binding.get(key) or "") != value
                for key, value in (
                    ("conversation_id", conv_id),
                    ("request_id", request_id),
                    ("turn_id", turn_id),
                    ("run_id", str(execution_binding.get("run_id") or turn_id)),
                )
            )
        ):
            continue
        run_id = str(turn_binding.get("run_id") or turn_id)
        try:
            stream = _DURABLE_TRANSPORT.get_stream(conv_id, run_id)
        except (RuntimeError, TypeError, ValueError):
            continue
        stream_binding = stream.get("binding")
        if (
            not isinstance(stream_binding, dict)
            or str(stream_binding.get("conversation_id") or "") != conv_id
            or str(stream_binding.get("run_id") or "") != run_id
            or stream.get("authority_state") not in {"active", "cancelled"}
        ):
            continue
        if stream.get("authority_state") != "cancelled":
            turn_meta = {
                **turn_binding,
                "client_request_id": request_id,
                "persisted": True,
                "_execution_epoch": str(stream.get("execution_epoch") or ""),
                "_terminal_reason_code": str(
                    terminal.get("cancellation_reason")
                    or terminal.get("reason_code")
                    or "authoritative_user_cancellation"
                ),
                "_superseded_by_turn_id": str(
                    terminal.get("superseded_by_turn_id") or ""
                ),
            }
            try:
                _turn_status_frame(turn_meta, "cancelled")
            except (RuntimeError, TypeError, ValueError):
                continue
        result = _cancelled_command_result(
            turn_binding,
            terminal,
            reason_code=str(
                terminal.get("cancellation_reason")
                or terminal.get("reason_code")
                or "authoritative_user_cancellation"
            ),
            superseded_by_turn_id=str(
                terminal.get("superseded_by_turn_id") or ""
            ),
        )
        try:
            claim = _DURABLE_TRANSPORT.complete_command(
                command_id,
                execution_epoch=execution_epoch,
                result=result,
            )
        except (RuntimeError, TypeError, ValueError):
            continue
        if (
            claim.disposition is InboxDisposition.DUPLICATE_COMPLETED
            and (claim.result or {}).get("state") in {"cancelled", "superseded"}
            and (claim.result or {}).get("terminal_state") == "user_cancelled"
        ):
            recovered.append(command_id)
    return recovered


async def _settle_websocket_command_owner(owner: dict | None) -> None:
    """Close the receive-loop ownership scope without replaying execution."""
    if not owner or owner.get("transferred"):
        return
    claim_task = owner["claim_task"]
    try:
        try:
            claim = await asyncio.shield(claim_task)
        except Exception:
            # Claim persistence can fail before returning its receipt. Settle
            # only our preassigned epoch; absence/another owner is not ours.
            try:
                await asyncio.to_thread(
                    _DURABLE_TRANSPORT.settle_unstarted_command,
                    owner["command_id"], execution_epoch=owner["execution_epoch"],
                )
            except ValueError:
                pass
            return
        if claim.disposition is not InboxDisposition.CLAIMED:
            return
        settled = await asyncio.to_thread(
            _DURABLE_TRANSPORT.settle_unstarted_command,
            claim.command_id, execution_epoch=claim.execution_epoch,
        )
        if not settled:
            # Acceptance might have committed before dispatch failed. Do not
            # classify this as safe pre-turn release or fresh execution.
            await asyncio.to_thread(
                _DURABLE_TRANSPORT.complete_command,
                claim.command_id, execution_epoch=claim.execution_epoch,
                result={"state": "outcome_unknown", "accepted": None,
                        "reason_code": "owner_exited_after_acceptance_fence"},
            )
    except Exception as exc:
        print("[WS command] ownership settlement failed: "
              f"{type(exc).__name__} command={owner['command_id']}", flush=True)
        raise


async def _websocket_owned_messages(ws, owners: list):
    async for raw_msg in ws.iter_text():
        yield raw_msg
        # Executed before the next receive even when dispatch used continue.
        if owners:
            await _settle_websocket_command_owner(owners[-1])
            owners.pop()


def _application_transport_coordination_enabled() -> bool:
    try:
        from osia.transport_coordinator import (
            application_transport_coordination_enabled,
        )

        return application_transport_coordination_enabled()
    except Exception:
        return False


def _legacy_request_id() -> str:
    return "legacy_req_" + secrets.token_hex(16)


def _new_turn_id() -> str:
    return _TURN_ID_PREFIX + secrets.token_hex(16)


@contextmanager
def _turn_event_sequence_process_lock(path: Path):
    """Serialize one sequence reservation across overlapping server processes."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _next_durable_turn_event_sequence(turn_meta: dict) -> int:
    """Reserve one monotonic sequence before a canonical event can be sent."""

    binding = {
        key: str(turn_meta.get(key) or "").strip()
        for key in (
            "conversation_id", "request_id", "turn_id", "run_id",
            "provider_id", "model_id", "effective_reasoning",
        )
    }
    if any(not value for value in binding.values()):
        raise ValueError("turn event sequence binding is incomplete")
    digest = hashlib.sha256(
        binding["turn_id"].encode("utf-8")
    ).hexdigest()
    path = _TURN_EVENT_SEQUENCE_DIR / f"{digest}.json"
    with _TURN_EVENT_SEQUENCE_LOCK, _turn_event_sequence_process_lock(path):
        stored_sequence = 0
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            persisted = None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("turn event sequence state is unreadable") from exc
        if isinstance(persisted, dict):
            persisted_binding = persisted.get("binding")
            if persisted_binding != binding:
                raise ValueError("turn event sequence binding changed")
            try:
                stored_sequence = max(0, int(persisted.get("sequence") or 0))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("turn event sequence state is invalid")
        current_sequence = max(
            0,
            int(turn_meta.get("_event_sequence") or 0),
            stored_sequence,
        )
        if current_sequence == 0:
            # Legacy turns did not persist their last sequence. A microsecond
            # epoch is above their small counters and remains within int64.
            current_sequence = time.time_ns() // 1_000
        sequence = current_sequence + 1
        state = {"binding": binding, "sequence": sequence}
        _TURN_EVENT_SEQUENCE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            path.name + f".{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_text(
            json.dumps(
                state,
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        turn_meta["_event_sequence"] = sequence
        return sequence


def _resolve_chat_route_binding(turn_directives=None) -> dict[str, str]:
    """Resolve the exact provider route before a canonical Chat turn is accepted."""

    from osia.agent import MODEL as DEFAULT_MODEL
    from osia.config import load_settings
    from osia.models import provider_for_model_cached
    from osia.reasoning import resolve_reasoning_option_id

    settings = load_settings()
    model_override = getattr(turn_directives, "model_override", None)
    if model_override is not None:
        provider_id = str(model_override.provider_id or "").strip().lower()
        model_id = str(model_override.model_id or "").strip()
    else:
        model_id = str(settings.get("selected_model") or DEFAULT_MODEL).strip()
        provider_id = str(
            settings.get("selected_model_provider") or ""
        ).strip().lower()
        if not provider_id and model_id:
            provider_id = str(
                provider_for_model_cached(model_id) or ""
            ).strip().lower()
    reasoning_override = getattr(turn_directives, "reasoning_override", None)
    effective_reasoning = str(
        reasoning_override
        if reasoning_override is not None else
        resolve_reasoning_option_id(provider_id, model_id, settings)
    ).strip().lower()
    binding = {
        "provider_id": provider_id,
        "model_id": model_id,
        "effective_reasoning": effective_reasoning,
    }
    if any(not value for value in binding.values()):
        raise RuntimeError("canonical Chat route binding is incomplete")
    return binding


def _new_turn_meta(
    conv_id: str,
    client_request_id: str | None,
    *,
    turn_directives=None,
) -> dict:
    request_id = client_request_id or ""
    turn_id = _new_turn_id()
    route = _resolve_chat_route_binding(turn_directives)
    from osia.turn_terminality import live_process_identity

    _owner_alive, owner_process_identity = live_process_identity(os.getpid())
    assistant_message_id = _MESSAGE_ID_PREFIX + secrets.token_hex(16)
    return {
        "conversation_id": conv_id,
        "request_id": request_id,
        "client_request_id": request_id,
        "turn_id": turn_id,
        "run_id": turn_id,
        **route,
        "user_message_id": _MESSAGE_ID_PREFIX + secrets.token_hex(16),
        "assistant_message_id": assistant_message_id,
        "_event_sequence": 0,
        "persisted": False,
        "_pinned_provider_id": route["provider_id"],
        "_pinned_model_id": route["model_id"],
        "_pinned_effective_reasoning": route["effective_reasoning"],
        "_route_bound_at_accept": True,
        "_execution_owner": "server",
        "_execution_owner_pid": os.getpid(),
        "_execution_owner_process_identity": owner_process_identity,
        "_post_turn_media_lease_id": f"media_{assistant_message_id}",
        "_post_turn_media_owner_pid": os.getpid(),
        "_post_turn_media_owner_process_identity": owner_process_identity,
    }


def _bind_pre_agent_cancellation_authority(turn_meta: dict) -> None:
    """Persist an exact Stop even before the per-turn Agent is constructed."""

    binding = {
        key: str(turn_meta.get(key) or "")
        for key in (
            "conversation_id", "request_id", "turn_id", "run_id",
            "provider_id", "model_id", "effective_reasoning",
        )
    }
    objective = str(turn_meta.get("_terminality_objective") or "").strip()
    if any(not value for value in binding.values()) or not objective:
        return

    def _request(
        turn_id: str,
        *,
        effect_possible: bool = False,
        reason_code: str = "",
        superseded_by_turn_id: str = "",
    ) -> bool:
        if str(turn_id or "") != binding["turn_id"]:
            return False
        from osia.turn_terminality import TurnTerminalityController

        controller = TurnTerminalityController(
            turn_id=binding["turn_id"],
            objective=objective,
            execution_required=True,
        )
        existing_binding = controller.snapshot().get("execution_binding")
        if existing_binding:
            if existing_binding != binding:
                return False
        else:
            controller.set_execution_binding(binding)
        return controller.try_request_cancellation(
            effect_possible=bool(effect_possible),
            reason_code=reason_code,
            superseded_by_turn_id=superseded_by_turn_id,
        )

    turn_meta["_terminality_cancel_intent"] = _request
    # Preserve the fast durable CAS separately from the Agent callback that
    # also asks its provider/tool worker to stop.
    turn_meta["_terminality_cancel_authority"] = _request
    turn_meta["_terminality_cancel"] = _request


def _turn_fields(turn_meta: dict | None) -> dict:
    if not isinstance(turn_meta, dict):
        return {}
    fields = {}
    req_id = str(
        turn_meta.get("request_id")
        or turn_meta.get("client_request_id")
        or ""
    )
    turn_id = str(turn_meta.get("turn_id") or "")
    run_id = str(turn_meta.get("run_id") or turn_id)
    if req_id:
        fields["request_id"] = req_id
        fields["client_request_id"] = req_id
    if turn_id:
        fields["turn_id"] = turn_id
    if run_id:
        fields["run_id"] = run_id
    route = {
        key: str(turn_meta.get(key) or "").strip()
        for key in ("provider_id", "model_id", "effective_reasoning")
    }
    if all(route.values()):
        fields.update(route)
    language = normalize_locale(turn_meta.get("language"))
    if language:
        fields["language"] = language
    return fields


def _turn_event_frame(
    turn_meta: dict,
    event_type: str,
    *,
    message_id: str = "",
    terminal: bool = False,
    cache_key: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Create one immutable, monotonically sequenced application event."""

    if cache_key:
        cached = turn_meta.get(cache_key)
        if isinstance(cached, dict):
            return dict(cached)
    conversation_id = str(turn_meta.get("conversation_id") or "")
    request_id = str(
        turn_meta.get("request_id")
        or turn_meta.get("client_request_id")
        or ""
    )
    turn_id = str(turn_meta.get("turn_id") or "")
    run_id = str(turn_meta.get("run_id") or turn_id)
    provider_id = str(turn_meta.get("provider_id") or "").strip()
    model_id = str(turn_meta.get("model_id") or "").strip()
    effective_reasoning = str(
        turn_meta.get("effective_reasoning") or ""
    ).strip()
    if not all((
        conversation_id, request_id, turn_id, run_id,
        provider_id, model_id, effective_reasoning,
    )):
        raise ValueError("turn event correlation or route binding is incomplete")
    event_payload = {
        "provider_id": provider_id,
        "model_id": model_id,
        "effective_reasoning": effective_reasoning,
    }
    language = normalize_locale(turn_meta.get("language"))
    if language:
        event_payload["language"] = language
    if message_id:
        event_payload["message_id"] = message_id
    if payload:
        event_payload.update(payload)
    # Payloads are presentation data only; they cannot replace the immutable
    # execution route accepted for this turn.
    event_payload.update({
        "provider_id": provider_id,
        "model_id": model_id,
        "effective_reasoning": effective_reasoning,
    })
    envelope = _DURABLE_TRANSPORT.append_event(
        conversation_id=conversation_id,
        request_id=request_id,
        turn_id=turn_id,
        run_id=run_id,
        event_type=event_type,
        payload=event_payload,
        attempt_id=str(event_payload.get("tool_attempt_id") or ""),
        execution_epoch=str(turn_meta.get("_execution_epoch") or ""),
        terminal=bool(terminal),
        idempotency_key=(
            f"{turn_id}:{cache_key}" if cache_key else ""
        ),
        state_updates={
            "last_event_type": event_type,
            "terminal": bool(terminal),
            "status": str(event_payload.get("status") or ""),
        },
    )
    frame = envelope.to_frame()
    if event_type == "audio" and isinstance(event_payload.get("data"), str):
        # Binary media is live-only.  The durable envelope records ordering and
        # metadata, never base64; conversation/media hydration owns reconnect.
        frame["data"] = event_payload["data"]
    turn_meta["_event_sequence"] = envelope.sequence
    turn_meta["_execution_epoch"] = envelope.execution_epoch
    if cache_key:
        turn_meta[cache_key] = dict(frame)
    return frame


def _turn_accepted_frame(turn_meta: dict) -> dict:
    return _turn_event_frame(
        turn_meta,
        "turn_accepted",
        message_id=str(turn_meta.get("user_message_id") or ""),
        cache_key="_accepted_frame",
        payload={"persisted": bool(turn_meta.get("persisted"))},
    )


def _turn_status_frame(turn_meta: dict, status: str) -> dict:
    terminal = status in {"done", "error", "cancelled", "canceled", "rejected"}
    payload = {"status": status, "persisted": bool(turn_meta.get("persisted"))}
    tool_attempt_id = str(turn_meta.get("_tool_attempt_id") or "")
    if tool_attempt_id:
        payload["tool_attempt_id"] = tool_attempt_id
    reason_code = str(turn_meta.get("_terminal_reason_code") or "")
    if reason_code:
        payload["reason_code"] = reason_code[:160]
    superseded_by = str(turn_meta.get("_superseded_by_turn_id") or "")
    if superseded_by:
        payload["superseded_by_turn_id"] = superseded_by[:256]
    return _turn_event_frame(
        turn_meta,
        "turn_status",
        message_id=str(turn_meta.get("assistant_message_id") or ""),
        terminal=terminal,
        cache_key=f"_terminal_frame_{status}" if terminal else None,
        payload=payload,
    )


def _terminal_status_from_result(result: dict | None) -> str:
    terminal_state = str((result or {}).get("terminal_state") or "")
    if terminal_state == "success_verified":
        return "done"
    if terminal_state == "user_cancelled":
        return "cancelled"
    return "error"


def _turn_terminal_frame(turn_meta: dict, result: dict | None) -> dict:
    frame = _turn_status_frame(turn_meta, _terminal_status_from_result(result))
    reason_code = str((result or {}).get("reason_code") or "")[:160]
    if reason_code:
        frame["reason_code"] = reason_code
    return frame


async def _broadcast_turn_status(
    turn_meta: dict | None,
    status: str,
    *,
    exclude: "_OutboundChannel | None" = None,
    frame: dict | None = None,
) -> None:
    if not isinstance(turn_meta, dict):
        return
    if frame is None:
        frame = _turn_status_frame(turn_meta, status)
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


async def _broadcast_execution_trace(
    frame: dict,
    *,
    primary: "_OutboundChannel | None" = None,
) -> None:
    """Deliver one optional standalone trace frame to each live Mobile mirror."""

    targets = []
    if primary is not None:
        targets.append(primary)
    targets.extend(client for client in list(_WS_CLIENTS) if client is not primary)
    for client in targets:
        try:
            await client.send_json(frame)
        except Exception:
            if client is not primary:
                _WS_CLIENTS.discard(client)


async def _turn_progress_loop(
    channel: "_OutboundChannel",
    turn_meta: dict | None,
    *,
    interval: float = _TURN_PROGRESS_INTERVAL_SECONDS,
) -> None:
    if not isinstance(turn_meta, dict):
        return
    try:
        while True:
            await asyncio.sleep(interval)
            frame = _turn_status_frame(turn_meta, "working")
            await channel.send_json(frame)
            await _broadcast_turn_status(
                turn_meta,
                "working",
                exclude=channel,
                frame=frame,
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[OSIA Server] turn progress loop stopped: {e}")


async def _cancel_turn_progress_task(task: "asyncio.Task | None") -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


_TURN_INPUT_IDENTITY_VERSION = "turn-input-v1"


def _turn_input_identity(
    base_content: str,
    *,
    kind: str,
    manifest: list[dict] | None = None,
) -> dict[str, str]:
    """Return deterministic base/full hashes without local paths or random ids."""

    stable_kind = str(kind or "chat").strip().lower()
    if not stable_kind:
        raise ValueError("turn input kind is required")
    base_hash = hashlib.sha256(str(base_content).encode("utf-8")).hexdigest()
    manifest_hash = ""
    if manifest:
        canonical_manifest = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_hash = hashlib.sha256(
            canonical_manifest.encode("utf-8")
        ).hexdigest()
    full_material = "\0".join((
        _TURN_INPUT_IDENTITY_VERSION,
        stable_kind,
        base_hash,
        manifest_hash,
    ))
    return {
        "client_content_hash": hashlib.sha256(
            full_material.encode("utf-8")
        ).hexdigest(),
        "client_base_content_hash": base_hash,
        "attachment_manifest_hash": manifest_hash,
        "input_identity_version": _TURN_INPUT_IDENTITY_VERSION,
        "input_kind": stable_kind,
    }


def _apply_turn_input_identity(turn_meta: dict, identity: dict[str, str]) -> None:
    for key in (
        "client_content_hash",
        "client_base_content_hash",
        "attachment_manifest_hash",
        "input_identity_version",
        "input_kind",
    ):
        turn_meta[key] = str(identity.get(key) or "")


def _assert_request_input_identity(
    record: dict,
    *,
    expected_user_content=None,
    expected_identity: dict[str, str] | None = None,
    legacy_client_content_hash: str = "",
    legacy_exact_user_content: str = "",
) -> None:
    """Reject a request-id replay whose exact supplied input has changed."""

    if isinstance(expected_identity, dict):
        expected_base_hash = str(
            expected_identity.get("client_base_content_hash") or ""
        )
        expected_full_hash = str(
            expected_identity.get("client_content_hash") or ""
        )
        expected_manifest_hash = str(
            expected_identity.get("attachment_manifest_hash") or ""
        )
        expected_version = str(
            expected_identity.get("input_identity_version") or ""
        )
        expected_kind = str(expected_identity.get("input_kind") or "")
    elif expected_user_content is not None:
        expected_base_hash = hashlib.sha256(
            str(expected_user_content).encode("utf-8")
        ).hexdigest()
        expected_full_hash = expected_base_hash
        expected_manifest_hash = ""
        expected_version = ""
        expected_kind = ""
    else:
        return
    if not expected_base_hash or not expected_full_hash:
        raise ValueError("request input identity is incomplete")

    stored_full_hash = str(record.get("client_content_hash") or "")
    stored_base_hash = str(record.get("client_base_content_hash") or "")
    stored_manifest_hash = str(record.get("attachment_manifest_hash") or "")
    stored_version = str(record.get("input_identity_version") or "")
    stored_kind = str(record.get("input_kind") or "")
    stored_user_content = str(
        record.get("user_content")
        or record.get("persisted_user_content")
        or record.get("_original_user_text")
        or ""
    )
    legacy_content_matches = bool(
        legacy_exact_user_content
        and stored_user_content == legacy_exact_user_content
    )
    persisted_attachments = (
        record.get("attachments")
        or record.get("attachment_metadata")
        or []
    )
    if (
        not legacy_content_matches
        and legacy_exact_user_content
        and isinstance(persisted_attachments, list)
        and persisted_attachments
    ):
        ordered_attachments = sorted(
            (
                item for item in persisted_attachments
                if isinstance(item, dict)
            ),
            key=lambda item: (
                item.get("logical_order")
                if type(item.get("logical_order")) is int else 0
            ),
        )
        display_names = ", ".join(
            str(item.get("display_name") or "")
            for item in ordered_attachments
            if str(item.get("display_name") or "")
        )
        legacy_content_matches = bool(
            display_names
            and stored_user_content
            == f"📎 {display_names}\n{legacy_exact_user_content}".strip()
        )
    if expected_manifest_hash:
        exact = bool(
            stored_full_hash == expected_full_hash
            and stored_base_hash == expected_base_hash
            and stored_manifest_hash == expected_manifest_hash
            and stored_version == expected_version
            and stored_kind == expected_kind
        )
        legacy_exact = bool(
            legacy_client_content_hash
            and not stored_version
            and stored_full_hash == legacy_client_content_hash
            and legacy_content_matches
        )
        if not exact and not legacy_exact:
            raise ValueError("request id was already used with different content")
        return

    if (
        stored_version == expected_version
        and stored_base_hash == expected_base_hash
        and (
            stored_kind == expected_kind
            or (stored_kind, expected_kind) in {
                ("legacy-attachments", "text"),
                ("legacy-audio-attachments", "audio"),
            }
        )
    ):
        return
    legacy_exact = bool(
        legacy_client_content_hash
        and not stored_version
        and not stored_kind
        and stored_full_hash == legacy_client_content_hash
        and legacy_content_matches
    )
    if not legacy_exact:
        raise ValueError("request id was already used with different content")


def _legacy_audio_replay_record(
    record: dict | None,
    legacy_audio_hash: str,
    *,
    require_complete: bool,
) -> bool:
    """Recognize only the exact pre-version, attachment-free audio shape."""

    if not isinstance(record, dict) or not legacy_audio_hash:
        return False
    if any(
        str(record.get(key) or "")
        for key in (
            "client_base_content_hash",
            "attachment_manifest_hash",
            "input_identity_version",
            "input_kind",
        )
    ):
        return False
    if str(record.get("client_content_hash") or "") != legacy_audio_hash:
        return False
    attachments = record.get("attachments") or record.get("attachment_metadata")
    if attachments:
        return False
    stored_transcript = str(
        record.get("user_content")
        or record.get("persisted_user_content")
        or ""
    )
    if (
        not stored_transcript.strip()
        or stored_transcript.startswith((
            "📎 ",
            "[Document/media attachments available",
            "[Document image available",
        ))
    ):
        return False
    if not require_complete:
        return True
    try:
        response_sequence = int(record.get("response_sequence") or 0)
        terminal_sequence = int(record.get("terminal_sequence") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(record.get("assistant_content"), str)
        and str(record.get("assistant_content") or "").strip()
        and str(record.get("assistant_message_id") or "")
        and str(record.get("response_event_id") or "")
        and response_sequence > 0
        and str(record.get("terminal_event_id") or "")
        and terminal_sequence > response_sequence
    )


def _transactional_attachment_manifest(batch) -> list[dict]:
    return [
        {
            "logical_order": int(item.logical_order),
            "display_name": str(item.display_name),
            "byte_size": int(item.bytes_written),
            "sha256": str(item.declared_sha256).lower(),
            "category": str(item.detected_category).lower(),
            "mime": str(item.detected_mime).lower(),
        }
        for item in sorted(
            batch.files.values(), key=lambda value: value.logical_order,
        )
    ]


def _legacy_attachment_manifest(
    paths: list[str],
    display_names: dict[str, str] | None = None,
) -> list[dict]:
    manifest = []
    names = display_names or {}
    for logical_order, raw_path in enumerate(paths):
        path = Path(raw_path)
        data = path.read_bytes()
        from osia.attachments import detect_image_mime

        image_mime = detect_image_mime(data) or _image_mime_for(path)
        manifest.append({
            "logical_order": logical_order,
            "display_name": str(names.get(str(path)) or path.suffix.lower()),
            "byte_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "category": "image" if image_mime else "file",
            "mime": image_mime or "application/octet-stream",
        })
    return manifest


async def _send_existing_turn_status(
    channel: "_OutboundChannel",
    conv_id: str,
    client_request_id: str | None,
    *,
    expected_user_content=None,
    expected_identity: dict[str, str] | None = None,
    legacy_client_content_hash: str = "",
    legacy_exact_user_content: str = "",
) -> bool:
    if not client_request_id or not _is_conversation_busy(conv_id):
        return False
    meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
    if (
        not meta
        or meta.get("persisted") is not True
        or meta.get("client_request_id") != client_request_id
    ):
        return False
    _assert_request_input_identity(
        meta,
        expected_user_content=expected_user_content,
        expected_identity=expected_identity,
        legacy_client_content_hash=legacy_client_content_hash,
        legacy_exact_user_content=legacy_exact_user_content,
    )
    await channel.send_json(_turn_accepted_frame(meta))
    await channel.send_json(
        _turn_status_frame(meta, _active_turn_working_status(conv_id))
    )
    return True


async def _send_existing_turn_status_for_request(
    channel: "_OutboundChannel",
    client_request_id: str | None,
    *,
    expected_user_content=None,
    expected_identity: dict[str, str] | None = None,
    legacy_client_content_hash: str = "",
    legacy_exact_user_content: str = "",
) -> bool:
    if not client_request_id:
        return False
    conv_id = _ACTIVE_CONV_BY_REQUEST.get(client_request_id)
    if not conv_id:
        return False
    return await _send_existing_turn_status(
        channel,
        conv_id,
        client_request_id,
        expected_user_content=expected_user_content,
        expected_identity=expected_identity,
        legacy_client_content_hash=legacy_client_content_hash,
        legacy_exact_user_content=legacy_exact_user_content,
    )


_RESUME_VERIFICATION_SUFFIXES = frozenset({
    (),
    ("action",),
    ("and", "verify"),
    ("and", "verify", "current", "action"),
    ("and", "verify", "the", "current", "action"),
    ("checking", "current", "action"),
    ("checking", "the", "current", "action"),
    ("et", "verifie"),
    ("et", "verifier"),
    ("et", "verifie", "l", "action", "en", "cours"),
    ("et", "verifier", "l", "action", "en", "cours"),
    ("la", "verification", "de", "l", "action", "en", "cours"),
})
_RESUME_VERBS = frozenset({
    "continue", "continuer", "relaunch", "relance", "relancer",
    "reprendre", "reprends", "resume",
})
_WAITING_STATUS_COMMANDS = frozenset({
    ("check",),
    ("check", "action"),
    ("check", "current", "action"),
    ("check", "status"),
    ("check", "current", "status"),
    ("status",),
    ("action", "status"),
    ("statut",),
    ("verifie", "le", "statut"),
    ("verifier", "le", "statut"),
    ("verifie", "l", "action", "en", "cours"),
    ("verifier", "l", "action", "en", "cours"),
})


def _active_turn_working_status(conv_id: str) -> str:
    """Return one additive non-terminal state for reconnect/status recovery."""

    meta = (
        _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        or _POST_TURN_MEDIA_META_BY_CONV.get(conv_id)
        or {}
    )
    remembered = str(meta.get("_tool_attempt_status") or "")
    try:
        from osia.tool_attempts import get_tool_attempt_store

        attempt = get_tool_attempt_store().find_compatible_suspended_turn(conv_id)
    except Exception:
        attempt = None
    if isinstance(attempt, dict):
        status = str(attempt.get("status") or "")
        if status == "waiting_verification":
            return "waiting_verification"
        if status == "accepted":
            return "action_starting"
        if status == "running":
            return "action_in_progress"
        if status == "pending":
            return "waiting_tool_result"
        if status == "cancellation_requested":
            return "cancellation_requested"
    if remembered in {
        "waiting_tool_result", "waiting_verification", "verifying",
        "resuming", "cancellation_requested", "action_in_progress",
        "action_running",
        "action_starting", "blocked_unknown_effect", "cancelled_proven",
    }:
        from osia.chat_routing import project_chat_tool_attempt_status

        return project_chat_tool_attempt_status(remembered)
    return "working"


async def _send_recovered_turn_state(
    channel: "_OutboundChannel", conv_id: str,
) -> bool:
    """Rebind a connected client to an existing turn with fresh event ids."""

    meta = (
        _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        or _POST_TURN_MEDIA_META_BY_CONV.get(conv_id)
    )
    if (
        not isinstance(meta, dict)
        or meta.get("persisted") is not True
        or not _is_conversation_busy(conv_id)
    ):
        return False
    accepted = _turn_event_frame(
        meta,
        "turn_accepted",
        message_id=str(meta.get("user_message_id") or ""),
        payload={
            "persisted": bool(meta.get("persisted")),
            "recovered": True,
        },
    )
    status = _turn_status_frame(meta, _active_turn_working_status(conv_id))
    status["recovered"] = True
    busy = _conversation_busy_changed_frame(conv_id, True, meta)
    busy["recovered"] = True
    await channel.send_json(accepted)
    await channel.send_json(status)
    await channel.send_json(busy)
    return True


def _normalized_waiting_command_tokens(text: str) -> tuple[str, ...]:
    raw = str(text or "").strip()
    if not raw or len(raw) > 160:
        return ()
    folded = unicodedata.normalize("NFKD", raw.casefold())
    ascii_text = "".join(
        char for char in folded if not unicodedata.combining(char)
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()
    tokens = tuple(normalized.split())
    return tokens if len(tokens) <= 8 else ()


def _is_resume_reconciliation_command(text: str) -> bool:
    """Match only an anchored resume verb plus a bounded verification suffix."""

    original = unicodedata.normalize("NFC", str(text or "")).casefold().lstrip()
    if re.match(r"^r(?:ésume|ésumé)(?:\W|$)", original):
        # French "résume/résumé" means summarize/summary, not resume work.
        return False
    tokens = _normalized_waiting_command_tokens(text)
    return bool(
        tokens
        and tokens[0] in _RESUME_VERBS
        and tokens[1:] in _RESUME_VERIFICATION_SUFFIXES
    )


async def _handle_waiting_tool_command(
    channel: "_OutboundChannel",
    conv_id: str,
    text: str,
) -> bool:
    """Resolve control prose against a suspended attempt without a new turn."""

    from osia.tool_attempts import get_tool_attempt_coordinator

    tokens = _normalized_waiting_command_tokens(text)
    normalized = " ".join(tokens)
    cancel_requested = normalized in {
        "cancel", "cancel action", "stop action", "annule", "annuler",
        "annule l action", "arrete l action",
    }
    coordinator = get_tool_attempt_coordinator()
    suspended_attempt = coordinator.store.find_compatible_suspended_turn(conv_id)
    if suspended_attempt is None:
        meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        if (
            cancel_requested
            and isinstance(meta, dict)
            and _is_conversation_busy(conv_id)
        ):
            # The provider is still planning and no tool receipt exists yet.
            # Carry one bounded intent to the first accepted tool boundary;
            # this never creates a second turn or dispatches an action.
            terminality_cancel = meta.get("_terminality_cancel")
            accepted = False
            if callable(terminality_cancel):
                try:
                    accepted = terminality_cancel(
                        str(meta.get("turn_id") or ""),
                        effect_possible=False,
                    )
                except Exception:
                    accepted = False
            if accepted is not True:
                await _send_cancellation_rejection(
                    channel,
                    "chat",
                    "cancellation_unavailable",
                    **{
                        key: str(meta.get(key) or "")
                        for key in (
                            "conversation_id", "request_id",
                            "turn_id", "run_id",
                        )
                    },
                )
                return True
            meta["_tool_cancel_requested"] = True
            await channel.send_json(
                _turn_status_frame(meta, "cancellation_requested")
            )
            return True
        return False
    if cancel_requested:
        meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        if (
            not isinstance(meta, dict)
            or not _is_conversation_busy(conv_id)
            or not _tool_attempt_matches_turn_meta(
                suspended_attempt, meta, conversation_id=conv_id,
            )
        ):
            await _send_cancellation_rejection(
                channel,
                "chat",
                "active_receipt_mismatch",
                conversation_id=conv_id,
                **(
                    {
                        key: str(meta.get(key) or "")
                        for key in ("request_id", "turn_id", "run_id")
                    }
                    if isinstance(meta, dict) else {}
                ),
            )
            return True
        terminality_cancel = (
            meta.get("_terminality_cancel")
            if isinstance(meta, dict) else None
        )
        accepted = False
        if callable(terminality_cancel):
            try:
                accepted = terminality_cancel(
                    str(meta.get("turn_id") or ""),
                    effect_possible=True,
                )
            except Exception:
                accepted = False
        if accepted is not True:
            await _send_cancellation_rejection(
                channel,
                "chat",
                "cancellation_unavailable",
                **(
                    {
                        key: str(meta.get(key) or "")
                        for key in (
                            "conversation_id", "request_id",
                            "turn_id", "run_id",
                        )
                    }
                    if isinstance(meta, dict) else
                    {"conversation_id": conv_id}
                ),
            )
            return True
        meta["_tool_cancel_requested"] = True
        try:
            handled = coordinator.request_cancellation(conv_id)
        except Exception:
            handled = False
        status = "cancellation_requested"
        if not handled:
            # The terminality Stop CAS already won. Consume this control
            # command and retain the durable intent for exact recovery.
            await channel.send_json(_turn_status_frame(meta, status))
            return True
    elif _is_resume_reconciliation_command(text):
        handled = coordinator.request_reconciliation(conv_id)
        status = "verifying"
    elif tokens in _WAITING_STATUS_COMMANDS:
        handled = True
        status = _active_turn_working_status(conv_id)
    else:
        # Only recognized waiting-turn controls belong to this bounded path.
        # Ordinary text is a new user turn and must reach canonical turn
        # acceptance, where it supersedes this conversation's active turn.
        return False
    if handled:
        meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        if isinstance(meta, dict):
            await channel.send_json(_turn_status_frame(meta, status))
    return handled


def _request_exact_tool_attempt_cancellation(attempt_id: str) -> bool:
    """Cancel the accepted receipt named by the tool-start callback."""

    if not attempt_id:
        return False
    try:
        from osia.tool_attempts import (
            ToolAttemptStatus,
            get_tool_attempt_store,
        )

        requested = get_tool_attempt_store().request_cancellation(attempt_id)
        return ToolAttemptStatus(requested["status"]) in {
            ToolAttemptStatus.CANCELLATION_REQUESTED,
            ToolAttemptStatus.CANCELLED_PROVEN,
        }
    except Exception:
        return False


def _tool_attempt_matches_turn_meta(
    attempt: dict,
    turn_meta: dict,
    *,
    conversation_id: str = "",
) -> bool:
    """Require one durable tool receipt to belong to this exact Chat turn."""

    if not isinstance(attempt, dict) or not isinstance(turn_meta, dict):
        return False
    expected = {
        "conversation_id": str(
            turn_meta.get("conversation_id") or conversation_id or ""
        ),
        "request_id": str(
            turn_meta.get("request_id")
            or turn_meta.get("client_request_id")
            or ""
        ),
        "turn_id": str(turn_meta.get("turn_id") or ""),
        "provider_id": str(turn_meta.get("provider_id") or ""),
        "model_id": str(turn_meta.get("model_id") or ""),
        "final_response_message_id": str(
            turn_meta.get("assistant_message_id") or ""
        ),
    }
    remembered_attempt_id = str(
        turn_meta.get("_tool_attempt_id")
        or turn_meta.get("_existing_tool_attempt_id")
        or ""
    )
    return bool(
        all(expected.values())
        and (not conversation_id or expected["conversation_id"] == conversation_id)
        and str(attempt.get("owner_scope") or "chat") == "chat"
        and all(
            str(attempt.get(key) or "") == value
            for key, value in expected.items()
        )
        and (
            not remembered_attempt_id
            or str(attempt.get("tool_attempt_id") or "")
            == remembered_attempt_id
        )
    )


def _apply_queued_tool_cancellation(turn_meta: dict, attempt_id: str) -> bool:
    """Apply one turn-scoped cancellation intent without consuming it."""

    return bool(
        isinstance(turn_meta, dict)
        and turn_meta.get("_tool_cancel_requested") is True
        and _request_exact_tool_attempt_cancellation(attempt_id)
    )


def _clean_cancellation_target_id(value, *, maximum: int = 256) -> str:
    target = str(value or "").strip()
    if not target or len(target) > maximum:
        return ""
    if not all(char.isalnum() or char in "_.:-" for char in target):
        return ""
    return target


async def _send_cancellation_rejection(
    channel: "_OutboundChannel",
    scope: str,
    reason_code: str,
    **target,
) -> None:
    await channel.send_json({
        "type": "cancellation_control",
        "schema_version": 1,
        "scope": scope,
        "state": "rejected",
        "reason_code": reason_code,
        **{key: value for key, value in target.items() if value},
    })


async def _propagate_committed_turn_cancellation(
    callback, turn_id: str, *, effect_possible: bool,
) -> None:
    """Run slower tool/provider shutdown after the durable terminal projection."""

    try:
        await asyncio.to_thread(
            callback,
            turn_id,
            effect_possible=bool(effect_possible),
        )
    except Exception:
        # The durable tombstone and generation fence already won. Provider
        # shutdown is best-effort and cannot reactivate this execution owner.
        pass


def _durable_cancelled_chat_terminal_frame(
    *,
    conversation_id: str,
    request_id: str,
    turn_id: str,
    run_id: str,
    tool_attempt_id: str = "",
) -> dict | None:
    """Recover one exact historical Stop without reviving active ownership."""

    try:
        stream = _DURABLE_TRANSPORT.get_stream(conversation_id, run_id)
        if stream.get("authority_state") != "cancelled":
            return None
        frames = _DURABLE_TRANSPORT.replay(
            conversation_id=conversation_id,
            run_id=run_id,
        )
    except Exception:
        return None
    for frame in reversed(frames):
        if (
            frame.get("terminal") is True
            and frame.get("event_type") == "turn_status"
            and frame.get("status") in {"cancelled", "canceled"}
            and str(frame.get("conversation_id") or "") == conversation_id
            and str(frame.get("request_id") or "") == request_id
            and str(frame.get("turn_id") or "") == turn_id
            and str(frame.get("run_id") or "") == run_id
            and (
                not tool_attempt_id
                or str(frame.get("tool_attempt_id") or "") == tool_attempt_id
            )
        ):
            return dict(frame)
    return None


def _tool_attempt_may_have_effect(attempt: dict | None) -> bool:
    """Use the durable attempt boundary, never parent-turn cancellation, as fact."""

    if not isinstance(attempt, dict):
        return False
    from osia.tool_attempts import TERMINAL_STATUSES, ToolAttemptStatus

    try:
        status = ToolAttemptStatus(attempt.get("status"))
    except (TypeError, ValueError):
        return True
    action_truth = attempt.get("action_truth")
    effect_truth = (
        str(action_truth.get("effect_truth") or "")
        if isinstance(action_truth, dict) else ""
    )
    if status in TERMINAL_STATUSES:
        return bool(
            status is ToolAttemptStatus.UNKNOWN_AFTER_START
            or effect_truth == "possible"
        )
    dispatch_state = str(attempt.get("dispatch_state") or "")
    if (
        dispatch_state in {"not_dispatched", "cancelled_before_effect"}
        and not attempt.get("owner_dispatch_claimed_at")
    ):
        return False
    return bool(
        attempt.get("owner_dispatch_claimed_at")
        or dispatch_state in {
            "dispatch_started", "effect_unknown", "effect_confirmed",
            "effect_applied",
        }
        or effect_truth in {"possible", "observed", "verified"}
    )


async def _commit_canonical_chat_cancellation(
    turn_meta: dict,
    *,
    reason_code: str,
    superseded_by_turn_id: str = "",
    new_turn_meta: dict | None = None,
    tool_attempt: dict | None = None,
    channel: "_OutboundChannel | None" = None,
    streaming_tasks: dict | None = None,
    control_message: dict | None = None,
    send_busy_false: bool = False,
) -> dict | None:
    """Win one turn-cancellation CAS and settle its durable command truthfully."""

    if not isinstance(turn_meta, dict):
        return None
    conv_id = str(turn_meta.get("conversation_id") or "")
    request_id = str(
        turn_meta.get("request_id") or turn_meta.get("client_request_id") or ""
    )
    turn_id = str(turn_meta.get("turn_id") or "")
    run_id = str(turn_meta.get("run_id") or turn_id)
    if not all((conv_id, request_id, turn_id, run_id)):
        return None

    from osia.tool_attempts import TERMINAL_STATUSES, ToolAttemptStatus
    from osia.turn_terminality import load_persisted_terminal_result

    def exact_terminal():
        terminal = load_persisted_terminal_result(turn_id)
        binding = terminal.get("execution_binding") if isinstance(terminal, dict) else None
        if (
            not isinstance(binding, dict)
            or any(
                str(binding.get(key) or "") != value
                for key, value in (
                    ("conversation_id", conv_id), ("request_id", request_id),
                    ("turn_id", turn_id), ("run_id", run_id),
                )
            )
        ):
            return None
        return terminal

    terminal = exact_terminal()
    if terminal and terminal.get("terminal_state") != "user_cancelled":
        # A previously committed normal final is immutable. Its owner can
        # finish receipt delivery while a newer turn proceeds independently.
        return None

    store = None
    attempt = tool_attempt if isinstance(tool_attempt, dict) else None
    attempt_id = str(
        turn_meta.get("_tool_attempt_id")
        or turn_meta.get("_existing_tool_attempt_id") or ""
    )
    if not attempt_id and isinstance(attempt, dict):
        attempt_id = str(attempt.get("tool_attempt_id") or "")
    try:
        from osia.tool_attempts import get_tool_attempt_store

        store = get_tool_attempt_store()
        if attempt is None and attempt_id:
            attempt = store.get_attempt(attempt_id)
        if attempt is None:
            candidate = store.find_compatible_suspended_turn(conv_id)
            if isinstance(candidate, dict) and _tool_attempt_matches_turn_meta(
                candidate, turn_meta, conversation_id=conv_id,
            ):
                attempt = candidate
                attempt_id = str(candidate.get("tool_attempt_id") or "")
    except Exception:
        store = None
        attempt = None
    if isinstance(attempt, dict) and not _tool_attempt_matches_turn_meta(
        attempt, turn_meta, conversation_id=conv_id,
    ):
        attempt = None
        attempt_id = ""

    if isinstance(attempt, dict) and attempt_id:
        try:
            attempt_status = ToolAttemptStatus(attempt.get("status"))
        except (TypeError, ValueError):
            attempt_status = None
        if attempt_status in TERMINAL_STATUSES:
            # A parent turn may still list a terminal attempt as active when
            # its result callback raced Stop. Reconcile from the exact durable
            # ToolAttempt before deciding whether cancellation implies any
            # possible effect; completed success/failure stays factual.
            try:
                from osia.turn_terminality import TurnTerminalityController

                objective = str(
                    turn_meta.get("_terminality_objective")
                    or turn_meta.get("_original_user_text") or ""
                ).strip()
                if objective:
                    controller = TurnTerminalityController(
                        turn_id=turn_id,
                        objective=objective,
                        execution_required=True,
                    )
                    expected_binding = {
                        key: str(turn_meta.get(key) or "")
                        for key in (
                            "conversation_id", "request_id", "turn_id", "run_id",
                            "provider_id", "model_id", "effective_reasoning",
                        )
                    }
                    existing_binding = (
                        controller.snapshot().get("execution_binding") or {}
                    )
                    if not existing_binding:
                        controller.set_execution_binding(expected_binding)
                        existing_binding = expected_binding
                    if existing_binding == expected_binding:
                        controller.reconcile_tool_attempt(attempt_id)
            except Exception:
                # A missing/mismatched terminal ToolAttempt cannot justify
                # rewriting its result or erasing uncertainty.
                pass

    effect_possible = _tool_attempt_may_have_effect(attempt)
    terminality_authority = (
        turn_meta.get("_terminality_cancel_authority")
        or turn_meta.get("_terminality_cancel_intent")
        or turn_meta.get("_terminality_cancel")
    )
    accepted = bool(
        terminal
        and terminal.get("terminal_state") == "user_cancelled"
        and terminal.get("final_text") == ""
    )
    if not accepted and callable(terminality_authority):
        try:
            accepted = terminality_authority(
                turn_id, effect_possible=effect_possible,
                reason_code=reason_code,
                superseded_by_turn_id=superseded_by_turn_id,
            ) is True
        except Exception:
            accepted = False
    if not accepted:
        terminal = exact_terminal()
        accepted = bool(
            terminal
            and terminal.get("terminal_state") == "user_cancelled"
            and terminal.get("final_text") == ""
        )
    if not accepted:
        return None
    terminal = exact_terminal()
    if (
        not isinstance(terminal, dict)
        or terminal.get("terminal_state") != "user_cancelled"
        or terminal.get("final_text") != ""
    ):
        return None

    turn_meta["_tool_cancel_requested"] = True
    turn_meta["_terminal_reason_code"] = str(reason_code or "turn_cancelled")[:160]
    turn_meta["_superseded_by_turn_id"] = str(superseded_by_turn_id or "")
    if attempt_id:
        turn_meta["_tool_attempt_id"] = attempt_id
    if isinstance(attempt, dict) and store is not None:
        try:
            status = ToolAttemptStatus(attempt.get("status"))
        except (TypeError, ValueError):
            status = None
        if status is not None and status not in TERMINAL_STATUSES:
            try:
                attempt = store.request_cancellation(attempt_id)
            except Exception:
                # The turn tombstone still wins; the attempt remains factual
                # uncertainty until its own owner supplies a terminal receipt.
                pass
        turn_meta["_tool_attempt_status"] = str(attempt.get("status") or "")

    cancel_generation = turn_meta.get("_execution_generation_cancel")
    if callable(cancel_generation):
        try:
            cancel_generation(turn_id)
        except Exception:
            # The durable cancellation tombstone remains authoritative; the
            # worker/task cancellation and ToolAttempt boundary still apply.
            pass

    terminal = exact_terminal() or terminal or {}
    result = _cancelled_command_result(
        terminal.get("execution_binding") or {
            "conversation_id": conv_id, "request_id": request_id,
            "turn_id": turn_id, "run_id": run_id,
        },
        terminal,
        reason_code=reason_code,
        superseded_by_turn_id=superseded_by_turn_id,
        tool_attempt=attempt,
    )
    frame = _turn_status_frame(turn_meta, "cancelled")

    # Persist both terminal facts before cancelling the orchestration task.
    # Its CancelledError recovery is allowed to race, but the command CAS keeps
    # this cancellation receipt as the winner and never invents a model final.
    try:
        _complete_durable_command_now(turn_meta, result)
        if isinstance(control_message, dict):
            control_result = {
                **result,
                "control": "cancel_active_work",
                "state": "cancelled",
                "command_id": str(
                    control_message.get("_durable_command_id") or ""
                ),
            }
            _complete_durable_command_now(control_message, control_result)
            control_message["_cancellation_command_result"] = control_result
    except (RuntimeError, TypeError, ValueError):
        # Keep the persisted TurnTerminality/stream tombstone authoritative;
        # startup reconciliation retries the exact command epoch.
        pass

    active_task = _ACTIVE_TURNS_BY_CONV.get(conv_id)
    active_meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
    if active_meta is turn_meta:
        if isinstance(new_turn_meta, dict):
            new_turn_meta["_supersedes_turn_id"] = turn_id
            new_turn_meta["_supersession_history_from_store"] = True
            if active_task is not None:
                new_turn_meta["_superseded_previous_task"] = active_task
        if active_task is not None:
            _clear_conversation_turn(conv_id, active_task)
        else:
            _ACTIVE_TURN_META_BY_CONV.pop(conv_id, None)
            request_id = str(
                turn_meta.get("request_id")
                or turn_meta.get("client_request_id") or ""
            )
            if request_id:
                _ACTIVE_CONV_BY_REQUEST.pop(request_id, None)
    if isinstance(streaming_tasks, dict):
        local_task = streaming_tasks.get(conv_id)
        if local_task is not None:
            if isinstance(new_turn_meta, dict):
                new_turn_meta.setdefault("_supersedes_turn_id", turn_id)
                new_turn_meta.setdefault("_supersession_history_from_store", True)
                new_turn_meta.setdefault("_superseded_previous_task", local_task)
    else:
        local_task = None
    current_task = asyncio.current_task()
    tasks = {task for task in (active_task, local_task) if task is not None}
    for task in tasks:
        if task is current_task or task.done():
            continue
        task.cancel()

    worker_cancel = turn_meta.get("_terminality_cancel")
    if callable(worker_cancel) and worker_cancel is not terminality_authority:
        propagation = asyncio.create_task(
            _propagate_committed_turn_cancellation(
                worker_cancel, turn_id, effect_possible=effect_possible,
            )
        )
        _PENDING_FINAL_PERSIST_TASKS.add(propagation)
        propagation.add_done_callback(_PENDING_FINAL_PERSIST_TASKS.discard)

    if channel is not None:
        await channel.send_json(frame)
    await _broadcast_turn_status(
        turn_meta,
        "cancelled",
        exclude=channel,
        frame=frame,
    )
    if send_busy_false and active_meta is turn_meta and active_task is not None:
        # This is only the live ownership projection; the cancellation turn
        # status above is the sole durable terminal event for the old run.
        busy_frame = _conversation_busy_changed_frame(conv_id, False, turn_meta)
        if channel is not None:
            await channel.send_json(busy_frame)
        await _broadcast_conversation_busy_changed(
            conv_id, False, frame=busy_frame, exclude=channel,
        )
    if (
        isinstance(control_message, dict)
        and control_message.get("_durable_command_id")
        and channel is not None
    ):
        await channel.send_json({
            "type": "cancellation_control",
            "schema_version": 1,
            "scope": "chat",
            "state": "cancelled",
            "reason_code": str(reason_code or "user_cancelled")[:160],
            "command_id": str(
                control_message.get("_durable_command_id") or ""
            ),
            "conversation_id": conv_id,
            "request_id": request_id,
            "turn_id": turn_id,
            "run_id": run_id,
        })
    return result


def _history_for_superseding_turn(
    conversation_id: str,
    turn_meta: dict,
) -> list:
    """Reload committed history while omitting cancelled turn instructions."""

    conversation = conversations.get_conversation(conversation_id)
    messages = list(
        conversation.get("messages") or []
        if isinstance(conversation, dict) else []
    )
    cancelled_turn_ids = {
        str(item.get("_turn_id") or "")
        for item in messages
        if isinstance(item, dict)
        and item.get("role") == "user"
        and conversations._has_exact_user_cancelled_terminal(item)
        and str(item.get("_turn_id") or "")
    }
    current_turn_id = str(turn_meta.get("turn_id") or "")
    current_request_id = str(
        turn_meta.get("request_id") or turn_meta.get("client_request_id") or ""
    )
    history = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        item_turn_id = str(item.get("_turn_id") or "")
        item_request_id = str(
            item.get("_request_id") or item.get("_client_request_id") or ""
        )
        if item_turn_id in cancelled_turn_ids:
            continue
        if (
            item.get("role") == "user"
            and item_turn_id == current_turn_id
            and item_request_id == current_request_id
        ):
            continue
        history.append(item)
    return history


async def _supersede_active_conversation_turn(
    channel: "_OutboundChannel",
    conversation_id: str,
    new_turn_meta: dict,
    *,
    streaming_tasks: dict | None = None,
) -> bool:
    """Supersede only this conversation's prior canonical turn at acceptance."""

    # Repair accepted commands whose turn already has a durable cancellation
    # tombstone, including an owner that exited after the old turn was stopped.
    repaired = _reconcile_cancelled_websocket_commands(
        conversation_id=conversation_id,
    )
    if repaired:
        new_turn_meta["_supersession_history_from_store"] = True

    old_meta = _ACTIVE_TURN_META_BY_CONV.get(conversation_id)
    old_task = _ACTIVE_TURNS_BY_CONV.get(conversation_id)
    if not isinstance(old_meta, dict):
        existing_conversation = conversations.get_conversation(conversation_id)
        if isinstance(existing_conversation, dict) and any(
            isinstance(item, dict)
            and item.get("role") == "user"
            and conversations._has_exact_user_cancelled_terminal(item)
            for item in existing_conversation.get("messages") or []
        ):
            new_turn_meta["_supersession_history_from_store"] = True
        # A terminal TTS worker does not own the chat turn, but its lease can
        # otherwise reject a new committed user message. Stop only that exact
        # post-turn media task and release its existing lease.
        media_task = _POST_TURN_MEDIA_BY_CONV.get(conversation_id)
        media_meta = _POST_TURN_MEDIA_META_BY_CONV.get(conversation_id)
        if media_task is not None:
            if isinstance(media_meta, dict):
                media_meta["_response_media_cancelled"] = True
                lease_id = str(media_meta.get("_post_turn_media_lease_id") or "")
                if lease_id:
                    try:
                        conversations.release_post_turn_media_cas(
                            conversation_id, lease_id,
                        )
                        media_meta["_post_turn_media_lease_released"] = True
                    except Exception:
                        pass
            if not media_task.done():
                media_task.cancel()
            if _POST_TURN_MEDIA_BY_CONV.get(conversation_id) is media_task:
                _POST_TURN_MEDIA_BY_CONV.pop(conversation_id, None)
                _POST_TURN_MEDIA_META_BY_CONV.pop(conversation_id, None)
            new_turn_meta["_supersession_history_from_store"] = True
        elif old_task is not None and not old_task.done():
            # Do not cancel an uncorrelated background owner.
            return False
        return True

    old_turn_id = str(old_meta.get("turn_id") or "")
    if not old_turn_id or old_turn_id == str(new_turn_meta.get("turn_id") or ""):
        return False

    from osia.turn_terminality import load_persisted_terminal_result

    old_terminal = load_persisted_terminal_result(old_turn_id)
    if old_terminal and old_terminal.get("terminal_state") != "user_cancelled":
        old_request_id = str(
            old_meta.get("request_id") or old_meta.get("client_request_id") or ""
        )
        found = conversations.find_correlated_turn(
            conversation_id, old_request_id,
        ) if old_request_id else None
        if not isinstance(found, dict) or found.get("assistant_content") is None:
            # Its normal terminal reservation exists, but its required
            # assistant receipt is not persisted yet. Keep normal final truth.
            return False
        # RACE 1: the normal final won just before this request. Keep its
        # durable command unchanged, detach the finishing owner, and let the
        # new turn reload that already persisted history.
        if old_task is not None:
            new_turn_meta["_superseded_previous_task"] = old_task
        new_turn_meta["_supersedes_turn_id"] = old_turn_id
        new_turn_meta["_supersession_history_from_store"] = True
        if _ACTIVE_TURN_META_BY_CONV.get(conversation_id) is old_meta:
            _clear_conversation_turn(conversation_id, old_task)
        return True

    result = await _commit_canonical_chat_cancellation(
        old_meta,
        reason_code="turn_superseded",
        superseded_by_turn_id=str(new_turn_meta.get("turn_id") or ""),
        new_turn_meta=new_turn_meta,
        streaming_tasks=streaming_tasks,
        send_busy_false=False,
    )
    if result is None:
        return False
    new_turn_meta["_supersession_history_from_store"] = True
    return True


async def _handle_correlated_chat_cancellation(
    channel: "_OutboundChannel",
    msg: dict,
    *,
    streaming_tasks: dict | None = None,
) -> bool:
    """Request cancellation only for the exact active canonical chat turn."""

    conv_id = _clean_cancellation_target_id(msg.get("conversation_id"))
    request_id = _clean_client_request_id(
        msg.get("request_id") or msg.get("client_request_id")
    )
    turn_id = _clean_cancellation_target_id(msg.get("turn_id"))
    supplied_run_id = (
        _clean_cancellation_target_id(msg.get("run_id"))
        if msg.get("run_id") else ""
    )
    run_id = supplied_run_id or turn_id
    supplied_attempt_id = _clean_cancellation_target_id(
        msg.get("tool_attempt_id"), maximum=160,
    ) if msg.get("tool_attempt_id") else ""
    target = {
        "conversation_id": conv_id,
        "request_id": request_id or "",
        "client_request_id": request_id or "",
        "turn_id": turn_id,
        "run_id": run_id,
        "tool_attempt_id": supplied_attempt_id,
    }

    def _settle_control(result: dict) -> None:
        msg["_cancellation_command_result"] = result
        _complete_durable_command_now(msg, result)

    async def _reject(reason_code: str) -> bool:
        result = {
            "accepted": False,
            "state": "rejected",
            "reason_code": str(reason_code or "cancellation_rejected")[:128],
            **{
                key: target[key]
                for key in ("conversation_id", "request_id", "turn_id", "run_id")
                if target.get(key)
            },
        }
        _settle_control(result)
        await _send_cancellation_rejection(
            channel, "chat", reason_code, **target,
        )
        return False

    if (
        not conv_id
        or request_id is None
        or not turn_id
        or (msg.get("run_id") and not supplied_run_id)
    ):
        return await _reject("incomplete_target")
    meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
    active_target_matches = bool(
        isinstance(meta, dict)
        and str(meta.get("turn_id") or "") == turn_id
        and str(meta.get("run_id") or meta.get("turn_id") or "") == run_id
        and str(meta.get("request_id") or meta.get("client_request_id") or "")
        == request_id
    )
    if not active_target_matches:
        _reconcile_cancelled_websocket_commands(conversation_id=conv_id)
        historical = _durable_cancelled_chat_terminal_frame(
            conversation_id=conv_id,
            request_id=request_id,
            turn_id=turn_id,
            run_id=run_id,
            tool_attempt_id=supplied_attempt_id,
        )
        if historical is not None:
            terminal = None
            try:
                from osia.turn_terminality import load_persisted_terminal_result

                terminal = load_persisted_terminal_result(turn_id)
            except Exception:
                terminal = None
            result = _cancelled_command_result(
                (terminal or {}).get("execution_binding") or target,
                terminal or {"reservation_id": ""},
                reason_code="turn_cancelled",
            )
            result.update({
                "control": "cancel_active_work",
                "command_id": str(msg.get("_durable_command_id") or ""),
            })
            _settle_control(result)
            if msg.get("_durable_command_id"):
                await channel.send_json({
                    "type": "cancellation_control",
                    "schema_version": 1,
                    "scope": "chat",
                    "state": "cancelled",
                    "reason_code": "turn_cancelled",
                    "command_id": str(msg.get("_durable_command_id") or ""),
                    **{key: target[key] for key in (
                        "conversation_id", "request_id", "turn_id", "run_id",
                    )},
                })
            await channel.send_json(historical)
            return True
    if not isinstance(meta, dict):
        return await _reject("target_not_active")
    active_run_id = str(meta.get("run_id") or meta.get("turn_id") or "")
    if (
        str(meta.get("turn_id") or "") != turn_id
        or active_run_id != run_id
        or str(meta.get("request_id") or meta.get("client_request_id") or "")
        != request_id
    ):
        await _reject("target_mismatch")
        await _send_recovered_turn_state(channel, conv_id)
        return False
    remembered_control = meta.get("_cancellation_control_target")
    if (
        isinstance(remembered_control, tuple)
        and len(remembered_control) == 5
        and remembered_control[:4] == (conv_id, request_id, turn_id, run_id)
        and (
            not supplied_attempt_id
            or supplied_attempt_id == remembered_control[4]
        )
    ):
        terminal = None
        try:
            from osia.turn_terminality import load_persisted_terminal_result

            terminal = load_persisted_terminal_result(turn_id)
        except Exception:
            terminal = None
        result = _cancelled_command_result(
            (terminal or {}).get("execution_binding") or target,
            terminal or {"reservation_id": ""},
            reason_code="turn_cancelled",
        )
        result.update({
            "control": "cancel_active_work",
            "command_id": str(msg.get("_durable_command_id") or ""),
        })
        _settle_control(result)
        if msg.get("_durable_command_id"):
            await channel.send_json({
                "type": "cancellation_control",
                "schema_version": 1,
                "scope": "chat",
                "state": "cancelled",
                "reason_code": "turn_cancelled",
                "command_id": str(msg.get("_durable_command_id") or ""),
                **{key: target[key] for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                )},
            })
        await channel.send_json(_turn_status_frame(meta, "cancelled"))
        return True
    if not _is_conversation_busy(conv_id):
        return await _reject("target_not_active")

    from osia.tool_attempts import get_tool_attempt_store

    store = get_tool_attempt_store()
    attempt = store.find_compatible_suspended_turn(conv_id)
    if isinstance(attempt, dict) and not _tool_attempt_matches_turn_meta(
        attempt, meta, conversation_id=conv_id,
    ):
        return await _reject("active_receipt_mismatch")
    remembered_attempt_id = str(meta.get("_tool_attempt_id") or "")
    active_attempt_id = str(
        (attempt or {}).get("tool_attempt_id") or remembered_attempt_id
    )
    if supplied_attempt_id and supplied_attempt_id != active_attempt_id:
        return await _reject("tool_attempt_mismatch")

    control_target = (
        conv_id, request_id, turn_id, run_id, active_attempt_id,
    )
    meta["_cancellation_control_target"] = control_target
    result = await _commit_canonical_chat_cancellation(
        meta,
        reason_code="user_cancelled",
        tool_attempt=attempt,
        channel=channel,
        streaming_tasks=streaming_tasks,
        control_message=msg,
        send_busy_false=True,
    )
    if result is None:
        return await _reject("target_not_cancellable")
    return True


async def _resume_incomplete_persisted_chat_turn(
    channel: "_OutboundChannel",
    found: dict,
    *,
    owner_turn_meta: dict | None = None,
) -> bool:
    """Reattach one persisted direct-Chat turn without replaying its action."""

    conv_id = str(found.get("conversation_id") or "")
    request_id = str(found.get("request_id") or "")
    turn_id = str(found.get("turn_id") or "")
    conversation = conversations.get_conversation(conv_id)
    messages = list((conversation or {}).get("messages") or [])
    user_index = next((
        index for index, item in enumerate(messages)
        if isinstance(item, dict)
        and item.get("role") == "user"
        and str(item.get("_request_id") or item.get("_client_request_id") or "")
        == request_id
        and str(item.get("_turn_id") or "") == turn_id
    ), -1)
    if user_index < 0:
        return False
    user_message = messages[user_index]
    objective = user_message.get("_terminality_objective")
    if not isinstance(objective, str) or not objective.strip():
        objective = user_message.get("content")
    if not isinstance(objective, str) or not objective.strip():
        return False
    persisted_directives = user_message.get("_turn_directives_v1")
    turn_directives = None
    if persisted_directives is not None:
        if not isinstance(persisted_directives, dict):
            return False
        try:
            from osia.slash_commands import SlashTurnDirectivesV1

            turn_directives = SlashTurnDirectivesV1.from_wire(
                persisted_directives
            )
        except Exception:
            return False

    try:
        from osia.turn_terminality import (
            TurnTerminalityController,
            load_persisted_terminal_result,
        )

        controller = TurnTerminalityController(
            turn_id=turn_id,
            objective=objective,
            execution_required=True,
        )
        terminality_snapshot = controller.snapshot()
        execution_binding = terminality_snapshot.get("execution_binding") or {}
        persisted_terminal = load_persisted_terminal_result(turn_id)
    except Exception:
        return False
    expected_binding = {
        "conversation_id": conv_id,
        "request_id": request_id,
        "turn_id": turn_id,
        "run_id": turn_id,
    }
    if (
        not isinstance(execution_binding, dict)
        or any(
            str(execution_binding.get(key) or "") != value
            for key, value in expected_binding.items()
        )
        or any(
            not str(execution_binding.get(key) or "")
            for key in ("provider_id", "model_id", "effective_reasoning")
        )
        or str(user_message.get("_run_id") or turn_id) != turn_id
    ):
        return False

    assistant_message_id = str(
        user_message.get("_assistant_message_id") or ""
    )
    try:
        from osia.tool_attempts import (
            RECOVERABLE_STATUSES,
            TERMINAL_STATUSES,
            ToolAttemptStatus,
            get_tool_attempt_store,
        )

        continuation_store = get_tool_attempt_store()
        suspended = continuation_store.find_resumable_chat_turn(conv_id)
    except Exception:
        return False
    try:
        suspended_status = (
            ToolAttemptStatus(suspended.get("status"))
            if isinstance(suspended, dict) else None
        )
    except (TypeError, ValueError):
        suspended_status = None

    if persisted_terminal is not None:
        terminal_binding = persisted_terminal.get("execution_binding") or {}
        if terminal_binding != execution_binding:
            return False
        if not _clean_cancellation_target_id(assistant_message_id):
            return False
        from osia.agent import strip_tool_artifacts
        from osia.execution_trace import (
            ExecutionTraceLedger,
            terminal_display_key,
            validate_persisted_trace,
        )
        from osia.tool_attempts import is_internal_tool_control_text

        reserved_text = str(persisted_terminal.get("final_text") or "")
        final_text = strip_tool_artifacts(reserved_text).strip()
        if (
            not final_text
            or final_text != reserved_text.strip()
            or is_internal_tool_control_text(final_text)
        ):
            return False
        terminal_key = terminal_display_key(
            str(persisted_terminal.get("terminal_state") or "")
        )
        if not terminal_key:
            return False
        safe_trace = validate_persisted_trace(
            persisted_terminal.get("execution_trace")
        )
        if safe_trace is not None and (
            any(
                str(safe_trace.get(key) or "")
                != str(execution_binding.get(key) or "")
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            )
            or not any(
                isinstance(event, dict) and event.get("terminal") is True
                for event in safe_trace.get("events") or ()
            )
        ):
            safe_trace = None
        if safe_trace is None:
            recovery_trace = ExecutionTraceLedger(**execution_binding)
            recovery_trace.record(
                kind="terminal",
                display_key=terminal_key,
                terminal=True,
                dedupe_key="terminal",
            )
            safe_trace = validate_persisted_trace(recovery_trace.snapshot())
        if safe_trace is None:
            return False
        recovered_turn_meta = {
            "conversation_id": conv_id,
            "request_id": request_id,
            "client_request_id": request_id,
            "turn_id": turn_id,
            "run_id": str(execution_binding.get("run_id") or turn_id),
            "provider_id": str(execution_binding.get("provider_id") or ""),
            "model_id": str(execution_binding.get("model_id") or ""),
            "effective_reasoning": str(
                execution_binding.get("effective_reasoning") or ""
            ),
            "user_message_id": str(user_message.get("_message_id") or ""),
            "assistant_message_id": assistant_message_id,
            "_event_sequence": 0,
            "persisted": True,
            "_execution_trace_v1": safe_trace,
        }
        if isinstance(owner_turn_meta, dict):
            exact_owner_binding = {
                "conversation_id": conv_id,
                "request_id": request_id,
                "turn_id": turn_id,
                "run_id": str(execution_binding.get("run_id") or turn_id),
                "provider_id": str(execution_binding.get("provider_id") or ""),
                "model_id": str(execution_binding.get("model_id") or ""),
                "effective_reasoning": str(
                    execution_binding.get("effective_reasoning") or ""
                ),
                "user_message_id": str(user_message.get("_message_id") or ""),
                "assistant_message_id": assistant_message_id,
            }
            if any(
                str(owner_turn_meta.get(key) or "") != value
                for key, value in exact_owner_binding.items()
            ):
                return False
            turn_meta = owner_turn_meta
            turn_meta["persisted"] = True
            turn_meta["_execution_trace_v1"] = safe_trace
            turn_meta.setdefault("_event_sequence", 0)
        else:
            turn_meta = recovered_turn_meta
        from osia.document_artifacts import normalize_references

        artifact_refs = normalize_references(
            persisted_terminal.get("artifact_refs")
        )
        if artifact_refs:
            turn_meta["_artifact_refs"] = artifact_refs
        language = normalize_locale(user_message.get("_language"))
        if language:
            turn_meta["language"] = language
        response_frame = _turn_event_frame(
            turn_meta,
            "response",
            message_id=assistant_message_id,
            payload={"content": final_text},
        )
        terminal_frame = _turn_terminal_frame(turn_meta, persisted_terminal)
        continuation_id = ""
        continuation_claim = None
        if suspended_status in TERMINAL_STATUSES:
            continuation_id = str(suspended.get("tool_attempt_id") or "")
            if (
                continuation_store is None
                or not continuation_id
                or str(suspended.get("owner_scope") or "chat") != "chat"
                or any(
                    str(suspended.get(key) or "")
                    != str(execution_binding.get(key) or "")
                    for key in (
                        "conversation_id", "request_id", "turn_id",
                        "provider_id", "model_id",
                    )
                )
                or str(suspended.get("final_response_message_id") or "")
                != assistant_message_id
            ):
                return False
            continuation_claim = await asyncio.to_thread(
                continuation_store.claim_continuation, continuation_id,
            )
            if continuation_claim is None:
                return False
        release_continuation_claim = continuation_claim is not None
        try:
            if not _persist_completed_assistant_message(
                conv_id,
                final_text,
                turn_meta,
                response_frame,
                terminal_frame,
            ):
                return False
            if continuation_claim is not None:
                completed = await asyncio.to_thread(
                    continuation_store.complete_continuation,
                    continuation_id,
                    continuation_claim,
                    final_response_message_id=assistant_message_id,
                )
                if not completed:
                    return False
                release_continuation_claim = False
            await _broadcast_committed_receipt(
                conv_id,
                turn_meta.get("_assistant_commit"),
            )
            terminal_frame["persisted"] = True
            await channel.send_json(response_frame)
            await channel.send_json(terminal_frame)
            return True
        finally:
            if release_continuation_claim:
                try:
                    await asyncio.to_thread(
                        continuation_store.release_continuation,
                        continuation_id,
                        continuation_claim,
                    )
                except Exception:
                    pass

    if (
        terminality_snapshot.get("terminal_state")
        or terminality_snapshot.get("final_reservation")
        or not isinstance(terminality_snapshot.get("strategy_snapshot"), dict)
        or not terminality_snapshot.get("strategy_snapshot")
    ):
        return False

    if not isinstance(suspended, dict):
        return False
    suspended_message_id = str(
        suspended.get("final_response_message_id") or ""
    )
    if (
        (
            suspended_status not in RECOVERABLE_STATUSES
            and not (
                suspended_status in TERMINAL_STATUSES
                and suspended.get("continuation_state") != "complete"
            )
        )
        or str(suspended.get("owner_scope") or "chat") != "chat"
        or any(
            str(suspended.get(key) or "") != expected
            for key, expected in (
                ("conversation_id", conv_id),
                ("request_id", request_id),
                ("turn_id", turn_id),
                ("provider_id", str(execution_binding["provider_id"])),
                ("model_id", str(execution_binding["model_id"])),
            )
        )
        or not str(suspended.get("tool_attempt_id") or "")
        or not suspended_message_id
        or (assistant_message_id and assistant_message_id != suspended_message_id)
    ):
        return False
    assistant_message_id = suspended_message_id
    attachment_manifest_hash = str(
        user_message.get("_attachment_manifest_hash") or ""
    )
    durable_agent_input = user_message.get("_durable_agent_input_v1")
    if attachment_manifest_hash and (
        not isinstance(durable_agent_input, str)
        or not durable_agent_input.strip()
    ):
        # Pre-migration attachment turns did not persist their opaque source
        # bindings. Never continue them as text-only provider input.
        return False
    resume_input = (
        durable_agent_input if attachment_manifest_hash else objective
    )

    run_id = str(execution_binding["run_id"])
    turn_meta = {
        "conversation_id": conv_id,
        "request_id": request_id,
        "client_request_id": request_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "provider_id": str(execution_binding.get("provider_id") or ""),
        "model_id": str(execution_binding.get("model_id") or ""),
        "effective_reasoning": str(
            execution_binding.get("effective_reasoning") or ""
        ),
        "user_message_id": str(user_message.get("_message_id") or ""),
        "assistant_message_id": assistant_message_id,
        "client_content_hash": str(
            user_message.get("_client_content_hash") or ""
        ),
        "client_base_content_hash": str(
            user_message.get("_client_base_content_hash") or ""
        ),
        "attachment_manifest_hash": attachment_manifest_hash,
        "input_identity_version": str(
            user_message.get("_input_identity_version") or ""
        ),
        "input_kind": str(user_message.get("_input_kind") or ""),
        "persisted_user_content": user_message.get("content"),
        "_terminality_objective": objective,
        "_accepted_user_already_persisted": True,
        "_event_sequence": 0,
        "persisted": True,
        "_pinned_provider_id": str(
            execution_binding.get("provider_id") or ""
        ),
        "_pinned_model_id": str(execution_binding.get("model_id") or ""),
        "_pinned_effective_reasoning": str(
            execution_binding.get("effective_reasoning") or ""
        ),
        "_existing_tool_attempt_id": str(
            suspended.get("tool_attempt_id") or ""
        ),
    }
    if attachment_manifest_hash:
        turn_meta["_durable_agent_input_v1"] = durable_agent_input
    if turn_directives is not None:
        turn_meta["turn_directives"] = turn_directives
        turn_meta["_turn_directives_v1"] = dict(persisted_directives)
    language = normalize_locale(user_message.get("_language"))
    if language:
        turn_meta["language"] = language
    _bind_pre_agent_cancellation_authority(turn_meta)

    async def _resume() -> None:
        try:
            await _run_agent_and_respond(
                channel,
                resume_input,
                messages[:user_index],
                conv_id,
                turn_meta=turn_meta,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "[OSIA Server] persisted Chat recovery failed "
                f"category={type(exc).__name__}"
            )
            await _finalize_admitted_turn_after_worker_failure(
                channel,
                conv_id,
                turn_meta,
                messages[:user_index],
            )

    task = asyncio.create_task(_resume())
    if not _claim_conversation_turn(conv_id, task, turn_meta):
        task.cancel()
        await _send_existing_turn_status(channel, conv_id, request_id)
        return True
    await channel.send_json(_turn_accepted_frame(turn_meta))
    await channel.send_json(_turn_status_frame(turn_meta, "recovering"))
    await _broadcast_conversation_busy_changed(conv_id, True, turn_meta)

    def _clear(done_task) -> None:
        meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        if _clear_conversation_turn(conv_id, done_task):
            asyncio.create_task(
                _broadcast_conversation_busy_changed(conv_id, False, meta)
            )

    task.add_done_callback(_clear)
    return True


def _reserve_failed_server_turn_terminal(
    agent,
    turn_meta: dict,
    *,
    objective: str,
    language: str,
) -> dict | None:
    """Reserve/recover one setup failure without replaying provider or tools."""

    from osia.turn_terminality import (
        TerminalState,
        TurnTerminalityController,
        load_persisted_execution_binding,
        load_persisted_terminal_result,
    )

    turn_id = str(turn_meta.get("turn_id") or "")
    expected = {
        "conversation_id": str(turn_meta.get("conversation_id") or ""),
        "request_id": str(turn_meta.get("request_id") or ""),
        "turn_id": turn_id,
        "run_id": str(turn_meta.get("run_id") or turn_id),
        "provider_id": str(
            turn_meta.get("provider_id")
            or turn_meta.get("_pinned_provider_id")
            or ""
        ),
        "model_id": str(
            turn_meta.get("model_id")
            or turn_meta.get("_pinned_model_id")
            or ""
        ),
        "effective_reasoning": str(
            turn_meta.get("effective_reasoning")
            or turn_meta.get("_pinned_effective_reasoning")
            or ""
        ),
    }
    stable_objective = str(objective or "").strip()[:32_000]
    if not stable_objective or any(not value for value in expected.values()):
        return None

    binding = load_persisted_execution_binding(turn_id)
    if binding is None:
        trace_binding = getattr(agent, "execution_trace_binding", None)
        if callable(trace_binding):
            try:
                binding = trace_binding(turn_id)
            except Exception:
                binding = None
    if binding is None:
        binding = dict(expected)
    if not isinstance(binding, dict) or any(
        str(binding.get(key) or "") != value
        for key, value in expected.items()
    ):
        return None

    persisted = load_persisted_terminal_result(turn_id)
    if isinstance(persisted, dict):
        return (
            persisted
            if persisted.get("execution_binding") == binding else
            None
        )

    controller = getattr(agent, "_terminality_controller", None)
    owns_controller = bool(
        controller is not None
        and str(getattr(controller, "turn_id", "") or "") == turn_id
    )
    if not owns_controller:
        from osia.execution_strategy import derive_semantic_mission
        from osia.goal_contract import (
            goal_contract_from_dict,
            goal_contract_from_semantic_mission,
        )

        controller = TurnTerminalityController(
            turn_id=turn_id,
            objective=stable_objective,
            execution_required=True,
        )
        controller_snapshot = controller.snapshot()
        persisted_contract = controller_snapshot.get("goal_contract")
        contract = (
            goal_contract_from_dict(persisted_contract)
            if isinstance(persisted_contract, dict) and persisted_contract else
            goal_contract_from_semantic_mission(
                derive_semantic_mission(stable_objective),
                execution_binding=binding,
            )
        )
        controller.set_execution_binding(binding)
        controller.set_goal_contract(contract)

    snapshot = controller.snapshot()
    cancellation = snapshot.get("cancellation") or {}
    exact_attempt_id = str(
        turn_meta.get("_tool_attempt_id")
        or turn_meta.get("_existing_tool_attempt_id")
        or ""
    )
    if cancellation.get("requested") and exact_attempt_id:
        try:
            from osia.tool_attempts import (
                ToolAttemptStatus,
                get_tool_attempt_store,
            )

            exact_attempt = get_tool_attempt_store().get_attempt(
                exact_attempt_id
            )
            exact_cancelled_before_effect = bool(
                _tool_attempt_matches_turn_meta(exact_attempt, turn_meta)
                and ToolAttemptStatus(exact_attempt.get("status"))
                is ToolAttemptStatus.CANCELLED_PROVEN
                and str(exact_attempt.get("dispatch_state") or "")
                == "cancelled_before_effect"
            )
        except Exception:
            exact_attempt = None
            exact_cancelled_before_effect = False
        if exact_cancelled_before_effect:
            evidence = {
                "status": "cancelled_proven",
                "dispatch_state": "cancelled_before_effect",
                "terminal_outcome": str(
                    exact_attempt.get("terminal_outcome") or ""
                )[:80],
                "reason_code": str(
                    exact_attempt.get("reason_code") or ""
                )[:160],
            }
            controller.set_attempt_state(
                exact_attempt_id, status="completed",
            )
            controller.record_typed_evidence(
                outcome="cancelled_proven",
                evidence=evidence,
                attempt_id=exact_attempt_id,
                reason_code=(
                    str(exact_attempt.get("reason_code") or "")
                    or "cancellation_acknowledged_before_effect"
                ),
            )
            controller.refine_cancellation_proven_before_effect()
            snapshot = controller.snapshot()
    if any(
        str((snapshot.get("execution_binding") or {}).get(key) or "") != value
        for key, value in expected.items()
    ):
        return None
    blockers = bool(
        snapshot.get("active_attempts")
        or snapshot.get("permission_state") == "pending"
    )
    active_invocations = [
        item for item in snapshot.get("invocations") or []
        if isinstance(item, dict) and item.get("status") == "active"
    ]
    if active_invocations:
        # The outer worker has returned with an exception. Only invocations on
        # this exact in-memory controller, owned by this process, can therefore
        # be closed as a failed provider boundary. Durable attempts/effects keep
        # their own blockers above and are never guessed away.
        if blockers or not owns_controller or any(
            item.get("owner_pid") not in (None, os.getpid())
            for item in active_invocations
        ):
            return None
        for item in active_invocations:
            invocation_id = str(item.get("invocation_id") or "")
            if invocation_id:
                controller.record_provider_invocation_completed(
                    invocation_id,
                    native_status="failed",
                )
                controller.record_typed_evidence(
                    outcome="failed_before_effect",
                    evidence="server_turn_worker_failed",
                    reason_code="durable_turn_setup_failed",
                )
        snapshot = controller.snapshot()
    if (
        snapshot.get("active_attempts")
        or snapshot.get("permission_state") == "pending"
        or any(
            isinstance(item, dict) and item.get("status") == "active"
            for item in snapshot.get("invocations") or []
        )
    ):
        return None

    controller.set_strategy_frontier(())
    cancellation = snapshot.get("cancellation") or {}
    hard_safety_reason = str(snapshot.get("hard_safety_reason") or "")
    if (
        cancellation.get("requested")
        and not cancellation.get("effect_possible")
        and not snapshot.get("unknown_effects")
    ):
        terminal_state = TerminalState.USER_CANCELLED.value
        reason_code = "authoritative_user_cancellation"
        final_text = localized_text(
            "chat.status_cancelled_proven", language,
        )
    elif cancellation.get("requested") and snapshot.get("unknown_effects"):
        terminal_state = TerminalState.BLOCKED_VERIFIED.value
        reason_code = "durable_outcome_unknown"
        final_text = localized_text(
            "chat.status_outcome_unknown", language,
        )
    elif hard_safety_reason:
        terminal_state = TerminalState.HARD_SAFETY_BLOCK.value
        reason_code = hard_safety_reason
        final_text = (
            "Cette action est bloquée par une barrière de sécurité absolue."
            if language == "fr" else
            "This action is blocked by an absolute safety barrier."
        )
    elif snapshot.get("unknown_effects"):
        terminal_state = TerminalState.BLOCKED_VERIFIED.value
        reason_code = "durable_outcome_unknown"
        final_text = localized_text(
            "chat.status_outcome_unknown", language,
        )
    else:
        terminal_state = TerminalState.BLOCKED_VERIFIED.value
        first_causal = snapshot.get("first_causal_error") or {}
        first_reason = str(
            first_causal.get("reason_code")
            if isinstance(first_causal, dict) else ""
        )
        completed_provider_turns = sum(
            1 for item in snapshot.get("invocations") or []
            if isinstance(item, dict)
            and item.get("status") == "provider_invocation_completed"
        )
        if first_reason == "provider_output_truncated":
            reason_code = first_reason
            final_text = (
                "ERROR: le fournisseur a interrompu sa réponse à la limite "
                "de sortie ; la réponse partielle est incomplète."
                if language == "fr" else
                "ERROR: the provider ended its response at the output-length "
                "limit; the partial response is incomplete."
            )
        elif first_reason in {
            "TARGET_PARSE_AMBIGUOUS", "TARGET_CONFLICT", "TARGET_MISSING",
            "TARGET_AMBIGUOUS", "TARGET_STALE",
        }:
            reason_code = first_reason
            final_text = (
                "Je n’ai pas pu identifier de façon fiable la fenêtre "
                "d’application demandée."
                if language == "fr" else
                "I could not reliably identify the requested application window."
            )
        elif first_reason:
            reason_code = first_reason
            final_text = (
                "L’exécution ne peut pas progresser de façon sûre avec "
                "l’état actuel."
                if language == "fr" else
                "Execution cannot safely progress from the current state."
            )
        elif completed_provider_turns:
            reason_code = "WAIT_WITHOUT_PENDING_TOOL"
            final_text = (
                "L’exécution ne peut pas progresser de façon sûre avec "
                "l’état actuel."
                if language == "fr" else
                "Execution cannot safely progress from the current state."
            )
        else:
            reason_code = "durable_turn_setup_failed"
            final_text = localized_text("provider_failure", language)
    if not controller.reserve_final(
        terminal_state,
        reason_code=reason_code,
        provider_route=f"{binding['provider_id']}:{binding['model_id']}",
        final_text=final_text,
    ):
        persisted = load_persisted_terminal_result(turn_id)
        return (
            persisted
            if isinstance(persisted, dict)
            and persisted.get("execution_binding") == binding else
            None
        )
    try:
        trace_getter = getattr(agent, "execution_trace_snapshot", None)
        trace = trace_getter(turn_id) if callable(trace_getter) else None
        controller.attach_final_projection(
            execution_trace=trace,
            artifact_refs=getattr(agent, "last_run_artifacts", None),
        )
    except Exception:
        # The final reservation is already durable. Projection attachment can
        # be reconstructed as a terminal-only compatibility trace on recovery.
        pass
    persisted = load_persisted_terminal_result(turn_id)
    return (
        persisted
        if isinstance(persisted, dict)
        and persisted.get("execution_binding") == binding else
        None
    )


def _persisted_turn_route_binding(found: dict) -> dict[str, str] | None:
    """Return one exact persisted seven-field binding for replay/media."""

    conversation_id = str(found.get("conversation_id") or "")
    request_id = str(found.get("request_id") or "")
    turn_id = str(found.get("turn_id") or "")
    run_id = str(found.get("run_id") or turn_id)
    expected = {
        "conversation_id": conversation_id,
        "request_id": request_id,
        "turn_id": turn_id,
        "run_id": run_id,
    }
    if any(not value for value in expected.values()):
        return None
    try:
        from osia.turn_terminality import load_persisted_execution_binding

        binding = load_persisted_execution_binding(turn_id)
    except Exception:
        binding = None
    if isinstance(binding, dict):
        if any(str(binding.get(key) or "") != value for key, value in expected.items()):
            return None
        route = {
            key: str(binding.get(key) or "")
            for key in ("provider_id", "model_id", "effective_reasoning")
        }
    else:
        # The accepted user receipt is durable before provider setup. It is the
        # only available immutable route if the process stopped in that gap.
        route = {
            key: str(found.get(key) or "")
            for key in ("provider_id", "model_id", "effective_reasoning")
        }
    if any(not value for value in route.values()):
        return None
    if any(
        found.get(key) and str(found.get(key)) != value
        for key, value in route.items()
    ):
        return None
    return {**expected, **route}


async def _send_persisted_turn_for_request(
    channel: "_OutboundChannel",
    conv_id: str,
    request_id: str,
    *,
    expected_user_content=None,
    expected_identity: dict[str, str] | None = None,
    legacy_client_content_hash: str = "",
    legacy_exact_user_content: str = "",
) -> bool:
    """Replay one completed request or reject an incomplete persisted request."""

    found = conversations.find_correlated_turn(conv_id, request_id)
    if found is None:
        return False
    _assert_request_input_identity(
        found,
        expected_user_content=expected_user_content,
        expected_identity=expected_identity,
        legacy_client_content_hash=legacy_client_content_hash,
        legacy_exact_user_content=legacy_exact_user_content,
    )
    # A persisted user receipt is intentionally visible while its provider
    # work is still live. Replays of that request must reattach to the exact
    # active turn, never reinterpret the temporary lack of an assistant
    # receipt as a terminally incomplete turn.
    if await _send_existing_turn_status(
        channel,
        conv_id,
        request_id,
        expected_user_content=expected_user_content,
        expected_identity=expected_identity,
        legacy_client_content_hash=legacy_client_content_hash,
        legacy_exact_user_content=legacy_exact_user_content,
    ):
        return True
    turn_id = str(found.get("turn_id") or "")
    cancelled_terminal = _durable_cancelled_chat_terminal_frame(
        conversation_id=conv_id,
        request_id=request_id,
        turn_id=turn_id,
        run_id=str(found.get("run_id") or turn_id),
    )
    if cancelled_terminal is not None:
        await channel.send_json(cancelled_terminal)
        return True
    user_message_id = str(found.get("user_message_id") or "")
    assistant_message_id = str(found.get("assistant_message_id") or "")
    response_event_id = str(found.get("response_event_id") or "")
    response_sequence = int(found.get("response_sequence") or 0)
    terminal_event_id = str(found.get("terminal_event_id") or "")
    terminal_sequence = int(found.get("terminal_sequence") or 0)
    assistant_content = found.get("assistant_content")
    terminal_status = str(found.get("terminal_status") or "done")
    persisted_binding = _persisted_turn_route_binding(found)
    if persisted_binding is None:
        await channel.send_json({
            "type": "status",
            "content": "error",
            "error": "persisted_route_binding_unavailable",
            "conversation_id": conv_id,
            "request_id": request_id,
            "client_request_id": request_id,
        })
        return True
    common = {
        **persisted_binding,
        "client_request_id": request_id,
    }
    replay_language = normalize_locale(found.get("language"))
    if replay_language:
        common["language"] = replay_language
    if (
        assistant_message_id
        and response_event_id
        and response_sequence > 0
        and terminal_event_id
        and terminal_sequence > response_sequence
        and isinstance(assistant_content, str)
    ):
        await channel.send_json({
            "type": "response",
            **common,
            "message_id": assistant_message_id,
            "event_id": response_event_id,
            "event_type": "response",
            "sequence": response_sequence,
            "terminal": False,
            "content": assistant_content,
            "replayed": True,
        })
        await channel.send_json({
            "type": "turn_status",
            **common,
            "message_id": assistant_message_id,
            "event_id": terminal_event_id,
            "event_type": "turn_status",
            "sequence": terminal_sequence,
            "terminal": True,
            "status": terminal_status,
            "persisted": True,
            "replayed": True,
            **{
                key: found[key]
                for key in ("stage", "error_category", "reason_code")
                if found.get(key)
            },
        })
        return True
    if await _resume_incomplete_persisted_chat_turn(channel, found):
        return True
    # A dead execution owner with no active effect can be closed through the
    # same durable terminality CAS used by the live worker.  Never manufacture
    # an ephemeral terminal event here: reconnect retries must replay the one
    # persisted assistant receipt or remain non-terminal.
    owner_pid = found.get("execution_owner_pid")
    owner_identity = str(
        found.get("execution_owner_process_identity") or ""
    )
    owner_alive = True
    if isinstance(owner_pid, int) and not isinstance(owner_pid, bool) and owner_pid > 0:
        from osia.turn_terminality import live_process_identity

        process_alive, live_identity = live_process_identity(owner_pid)
        owner_alive = bool(
            process_alive
            and (
                not owner_identity
                or not live_identity
                or owner_identity == live_identity
            )
        )
    if not owner_alive:
        recovery_meta = {
            **persisted_binding,
            "assistant_message_id": assistant_message_id or user_message_id,
        }
        objective = str(
            found.get("terminality_objective")
            or found.get("user_content")
            or ""
        )
        try:
            await asyncio.to_thread(
                _reserve_failed_server_turn_terminal,
                None,
                recovery_meta,
                objective=objective,
                language=replay_language or "en",
            )
        except Exception:
            # Corrupt/indeterminate orphan state is not terminal evidence.
            # Leave the request reattachable and report a non-terminal status.
            pass
        if await _resume_incomplete_persisted_chat_turn(channel, found):
            return True
    replay_meta = {
        **persisted_binding,
        "client_request_id": request_id,
        "assistant_message_id": assistant_message_id or user_message_id,
        "persisted": True,
    }
    if replay_language:
        replay_meta["language"] = replay_language
    recovering_frame = _turn_status_frame(replay_meta, "recovering")
    recovering_frame.update({
        "error": "persisted_turn_incomplete",
        "replayed": True,
    })
    await channel.send_json(recovering_frame)
    return True


def _persist_accepted_user_message(
    conv_id: str | None,
    content,
    turn_meta: dict | None,
) -> bool:
    if not conv_id or not isinstance(turn_meta, dict):
        return False
    if turn_meta.get("_accepted_user_already_persisted") is True:
        return True
    request_id = str(
        turn_meta.get("request_id")
        or turn_meta.get("client_request_id")
        or ""
    )
    turn_id = str(turn_meta.get("turn_id") or "")
    message_id = str(turn_meta.get("user_message_id") or "")
    if not request_id or not turn_id or not message_id:
        return False
    try:
        command_id = str(turn_meta.get("_durable_command_id") or "")
        if command_id:
            _DURABLE_TRANSPORT.mark_command_running(
                command_id,
                execution_epoch=str(turn_meta.get("_durable_command_epoch") or ""),
                execution_binding={"conversation_id": conv_id,
                                   "request_id": request_id, "turn_id": turn_id},
            )
        metadata = {
            "_durable_command_id": str(turn_meta.get("_durable_command_id") or ""),
            "_durable_command_epoch": str(turn_meta.get("_durable_command_epoch") or ""),
            "_conversation_id": conv_id,
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": turn_id,
            "_run_id": str(turn_meta.get("run_id") or turn_id),
            "_provider_id": str(turn_meta.get("provider_id") or ""),
            "_model_id": str(turn_meta.get("model_id") or ""),
            "_effective_reasoning": str(
                turn_meta.get("effective_reasoning") or ""
            ),
            "_message_id": message_id,
            "_assistant_message_id": str(
                turn_meta.get("assistant_message_id") or ""
            ),
            "_terminality_objective": str(
                turn_meta.get("_terminality_objective") or ""
            )[:32_000],
            "_client_content_hash": str(
                turn_meta.get("client_content_hash") or ""
            ),
            "_client_base_content_hash": str(
                turn_meta.get("client_base_content_hash") or ""
            ),
            "_attachment_manifest_hash": str(
                turn_meta.get("attachment_manifest_hash") or ""
            ),
            "_input_identity_version": str(
                turn_meta.get("input_identity_version") or ""
            ),
            "_input_kind": str(turn_meta.get("input_kind") or ""),
            "_execution_owner": str(
                turn_meta.get("_execution_owner") or "server"
            ),
            "_execution_owner_pid": int(
                turn_meta.get("_execution_owner_pid") or os.getpid()
            ),
            "_execution_owner_process_identity": str(
                turn_meta.get("_execution_owner_process_identity") or ""
            ),
        }
        language = normalize_locale(turn_meta.get("language"))
        if language:
            metadata["_language"] = language
        attachment_metadata = turn_meta.get("attachment_metadata")
        if isinstance(attachment_metadata, list) and attachment_metadata:
            metadata["_attachments"] = attachment_metadata
        durable_agent_input = turn_meta.get("_durable_agent_input_v1")
        if (
            str(turn_meta.get("attachment_manifest_hash") or "")
            and isinstance(durable_agent_input, str)
            and durable_agent_input.strip()
        ):
            metadata["_durable_agent_input_v1"] = durable_agent_input
        turn_directives = turn_meta.get("_turn_directives_v1")
        if isinstance(turn_directives, dict):
            metadata["_turn_directives_v1"] = turn_directives
        receipt = conversations.commit_message(
            conv_id,
            "user",
            content,
            metadata=metadata,
        )
    except Exception as e:
        error_text = str(e)
        turn_meta["_user_commit_error"] = (
            "conversation_busy"
            if error_text.startswith("conversation has an active ")
            else "request_id_conflict"
            if "request" in error_text or "correlation conflict" in error_text
            else "persistence_unavailable"
        )
        print(
            "[OSIA Server] failed to persist accepted user turn: "
            f"{type(e).__name__}"
        )
        return False
    turn_meta["_user_commit"] = receipt
    if receipt is not None and command_id:
        _DURABLE_TRANSPORT.mark_command_running(
            command_id,
            execution_epoch=str(turn_meta.get("_durable_command_epoch") or ""),
            execution_binding={"conversation_id": conv_id,
                               "request_id": request_id, "turn_id": turn_id},
            accepted=True,
        )
    return receipt is not None


def _persist_completed_assistant_message(
    conv_id: str | None,
    content: str,
    turn_meta: dict | None,
    response_frame: dict,
    terminal_frame: dict,
) -> bool:
    """Persist exactly one final assistant message for one accepted user turn."""

    if not conv_id or not isinstance(turn_meta, dict):
        return False
    request_id = str(
        turn_meta.get("request_id")
        or turn_meta.get("client_request_id")
        or ""
    )
    turn_id = str(turn_meta.get("turn_id") or "")
    message_id = str(turn_meta.get("assistant_message_id") or "")
    run_id = str(turn_meta.get("run_id") or turn_id)
    provider_id = str(turn_meta.get("provider_id") or "")
    model_id = str(turn_meta.get("model_id") or "")
    effective_reasoning = str(turn_meta.get("effective_reasoning") or "")
    response_event_id = str(response_frame.get("event_id") or "")
    response_sequence = response_frame.get("sequence")
    event_id = str(terminal_frame.get("event_id") or "")
    sequence = terminal_frame.get("sequence")
    if (
        not request_id
        or not turn_id
        or not message_id
        or not run_id
        or not provider_id
        or not model_id
        or not effective_reasoning
        or not response_event_id
        or not isinstance(response_sequence, int)
        or response_sequence <= 0
        or not event_id
        or not isinstance(sequence, int)
        or sequence <= 0
    ):
        return False
    try:
        metadata = {
            "_durable_command_id": str(turn_meta.get("_durable_command_id") or ""),
            "_durable_command_epoch": str(turn_meta.get("_durable_command_epoch") or ""),
            "_conversation_id": conv_id,
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": turn_id,
            "_run_id": run_id,
            "_provider_id": provider_id,
            "_model_id": model_id,
            "_effective_reasoning": effective_reasoning,
            "_message_id": message_id,
            "_response_event_id": response_event_id,
            "_response_sequence": response_sequence,
            "_terminal_event_id": event_id,
            "_terminal_sequence": sequence,
            "_terminal": True,
            "_language": normalize_locale(turn_meta.get("language")) or "",
            "_terminal_status": str(terminal_frame.get("status") or "done"),
            "_stage": str(terminal_frame.get("stage") or ""),
            "_error_category": str(
                terminal_frame.get("error_category") or ""
            ),
            "_reason_code": str(terminal_frame.get("reason_code") or ""),
            "_execution_owner": str(
                turn_meta.get("_execution_owner") or "server"
            ),
            "_execution_owner_pid": int(
                turn_meta.get("_execution_owner_pid") or os.getpid()
            ),
            "_execution_owner_process_identity": str(
                turn_meta.get("_execution_owner_process_identity") or ""
            ),
        }
        post_turn_media_lease_id = str(
            turn_meta.get("_post_turn_media_lease_id") or ""
        ).strip()
        post_turn_media_owner_pid = int(
            turn_meta.get("_post_turn_media_owner_pid") or os.getpid()
        )
        post_turn_media_owner_identity = str(
            turn_meta.get("_post_turn_media_owner_process_identity") or ""
        )
        if post_turn_media_lease_id:
            metadata.update({
                "_post_turn_media_lease_id": post_turn_media_lease_id,
                "_post_turn_media_owner_pid": post_turn_media_owner_pid,
                "_post_turn_media_owner_process_identity": (
                    post_turn_media_owner_identity
                ),
            })
        from osia.document_artifacts import normalize_references
        artifact_refs = normalize_references(turn_meta.get("_artifact_refs"))
        if artifact_refs:
            metadata["_artifact_refs"] = artifact_refs
        from osia.execution_trace import validate_persisted_trace

        safe_trace = validate_persisted_trace(
            turn_meta.get("_execution_trace_v1")
        )
        if safe_trace is not None:
            metadata["_execution_trace_v1"] = safe_trace
        language_diagnostic = persisted_language_drift_diagnostic(
            turn_meta.get("_language_validation_diagnostic_v1")
        )
        if language_diagnostic is not None:
            metadata["_language_validation_diagnostic_v1"] = language_diagnostic
        from osia.tool_contracts import (
            persisted_computer_use_argument_diagnostic,
        )
        argument_diagnostic = persisted_computer_use_argument_diagnostic(
            turn_meta.get("_computer_use_argument_diagnostic_v1")
        )
        if (
            argument_diagnostic is not None
            and argument_diagnostic["conversation_id"] == conv_id
            and argument_diagnostic["request_id"] == request_id
            and argument_diagnostic["turn_id"] == turn_id
            and argument_diagnostic["provider_id"] == provider_id
            and argument_diagnostic["model_id"] == model_id
        ):
            # Persist only the sanitized diagnostic in private conversation
            # metadata. It is not added to any WebSocket event or model prompt.
            metadata["_computer_use_argument_diagnostic_v1"] = (
                argument_diagnostic
            )
        receipt = conversations.commit_message(
            conv_id,
            "assistant",
            content,
            metadata=metadata,
        )
    except Exception as exc:
        print(
            "[OSIA Server] failed to persist correlated assistant turn: "
            f"{type(exc).__name__}"
        )
        return False
    if not isinstance(receipt, dict):
        return False
    conversation = receipt.get("conversation")
    messages = (
        conversation.get("messages")
        if isinstance(conversation, dict) else None
    )
    if not isinstance(messages, list):
        return False
    matching = [
        item for item in messages
        if isinstance(item, dict)
        and str(item.get("_message_id") or "") == message_id
    ]
    if len(matching) != 1:
        return False
    persisted = matching[0]
    persisted_media_lease = conversation.get("_post_turn_media_lease")
    exact_values = {
        "_conversation_id": conv_id,
        "_request_id": request_id,
        "_client_request_id": request_id,
        "_turn_id": turn_id,
        "_run_id": run_id,
        "_provider_id": provider_id,
        "_model_id": model_id,
        "_effective_reasoning": effective_reasoning,
        "_message_id": message_id,
        "_operation_id": message_id,
        "_response_event_id": response_event_id,
        "_terminal_event_id": event_id,
        "_terminal_status": str(terminal_frame.get("status") or "done"),
    }
    if (
        persisted.get("role") != "assistant"
        or persisted.get("content") != content
        or any(
            str(persisted.get(key) or "") != value
            for key, value in exact_values.items()
        )
        or persisted.get("_response_sequence") != response_sequence
        or persisted.get("_terminal_sequence") != sequence
        or persisted.get("_terminal") is not True
        or str(receipt.get("operation_id") or "") != message_id
        or str(receipt.get("message_id") or "") != message_id
        or (
            receipt.get("committed") is True
            and post_turn_media_lease_id
            and (
                not isinstance(persisted_media_lease, dict)
                or str(persisted_media_lease.get("lease_id") or "")
                != post_turn_media_lease_id
                or str(persisted_media_lease.get("turn_id") or "")
                != turn_id
                or str(persisted_media_lease.get("message_id") or "")
                != message_id
                or persisted_media_lease.get("owner_pid")
                != post_turn_media_owner_pid
                or str(
                    persisted_media_lease.get("owner_process_identity") or ""
                ) != post_turn_media_owner_identity
            )
        )
    ):
        return False
    command_id = str(turn_meta.get("_durable_command_id") or "")
    command_epoch = str(turn_meta.get("_durable_command_epoch") or "")
    if not command_id:
        for user in messages:
            if (isinstance(user, dict) and user.get("role") == "user"
                and str(user.get("_request_id") or "") == request_id
                and str(user.get("_turn_id") or "") == turn_id):
                command_id = str(user.get("_durable_command_id") or "")
                command_epoch = str(user.get("_durable_command_epoch") or "")
                break
    if command_id:
        try:
            _DURABLE_TRANSPORT.complete_command(
                command_id,
                execution_epoch=command_epoch,
                result={"accepted": True, "state": "turn_completed",
                        "conversation_id": conv_id, "request_id": request_id,
                        "turn_id": turn_id, "message_id": message_id,
                        "status": str(terminal_frame.get("status") or "done")},
            )
        except Exception as exc:
            print("[WS command] final settlement failed: "
                  f"{type(exc).__name__} command={command_id}", flush=True)
            return False
    turn_meta["_assistant_commit"] = receipt
    if post_turn_media_lease_id:
        exact_media_lease = bool(
            isinstance(persisted_media_lease, dict)
            and (
                str(persisted_media_lease.get("lease_id") or "")
                == post_turn_media_lease_id
                and str(persisted_media_lease.get("turn_id") or "")
                == turn_id
                and str(persisted_media_lease.get("message_id") or "")
                == message_id
                and persisted_media_lease.get("owner_pid")
                == post_turn_media_owner_pid
                and str(
                    persisted_media_lease.get("owner_process_identity") or ""
                ) == post_turn_media_owner_identity
            )
        )
        turn_meta["_post_turn_media_lease_committed"] = exact_media_lease
        if not exact_media_lease:
            turn_meta["_post_turn_media_lease_released"] = True
    return True


def _claim_conversation_turn(
    conv_id: str,
    task: "asyncio.Future",
    turn_meta: dict | None = None,
) -> bool:
    if _is_conversation_busy(conv_id):
        return False
    _ACTIVE_TURNS_BY_CONV[conv_id] = task
    if isinstance(turn_meta, dict):
        _ACTIVE_TURN_META_BY_CONV[conv_id] = turn_meta
        req_id = str(turn_meta.get("client_request_id") or "")
        if req_id:
            _ACTIVE_CONV_BY_REQUEST[req_id] = conv_id
    return True


def _clear_conversation_turn(
    conv_id: str,
    task: "asyncio.Future | None" = None,
) -> bool:
    current = _ACTIVE_TURNS_BY_CONV.get(conv_id)
    if current is None:
        return False
    if task is not None and current is not task:
        return False
    _ACTIVE_TURNS_BY_CONV.pop(conv_id, None)
    meta = _ACTIVE_TURN_META_BY_CONV.pop(conv_id, None)
    req_id = str((meta or {}).get("client_request_id") or "")
    if req_id:
        _ACTIVE_CONV_BY_REQUEST.pop(req_id, None)
    return True


async def _send_conversation_busy(channel: "_OutboundChannel", conv_id: str) -> None:
    await channel.send_json(_conversation_busy_frame(conv_id))


async def _broadcast_conversation_busy_changed(
    conv_id: str,
    busy: bool,
    turn_meta: dict | None = None,
    *,
    frame: dict | None = None,
    exclude: "_OutboundChannel | None" = None,
) -> None:
    if frame is None:
        frame = _conversation_busy_changed_frame(conv_id, busy, turn_meta)
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


# Mobile mirrors the desktop GUI's connector controls through the WS handlers
# (mcp_connect / mcp_disconnect / mcp_status, below). The server owns its OWN
# process-wide MCPManager singleton (osia.mcp.get_manager) — distinct from the
# desktop app's, since each process bridges its connected MCP tools into ITS
# OWN agent. So this fan-out keeps every connected MOBILE client in sync with
# THIS process's manager; it does not (and cannot) reach the desktop GUI, which
# is not a WebSocket client and drives its own in-process manager. Cross-process
# (desktop ↔ server) MCP state is intentionally independent.
#
# SECRET DISCIPLINE: a server's config round-trips as the `mcp_servers` setting
# (non-secret — env is always {}); its token lives ONLY in the gitignored
# api_keys.json under `mcp:<id>`. _mcp_build_connect_cfg injects the token into
# an IN-MEMORY env for the handshake — it is never persisted to settings.json
# nor echoed in any frame. The status frames carry only {status, error
# (sanitized by the manager), tools (a count)} — never env/token.

def _mcp_find_saved_server(server_id: str):
    """Return the saved (non-secret) config dict for ``server_id`` from the
    ``mcp_servers`` setting, or ``None`` if absent / malformed."""
    if not server_id:
        return None
    try:
        from osia.config import get_setting
        servers = get_setting("mcp_servers", []) or []
    except Exception:
        servers = []
    if not isinstance(servers, list):
        return None
    for s in servers:
        if isinstance(s, dict) and s.get("id") == server_id:
            return s
    return None


def _mcp_connectable(saved: dict) -> bool:
    """True iff the manager can connect this server now: a supported transport
    AND, for OAuth servers (WOR-44 Phase 5, 5-oauth-a), a non-empty URL (the
    OAuth flow needs an endpoint to discover against). Mirrors the desktop GUI's
    _mcp_connectable so PC and mobile agree on what the MANAGER can connect.

    NOTE: this predicate says the manager *can* connect an OAuth server — it does
    NOT mean a mobile client may TRIGGER it. The interactive OAuth browser login
    is desktop-only in this sub-step, so the mobile ``mcp_connect`` handler below
    rejects OAuth with a "connect on PC" hint (5-oauth-c adds the phone flow)."""
    try:
        from osia.mcp_client import SUPPORTED_TRANSPORTS
    except Exception:
        SUPPORTED_TRANSPORTS = ("stdio",)
    return (
        isinstance(saved, dict)
        and saved.get("transport") in SUPPORTED_TRANSPORTS
        and (
            saved.get("auth_type") != "oauth"
            or bool(str(saved.get("url") or "").strip())
        )
    )


def _mcp_build_connect_cfg(saved: dict) -> dict:
    """Build the IN-MEMORY connect config for one saved server: a copy of the
    non-secret saved cfg with the per-server token (from the gitignored
    api_keys.json ``mcp:<id>`` store) injected into ``env[token_env]`` (stdio) or
    an ``Authorization: Bearer`` header (streamable_http), plus any non-secret
    ``extra_env`` values. The env/headers exist only for the manager handshake —
    they are NEVER persisted (settings.json keeps env={} and strips any headers)
    nor echoed in a frame. Mirrors the desktop GUI's _mcp_build_connect_cfg.

    WOR-44: the body now lives in
    ``osia.mcp_client.reconnect.build_headless_connect_cfg`` so the startup
    auto-reconnect routine shares ONE headless (no-Qt) builder. This wrapper
    preserves the existing call site and behaviour byte-for-byte."""
    from osia.mcp_client.reconnect import build_headless_connect_cfg
    return build_headless_connect_cfg(saved)


def _mcp_set_enabled(server_id: str, enabled: bool) -> bool:
    """Persist ``mcp_servers[id].enabled = enabled`` to settings.json (atomic),
    making the shared flag AUTHORITATIVE for the desktop↔server MCP mirror
    (WOR-44 Phase 2, direction B).

    A mobile ``mcp_connect`` / ``mcp_disconnect`` drives THIS server's manager
    AND now writes the user's intent here, so the desktop reconcile daemon
    (``osia.mcp_client.reconnect.reconcile_to_enabled``) mirrors the change onto
    the PC even though the mobile sent no separate ``set_setting``. Reuses the
    existing atomic write (``set_setting`` → ``save_settings`` = temp + fsync +
    ``os.replace``), so a concurrent reader never sees a torn file. Idempotent:
    writing the same value is a harmless no-op (we skip the disk write when the
    flag already matches). We flip ONLY ``enabled`` on the matching entry and
    preserve every other field, EXCEPT that — defence-in-depth — we re-enforce the
    WOR-44 secret-storage contract on every entry at this write boundary
    (``env={}`` + no ``headers``), like the ``set_setting`` mcp_servers handler and
    the desktop ``_mcp_upsert_server``: normally a no-op (the list is already
    secret-free), but it keeps this path self-contained-safe rather than trusting a
    global "on-disk is clean" invariant — a token can never land in the git-tracked
    settings.json here.

    NOTE on cross-process writes: ``set_setting`` is atomic per write but spans no
    read-to-write lock, so two writers racing on ``mcp_servers`` (this worker and
    the desktop GUI, on different ids) can last-writer-wins. A lost ENABLE
    self-heals (the desktop reconcile keeps converging toward ``enabled``); a lost
    DISABLE would be re-connected by the reconcile until the user re-acts. Both
    need two simultaneous human-paced actions inside a sub-ms window — rare,
    recoverable, and out of scope (the boundary ``save_settings`` documents).

    Returns True iff a saved server with that id existed AND the value changed;
    False otherwise (unknown id, already at the target value, or a write error)."""
    if not server_id:
        return False
    try:
        from osia.config import get_setting, set_setting
        servers = get_setting("mcp_servers", []) or []
        if not isinstance(servers, list):
            return False
        changed = False
        for s in servers:
            if not isinstance(s, dict):
                continue
            # Defence-in-depth: re-enforce the WOR-44 secret-storage contract at
            # THIS write boundary (no-op for the already-clean list) so a token can
            # never reach git-tracked settings.json through this path — mirrors the
            # set_setting mcp_servers handler and the desktop _mcp_upsert_server.
            if s.get("env") != {}:
                s["env"] = {}
            s.pop("headers", None)
            if s.get("id") == server_id and bool(s.get("enabled")) != bool(enabled):
                s["enabled"] = bool(enabled)
                changed = True
        if changed:
            set_setting("mcp_servers", servers)
        return changed
    except Exception as e:
        print(
            f"[OSIA Server] _mcp_set_enabled({server_id!r}, {enabled}) failed: {e}"
        )
        return False


def _mcp_status_frame(server_id=None) -> dict:
    """Build an ``mcp_status`` frame from the MCPManager snapshot. Carries only
    per-server ``{status, error, tools}`` (a count) — never any env/token: the
    manager's servers() snapshot is secret-free and its error strings are
    sanitized at the source. With ``server_id`` set, reports just that server
    (DISCONNECTED if the manager has never seen it, so the client gets a
    definitive answer); otherwise reports every server the manager knows."""
    try:
        from osia.mcp_client import get_manager
        snap = get_manager().servers()
    except Exception:
        snap = {}
    if not isinstance(snap, dict):
        snap = {}

    def _one(info: dict) -> dict:
        err = info.get("error", "") or ""
        if len(err) > 300:
            err = err[:299] + "…"
        return {
            "status": info.get("status", "disconnected") or "disconnected",
            "error": err,
            "tools": int(info.get("tools", 0) or 0),
        }

    if server_id:
        info = snap.get(server_id)
        servers = {
            server_id: _one(info) if isinstance(info, dict)
            else {"status": "disconnected", "error": "", "tools": 0}
        }
    else:
        servers = {
            sid: _one(info) for sid, info in snap.items()
            if isinstance(info, dict)
        }
    return {"type": "mcp_status", "servers": servers}


_LEGACY_GMAIL_MCP_HOST = "gmailmcp.googleapis.com"
_NATIVE_GMAIL_REST_ID = "gmail-rest"
_MOBILE_MCP_SERVER_SECRET_KEYS = {
    "access_token",
    "clientSecret",
    "client_secret",
    "oauth_client_secret",
    "oauth_token",
    "refresh_token",
    "token",
}


def _gmail_rest_config_snapshot() -> dict:
    """Token-safe native Gmail REST status for the mobile config snapshot."""
    from osia.integrations import google_oauth

    try:
        from osia.tools.gmail import GMAIL_REST_KEY, gmail_connection_status

        raw = gmail_connection_status(validate=False)
        status = raw if isinstance(raw, dict) else {}
    except Exception:
        GMAIL_REST_KEY = _NATIVE_GMAIL_REST_ID
        status = {
            "connected": False,
            "status": "failed",
            "error": "Unable to read Gmail REST status.",
        }

    caps = status.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    connected = bool(status.get("connected"))
    state = str(
        status.get("status") or ("connected" if connected else "disconnected")
    ).strip() or ("connected" if connected else "disconnected")
    email = status.get("emailAddress")
    email = email.strip() if isinstance(email, str) else None
    if not email:
        email = None
    capabilities = {
        "readonly": bool(
            caps.get("readOnly")
            or caps.get("readonly")
            or status.get("readOnlyConnected")
        ),
        "compose": bool(caps.get("compose") or status.get("composeEnabled")),
        "modify": bool(caps.get("modify") or status.get("modifyEnabled")),
    }
    redirect_uri = google_oauth.mobile_redirect_uri()
    client_configured = google_oauth.oauth_client_configured(GMAIL_REST_KEY)
    mobile_configured = bool(
        redirect_uri
        and google_oauth.oauth_client_supports_redirect(
            GMAIL_REST_KEY, redirect_uri
        )
    )
    attempt = google_oauth.remote_authorization_status(GMAIL_REST_KEY)
    connecting = bool(attempt.get("connecting"))
    error_category = str(attempt.get("error_category") or "").strip()
    message = str(attempt.get("message") or "").strip()[:300]
    if connecting:
        state = "authorization_pending"
        message = "Complete Google authorization in the browser."
    elif connected:
        state = "connected"
        error_category = ""
        message = ""
    elif not mobile_configured:
        state = "configuration_missing"
        error_category = "configuration_missing"
        message = (
            "Configure Google OAuth on the PC for the Mobile callback, then retry."
        )
    elif error_category:
        state = str(attempt.get("status") or error_category).strip()
    else:
        state = "authorization_required"
        message = "Connect Gmail to authorize the PC-hosted OSIA instance."
    attempt_id = str(attempt.get("attempt_id") or "").strip() if connecting else ""
    expires_at = attempt.get("expires_at") if connecting else None
    return {
        "type": "native_rest",
        "id": _NATIVE_GMAIL_REST_ID,
        "connected": connected,
        "connecting": connecting,
        "status": state,
        "emailAddress": email,
        "capabilities": capabilities,
        "errorCategory": error_category,
        "message": message,
        # ``reason`` is retained additively for older Mobile builds.
        "reason": message,
        "oauthClientConfigured": client_configured,
        "mobileOAuthConfigured": mobile_configured,
        "userActionRequired": bool(not connected and not connecting),
        "attemptId": attempt_id or None,
        "attemptExpiresAt": expires_at,
        "canConnect": bool(mobile_configured and not connected and not connecting),
        "canCancel": connecting,
        "canReconnect": bool(mobile_configured and connected and not connecting),
        "canDisconnect": bool(connected and not connecting),
        "configureOnPc": not mobile_configured,
        "elevateOnPc": bool(
            connected and (not capabilities["compose"] or not capabilities["modify"])
        ),
        "guideAvailable": True,
    }


def _is_native_gmail_rest_mobile_action(msg_type: str, server_id: str) -> bool:
    return (
        msg_type in {"mcp_connect", "mcp_disconnect", "mcp_oauth_cancel"}
        and server_id == _NATIVE_GMAIL_REST_ID
    )


async def _broadcast_gmail_rest_config() -> None:
    cfg = _build_config_snapshot()
    await _broadcast_config({"type": "config", "config": cfg})


def _gmail_rest_oauth_url_frame(result: dict, request_id: str = "") -> dict:
    """Build the presentation-only OAuth URL frame for the requesting phone."""
    return {
        "type": "mcp_oauth_url",
        "id": _NATIVE_GMAIL_REST_ID,
        "url": str(result.get("auth_url") or ""),
        "attempt_id": str(result.get("attempt_id") or ""),
        "expires_at": result.get("expires_at"),
        **({"request_id": request_id} if request_id else {}),
    }


async def _gmail_rest_oauth_watchdog(attempt_id: str, expires_at: float) -> None:
    finished = False
    try:
        from osia.integrations.google_oauth import remote_authorization_status

        next_expiry = float(expires_at)
        while True:
            await asyncio.sleep(max(0.0, next_expiry - time.time()) + 0.1)
            status = remote_authorization_status(_NATIVE_GMAIL_REST_ID)
            if not status.get("connecting"):
                finished = True
                await _broadcast_gmail_rest_config()
                return
            next_expiry = float(status.get("expires_at") or (time.time() + 1.0))
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        _GMAIL_OAUTH_WATCHDOG_TASKS.pop(attempt_id, None)
        if finished:
            _GMAIL_OAUTH_ATTEMPT_OWNERS.pop(attempt_id, None)


async def _begin_native_gmail_rest_mobile_connect(owner=None) -> dict:
    """Start/reuse the PC-owned Gmail flow and broadcast its safe snapshot."""
    from osia.integrations import google_oauth
    from osia.tools.gmail import GMAIL_REST_KEY, gmail_reconnect_scopes

    redirect_uri = google_oauth.mobile_redirect_uri()
    if not (
        redirect_uri
        and google_oauth.oauth_client_supports_redirect(
            GMAIL_REST_KEY, redirect_uri
        )
    ):
        await _broadcast_gmail_rest_config()
        return {
            "ok": False,
            "error_category": "configuration_missing",
            "message": "Configure Google OAuth on the PC for the Mobile callback.",
        }
    server_loop = asyncio.get_running_loop()

    def _on_complete(_result: dict) -> None:
        def _publish() -> None:
            _GMAIL_OAUTH_ATTEMPT_OWNERS.clear()
            asyncio.create_task(_broadcast_gmail_rest_config())

        server_loop.call_soon_threadsafe(_publish)

    try:
        result = await asyncio.to_thread(
            google_oauth.begin_remote_authorization,
            GMAIL_REST_KEY,
            gmail_reconnect_scopes(),
            redirect_uri=redirect_uri,
            force_consent=True,
            on_complete=_on_complete,
        )
    except Exception as exc:
        from osia.oauth_relay import OAuthRelayError

        if isinstance(exc, OAuthRelayError):
            await _broadcast_gmail_rest_config()
            return {
                "ok": False,
                "error_category": exc.code,
                "message": str(exc),
            }
        await _broadcast_gmail_rest_config()
        return {
            "ok": False,
            "error_category": "authentication_failed",
            "message": "Unable to start Google authorization. Try again.",
        }
    attempt_id = str(result.get("attempt_id") or "")
    previous_owner = _GMAIL_OAUTH_ATTEMPT_OWNERS.get(attempt_id)
    if (
        previous_owner is not None
        and previous_owner is not owner
        and previous_owner in _WS_CLIENTS
    ):
        return {
            "ok": False,
            "error_category": "authorization_pending",
            "message": "Google authorization is already pending on another device.",
        }
    if attempt_id:
        _GMAIL_OAUTH_ATTEMPT_OWNERS[attempt_id] = owner
    await _broadcast_gmail_rest_config()
    expires_at = result.get("expires_at")
    if (
        attempt_id
        and isinstance(expires_at, (int, float))
        and attempt_id not in _GMAIL_OAUTH_WATCHDOG_TASKS
    ):
        task = asyncio.create_task(
            _gmail_rest_oauth_watchdog(attempt_id, float(expires_at))
        )
        _GMAIL_OAUTH_WATCHDOG_TASKS[attempt_id] = task
    return {"ok": True, **result}


async def _cancel_native_gmail_rest_mobile_connect(
    attempt_id: str = "", owner=None
) -> bool:
    from osia.integrations.google_oauth import cancel_remote_authorization

    if not attempt_id and owner is not None:
        attempt_id = next(
            (
                aid
                for aid, registered_owner in _GMAIL_OAUTH_ATTEMPT_OWNERS.items()
                if registered_owner is owner
            ),
            "",
        )
    registered_owner = _GMAIL_OAUTH_ATTEMPT_OWNERS.get(attempt_id)
    if (
        registered_owner is not None
        and registered_owner is not owner
        and registered_owner in _WS_CLIENTS
    ):
        return False
    cancelled = cancel_remote_authorization(
        _NATIVE_GMAIL_REST_ID,
        attempt_id=attempt_id or None,
    )
    if attempt_id:
        _GMAIL_OAUTH_ATTEMPT_OWNERS.pop(attempt_id, None)
        task = _GMAIL_OAUTH_WATCHDOG_TASKS.pop(attempt_id, None)
        if task is not None and not task.done():
            task.cancel()
    await _broadcast_gmail_rest_config()
    return cancelled


def _disconnect_native_gmail_rest_and_broadcast(loop) -> None:
    """Disconnect native Gmail REST, then push the existing safe config frame."""
    try:
        from osia.integrations.google_oauth import cancel_remote_authorization
        from osia.tools.gmail import gmail_disconnect

        cancel_remote_authorization(_NATIVE_GMAIL_REST_ID)
        gmail_disconnect()
    except Exception as exc:
        print(
            "[OSIA Server] gmail-rest disconnect worker failed: "
            f"{type(exc).__name__}"
        )
        return
    try:
        cfg = _build_config_snapshot()
        asyncio.run_coroutine_threadsafe(
            _broadcast_config({"type": "config", "config": cfg}), loop
        )
    except Exception as e:
        print(f"[OSIA Server] gmail-rest config broadcast failed: {e}")


def _is_legacy_gmail_mcp_server(entry: dict) -> bool:
    sid = str(entry.get("id") or "").strip().lower()
    url = str(entry.get("url") or "").strip().lower()
    return sid in {"gmail", "gmail-mcp-legacy"} and _LEGACY_GMAIL_MCP_HOST in url


def _sanitize_mobile_mcp_servers(servers) -> list:
    """Return the mobile-facing connector list without legacy Gmail as Gmail."""
    clean = []
    legacy_seen = False
    if not isinstance(servers, list):
        return clean
    for server in servers:
        if not isinstance(server, dict):
            continue
        entry = dict(server)
        entry["env"] = {}
        entry.pop("headers", None)
        for key in _MOBILE_MCP_SERVER_SECRET_KEYS:
            entry.pop(key, None)
        if _is_legacy_gmail_mcp_server(entry):
            if legacy_seen:
                continue
            entry.update({
                "id": "gmail-mcp-legacy",
                "name": "Gmail (legacy MCP)",
                "hidden": True,
                "temporarily_unavailable": True,
                "enabled": False,
            })
            legacy_seen = True
        clean.append(entry)
    return clean


async def _broadcast_mcp_status(
    frame: dict, exclude: "_OutboundChannel | None" = None
) -> None:
    """Best-effort fan-out of an ``mcp_status`` frame to every connected client
    (WOR-44 Phase 4). Like ``_broadcast_conversations``, each frame is enqueued
    onto the connection's outbound channel — the real send is performed by that
    connection's own writer task (never from here). Keeps every mobile client in
    sync with the server's MCPManager as connect/disconnect transitions occur.
    Called either awaited on the loop, or hopped from a connect/disconnect
    worker thread via ``run_coroutine_threadsafe`` (the _broadcast_autonomous
    thread→loop pattern)."""
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


def _normalize_mobile_reasoning_setting(value) -> dict:
    from osia.config import normalize_reasoning_settings
    return normalize_reasoning_settings(value)


def _safe_api_key_status_frame(stored_keys, providers) -> dict:
    """Build the legacy API-key response without returning credential values."""
    stored = stored_keys if isinstance(stored_keys, dict) else {}
    provider_ids = [str(provider) for provider in providers]
    return {
        "type": "api_keys",
        "keys": {provider: "" for provider in provider_ids},
        "keys_set": {provider: bool(stored.get(provider)) for provider in provider_ids},
    }


def _build_config_snapshot() -> dict:
    """Build the mobile config snapshot — the ``config`` payload of a
    ``{"type": "config", "config": {...}}`` frame. ONE source of truth shared by
    the ``get_config`` WS request/response handler and the unsolicited config
    broadcast (the MCP mirror, Fix 2), so a pushed snapshot is BYTE-FOR-BYTE the
    same shape the mobile already repaints from after a ``get_config`` — no new
    frame type, no partial frame that could clobber unrelated config state on the
    phone.

    Credential values are never sent to Mobile. Provider/MCP keys and fallback
    keys are represented only by configured-status metadata. Synchronous file
    reads — callers run it OFF the event loop where possible. May raise; callers
    handle (``get_config`` already wraps it)."""
    from osia.config import load_settings, load_api_keys, get_setting
    from osia import personality
    cfg = load_settings()
    try:
        from osia import scheduled_tasks

        cfg["cron_jobs"] = scheduled_tasks.sanitized_mobile_jobs(
            cfg.get("cron_jobs")
        )
    except Exception:
        cfg["cron_jobs"] = []
    cfg["reasoning"] = _normalize_mobile_reasoning_setting(cfg.get("reasoning"))
    cfg["mcp_servers"] = _sanitize_mobile_mcp_servers(cfg.get("mcp_servers"))
    try:
        from osia.voice.stt import available_whisper_model_sizes

        cfg["stt_local_models_available"] = sorted(
            available_whisper_model_sizes().intersection({"small", "medium"})
        )
    except Exception:
        cfg["stt_local_models_available"] = []
    cfg["gmail_rest"] = _gmail_rest_config_snapshot()
    try:
        from osia.slash_commands import safe_availability_overlay

        cfg["slash_commands"] = safe_availability_overlay(
            str(cfg.get("selected_model_provider") or "")
        )
    except Exception:
        cfg["slash_commands"] = {
            "schema_version": 1,
            "current_web_available": False,
            "connectors": [],
        }
    # Mobile can manage fallback slots without receiving their credential values.
    raw_fallbacks = get_setting("api_key_fallbacks", {})
    if isinstance(raw_fallbacks, dict):
        cfg["api_key_fallbacks_set"] = {
            str(provider): [bool(key) for key in keys]
            for provider, keys in raw_fallbacks.items()
            if isinstance(keys, list)
        }
    else:
        cfg["api_key_fallbacks_set"] = {}
    cfg.pop("api_key_fallbacks", None)
    # Desktop-only capsule position is meaningless to the phone.
    cfg.pop("island_x", None)
    cfg.pop("island_y", None)
    # Mirror the two hierarchy chains under the flat names the mobile app writes
    # back via set_setting, so the round-trip is symmetric.
    hierarchy = cfg.get("ai_hierarchy") or {}
    cfg["heavy_chain"] = list(hierarchy.get("heavy") or [])
    cfg["medium_chain"] = list(hierarchy.get("medium") or [])
    personality_selection = personality.resolve_personality()
    cfg["personality"] = personality_selection.text
    cfg["personality_id"] = personality_selection.identifier
    cfg["personality_status"] = (
        "fallback" if personality_selection.fallback_reason else "ok"
    )
    cfg["skills"] = [
        {
            "name": skill.filename,
            "enabled": skill.enabled,
            "text": skill.text,
            "available": skill.available,
            "status": skill.status,
        }
        for skill in personality.discover_skills()
    ]
    cfg["api_keys_set"] = {
        p: bool(k) for p, k in load_api_keys().items()
    }
    return cfg


async def _broadcast_config(
    frame: dict, exclude: "_OutboundChannel | None" = None
) -> None:
    """Best-effort fan-out of a ``config`` frame to every connected client —
    same enqueue-onto-the-connection's-channel pattern as
    ``_broadcast_mcp_status`` / ``_broadcast_conversations``. Used by the MCP
    mirror (Fix 2) to PUSH a fresh config snapshot when the PC connects/
    disconnects a connector, so phones refresh their connector list + enabled
    state (config is otherwise pull-only via ``get_config``). Reuses the existing
    ``{"type": "config"}`` shape the mobile already repaints from; introduces no
    new message type."""
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


async def _broadcast_personality_config() -> None:
    """Push the existing full snapshot after a personality/skill mutation."""

    try:
        await _broadcast_config({
            "type": "config",
            "config": _build_config_snapshot(),
        })
    except Exception:
        print("[OSIA Server] personality config broadcast failed (details withheld)")


def _memory_store() -> MemoryStore:
    return MemoryStore()


def _memory_error_code(exc: Exception) -> str:
    if isinstance(exc, (MemoryValidationError, MemorySecretError)):
        return "validation_error"
    if isinstance(exc, (OSError, OsiaMemoryError)):
        return "storage_error"
    return "unknown"


async def _send_memory_error(
    channel: "_OutboundChannel",
    code: str,
    message: str,
) -> None:
    await channel.send_json({
        "type": "memory_error",
        "code": code,
        "message": message,
    })


async def _broadcast_memory_updated(
    memory: dict,
    exclude: "_OutboundChannel | None" = None,
) -> None:
    frame = {"type": "memory_updated", "memory": memory}
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json(frame)
        except Exception:
            _WS_CLIENTS.discard(client)


async def _broadcast_mobile_catalogue(
    *, refresh_account: bool, include_account_state: bool = False
) -> dict:
    """Rebuild once and fan one sanitized catalogue to every mobile client."""
    global _MOBILE_CATALOGUE_BROADCAST_LOCK
    from osia.mobile_model_catalogue import build_catalogue

    if _MOBILE_CATALOGUE_BROADCAST_LOCK is None:
        _MOBILE_CATALOGUE_BROADCAST_LOCK = asyncio.Lock()
    async with _MOBILE_CATALOGUE_BROADCAST_LOCK:
        catalogue = await asyncio.to_thread(
            build_catalogue,
            refresh_local=False,
            refresh_account=refresh_account,
        )
        frames = [{
            "type": "models_list",
            "catalogue": {
                "version": catalogue["version"],
                "providers": catalogue["providers"],
            },
            "models": catalogue["models"],
            "current": catalogue["current"],
            "current_provider": catalogue["currentProvider"],
            "account": catalogue["account"],
        }]
        if include_account_state:
            frames.append({
                "type": "chatgpt_account_state",
                "account": catalogue["account"],
            })
        for frame in frames:
            for client in list(_WS_CLIENTS):
                try:
                    await client.send_json(frame)
                except Exception:
                    _WS_CLIENTS.discard(client)
        return catalogue


@app.post("/internal/mobile_catalogue_changed")
async def internal_mobile_catalogue_changed(request: Request):
    """Loopback-only PC Settings signal; carries no key or provider value."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    await _broadcast_mobile_catalogue(refresh_account=False)
    return {"ok": True, "clients": len(_WS_CLIENTS)}


@app.post("/internal/chatgpt_account_changed")
async def internal_chatgpt_account_changed(request: Request):
    """Loopback-only PC account signal; broadcasts sanitized state/catalogue."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    await _broadcast_mobile_catalogue(
        refresh_account=True, include_account_state=True
    )
    return {"ok": True, "clients": len(_WS_CLIENTS)}


@app.post("/internal/new_chat")
async def internal_new_chat(request: Request):
    """Localhost-only trigger used by the desktop app (osia/main.py) when the
    user says "nouveau chat" / "new chat". Pushes the reset to every connected
    mobile device. Not part of the public mobile WS protocol."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    await _broadcast_new_chat()
    return {"ok": True, "clients": len(_WS_CLIENTS)}


@app.post("/internal/execution_trace")
async def internal_execution_trace(request: Request):
    """Mirror Desktop's canonical public feed through the existing WS events."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    from osia.execution_trace import validate_execution_trace_frame
    from osia.turn_terminality import load_persisted_execution_binding

    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid_trace"}
    if not isinstance(body, dict) or set(body) != {"frame", "announce"} or type(body["announce"]) is not bool:
        return {"ok": False, "error": "invalid_trace"}
    frame = validate_execution_trace_frame(body["frame"])
    if frame is None:
        return {"ok": False, "error": "invalid_trace"}
    binding = load_persisted_execution_binding(frame["turn_id"])
    if binding is None or any(frame.get(name) != item for name, item in binding.items()):
        return {"ok": False, "error": "stale_binding"}
    if body["announce"]:
        accepted = _turn_accepted_frame(dict(binding))
        for client in list(_WS_CLIENTS):
            try:
                await client.send_json(accepted)
            except Exception:
                _WS_CLIENTS.discard(client)
    await _broadcast_execution_trace(frame)
    if frame["terminal"]:
        status = {
            "execution.terminal.success": "done",
            "execution.terminal.cancelled": "cancelled",
        }.get(frame["display"]["key"], "error")
        await _broadcast_turn_status(dict(binding), status)
    return {"ok": True}


@app.post("/internal/conversation_changed")
async def internal_conversation_changed(request: Request):
    """Loopback-only invalidation from the desktop canonical writer."""

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    conv_id = str(body.get("conversation_id") or "").strip()
    action = str(body.get("action") or "upsert").strip().lower()
    if action not in {
        "upsert", "rename", "delete", "folders", "bulk_delete",
    }:
        action = "upsert"
    try:
        revision = max(0, int(body.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    if action == "folders":
        await _broadcast_conversations()
        broadcast = True
    elif action == "bulk_delete":
        raw_ids = body.get("deleted_ids")
        deleted_ids = list(dict.fromkeys(
            str(item or "")
            for item in raw_ids
            if isinstance(raw_ids, list) and str(item or "")
        )) if isinstance(raw_ids, list) else []
        if deleted_ids:
            snapshot = conversations.conversation_snapshot()
            present_ids = {
                str(item.get("id") or "")
                for item in snapshot.get("items") or []
            }
            missing = [item for item in deleted_ids if item in present_ids]
            if missing:
                print(
                    "[OSIA DeleteTrace] origin=PC phase=bulk_notification_rejected "
                    f"operation_id={str(body.get('operation_id') or '')!r} "
                    f"requested_count={len(deleted_ids)} "
                    f"canonical_present_count={len(missing)} "
                    f"snapshot_revision={int(snapshot.get('snapshot_revision') or 0)}",
                    flush=True,
                )
                broadcast = False
            else:
                frame = {
                    "type": "conversations_bulk_deleted",
                    "success": True,
                    "operation_id": str(body.get("operation_id") or ""),
                    "deleted_ids": deleted_ids,
                    "revision": int(snapshot.get("snapshot_revision") or 0),
                    "snapshot_revision": int(
                        snapshot.get("snapshot_revision") or 0
                    ),
                }
                for client in list(_WS_CLIENTS):
                    try:
                        await client.send_json(frame)
                        await client.send_json(snapshot)
                    except Exception:
                        _WS_CLIENTS.discard(client)
                print(
                    "[OSIA DeleteTrace] origin=PC phase=bulk_ack_queued "
                    f"operation_id={str(body.get('operation_id') or '')!r} "
                    f"deleted_count={len(deleted_ids)} "
                    f"snapshot_revision={int(snapshot.get('snapshot_revision') or 0)} "
                    "success=true",
                    flush=True,
                )
                broadcast = True
        else:
            broadcast = False
    else:
        broadcast = await _broadcast_canonical_conversation_change(
            conv_id,
            revision=revision,
            operation_id=str(body.get("operation_id") or ""),
            action=action,
        )
    return {
        "ok": (
            bool(conv_id)
            or action == "folders"
            or (action == "bulk_delete" and bool(broadcast))
        ),
        "broadcast": broadcast,
        "clients": len(_WS_CLIENTS),
    }


@app.post("/internal/cron_notification")
async def internal_cron_notification(request: Request):
    """Localhost-only desktop trigger for mobile cron notification fallback."""

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    frame = _cron_notification_frame(body)
    _remember_cron_notification(frame)
    await _broadcast_cron_notification(frame)
    conv_id = frame.get("conversation_id")
    if conv_id:
        await _broadcast_conversations()
        await _broadcast_conversation_history(str(conv_id))
    return {
        "ok": True,
        "clients": len(_WS_CLIENTS),
        "id": frame.get("id"),
    }


@app.post("/internal/mcp_sync")
async def internal_mcp_sync(request: Request):
    """Localhost-only desktop→server MCP state mirror — Phase 1, direction A.

    When the user connects/disconnects an MCP connector in the DESKTOP GUI
    (osia/gui/main_popup.py), the desktop POSTs ``{id, action}`` here so THIS
    server reconciles its OWN MCPManager to the shared ``mcp_servers[].enabled``
    intent and repaints every connected phone. The frame carries NO secret (only
    an id + action); not part of the public mobile WS protocol; never exposed
    beyond localhost (same loopback host guard as ``/internal/new_chat``).

    ``connect``  → ``reconnect_enabled_connectors(allow_oauth=True, only_id=id,
        trust_present_token=True)``: the EXACT headless connect path the boot
        sweep / mcp_connect handler use, scoped to one id. The desktop only sends
        ``connect`` AFTER its own ``connect()`` returned CONNECTED, so the SDK has
        already persisted a working OAuth token to disk by then —
        ``trust_present_token=True`` therefore relaxes the OAuth freshness gate to
        accept a present token even when the provider issued no numeric expiry
        (e.g. Linear), while a genuinely EXPIRED token is still SKIPPED. A browser
        pop is impossible regardless (the server-process webbrowser guard below).
    ``disconnect`` → ``get_manager().disconnect(id)`` (idempotent no-op if the
        server never had it connected).

    After the connect/disconnect resolves, the worker broadcasts BOTH an
    ``mcp_status`` frame (the live pill) AND a fresh ``config`` snapshot (so phones
    refresh their connector LIST + enabled state, which is otherwise pull-only via
    ``get_config`` — Fix 2). Both reuse frame shapes the mobile already repaints
    from; no new message type is introduced.

    ``connect()``/``disconnect()`` BLOCK, so the work runs on a short-lived
    daemon thread (never the event loop); the resolved status is broadcast to
    every client via the existing ``_broadcast_mcp_status``. Best-effort."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_id = body.get("id")
    server_id = raw_id.strip() if isinstance(raw_id, str) else ""
    raw_action = body.get("action")
    action = raw_action.strip().lower() if isinstance(raw_action, str) else ""
    if not server_id or action not in ("connect", "disconnect"):
        return {"ok": False, "error": "bad request"}

    loop = asyncio.get_running_loop()

    def _worker(sid=server_id, act=action, loop=loop):
        try:
            if act == "connect":
                from osia.mcp_client.reconnect import reconnect_enabled_connectors
                # only_id scopes the EXISTING headless sweep to this one id; all
                # gates (enabled / _connectable / _oauth_token_fresh) still apply.
                # trust_present_token=True: this is the EXPLICIT mirror path — the
                # desktop just proved the token works (its connect() returned
                # CONNECTED) and the server's webbrowser guard makes a pop
                # impossible, so an OAuth token with no numeric expiry (e.g.
                # Linear, which issues no expires_in) is TRUSTED here instead of
                # skipped. A genuinely EXPIRED token is still skipped (Fix 1).
                reconnect_enabled_connectors(
                    allow_oauth=True, only_id=sid, trust_present_token=True
                )
            else:
                from osia.mcp_client import get_manager
                get_manager().disconnect(sid)
        except Exception as e:
            print(f"[OSIA Server] mcp_sync worker {sid!r} ({act}): {e}")
        # Broadcast the RESOLVED status (connected/disconnected/failed) to every
        # connected phone — the existing mobile repaint path, unchanged (the live
        # status pill).
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast_mcp_status(_mcp_status_frame(sid)), loop
            )
        except Exception:
            pass
        # Fix 2: config is otherwise pull-only (mobile learns mcp_servers via
        # get_config), so a PC connect/disconnect would leave the phone's
        # connector LIST + enabled state stale. PUSH a fresh config snapshot too
        # (reusing the existing {"type":"config"} shape the mobile already
        # repaints from) so the list/enabled view matches the PC. The desktop
        # persisted the new mcp_servers[].enabled to settings.json BEFORE POSTing
        # here, and settings reads are mtime-invalidated cross-process, so this
        # snapshot reflects the just-applied change. Built on THIS worker thread
        # (off the event loop); only the fan-out hops to the loop.
        try:
            cfg = _build_config_snapshot()
            asyncio.run_coroutine_threadsafe(
                _broadcast_config({"type": "config", "config": cfg}), loop
            )
        except Exception as e:
            print(f"[OSIA Server] mcp_sync config broadcast {sid!r}: {e}")

    threading.Thread(
        target=_worker, name="osia-mcp-sync", daemon=True
    ).start()
    return {"ok": True}


# ── WOR-96 PART 1: OAuth-via-HTTPS-tunnel callback (public) + PC trigger ──────
# An OAuth MCP connect can complete via the fixed fallback/callback endpoint
# https://app.osia.space/oauth/callback IN ADDITION to the PC loopback
# (127.0.0.1:8975). Mobile normally uses the rendezvous Worker redirect to reach
# the current quick tunnel. The flow runs ENTIRELY in this server process: the
# tunnel redirect lands here AND the SDK's in-memory PKCE verifier/state live here
# (the SDK never persists them), so the same process/instance that started the
# flow performs the token exchange. The cross-loop state registry + transport live
# in ``osia.mcp_client.oauth`` (imported lazily below) so that package stays
# standalone (it must not import ``osia.server``); the registry instance is a
# module singleton in THIS process.

# /oauth/callback is the ONE route intentionally reachable from the public
# internet, so it carries a small in-memory token-bucket rate limit (global +
# per-IP) as defence-in-depth. ``state`` is 256-bit and the registry rejects any
# unknown/expired state (the SDK also re-checks it via secrets.compare_digest), so
# brute force is already infeasible; this just caps abuse / log-spam.
_OAUTH_CB_LOCK = threading.Lock()
_OAUTH_CB_HITS_GLOBAL: list = []        # recent hit timestamps, all IPs
_OAUTH_CB_HITS_PER_IP: dict = {}        # ip -> recent hit timestamps
_OAUTH_CB_WINDOW_S = 60.0
_OAUTH_CB_MAX_GLOBAL = 120              # callbacks/min across all IPs
_OAUTH_CB_MAX_PER_IP = 30              # callbacks/min per IP

_OAUTH_CB_NOT_FOUND_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>OSIA</title></head><body style='font-family:sans-serif;"
    "background:#15151a;color:#e8e8ea;text-align:center;padding-top:18vh'>"
    "<h2>Not found</h2><p>This authorization link is invalid or has expired. "
    "Start the connection again from OSIA.</p></body></html>"
)
_OAUTH_CB_BUSY_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>OSIA</title></head><body style='font-family:sans-serif;"
    "background:#15151a;color:#e8e8ea;text-align:center;padding-top:18vh'>"
    "<h2>Please wait</h2><p>Too many requests — try again in a moment.</p>"
    "</body></html>"
)


def _oauth_cb_rate_ok(ip: str) -> bool:
    """True iff a /oauth/callback hit from ``ip`` is within budget. Sliding 60s
    window, global + per-IP. Best-effort, never raises."""
    now = time.time()
    cutoff = now - _OAUTH_CB_WINDOW_S
    with _OAUTH_CB_LOCK:
        g = [t for t in _OAUTH_CB_HITS_GLOBAL if t >= cutoff]
        per = [t for t in _OAUTH_CB_HITS_PER_IP.get(ip, []) if t >= cutoff]
        if len(g) >= _OAUTH_CB_MAX_GLOBAL or len(per) >= _OAUTH_CB_MAX_PER_IP:
            _OAUTH_CB_HITS_GLOBAL[:] = g
            _OAUTH_CB_HITS_PER_IP[ip] = per
            return False
        g.append(now)
        per.append(now)
        _OAUTH_CB_HITS_GLOBAL[:] = g
        _OAUTH_CB_HITS_PER_IP[ip] = per
        # Bound the per-IP dict: drop IPs with no hits in the window.
        if len(_OAUTH_CB_HITS_PER_IP) > 1024:
            for k in [k for k, v in _OAUTH_CB_HITS_PER_IP.items()
                      if not any(t >= cutoff for t in v)]:
                _OAUTH_CB_HITS_PER_IP.pop(k, None)
        return True


@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    """PUBLIC OAuth 2.1 redirect endpoint — WOR-96 PART 1.

    Reached either through the rendezvous Worker forwarding to the current quick
    tunnel, or through the fixed app-domain fallback endpoint. An OAuth provider
    redirects the user's browser here with ``?code=&state=`` after an interactive
    login that an MCP connect flow started IN THIS PROCESS (the tunnel branch of
    ``MCPManager._serve``). We match ``state`` to the in-flight flow and hand
    ``(code, state)`` to the waiting provider's ``callback_handler`` — across the
    FastAPI ↔ MCP-manager event loops via a
    ``concurrent.futures.Future`` — then show a neutral close-tab page.

    This is the ONLY route intentionally exposed to the public internet (so it has
    NO loopback host guard, unlike every /internal/* route). It is deliberately
    defensive: it resolves ONLY a ``state`` already registered by an in-flight flow
    (unknown / expired / replayed → 404, no side effect), is rate-limited, returns
    NO secret in the body, and never logs the code/state values. The SDK still
    performs the authoritative PKCE ``state`` check (secrets.compare_digest) on the
    receiving side."""
    from fastapi.responses import HTMLResponse

    ip = request.client.host if request.client else ""
    if not _oauth_cb_rate_ok(ip):
        return HTMLResponse(_OAUTH_CB_BUSY_HTML, status_code=429)

    params = request.query_params
    state = (params.get("state") or "").strip()
    code = (params.get("code") or "").strip()
    error = (params.get("error") or "").strip()

    # Well-formed-input gate before touching the registry.
    if not state or (not code and not error):
        return HTMLResponse(_OAUTH_CB_NOT_FOUND_HTML, status_code=404)

    # Native Gmail REST reuses this established public callback but keeps its
    # own PKCE registry/token exchange in google_oauth. Try it first; an unknown
    # state has no side effect and falls through to the generic MCP registry.
    try:
        from osia.integrations import google_oauth

        google_result = await asyncio.to_thread(
            google_oauth.complete_remote_authorization,
            state,
            code or None,
            error or None,
        )
    except Exception:
        google_result = {"matched": False, "success": False}
    if google_result.get("matched"):
        _GMAIL_OAUTH_ATTEMPT_OWNERS.clear()
        try:
            await _broadcast_gmail_rest_config()
        except Exception:
            pass
        if google_result.get("success"):
            return HTMLResponse(google_oauth._CLOSE_TAB_HTML, status_code=200)
        return HTMLResponse(_OAUTH_CB_NOT_FOUND_HTML, status_code=400)

    try:
        from osia.mcp_client.oauth import resolve_oauth_flow, _CLOSE_TAB_HTML
        matched = resolve_oauth_flow(state, code or None, error or None)
    except Exception:
        matched = False
        _CLOSE_TAB_HTML = ""  # noqa: F841 (only used on the success path below)

    if not matched:
        # Unknown / expired / already-consumed state — reveal nothing.
        return HTMLResponse(_OAUTH_CB_NOT_FOUND_HTML, status_code=404)
    if error:
        # Provider returned an error; the waiting flow was failed. Neutral page.
        return HTMLResponse(_OAUTH_CB_NOT_FOUND_HTML, status_code=400)
    return HTMLResponse(_CLOSE_TAB_HTML, status_code=200)


# WOR-97: safety bound on an interactive OAuth login. If the user abandons the
# flow (goes to create an account, cancels, closes the browser, tunnel Error
# 1033, …) the SDK's callback wait (osia.mcp_client.oauth.OAUTH_CALLBACK_TIMEOUT
# = 300s) is the ONLY thing that eventually resets the connector — a 5-minute
# "connecting…". This shorter bound resets it to DISCONNECTED far sooner. Kept
# strictly BELOW OAUTH_CALLBACK_TIMEOUT and high enough not to cut off a slow but
# legitimate account creation; the 300s SDK wait remains the hard backstop. NOT a
# change to OAUTH_CALLBACK_TIMEOUT (which is shared with the loopback PC flow).
OAUTH_SAFETY_TIMEOUT = 165.0

# WOR-97: strong refs to in-flight per-flow safety-watchdog tasks, keyed by the
# OSIA-generated flow_id (asyncio holds only a WEAK ref to a bare ensure_future
# result, so a fire-and-forget watchdog could be GC'd mid-sleep). The worker
# cancels its flow's watchdog on resolve via _cancel_oauth_watchdog; the task's
# own done-callback prunes the entry as a backstop.
_OAUTH_WATCHDOG_TASKS: dict = {}


def _cancel_oauth_watchdog(flow_id: str) -> None:
    """Cancel + drop the safety watchdog for ``flow_id``. MUST run on the FastAPI
    loop (the worker thread hops here via ``call_soon_threadsafe``). Best-effort;
    a no-op for an unknown/already-finished flow."""
    task = _OAUTH_WATCHDOG_TASKS.pop(flow_id, None)
    if task is not None and not task.done():
        try:
            task.cancel()
        except Exception:
            pass


async def _begin_oauth_tunnel_connect(server_id: str, saved: dict) -> dict:
    """WOR-96 PART 2a — start an OAuth connect that completes via the HTTPS tunnel
    callback (server process) and return the authorize URL the provider surfaces.

    Shared core of BOTH triggers: the loopback POST
    ``/internal/mcp_oauth_tunnel_connect`` (PART 1 — the desktop opens the returned
    URL in the PC browser) and the mobile WS ``mcp_connect`` handler for OAuth
    connectors (PART 2a — the URL is pushed to the requesting phone). The OAuth
    provider runs IN THIS process — where the tunnel redirect lands and the SDK's
    in-memory PKCE verifier/state live — on a daemon thread; we wait (bounded, off
    the event loop) for the authorize URL the provider surfaces.

    Returns ``{"ok": True, "auth_url": ..., "flow_id": ...}`` on success, or
    ``{"ok": False, "error": ...}`` — including ``"already connected"/"already
    connecting"`` so a SECOND trigger for the same connector returns at once
    instead of hanging the full 30s wait. Carries NO secret (only the public
    authorize URL). The token exchange completes later, server-side, when the user
    finishes login and the provider redirects to ``/oauth/callback``."""
    if not isinstance(saved, dict):
        return {"ok": False, "error": "unknown server"}
    if str(saved.get("auth_type") or "").strip().lower() != "oauth":
        return {"ok": False, "error": "not an oauth connector"}
    if str(saved.get("transport") or "").strip().lower() != "streamable_http":
        return {"ok": False, "error": "unsupported transport"}
    if not str(saved.get("url") or "").strip():
        return {"ok": False, "error": "missing url"}
    # Already CONNECTED → don't start a redundant flow (connect() would be an
    # idempotent no-op that never surfaces a new URL, so the wait would just time
    # out). Report the current state instead.
    #
    # WOR-97: a CONNECTING connector here is an OAuth flow whose interactive login
    # is still pending — or was ABANDONED and is parked in callback_handler. Do
    # NOT refuse: a re-tap must recover a stuck "connecting…" instead of being
    # locked out for the full callback timeout. The worker below cancels the
    # in-flight flow (cancel_pending) BEFORE starting a fresh connect, so the new
    # flow surfaces a new authorize URL. (Reached only for OAuth — validated
    # above; the cancel path is exercised solely by OAuth connectors.)
    try:
        from osia.mcp_client import get_manager, STATUS_CONNECTED
        cur = get_manager().status(server_id)
        if cur == STATUS_CONNECTED:
            return {"ok": False, "error": "already connected"}
    except Exception:
        pass

    from osia.mcp_client.oauth import (
        register_oauth_trigger_flow,
        wait_oauth_auth_url,
        fail_oauth_trigger_flow,
        cleanup_oauth_trigger_flow,
    )
    from osia.mcp_client.reconnect import build_headless_connect_cfg

    flow_id = secrets.token_urlsafe(16)
    loop = asyncio.get_running_loop()
    register_oauth_trigger_flow(flow_id)

    def _worker(sid=server_id, fid=flow_id, saved=saved, loop=loop):
        status = "failed"
        try:
            cfg = build_headless_connect_cfg(saved)
            cfg["oauth_callback_mode"] = "tunnel"
            cfg["oauth_flow_id"] = fid
            # WOR-203: every new Mobile flow uses the stable relay callback.
            from osia.oauth_relay import OAUTH_RELAY_REDIRECT_URI
            cfg["oauth_redirect_uri"] = OAUTH_RELAY_REDIRECT_URI
            from osia.mcp_client import get_manager, STATUS_CONNECTING
            mgr = get_manager()
            # WOR-97: if a PRIOR OAuth flow for this id is still parked CONNECTING
            # (abandoned login), cancel it first so this connect starts fresh and
            # surfaces a NEW authorize URL — instead of connect() hitting the
            # same-connector guard. Bounded (≤10s), off the event loop (we're on a
            # daemon thread); a no-op unless CONNECTING. connect()'s own guard
            # would also cancel it, but doing it here keeps the restart explicit
            # and fully unwound before the fresh connect re-registers the conn.
            if mgr.status(sid) == STATUS_CONNECTING:
                mgr.cancel_pending(sid)
            status = mgr.connect(cfg)
        except Exception as e:
            safe_code = str(getattr(e, "code", "oauth_connect_failed"))
            print(
                "[OSIA Server] mcp_oauth_tunnel_connect "
                f"kind=mcp accepted=false reason={safe_code}"
            )
            try:
                fail_oauth_trigger_flow(fid, safe_code)
            except Exception:
                pass
        # If connect resolved WITHOUT ever surfacing the authorize URL (e.g.
        # discovery/DCR failed before redirect_handler), release the waiter so the
        # trigger returns promptly. No-op once a URL was surfaced.
        try:
            from osia.mcp_client import STATUS_CONNECTED
            if status != STATUS_CONNECTED:
                fail_oauth_trigger_flow(fid, "connect did not complete")
            else:
                # Parity with the other connect paths: a successful connect marks
                # the connector enabled so the shared mcp_servers[].enabled flag is
                # authoritative and the desktop reconcile daemon mirrors it.
                _mcp_set_enabled(sid, True)
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast_mcp_status(_mcp_status_frame(sid)), loop
            )
        except Exception:
            pass
        # WOR-97: the flow resolved (connected or failed) — cancel ITS safety
        # watchdog so it can never reset a connector that completed, nor a LATER
        # flow for the same id. Thread→loop hop; best-effort.
        try:
            loop.call_soon_threadsafe(_cancel_oauth_watchdog, fid)
        except Exception:
            pass
        # Drop the trigger entry (the state→future entry is freed by the
        # callback_handler). Best-effort.
        try:
            cleanup_oauth_trigger_flow(fid)
        except Exception:
            pass

    threading.Thread(
        target=_worker, name="osia-mcp-oauth-tunnel", daemon=True
    ).start()

    # WOR-97: arm the safety watchdog for THIS flow. If the login is abandoned,
    # it resets the connector to DISCONNECTED at OAUTH_SAFETY_TIMEOUT (well before
    # the SDK's 300s callback backstop) and repaints every pill. Scoped to this
    # flow_id and cancelled by _worker on resolve, so it can only ever cancel the
    # flow it was armed for. Runs on THIS (FastAPI) loop.
    async def _safety_watchdog(sid=server_id, loop=loop):
        try:
            await asyncio.sleep(OAUTH_SAFETY_TIMEOUT)
        except asyncio.CancelledError:
            return
        try:
            from osia.mcp_client import get_manager, STATUS_CONNECTING
            mgr = get_manager()
            # Only act on a STILL-CONNECTING connector — a resolved flow (the
            # watchdog wasn't cancelled in time) is left untouched. cancel_pending
            # blocks (≤10s), so keep it OFF the event loop; it is itself a no-op
            # unless CONNECTING and defers to a real CONNECTED (TOCTOU-safe).
            if mgr.status(sid) != STATUS_CONNECTING:
                return
            await loop.run_in_executor(None, mgr.cancel_pending, sid)
            await _broadcast_mcp_status(_mcp_status_frame(sid))
        except Exception:
            pass

    _wd_task = asyncio.ensure_future(_safety_watchdog())
    _OAUTH_WATCHDOG_TASKS[flow_id] = _wd_task
    _wd_task.add_done_callback(
        lambda _t, fid=flow_id: _OAUTH_WATCHDOG_TASKS.pop(fid, None)
    )

    # Wait (bounded) OFF the event loop for the provider to surface the authorize
    # URL (discovery + DCR run first; usually a few seconds). The connect keeps
    # running on its daemon thread regardless.
    auth_url, err = await loop.run_in_executor(
        None, wait_oauth_auth_url, flow_id, 30.0
    )
    if auth_url:
        return {"ok": True, "auth_url": auth_url, "flow_id": flow_id}
    return {"ok": False, "error": err or "timed out waiting for authorize url"}


@app.post("/internal/mcp_oauth_tunnel_connect")
async def internal_mcp_oauth_tunnel_connect(request: Request):
    """Localhost-only trigger — WOR-96 PART 1. The DESKTOP posts ``{id}`` here to
    start an OAuth connect that completes via the HTTPS tunnel callback, IN THIS
    server process (so the redirect lands in the same process that holds the PKCE
    state). The flow itself lives in the shared ``_begin_oauth_tunnel_connect``
    (reused by the mobile WS ``mcp_connect`` handler in PART 2a); this route adds
    only the loopback host guard + body parse and returns the authorize URL so the
    DESKTOP opens it in the PC browser (this process's ``webbrowser`` is
    neutralised by the headless guard). The token exchange completes later when the
    user finishes login and the provider redirects to ``/oauth/callback``.

    Loopback-only (same host guard as ``/internal/mcp_sync``); carries only an id
    (no secret). Not part of the public mobile WS protocol."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_id = body.get("id")
    server_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not server_id:
        return {"ok": False, "error": "bad request"}

    saved = _mcp_find_saved_server(server_id)
    if not isinstance(saved, dict):
        return {"ok": False, "error": "unknown server"}
    return await _begin_oauth_tunnel_connect(server_id, saved)


@app.post("/internal/mcp_oauth_cancel")
async def internal_mcp_oauth_cancel(request: Request):
    """Localhost-only trigger — WOR-97. The DESKTOP posts ``{id}`` here to abort an
    in-flight OAuth login that runs in THIS server process (a tunnel/PART-1 flow:
    the desktop manager never had it CONNECTING, so it cannot cancel it locally).
    Routes to ``MCPManager.cancel_pending`` on a daemon thread (it BLOCKS ≤10s) and
    broadcasts the reset so every connected pill repaints to disconnected.

    Loopback-only (same host guard as ``/internal/mcp_sync``); carries only an id
    (no secret); idempotent no-op unless the connector is CONNECTING; never sets
    ``enabled``. Not part of the public mobile WS protocol (mobile uses the
    ``mcp_oauth_cancel`` WS frame instead)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_id = body.get("id")
    server_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not server_id:
        return {"ok": False, "error": "bad request"}

    loop = asyncio.get_running_loop()

    def _cancel_worker(sid=server_id, loop=loop):
        try:
            from osia.mcp_client import get_manager
            get_manager().cancel_pending(sid)
        except Exception as e:
            print(f"[OSIA Server] internal mcp_oauth_cancel worker {sid!r}: {e}")
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast_mcp_status(_mcp_status_frame(sid)), loop
            )
        except Exception:
            pass

    threading.Thread(
        target=_cancel_worker,
        name="osia-mcp-oauth-cancel-internal", daemon=True,
    ).start()
    return {"ok": True}


@app.post("/internal/mcp_oauth_switch")
async def internal_mcp_oauth_switch(request: Request):
    """Localhost-only trigger — WOR-98 (PC "Switch account"). The DESKTOP posts
    ``{id}`` here to switch the account on a CONNECTED OAuth connector: tear the
    session down, REVOKE the grant at the AS (RFC 7009, best-effort) AND forget the
    DCR client blob (``delete_mcp_oauth``) so the next Connect re-runs DCR + a fresh
    login (clean slate / different account). This is the loopback-bridge twin of the
    mobile ``mcp_disconnect`` WS frame carrying ``revoke=True, forget_client=True``
    — it runs the SAME server-side path (``_disconnect_worker``'s revoke+forget
    branch). Because ``oauth_tokens.json`` is shared with the desktop via
    CONFIG_DIR, this one route serves BOTH loopback and tunnel connectors. After
    the teardown it broadcasts the reset so every connected pill repaints to
    disconnected.

    Loopback-only (same host guard as ``/internal/mcp_sync``); carries only an id
    (no secret); persists ``enabled=False`` (no auto-reconnect). Not part of the
    public mobile WS protocol (mobile uses the ``mcp_disconnect`` WS frame)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_id = body.get("id")
    server_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not server_id:
        return {"ok": False, "error": "bad request"}

    # Read the saved connector to gate the OAuth revoke (only OAuth has a grant)
    # and supply the AS URL for revocation endpoint discovery. A missing/unknown
    # server still clears local state below (idempotent), it just skips revoke.
    saved = _mcp_find_saved_server(server_id)
    _sw_is_oauth = (
        isinstance(saved, dict)
        and str(saved.get("auth_type") or "").strip().lower() == "oauth"
    )
    _sw_url = str(saved.get("url") or "").strip() if isinstance(saved, dict) else ""

    loop = asyncio.get_running_loop()

    def _switch_worker(sid=server_id, loop=loop, is_oauth=_sw_is_oauth, url=_sw_url):
        # Mirror _disconnect_worker's revoke=True, forget=True path: session DOWN
        # → revoke at the AS (RFC 7009) → drop the WHOLE OAuth blob (token + DCR
        # client) → enabled=False → broadcast. Order matters: revoke reads the
        # token BEFORE delete_mcp_oauth removes it. Each step best-effort;
        # revoke_oauth_token NEVER raises and NEVER logs token values.
        try:
            from osia.mcp_client import get_manager
            get_manager().disconnect(sid)
        except Exception as e:
            print(f"[OSIA Server] internal mcp_oauth_switch worker {sid!r}: {e}")
        if is_oauth:
            try:
                from osia.mcp_client.oauth import revoke_oauth_token
                asyncio.run(revoke_oauth_token(sid, url))
            except Exception:
                pass
            try:
                from osia.config import delete_mcp_oauth
                delete_mcp_oauth(sid)
            except Exception:
                pass
        try:
            _mcp_set_enabled(sid, False)
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast_mcp_status(_mcp_status_frame(sid)), loop
            )
        except Exception:
            pass

    threading.Thread(
        target=_switch_worker,
        name="osia-mcp-oauth-switch-internal", daemon=True,
    ).start()
    return {"ok": True}


# NOTE (WOR-47): the startup ``_warm_models`` handler that eagerly pre-loaded
# faster-whisper was REMOVED deliberately — do NOT reintroduce it. Importing
# ``osia.voice.stt`` on the event-loop thread at cold boot pulled in
# faster-whisper's native ctranslate2/torch extensions (its module-top
# ``from faster_whisper import WhisperModel``), which intermittently deadlocked
# or hard-crashed (STATUS_ACCESS_VIOLATION 0xC0000005) and took the whole
# process down before it could bind port 8765. Whisper is now lazy-loaded on
# the first local STT request (``osia.voice.stt._get_model``), so the server
# binds immediately and never imports Whisper at boot. The watchdog /
# faulthandler / port-guard in run_server.py + run_service.py (e1950f2) is the
# complementary supervisor-side cause fix.


@app.on_event("startup")
async def _recover_websocket_commands_at_startup() -> None:
    try:
        recovered = await asyncio.to_thread(_DURABLE_TRANSPORT.recover_unstarted_commands)
        cancelled = await asyncio.to_thread(
            _reconcile_cancelled_websocket_commands,
        )
    except Exception as exc:
        print(f"[WS command] startup settlement unavailable: {type(exc).__name__}")
        return
    if recovered:
        print(f"[WS command] settled {len(recovered)} dead pre-turn owner(s)")
    if cancelled:
        print(
            "[WS command] settled "
            f"{len(cancelled)} durably cancelled accepted command(s)"
        )


@app.on_event("startup")
async def _reconcile_legacy_uploads_at_startup() -> None:
    """Remove only server-owned legacy orphans from the dedicated directory."""

    removed = reconcile_legacy_uploads(UPLOAD_DIR)
    if removed:
        print(f"[OSIA Server] removed {removed} orphaned legacy upload(s)")


@app.on_event("startup")
async def _recover_tool_attempts_at_startup() -> None:
    """Resolve orphaned durable attempts without replaying their action."""

    try:
        from osia.provider_continuation import (
            resume_or_reconcile_provider_attempt,
        )
        from osia.tool_attempts import (
            get_tool_attempt_store,
            recover_orphaned_tool_attempts,
        )

        recovered = await asyncio.to_thread(recover_orphaned_tool_attempts)
        attempt_store = get_tool_attempt_store()
        await asyncio.to_thread(attempt_store.drain_action_outbox)
        unresolved = await asyncio.to_thread(
            attempt_store.list_unresolved_action_attempts
        )
        provider_terminalized = False
        for attempt in unresolved:
            if not (attempt.get("provider_references") or {}):
                continue
            continuation = await asyncio.to_thread(
                resume_or_reconcile_provider_attempt,
                str(attempt.get("tool_attempt_id") or ""),
                store=attempt_store,
            )
            provider_terminalized = provider_terminalized or (
                continuation.state.value in {
                    "completed_with_receipt", "failed_before_effect",
                    "explicit_not_applied",
                }
            )
        if provider_terminalized:
            recovered.extend(
                await asyncio.to_thread(recover_orphaned_tool_attempts)
            )
        if recovered:
            print(
                f"[OSIA Server] reconciled {len(recovered)} orphaned "
                "tool attempt(s) without replay"
            )
    except Exception as exc:
        print(
            "[OSIA Server] tool-attempt recovery skipped "
            f"category={type(exc).__name__}"
        )






@app.on_event("startup")
async def _start_server_cron_manager() -> None:
    """Start the server-side cron owner when this process holds the cron lock."""

    global _SERVER_CRON_MANAGER, _SERVER_CRON_UNSUBSCRIBE, _SERVER_CRON_LOOP
    _SERVER_CRON_LOOP = asyncio.get_running_loop()
    try:
        from osia import scheduled_tasks
        from osia.cron import CronManager

        manager = CronManager(
            _server_run_cron_task,
            notify_task=_server_notify_cron_task,
        )
        _SERVER_CRON_MANAGER = manager
        _SERVER_CRON_UNSUBSCRIBE = scheduled_tasks.add_change_callback(manager.wake)
        manager.start()
    except Exception as e:
        print(f"[OSIA Server CRON] scheduler startup skipped: {e}")


@app.on_event("startup")
async def _register_mcp_status_listener() -> None:
    """FIX 1(c): when the MCPManager demotes a connector to FAILED on an
    auth-failing tool call — outside any connect/disconnect this server drives —
    push the corrected status to every connected phone, so a token that proves
    invalid on first use stops showing a false CONNECTED. The listener fires from
    an arbitrary tool-call thread, so it hops back onto THIS event loop to fan out
    the existing ``_mcp_status_frame`` (protocol unchanged). Server-process only;
    the desktop GUI reflects its OWN manager via polling. Best-effort."""
    loop = asyncio.get_running_loop()

    def _on_status_change(sid: str, status: str) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast_mcp_status(_mcp_status_frame(sid)), loop
            )
        except Exception:
            pass

    try:
        from osia.mcp_client import set_status_listener
        set_status_listener(_on_status_change)
        print("[OSIA Server] MCP status-change listener registered.")
    except Exception as e:
        print(f"[OSIA Server] MCP status listener not registered: {e}")


@app.on_event("startup")
async def _resolve_x11_on_startup() -> None:
    """Resolve DISPLAY/XAUTHORITY before any browser tool can run.

    Fires once at server startup — well before the first WebSocket request can
    trigger a CDP/Firefox-BiDi browser launch — so the headless systemd service
    reaches the X display without a (stale-prone) manual systemd override.
    Linux-only effect; see ``_ensure_x11_env``.
    """
    _ensure_x11_env()


@app.on_event("startup")
async def _reconnect_mcp_connectors_on_startup() -> None:
    """WOR-44: auto-reconnect every ``enabled`` MCP connector this SERVER owns,
    reusing the SAME ``MCPManager.connect()`` path as the ``mcp_connect`` WS
    handler (idempotent; tools register inside connect()).

    OAuth is SKIPPED (``allow_oauth=False``): the interactive login opens a
    browser ON THE PC with nobody there — the same rationale as the mobile
    ``mcp_connect`` OAuth refusal above. Only stdio + token connectors reconnect
    headlessly, from the stored ``api_keys.json`` token.

    ``connect()`` blocks (bounded) and must NEVER run on the event loop, so the
    whole sweep runs on a daemon worker thread; each ATTEMPTED connector
    broadcasts the existing ``_mcp_status_frame`` (protocol unchanged) so every
    connected phone repaints. Best-effort: a failure here never crashes the
    server."""
    loop = asyncio.get_running_loop()

    def _worker() -> None:
        def _on_connected(sid: str, status: str) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    _broadcast_mcp_status(_mcp_status_frame(sid)), loop
                )
            except Exception:
                pass
        try:
            from osia.mcp_client.reconnect import reconnect_enabled_connectors
            reconnect_enabled_connectors(
                allow_oauth=False, on_connected=_on_connected
            )
        except Exception as e:
            print(f"[OSIA Server] MCP auto-reconnect skipped: {e}")

    threading.Thread(
        target=_worker, name="osia-mcp-reconnect", daemon=True
    ).start()


@app.on_event("startup")
async def _disable_browser_on_server() -> None:
    """SECURITY (Phase 1 §5): the headless mobile server must NEVER open a web
    browser. Real browser automation drives CDP / Firefox-BiDi directly
    (osia/tools/browser.py) and never calls ``webbrowser.open``, so neutralising
    it here is safe and is pure defence-in-depth: it guarantees that even the MCP
    SDK's one residual browser-pop case (a server-revoked-but-future-dated OAuth
    token surviving ``_oauth_token_fresh``) can never pop a login window on a
    machine with nobody there. The /internal/mcp_sync connect path is already
    fresh-token gated; this turns "extremely unlikely" into "structurally
    impossible".

    Server-process ONLY: this runs when uvicorn starts the app, NOT on import, so
    the desktop GUI process (which legitimately opens the OAuth browser on the
    PC) is untouched."""
    try:
        import webbrowser
        webbrowser.open = lambda *a, **k: False
        webbrowser.open_new = lambda *a, **k: False
        webbrowser.open_new_tab = lambda *a, **k: False
        print("[OSIA Server] webbrowser.open disabled (headless browser-pop guard).")
    except Exception as e:
        print(f"[OSIA Server] webbrowser guard not applied: {e}")


@app.on_event("shutdown")
async def _cleanup_on_server_shutdown() -> None:
    """Canonical server cleanup: cron owner, media workers and source cleanup.

    WOR-303: no execution engine owns shutdown any more; the canonical Chat
    runtime is per-request and needs no stop hook.
    """

    global _SERVER_CRON_MANAGER, _SERVER_CRON_UNSUBSCRIBE
    if _SERVER_CRON_UNSUBSCRIBE is not None:
        try:
            _SERVER_CRON_UNSUBSCRIBE()
        except Exception:
            pass
        _SERVER_CRON_UNSUBSCRIBE = None
    if _SERVER_CRON_MANAGER is not None:
        try:
            _SERVER_CRON_MANAGER.stop()
        except Exception as e:
            print(f"[OSIA Server CRON] stop on shutdown failed: {e}")
        _SERVER_CRON_MANAGER = None
    media_tasks = list(_POST_TURN_MEDIA_TASKS)
    for task in media_tasks:
        task.cancel()
    if media_tasks:
        await asyncio.gather(*media_tasks, return_exceptions=True)
    _POST_TURN_MEDIA_TASKS.clear()
    _POST_TURN_MEDIA_BY_CONV.clear()
    _POST_TURN_MEDIA_META_BY_CONV.clear()
    source_cleanup_tasks = list(_PENDING_SOURCE_CLEANUP_TASKS)
    for task in source_cleanup_tasks:
        task.cancel()
    if source_cleanup_tasks:
        await asyncio.gather(*source_cleanup_tasks, return_exceptions=True)
    _PENDING_SOURCE_CLEANUP_TASKS.clear()


@app.get("/")
async def root():
    return {"status": "ok", "service": "osia-mobile-server"}


@app.get("/health")
async def health():
    runtime = {
        "pid": os.getpid(),
        "checkout_root": _SERVER_RUNTIME_CHECKOUT_ROOT,
        "entrypoint": _SERVER_RUNTIME_ENTRYPOINT,
        "executable": _SERVER_RUNTIME_EXECUTABLE,
        "source_mode": _SERVER_RUNTIME_SOURCE_MODE,
        "source_fingerprint": _SERVER_RUNTIME_SOURCE_FINGERPRINT,
        "runtime_id": _SERVER_RUNTIME_ID,
    }
    if _SERVER_RUNTIME_SOURCE_MODE == "source" and not _SERVER_RUNTIME_SOURCE_FINGERPRINT:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "osia-mobile-server",
                "runtime": runtime,
                "reason_code": "source_fingerprint_unavailable",
            },
        )
    return {
        "status": "healthy",
        "service": "osia-mobile-server",
        "runtime": runtime,
    }


# ---------------------------------------------------------------------------
# Live system-metrics heartbeat (pushed to connected mobile clients)
# ---------------------------------------------------------------------------
def _read_cpu_temp() -> float | None:
    """Best-effort CPU temperature in °C, or None if unavailable.

    ``psutil.sensors_temperatures`` is Linux-only (absent on Windows/macOS,
    hence the getattr guard). Prefer well-known CPU sensor chips, then fall
    back to the first reading any chip reports.
    """
    try:
        sensors = getattr(psutil, "sensors_temperatures", None)
        if not sensors:
            return None
        temps = sensors()
        if not temps:
            return None
        for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            entries = temps.get(chip)
            if entries:
                return float(entries[0].current)
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass
    return None


def _read_gpu_temp() -> float | None:
    """Best-effort NVIDIA GPU temperature in °C via pynvml, or None.

    pynvml is an optional dependency; any failure (package missing, no NVIDIA
    driver, no GPU) yields None rather than raising.
    """
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            temp = pynvml.nvmlDeviceGetTemperature(
                handle, pynvml.NVML_TEMPERATURE_GPU
            )
            return float(temp)
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None


def _collect_heartbeat_data() -> dict:
    """Snapshot system metrics for a ``heartbeat_data`` frame.

    ``cpu_percent(interval=None)`` is non-blocking: it reports usage since the
    previous call, and the push loop's cadence supplies that spacing, so the
    event loop is never blocked.
    """
    vm = psutil.virtual_memory()
    return {
        "type": "heartbeat_data",
        "cpu_percent": float(psutil.cpu_percent(interval=None)),
        "ram_mb": int(vm.used / (1024 * 1024)),
        "ram_percent": float(vm.percent),
        "disk_percent": float(psutil.disk_usage(str(Path.home().anchor)).percent),
        "cpu_temp": _read_cpu_temp(),
        "gpu_temp": _read_gpu_temp(),
        "timestamp": datetime.now().isoformat(),
    }


async def _heartbeat_push_loop(channel: "_OutboundChannel") -> None:
    """Push ``heartbeat_data`` frames to one authenticated client.

    Settings are re-read every tick so toggling ``heartbeat_enabled`` or
    changing ``heartbeat_interval_seconds`` takes effect live (no reconnect).
    The cadence is seconds-granularity and is deliberately distinct from the
    desktop monitor's ``heartbeat_interval_minutes`` (osia/heartbeat.py).
    Cancelled by ``websocket_endpoint`` on disconnect.
    """
    # Prime cpu_percent so the first emitted sample reflects real usage rather
    # than psutil's 0.0 "first call" placeholder.
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    while True:
        try:
            from osia.config import load_settings
            settings = load_settings()
            try:
                interval = float(settings.get("heartbeat_interval_seconds", 5) or 5)
            except (TypeError, ValueError):
                interval = 5.0
            if interval <= 0:
                interval = 5.0
            try:
                from osia.tool_attempts import get_tool_attempt_coordinator

                get_tool_attempt_coordinator().observe_lifecycle_tick(
                    expected_interval=interval,
                )
            except Exception:
                pass
            if settings.get("heartbeat_enabled", False):
                await channel.send_json(_collect_heartbeat_data())
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A transient sampling/send failure must not kill the loop; the
            # main read loop owns disconnect detection and cancellation.
            print(f"[OSIA Server] heartbeat push error: {e}")
            await asyncio.sleep(5)


@app.post("/internal/tool_attempt_reconcile")
async def _internal_tool_attempt_reconcile(request: Request) -> dict:
    """Windows resume hook: trigger due read-only checks on the live owner."""

    host = str(getattr(request.client, "host", "") or "")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="loopback_required")
    from osia.tool_attempts import get_tool_attempt_coordinator

    triggered = await asyncio.to_thread(
        get_tool_attempt_coordinator().trigger_recovery,
        reason="windows_resume_event",
        overdue_only=True,
    )
    return {"ok": True, "triggered": len(triggered)}


# ---------------------------------------------------------------------------
# Per-connection agent reuse (perf fix #3)
# ---------------------------------------------------------------------------
# Soft cap on how many OSIAAgent instances ONE WebSocket connection keeps alive
# at once (one per conversation it has touched). Each agent owns an OpenAI/httpx
# client (SSL context + connection pool), so the per-connection cache is
# LRU-bounded to keep a session that hops across many conversations from leaking
# sockets/SSL contexts.
_AGENT_CACHE_MAX = 8


def _close_agent_client(agent) -> None:
    """Release ``agent``'s OpenAI/httpx client so its sockets and SSL context
    are freed promptly. Safe on a half-built agent; never raises. The caller
    MUST ensure the agent is idle (no worker thread mid-``run``) before calling
    — the OpenAI client is uncancellable from here and closing one in use would
    break the in-flight turn."""
    try:
        client = getattr(agent, "client", None)
        if client is not None:
            client.close()
    except Exception:
        pass


def _agent_needs_model_apply(agent, selected_model: str) -> bool:
    """Whether ``agent``'s client must be (re)built via ``set_model`` for
    ``selected_model`` this turn.

    Perf fix #3 reuses one OSIAAgent across the serialized turns of a single
    conversation, so ``set_model`` — which constructs a fresh ``OpenAI()``
    client (SSL context + certifi load, ~100-300 ms on Windows) — must run ONLY
    when the agent does not already hold a *clean primary* client for exactly
    this model. "Clean primary" = the last model we applied is this one, the
    agent is still on it (the task-hierarchy router can switch ``self.model``
    mid-turn), the API-key cursor is back at the primary key, and the endpoint
    is the primary (not a Bloc-5 fallback). ANY drift -> rebuild, which is
    exactly what the old per-message ``OSIAAgent() + set_model`` did every turn,
    so behaviour (per-turn reset to the primary key/endpoint and a fresh key
    chain) is preserved on every non-hot path; only the redundant rebuild on the
    hot path (same model, no prior error) is skipped.
    """
    if getattr(agent, "_applied_selected_model", None) != selected_model:
        return True                       # first turn for this agent, or model changed
    if getattr(agent, "model", None) != selected_model:
        return True                       # a prior turn's router left a different model
    if getattr(agent, "_api_key_index", 0) != 0:
        return True                       # a prior turn rotated to a fallback API key
    if getattr(agent, "_active_base_url", None) != getattr(agent, "_primary_base_url", None):
        return True                       # a prior turn fell back to the secondary endpoint
    return False


_SERVER_CRON_MANAGER = None
_SERVER_CRON_UNSUBSCRIBE = None
_SERVER_CRON_AGENT = None
_SERVER_CRON_LOOP = None


def _server_run_cron_task(
    task_text: str,
    conversation_id: str | None = None,
    model: dict | None = None,
    scheduled_execution: dict | None = None,
) -> dict:
    """Run one scheduled task from the background mobile server process."""

    global _SERVER_CRON_AGENT
    from osia.agent import OSIAAgent, clean_response
    from osia.config import load_settings

    if _SERVER_CRON_AGENT is None:
        _SERVER_CRON_AGENT = OSIAAgent()
    agent = _SERVER_CRON_AGENT
    model_meta = model if isinstance(model, dict) else {}
    selected_model = (
        model_meta.get("id") or load_settings().get("selected_model") or ""
    )
    provider = model_meta.get("provider") or None
    if selected_model:
        try:
            agent.set_model(selected_model, provider=provider)
        except Exception as e:
            print(f"[OSIA Server CRON] set_model({selected_model!r}) failed: {e}")

    conversation_stale = False
    terminal_turn_id = str(
        (scheduled_execution or {}).get("occurrence_id")
        or ("cron-" + secrets.token_hex(16))
    )
    conv = conversations.get_conversation(conversation_id) if conversation_id else None
    if conversation_id and conv is None:
        conversation_stale = True
        print(
            f"[OSIA Server CRON] linked conversation {conversation_id} not found; "
            "running task without conversation persistence"
        )
    try:
        agent._disable_hierarchy_for_turn = True
        if conv is not None:
            history = conv.get("messages") or []
            if scheduled_execution is not None:
                reply = agent.run_scheduled(
                    task_text,
                    scheduled_execution,
                    terminal_turn=True,
                    turn_id=terminal_turn_id,
                    history=history,
                    current_conversation_id=conversation_id,
                )
            else:
                reply = agent.run_terminal_turn(
                    task_text,
                    turn_id=terminal_turn_id,
                    history=history,
                    current_conversation_id=conversation_id,
                )
            display = clean_response(reply or "")
            conversations.commit_message(
                conversation_id,
                "user",
                "Scheduled task" if scheduled_execution is not None else task_text,
            )
            assistant_receipt = conversations.commit_message(
                conversation_id,
                "assistant",
                display,
            )
            try:
                loop = _SERVER_CRON_LOOP
                if loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        _broadcast_committed_receipt(
                            conversation_id,
                            assistant_receipt,
                        ),
                        loop,
                    )
            except Exception:
                pass
        else:
            agent.reset()
            if scheduled_execution is not None:
                reply = agent.run_scheduled(
                    task_text,
                    scheduled_execution,
                    terminal_turn=True,
                    turn_id=terminal_turn_id,
                )
            else:
                reply = agent.run_terminal_turn(
                    task_text,
                    turn_id=terminal_turn_id,
                )
            display = clean_response(reply or "")
        terminal = agent.terminality_result(terminal_turn_id)
        terminal_state = str((terminal or {}).get("terminal_state") or "")
        if conversation_stale:
            return {
                "status": terminal_state or "blocked_verified",
                "conversation_stale": True,
                "message": "linked conversation not found; ran unlinked",
            }
        if scheduled_execution is not None:
            from osia import scheduled_tasks

            blocked = scheduled_tasks.scheduled_execution_block_state(
                scheduled_execution
            )
            if blocked:
                return {"status": "failed", "message": blocked}
        return {"status": terminal_state or "blocked_verified"}
    except Exception as e:
        print(f"[OSIA Server CRON] task error: {e}")
        traceback.print_exc()
        return {"status": "failed", "message": str(e)}
    finally:
        agent._disable_hierarchy_for_turn = False


def _server_notify_cron_task(
    *,
    task_id: str | None,
    title: str | None,
    status: str,
    conversation_id: str | None,
) -> None:
    from osia.notifications import notify

    if status == "failed":
        body = "Tâche planifiée échouée"
    else:
        body = "Tâche planifiée terminée"
    notify("OSIA", body)
    frame = _cron_notification_frame({
        "task_id": task_id,
        "title": "Scheduled task",
        "status": status,
        "safe_category": "scheduled_task",
        "conversation_id": conversation_id,
    })
    _remember_cron_notification(frame)
    loop = _SERVER_CRON_LOOP
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast_cron_notification(frame), loop)
    except Exception as e:
        print(f"[OSIA Server CRON] notification broadcast failed: {e}")


_DOCUMENT_PROTOCOL_LOCK = threading.Lock()
_DOCUMENT_PROTOCOL_SESSION_ID = "document-session:" + secrets.token_urlsafe(24)
_DOCUMENT_PROTOCOL_STATE = {"scopes": OrderedDict()}
_DOCUMENT_PHASE_RANK = {
    "identifying": 0,
    "inspecting": 1,
    "extracting": 2,
    "indexing": 3,
    "ready": 4,
}
_DOCUMENT_SAFE_FAILURES = frozenset({
    "active_content_detected", "corrupted_document", "drm_protected",
    "encrypted_document", "expired_source", "external_reference_detected",
    "extraction_failure", "file_identity_mismatch", "invalid_source_reference",
    "missing_source", "ocr_unavailable", "protected_document",
    "resource_limit_exceeded", "source_changed", "source_unavailable",
    "unauthorized_source", "unsupported_format", "unsupported_legacy_format",
})
_DOCUMENT_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DOCUMENT_ITEM_RE = re.compile(r"^item_[0-9a-f]{24}$")
_DOCUMENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,31}$")
_BROWSER_PROTOCOL_SESSION_ID = "browser-session:" + secrets.token_urlsafe(24)
_BROWSER_PROTOCOL_SEQUENCE = 0
_BROWSER_PROGRESS_LOOP = None


def _document_progress_frame(
    payload: dict,
    *,
    owner_device: str = "",
    snapshot: bool = False,
) -> dict | None:
    """Validate and project one value-free document phase transition."""

    if not isinstance(payload, dict):
        return None
    scope_type = str(payload.get("scope_type") or "")
    scope_id = str(payload.get("scope_id") or "")
    item_id = str(payload.get("item_id") or "")
    phase = str(payload.get("phase") or "")
    if (
        scope_type != "chat"
        or not _DOCUMENT_SAFE_ID_RE.fullmatch(scope_id)
        or not _DOCUMENT_ITEM_RE.fullmatch(item_id)
        or phase not in {*_DOCUMENT_PHASE_RANK, "failed"}
    ):
        return None
    revision = payload.get("revision", 0)
    if type(revision) is not int or revision < 0:
        return None
    document_type = str(payload.get("document_type") or "document").lower()
    if not _DOCUMENT_TYPE_RE.fullmatch(document_type):
        document_type = "document"
    failure_category = ""
    if phase == "failed":
        candidate = str(payload.get("failure_category") or "")
        failure_category = (
            candidate if candidate in _DOCUMENT_SAFE_FAILURES
            else "extraction_failure"
        )
    raw_counts = payload.get("counts")
    counts = None
    if isinstance(raw_counts, dict) and set(raw_counts) == {"completed", "total"}:
        completed = raw_counts.get("completed")
        total = raw_counts.get("total")
        if (
            type(completed) is int and type(total) is int
            and 0 <= completed <= total <= 1_000_000
        ):
            counts = {"completed": completed, "total": total}

    scope_key = (scope_type, scope_id)
    with _DOCUMENT_PROTOCOL_LOCK:
        scopes = _DOCUMENT_PROTOCOL_STATE["scopes"]
        scope = scopes.get(scope_key)
        if scope is None:
            scope = {
                "sequence": 0,
                "revision": revision,
                "owner_device": str(owner_device or "")[:128],
                "terminal": False,
                "items": OrderedDict(),
            }
            scopes[scope_key] = scope
        if scope.get("terminal") or revision < int(scope.get("revision", 0)):
            return None
        if revision > int(scope.get("revision", 0)):
            scope["revision"] = revision
        item = scope["items"].get(item_id)
        if item is not None:
            if item.get("terminal"):
                return None
            prior_rank = int(item.get("rank", -1))
            if phase != "failed" and _DOCUMENT_PHASE_RANK[phase] <= prior_rank:
                return None
        rank = (
            int(item.get("rank", -1)) if item is not None else -1
        ) if phase == "failed" else _DOCUMENT_PHASE_RANK[phase]
        terminal = phase in {"ready", "failed"}
        sequence = int(scope.get("sequence", 0)) + 1
        scope["sequence"] = sequence
        item_state = {
            "item_id": item_id,
            "document_type": document_type,
            "phase": phase,
            "terminal": terminal,
            "failure_category": failure_category,
            "counts": counts,
            "rank": rank,
            "sequence": sequence,
        }
        scope["items"][item_id] = item_state
        scope["items"].move_to_end(item_id)
        while len(scope["items"]) > 10:
            scope["items"].popitem(last=False)
        scopes.move_to_end(scope_key)
        while len(scopes) > 256:
            scopes.popitem(last=False)

    return {
        "type": "document_progress",
        "schema_version": 1,
        "event_id": "document-event:" + secrets.token_urlsafe(24),
        "server_id": _MOBILE_SERVER_ID,
        "session_id": _DOCUMENT_PROTOCOL_SESSION_ID,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "plan_revision": revision,
        "sequence": sequence,
        "item_id": item_id,
        "document_type": document_type,
        "phase": phase,
        "terminal": terminal,
        "failure_category": failure_category,
        "counts": counts,
        "snapshot": bool(snapshot),
    }


def _document_progress_snapshot(
    scope_type: str,
    scope_id: str,
    *,
    owner_device: str = "",
) -> list[dict]:
    """Return latest safe item states for one authenticated scope."""

    scope_key = (str(scope_type or ""), str(scope_id or ""))
    frames = []
    with _DOCUMENT_PROTOCOL_LOCK:
        scope = _DOCUMENT_PROTOCOL_STATE["scopes"].get(scope_key)
        if scope is None:
            return []
        stored_owner = str(scope.get("owner_device") or "")
        if stored_owner and stored_owner != str(owner_device or ""):
            return []
        for item in scope.get("items", {}).values():
            scope["sequence"] = int(scope.get("sequence", 0)) + 1
            item["sequence"] = int(scope["sequence"])
            frames.append({
                "type": "document_progress",
                "schema_version": 1,
                "event_id": "document-event:" + secrets.token_urlsafe(24),
                "server_id": _MOBILE_SERVER_ID,
                "session_id": _DOCUMENT_PROTOCOL_SESSION_ID,
                "scope_type": scope_key[0],
                "scope_id": scope_key[1],
                "plan_revision": int(scope.get("revision", 0)),
                "sequence": int(scope["sequence"]),
                "item_id": item["item_id"],
                "document_type": item["document_type"],
                "phase": item["phase"],
                "terminal": bool(item["terminal"]),
                "failure_category": item["failure_category"],
                "counts": item["counts"],
                "snapshot": True,
            })
    return frames


def _retire_document_scope(scope_type: str, scope_id: str) -> None:
    with _DOCUMENT_PROTOCOL_LOCK:
        scope = _DOCUMENT_PROTOCOL_STATE["scopes"].get((scope_type, scope_id))
        if scope is not None:
            scope["terminal"] = True
























def _snapshot_ws_clients() -> tuple:
    """Race-tolerant snapshot of ``_WS_CLIENTS``. Broadcasts run on a daemon
    thread while the event-loop thread may add (auth) / discard (disconnect) —
    set iteration can raise "changed size during iteration"; retry briefly."""
    for _ in range(3):
        try:
            return tuple(_WS_CLIENTS)
        except RuntimeError:
            continue
    return ()




def _browser_progress_frame(payload: dict | None = None, *, snapshot=False) -> dict:
    """Build a value-free, versioned projection of browser progress."""
    global _BROWSER_PROTOCOL_SEQUENCE
    payload = payload if isinstance(payload, dict) else {}
    allowed_phases = {
        "snapshot", "waiting", "downloading", "finalizing", "complete",
        "failed", "cancelled",
    }
    phase = "snapshot" if snapshot else str(payload.get("phase") or "")
    if phase not in allowed_phases:
        phase = "failed"
    run_id = "chat"
    safe_site = ""
    browser_session_id = ""
    try:
        from osia.tools.browser_controller import _browser_owner_snapshot
        from osia.tools.browser_state import safe_origin

        owner_snapshot = _browser_owner_snapshot()
        safe_site = safe_origin(owner_snapshot.get("activeUrl"))[:512]
        browser_state = owner_snapshot.get("browserState")
        if isinstance(browser_state, dict):
            browser_session_id = str(
                browser_state.get("browser_session_id") or ""
            )[:96]
    except Exception:
        pass
    _BROWSER_PROTOCOL_SEQUENCE += 1
    sequence = _BROWSER_PROTOCOL_SEQUENCE
    byte_size = payload.get("bytes")
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
        byte_size = None
    artifact_summary = (
        {"private": True, "size": byte_size}
        if byte_size is not None and phase in {"finalizing", "complete"}
        else None
    )
    return {
        "type": "browser_progress",
        "schema_version": 1,
        "event_id": "browser-event:" + secrets.token_urlsafe(24),
        "sequence": sequence,
        "server_id": _MOBILE_SERVER_ID,
        "session_id": _BROWSER_PROTOCOL_SESSION_ID,
        "browser_session_id": browser_session_id,
        "run_id": run_id,
        "plan_revision": 0,
        "authorization_revision": 0,
        "safe_operation_phase": phase,
        "safe_site_display": safe_site,
        "safe_progress_text": {
            "snapshot": "Browser state refreshed",
            "waiting": "Waiting for the download",
            "downloading": "Downloading to private storage",
            "finalizing": "Finalizing the private artifact",
            "complete": "Private artifact ready",
            "failed": "Browser operation needs attention",
            "cancelled": "Browser operation cancelled",
        }[phase],
        "artifact_safe_summary": artifact_summary,
        "terminal": phase in {"complete", "failed", "cancelled"},
    }


def _on_browser_progress(payload: dict) -> None:
    """Thread-safe callback installed on the browser owner."""
    try:
        frame = _browser_progress_frame(payload)
        loop = _BROWSER_PROGRESS_LOOP
        if loop is None:
            return
        for channel in _snapshot_ws_clients():
            try:
                asyncio.run_coroutine_threadsafe(channel.send_json(frame), loop)
            except Exception:
                pass
    except Exception:
        print("[WOR-186] browser progress callback failed")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    authenticated = False
    authorization_origin_ref = "mobile_" + secrets.token_hex(16)
    # Single-writer funnel + its drain task. From here on, the receive loop,
    # streaming tasks and the heartbeat NEVER call ``ws.send_*`` directly —
    # they enqueue on ``out`` and ``writer_task`` does the real sends (so two
    # tasks can't corrupt the connection with concurrent writes). The auth
    # handshake and the restart ack are the only intentional exceptions (see
    # the inline notes): they send on ``ws`` directly because no other sender
    # is active yet / the process is about to be replaced.
    out = _OutboundChannel()
    writer_task: asyncio.Task = asyncio.create_task(_writer(ws, out))
    loop = asyncio.get_running_loop()
    # Stable opaque connection identity used as the durable-transport client id
    # when the device does not present a bound mobile identity yet.
    connection_id = "mobile-connection:" + secrets.token_urlsafe(24)
    mobile_device_id = ""

    def _bind_mobile_device(value) -> bool:
        nonlocal mobile_device_id
        device_id = _mobile_safe_id(value)
        if not device_id:
            return False
        if mobile_device_id and mobile_device_id != device_id:
            return False
        mobile_device_id = device_id
        return True

    unsubscribe_scheduled_tasks = None
    unsubscribe_api_key_changes = None
    unsubscribe_setting_changes = None
    unsubscribe_memory_changes = None
    unsubscribe_account_changes = None
    try:
        from osia import scheduled_tasks as _scheduled_tasks

        def _scheduled_tasks_changed() -> None:
            try:
                cfg = _build_config_snapshot()
                asyncio.run_coroutine_threadsafe(
                    _broadcast_config({"type": "config", "config": cfg}), loop
                )
            except Exception as e:
                print(f"[OSIA Server] scheduled-task config broadcast failed: {e}")

        unsubscribe_scheduled_tasks = _scheduled_tasks.add_change_callback(
            _scheduled_tasks_changed
        )
    except Exception as e:
        print(f"[OSIA Server] scheduled-task broadcast hook unavailable: {e}")
    # Per-session state.
    # ``session_history`` is the OpenAI-format message list (sans system
    # prompt) handed to the agent each turn. ``active_conv_id`` is the
    # conversation file currently being VIEWED / targeted for new turns. Both
    # reset on disconnect (function returns => GC).
    session_history: list = []
    active_conv_id: str | None = None
    # Files uploaded by the client that haven't been consumed by a turn yet.
    # The next text/audio turn consumes and clears this list.
    pending_files: list[str] = []
    pending_file_display_names: dict[str, str] = {}
    legacy_uploads = LegacyUploadManager(UPLOAD_DIR)
    attachment_batches = AttachmentBatchManager(ATTACHMENT_UPLOAD_DIR)

    async def _expire_attachment_batches() -> None:
        while True:
            await asyncio.sleep(15)
            attachment_batches.expire()

    attachment_cleanup_task = asyncio.create_task(_expire_attachment_batches())
    # Live system-metrics push task (heartbeat_data frames). Created once after
    # a successful auth, cancelled in the finally block on disconnect.
    heartbeat_task: asyncio.Task | None = None
    remote_catalogue_refresh_pending = False
    chatgpt_login_started_here = False
    catalogue_change_task: asyncio.Task | None = None
    account_change_task: asyncio.Task | None = None
    canonical_config_task: asyncio.Task | None = None
    catalogue_change_again = False
    account_change_again = False
    canonical_config_again = False
    canonical_config_keys: set[str] = set()

    async def _send_model_catalogue(
        *, refresh_local: bool = True, refresh_account: bool = True
    ) -> None:
        """Send one safe PC-authoritative catalogue without blocking the loop."""
        from osia.mobile_model_catalogue import build_catalogue

        catalogue = await asyncio.to_thread(
            build_catalogue,
            refresh_local=refresh_local,
            refresh_account=refresh_account,
        )
        await out.send_json({
            "type": "models_list",
            "catalogue": {
                "version": catalogue["version"],
                "providers": catalogue["providers"],
            },
            # Backward-compatible flat view for existing mobile consumers.
            "models": catalogue["models"],
            "current": catalogue["current"],
            "current_provider": catalogue["currentProvider"],
            "account": catalogue["account"],
        })

    def _canonical_config_changed(key: str) -> None:
        """Push the safe PC-owned snapshot after any canonical setting change."""
        if not authenticated:
            return
        loop.call_soon_threadsafe(_schedule_canonical_config_refresh, str(key))

    def _memory_changed(memory: dict) -> None:
        """Push PC-owned memory changes to this authenticated mobile client."""
        if not authenticated:
            return

        async def send_snapshot() -> None:
            try:
                await out.send_json({"type": "memory_updated", "memory": memory})
            except Exception:
                pass

        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(send_snapshot())
                if authenticated else None
            )
        except RuntimeError:
            pass

    def _schedule_canonical_config_refresh(key: str) -> None:
        nonlocal canonical_config_task, canonical_config_again
        canonical_config_keys.add(key)
        if canonical_config_task is not None and not canonical_config_task.done():
            canonical_config_again = True
            return

        async def refresh() -> None:
            nonlocal canonical_config_task, canonical_config_again
            try:
                while True:
                    canonical_config_again = False
                    changed = set(canonical_config_keys)
                    canonical_config_keys.clear()
                    cfg = await asyncio.to_thread(_build_config_snapshot)
                    await out.send_json({"type": "config", "config": cfg})
                    if changed.intersection({"selected_model", "selected_model_provider"}):
                        await _send_model_catalogue(
                            refresh_local=False, refresh_account=False
                        )
                    if not canonical_config_again:
                        break
            except Exception as exc:
                print(
                    "[OSIA Server] canonical config push failed: "
                    f"{type(exc).__name__}"
                )
            finally:
                canonical_config_task = None

        canonical_config_task = asyncio.create_task(refresh())

    def _request_remote_catalogue_refresh() -> None:
        """Coalesce remote discovery and push its safe result on this socket."""
        nonlocal remote_catalogue_refresh_pending
        from osia.mobile_model_catalogue import request_remote_refresh

        if remote_catalogue_refresh_pending:
            return
        remote_catalogue_refresh_pending = True

        def ready() -> None:
            nonlocal remote_catalogue_refresh_pending
            remote_catalogue_refresh_pending = False
            try:
                asyncio.run_coroutine_threadsafe(
                    _send_model_catalogue(
                        refresh_local=False, refresh_account=False
                    ),
                    loop,
                )
            except Exception:
                pass

        request_remote_refresh(ready)

    def _push_chatgpt_account_state(state: dict) -> None:
        """Thread-safe, sanitized account update callback for login workers."""
        nonlocal chatgpt_login_started_here
        if not state.get("connecting"):
            chatgpt_login_started_here = False
        try:
            asyncio.run_coroutine_threadsafe(
                out.send_json({"type": "chatgpt_account_state", "account": state}),
                loop,
            )
            if not state.get("connecting"):
                asyncio.run_coroutine_threadsafe(
                    _send_model_catalogue(
                        refresh_local=False, refresh_account=False
                    ),
                    loop,
                )
        except Exception:
            pass

    def _api_key_changed(provider_id: str) -> None:
        """Coalesce canonical PC/mobile key changes into one catalogue push."""
        from osia.models import API_PROVIDERS

        if not authenticated:
            return
        _canonical_config_changed("api_key_status")
        if provider_id not in API_PROVIDERS:
            return

        def schedule() -> None:
            nonlocal catalogue_change_task, catalogue_change_again
            if catalogue_change_task is not None and not catalogue_change_task.done():
                catalogue_change_again = True
                return

            async def refresh() -> None:
                nonlocal catalogue_change_task, catalogue_change_again
                try:
                    while True:
                        catalogue_change_again = False
                        await _send_model_catalogue(
                            refresh_local=False, refresh_account=False
                        )
                        if not catalogue_change_again:
                            break
                finally:
                    catalogue_change_task = None

            catalogue_change_task = asyncio.create_task(refresh())

        loop.call_soon_threadsafe(schedule)

    def _account_changed() -> None:
        """Push one fresh sanitized snapshot after a PC-side account change."""
        if not authenticated:
            return

        def schedule() -> None:
            nonlocal account_change_task, account_change_again
            if account_change_task is not None and not account_change_task.done():
                account_change_again = True
                return

            async def refresh() -> None:
                nonlocal account_change_task, account_change_again
                try:
                    from osia.mobile_model_catalogue import account_snapshot

                    while True:
                        account_change_again = False
                        state = await asyncio.to_thread(
                            account_snapshot, refresh_models=False
                        )
                        await out.send_json({
                            "type": "chatgpt_account_state", "account": state
                        })
                        await _send_model_catalogue(
                            refresh_local=False, refresh_account=False
                        )
                        if not account_change_again:
                            break
                finally:
                    account_change_task = None

            account_change_task = asyncio.create_task(refresh())

        loop.call_soon_threadsafe(schedule)

    try:
        from osia.config import (
            add_api_key_change_listener,
            add_setting_change_listener,
        )
        from osia.memory import add_memory_change_listener
        from osia.chatgpt_account.provider import add_account_change_listener

        unsubscribe_api_key_changes = add_api_key_change_listener(_api_key_changed)
        unsubscribe_setting_changes = add_setting_change_listener(
            _canonical_config_changed
        )
        unsubscribe_memory_changes = add_memory_change_listener(_memory_changed)
        unsubscribe_account_changes = add_account_change_listener(_account_changed)
    except Exception as e:
        print(f"[OSIA Server] mobile catalogue change hooks unavailable: {e}")

    # ── Streaming-turn machinery (head-of-line fix) ──────────────────────
    # A user message (text / audio / image) runs in a SEPARATE task so the
    # receive loop stays free to answer lightweight control messages
    # (load_conversation, …) within milliseconds, even while a long response
    # is streaming. The persistence-target conversation is captured AT SPAWN
    # TIME (passed by value into the turn), so a later load_conversation that
    # re-points the *viewed* conversation can never redirect or corrupt an
    # in-flight stream.
    streaming_tasks: dict = {}

    # ── Per-connection agent reuse (perf fix #3) ─────────────────────────
    # One cached OSIAAgent per conversation is reused across ordinary serialized
    # turns. A superseding turn may overlap an uncancellable old worker; in that
    # case _get_conn_agent replaces the busy cache entry so mutable history,
    # client, and terminality state are not shared. Different conversations can
    # also overlap as backgrounded streams, so they retain distinct agents. The
    # cache is local to this handler, LRU-bounded, and disposed in the finally
    # block.
    conn_agents: "OrderedDict[str, object]" = OrderedDict()

    def _get_conn_agent(conv_id: str):
        """Return this connection's OSIAAgent for ``conv_id`` (marking it
        most-recently-used), creating it on first use. When the cache exceeds
        the cap, evict the OLDEST IDLE agent and close its client — but skip any
        agent whose conversation is mid-stream (``_in_use``: its worker thread
        still holds the client) and never evict the one just requested. If every
        other cached agent is busy, leave the cache briefly over the cap rather
        than close a live client (the orphan is reclaimed by GC once its stream
        ends).
        """
        from osia.agent import OSIAAgent
        agent = conn_agents.get(conv_id)
        if agent is not None and not getattr(agent, "_in_use", False):
            conn_agents.move_to_end(conv_id)
            return agent
        # A superseding turn can start while its predecessor's uncancellable
        # worker thread still owns this agent. Its mutable history and
        # terminality controller cannot be shared across the two turns; let
        # the old worker keep its reference and give the new turn a fresh agent.
        agent = OSIAAgent()
        conn_agents[conv_id] = agent
        while len(conn_agents) > _AGENT_CACHE_MAX:
            victim_id = None
            for cid, cand in conn_agents.items():      # oldest -> newest
                if cid == conv_id:
                    continue                           # never evict the one we return
                if not getattr(cand, "_in_use", False):
                    victim_id = cid
                    break
            if victim_id is None:
                break                                  # all others busy: can't evict safely
            _close_agent_client(conn_agents.pop(victim_id))
        return agent

    def _invalidate_cached_models(provider: str | None = None):
        """Force cached agents (optionally only those on ``provider``) to
        re-apply set_model — which re-reads that provider's key chain from disk —
        on their NEXT turn. Called after a mid-session API-key edit so a swapped
        key is honoured: the per-conversation agent reuse (perf fix #3) skips
        set_model on the hot path, so without this a clean key swap would stay
        stale on an already-warmed conversation until a model change, a
        technical-error rotation, or disconnect. Cheap — clears a flag; the
        rebuild happens lazily on the next turn, exactly like the old
        rebuild-every-message behaviour, just deferred by one turn."""
        for _ag in conn_agents.values():
            if provider is None or getattr(_ag, "provider", None) == provider:
                _ag._applied_selected_model = None

    async def _ensure_active_conv(seed_text, client_request_id: str):
        """Resolve the conversation a turn will run in, creating it NOW
        (synchronously) when none is active.

        This used to happen lazily inside ``_run_agent_and_respond`` AFTER the
        agent run, which meant a brand-new conversation had no id at spawn
        time. Spawning turns then keyed them under a synthetic placeholder and
        left ``active_conv_id`` None until the turn finished — so two messages
        sent into a fresh chat each created a SEPARATE conversation, and a
        mid-stream delete/new-chat could not find the in-flight turn. Creating
        the conversation here makes the id real at spawn time, so every turn is
        keyed by its true id (serialised correctly) and reachable for
        cancellation. Has NO ``await`` suspension point (queue puts don't
        yield), so two back-to-back messages can't race into two conversations.

        ``seed_text`` seeds a smart title when it's a string (text turns);
        audio/image pass a non-string and fall back to the default title,
        which ``conversations.add_message`` upgrades from the first persisted
        user message just as before.

        Returns the conversation id, or ``None`` if creation failed — the
        creating disk write is wrapped so a transient failure aborts just this
        turn (the caller skips the spawn) instead of tearing down the whole
        connection, since this now runs in the receive-loop coroutine.
        """
        nonlocal active_conv_id, session_history
        if active_conv_id is not None:
            return active_conv_id
        try:
            seed_title = (conversations.smart_title(seed_text)
                          if isinstance(seed_text, str) else None)
            conv, created = conversations.resolve_request_conversation(
                client_request_id,
                title=seed_title,
            )
        except Exception as e:
            print(
                "[OSIA Server] create_conversation failed: "
                f"{type(e).__name__}"
            )
            await out.send_json({"type": "status", "content": "error"})
            return None
        active_conv_id = conv["id"]
        session_history = list(conv.get("messages") or [])
        if created:
            await _broadcast_canonical_conversation_change(
                active_conv_id,
                revision=int(conv.get("revision") or 0),
                operation_id=str(conv.get("last_operation_id") or ""),
                exclude=out,
            )
        await out.send_json({
            "type": "active_conversation",
            "id": conv["id"],
            "title": conv["title"],
        })
        # Defensive like the post-auth burst: a list failure must not abort
        # the turn now that the conversation already exists.
        try:
            snapshot = conversations.conversation_snapshot()
        except Exception as e:
            print(f"[OSIA Server] list_conversations() failed post-create: {e}")
            snapshot = {"type": "conversations", "items": []}
        await out.send_json(snapshot)
        return active_conv_id

    async def _resolve_turn_conversation(
        msg: dict,
        seed_text,
        client_request_id: str,
    ):
        """Resolve only an explicit target, except for a brand-new chat."""

        requested = str(msg.get("conversation_id") or "").strip()
        if requested:
            if conversations.get_conversation(requested) is None:
                await out.send_json({
                    "type": "status",
                    "content": "error",
                    "error": "conversation_not_found",
                    "conversation_id": requested,
                })
                return None
            return requested
        if active_conv_id is not None:
            if not _application_transport_coordination_enabled():
                return active_conv_id
            await out.send_json({
                "type": "status",
                "content": "error",
                "error": "missing_conversation_id",
            })
            return None
        return await _ensure_active_conv(seed_text, client_request_id)

    async def _accept_user_turn_before_spawn(
        conv_id: str,
        persisted_content,
        turn_meta: dict,
    ) -> bool:
        """Win the durable acceptance CAS before publishing a turn identity."""

        if not str(turn_meta.get("_terminality_objective") or "").strip():
            turn_meta["_terminality_objective"] = str(
                turn_meta.get("_original_user_text")
                or persisted_content
                or ""
            )[:32_000]
        _bind_pre_agent_cancellation_authority(turn_meta)
        if not await _supersede_active_conversation_turn(
            out,
            conv_id,
            turn_meta,
            streaming_tasks=streaming_tasks,
        ):
            _release_unaccepted_turn_sources(turn_meta, owner_id=conv_id)
            _complete_durable_command_now(turn_meta, {
                "accepted": False,
                "state": "failed_before_execution",
                "reason_code": "previous_turn_not_terminal_or_supersedable",
                "conversation_id": conv_id,
                "request_id": str(turn_meta.get("request_id") or ""),
                "turn_id": str(turn_meta.get("turn_id") or ""),
            })
            await out.send_json({
                "type": "status",
                "content": "error",
                "pre_accept_rejection": True,
                "error": "previous_turn_not_terminal_or_supersedable",
                "conversation_id": conv_id,
                "request_id": str(turn_meta.get("request_id") or ""),
                "client_request_id": str(turn_meta.get("request_id") or ""),
            })
            return False
        accepted = _persist_accepted_user_message(
            conv_id, persisted_content, turn_meta,
        )
        turn_meta["persisted"] = accepted
        if not accepted:
            _release_unaccepted_turn_sources(
                turn_meta, owner_id=conv_id,
            )
            await out.send_json({
                "type": "status",
                "content": "error",
                "pre_accept_rejection": True,
                "error": str(
                    turn_meta.get("_user_commit_error")
                    or "persistence_unavailable"
                ),
                "conversation_id": conv_id,
                "request_id": str(turn_meta.get("request_id") or ""),
                "client_request_id": str(
                    turn_meta.get("request_id") or ""
                ),
            })
            await _complete_durable_command(turn_meta, {
                "accepted": False,
                "state": "persistence_unavailable",
            })
            return False
        receipt = turn_meta.get("_user_commit")
        if isinstance(receipt, dict) and receipt.get("committed") is False:
            _release_unaccepted_turn_sources(
                turn_meta, owner_id=conv_id,
            )
            replayed = await _send_persisted_turn_for_request(
                out,
                conv_id,
                str(turn_meta.get("request_id") or ""),
            )
            if not replayed:
                await out.send_json({
                    "type": "status",
                    "content": "error",
                    "error": "persisted_turn_replay_unavailable",
                    "conversation_id": conv_id,
                    "request_id": str(turn_meta.get("request_id") or ""),
                    "client_request_id": str(
                        turn_meta.get("request_id") or ""
                    ),
                })
            await _complete_durable_command(turn_meta, {
                "accepted": True,
                "state": "duplicate_reattached",
            })
            return False
        turn_meta["_accepted_user_already_persisted"] = True
        await _broadcast_committed_receipt(conv_id, receipt)
        await _complete_durable_command(turn_meta, {
            "accepted": True,
            "state": "turn_accepted",
        })
        return True

    async def _run_turn(conv_id, prev, make_coro, turn_meta=None):
        nonlocal session_history, active_conv_id
        snapshot: list = []
        try:
            # Serialise turns on one conversation unless this accepted turn
            # superseded the predecessor. A terminalized predecessor must not
            # hold the new request behind its cancelled provider/tool worker.
            supersedes = bool(
                isinstance(turn_meta, dict)
                and turn_meta.get("_supersession_history_from_store")
            )
            if prev is not None and not prev.done() and not supersedes:
                try:
                    await prev
                except Exception:
                    pass
            # Snapshot the working history for THIS conversation right before
            # the turn runs — independent of whatever conversation is viewed.
            if supersedes:
                snapshot = _history_for_superseding_turn(conv_id, turn_meta)
            elif active_conv_id == conv_id:
                snapshot = list(session_history)
            else:
                conv = conversations.get_conversation(conv_id)
                snapshot = list(conv.get("messages") or []) if conv else []
            new_history, result_conv_id = await make_coro(snapshot)
        except asyncio.CancelledError:
            owner = (turn_meta or {}).get("_durable_command_owner")
            if isinstance(owner, dict):
                await _settle_websocket_command_owner({**owner, "transferred": False})
            raise
        except Exception as e:
            print(f"[OSIA Server] streaming turn failed: {e}")
            traceback.print_exc()
            recovered = await _finalize_admitted_turn_after_worker_failure(
                out,
                conv_id,
                turn_meta,
                snapshot,
            )
            if recovered is None:
                owner = (turn_meta or {}).get("_durable_command_owner")
                if isinstance(owner, dict):
                    await _settle_websocket_command_owner({**owner, "transferred": False})
                return
            new_history, result_conv_id = recovered
        if isinstance(turn_meta, dict) and not turn_meta.get("_user_commit"):
            owner = turn_meta.get("_durable_command_owner")
            if isinstance(owner, dict):
                await _settle_websocket_command_owner({**owner, "transferred": False})
        # Keep the in-memory pointer coherent ONLY when the user is still
        # viewing this conversation. Otherwise the new messages live on disk
        # and are reloaded on demand — the currently-viewed conversation is
        # never clobbered by a background stream completing.
        still_owner = bool(
            turn_meta is None
            or _ACTIVE_TURN_META_BY_CONV.get(result_conv_id) is turn_meta
        )
        if (
            result_conv_id is not None
            and active_conv_id == result_conv_id
            and still_owner
        ):
            session_history = new_history

    def _spawn_turn(conv_id, make_coro, turn_meta: dict | None = None):
        # conv_id is always a real id here (resolved via _ensure_active_conv
        # before spawning), so the task is keyed by it: a follow-up turn on
        # the same conversation is rejected before this function is called, and
        # delete/disconnect can find and cancel it.
        prev = streaming_tasks.get(conv_id)
        task = asyncio.create_task(
            _run_turn(conv_id, prev, make_coro, turn_meta)
        )
        if not _claim_conversation_turn(conv_id, task, turn_meta):
            task.cancel()
            if isinstance(turn_meta, dict) and turn_meta.get("persisted") is True:
                async def _recover_admitted_claim_conflict() -> None:
                    nonlocal session_history

                    current_task = asyncio.current_task()
                    if current_task is None:
                        return
                    while True:
                        incumbent = (
                            _ACTIVE_TURNS_BY_CONV.get(conv_id)
                            or _POST_TURN_MEDIA_BY_CONV.get(conv_id)
                        )
                        if incumbent is not None and incumbent is not current_task:
                            try:
                                await incumbent
                            except BaseException:
                                pass
                        await asyncio.sleep(0)
                        if _claim_conversation_turn(
                            conv_id, current_task, turn_meta,
                        ):
                            break
                        await asyncio.sleep(0.05)

                    streaming_tasks[conv_id] = current_task
                    turn_meta["_turn_publication_started"] = True
                    out.send_json_nowait(_turn_accepted_frame(turn_meta))
                    out.send_json_nowait(
                        _turn_status_frame(turn_meta, "recovering")
                    )
                    busy_frame = _conversation_busy_changed_frame(
                        conv_id, True, turn_meta,
                    )
                    out.send_json_nowait(busy_frame)
                    asyncio.create_task(
                        _broadcast_conversation_busy_changed(
                            conv_id,
                            True,
                            turn_meta,
                            frame=busy_frame,
                            exclude=out,
                        )
                    )
                    try:
                        stored = conversations.get_conversation(conv_id)
                        history = (
                            list(stored.get("messages") or [])
                            if isinstance(stored, dict) else []
                        )
                        recovered = (
                            await _finalize_admitted_turn_after_worker_failure(
                                out, conv_id, turn_meta, history,
                            )
                        )
                        if recovered is not None and active_conv_id == conv_id:
                            session_history = recovered[0]
                    finally:
                        if streaming_tasks.get(conv_id) is current_task:
                            streaming_tasks.pop(conv_id, None)
                        if _clear_conversation_turn(conv_id, current_task):
                            asyncio.create_task(
                                _broadcast_conversation_busy_changed(
                                    conv_id, False, turn_meta,
                                )
                            )

                recovery_task = asyncio.create_task(
                    _recover_admitted_claim_conflict()
                )
                owner = turn_meta.get("_durable_command_owner")
                if isinstance(owner, dict):
                    owner["transferred"] = True
                _PENDING_FINAL_PERSIST_TASKS.add(recovery_task)
                recovery_task.add_done_callback(
                    _PENDING_FINAL_PERSIST_TASKS.discard
                )
            elif isinstance(turn_meta, dict):
                out.send_json_nowait({
                    "type": "status",
                    "content": "error",
                    "error": "conversation_busy",
                    "pre_accept_rejection": True,
                    "conversation_id": conv_id,
                    "request_id": str(
                        turn_meta.get("request_id")
                        or turn_meta.get("client_request_id")
                        or ""
                    ),
                    "client_request_id": str(
                        turn_meta.get("request_id")
                        or turn_meta.get("client_request_id")
                        or ""
                    ),
                })
            return False
        streaming_tasks[conv_id] = task
        if isinstance(turn_meta, dict):
            owner = turn_meta.get("_durable_command_owner")
            if isinstance(owner, dict):
                owner["transferred"] = True
        if isinstance(turn_meta, dict) and turn_meta.get("persisted") is True:
            turn_meta["_turn_publication_started"] = True
            out.send_json_nowait(_turn_accepted_frame(turn_meta))
            out.send_json_nowait(_turn_status_frame(turn_meta, "working"))
            busy_frame = _conversation_busy_changed_frame(conv_id, True, turn_meta)
            out.send_json_nowait(busy_frame)
            asyncio.create_task(
                _broadcast_conversation_busy_changed(
                    conv_id,
                    True,
                    turn_meta,
                    frame=busy_frame,
                    exclude=out,
                )
            )
        elif turn_meta is None:
            asyncio.create_task(
                _broadcast_conversation_busy_changed(conv_id, True, turn_meta)
            )

        def _clear(t):
            if (t.cancelled() or t.exception() is not None) and isinstance(turn_meta, dict):
                owner = turn_meta.get("_durable_command_owner")
                if isinstance(owner, dict):
                    settlement = asyncio.create_task(_settle_websocket_command_owner(
                        {**owner, "transferred": False},
                    ))
                    _PENDING_FINAL_PERSIST_TASKS.add(settlement)
                    settlement.add_done_callback(_PENDING_FINAL_PERSIST_TASKS.discard)
            if streaming_tasks.get(conv_id) is t:
                streaming_tasks.pop(conv_id, None)
            meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
            if _clear_conversation_turn(conv_id, t) and bool(
                (meta or {}).get("_turn_publication_started")
            ):
                asyncio.create_task(
                    _broadcast_conversation_busy_changed(conv_id, False, meta)
                )

        task.add_done_callback(_clear)
        return True

    async def _deferred_load_history(conv_id, inflight):
        """Finish a ``load_conversation`` that hit a LIVE DETACHED turn — OFF the
        receive loop (WOR-62 recovery WITHOUT the WOR-53 regression).

        ``inflight`` is the detached turn still finishing for ``conv_id``; it
        persists its reply only at END of turn, so disk is stale until it
        completes. We therefore wait for it before reading disk. Doing that wait
        inline on the receive loop parked the loop and blocked every later frame
        (the WOR-53 break); doing it HERE keeps the loop free. ``shield`` so our
        bounded wait's timeout cancels only the wait, never the turn — the turn
        must run to completion to persist (the whole point of WOR-62).
        """
        nonlocal session_history, active_conv_id
        try:
            await asyncio.wait_for(
                asyncio.shield(inflight),
                _LOAD_CONV_AWAIT_TIMEOUT,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            # Best-effort: on timeout the turn keeps running and its reply shows
            # on a later reload; on a turn-cancel or any unexpected await error we
            # read disk anyway. Swallowing CancelledError is safe HERE (unlike the
            # old inline version) because this is a standalone task — never the
            # receive loop — so it can't mask connection teardown.
            pass
        try:
            conv = conversations.get_conversation(conv_id)
            if conv is None:
                # Deleted during recovery (e.g. delete_conversation arrived while
                # we awaited): nothing to show and the user has moved on —
                # best-effort, emit no frame.
                return
            messages = list(conv.get("messages") or [])
            # Adopt this history as the live in-memory session ONLY if the user is
            # STILL viewing conv_id (mirrors _run_turn's active_conv_id guard).
            # If they switched to another conversation while we awaited, its
            # session_history must NOT be clobbered by this background completion.
            # (active_conv_id is already == conv_id inside this guard, so only
            # session_history needs refreshing — exactly like _run_turn.)
            if active_conv_id == conv_id:
                session_history = list(messages)
            # ALWAYS send the history frame regardless of the guard: it carries
            # ``conversation_id`` so the client routes it to conv_id's tab even
            # while viewing another conversation (WOR-53 per-conv routing). Send
            # the disk ``messages`` (not session_history, which may now be
            # another conversation's).
            await out.send_json(_conversation_history_frame(conv))
        except Exception:
            # Best-effort background recovery: never surface an error here as an
            # unretrieved-task warning. The turn already persisted; a later
            # reload re-sends the history.
            pass

    first_message_logged = False
    command_owners: list[dict] = []
    receive_task = asyncio.current_task()

    def _writer_owner_exited(_task) -> None:
        # A dead writer can otherwise leave dispatch parked before acceptance
        # even though the transport has already closed (e.g. a failed PONG).
        # Accepted workers retain their independent canonical ownership.
        if (out._closed and receive_task is not None
            and any(not owner.get("transferred") for owner in command_owners)):
            print("[WS command] writer exited before ownership transfer", flush=True)
            receive_task.cancel()

    writer_task.add_done_callback(_writer_owner_exited)
    from osia.turn_terminality import live_process_identity
    _alive, owner_process_identity = live_process_identity(os.getpid())
    try:
        async for raw_msg in _websocket_owned_messages(ws, command_owners):
            # DEBUG: surface the very first frame the client sends so we can
            # see whether auth arrives as text and in the expected shape.
            if not first_message_logged:
                first_message_logged = True
                print(f"[WS] first message received ({len(raw_msg)} chars)")
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                print(f"[WS] bad JSON frame (not closing, {len(raw_msg)} chars)")
                # Post-auth, funnel through the writer (heartbeat/streams may be
                # sending). Pre-auth there is no other sender AND the auth burst
                # below sends on ws directly — so send directly too, never
                # queuing a frame the writer could race against that burst.
                if authenticated:
                    await out.send_json({"type": "status", "content": "error"})
                else:
                    await ws.send_json({"type": "status", "content": "error"})
                continue

            msg_type = msg.get("type", "")

            # --- Auth ---
            if msg_type == "auth":
                raw_token = msg.get("token", "")
                # Compare against the stored token, which is itself stripped
                # on load — tolerate stray whitespace/newlines from clients.
                token = raw_token.strip() if isinstance(raw_token, str) else ""
                matched = token == MOBILE_TOKEN
                print(f"[WS] auth attempt received (matches={matched})")
                if matched:
                    authenticated = True
                    # Browser work can originate from ordinary Chat. Capture the
                    # authenticated server loop and install the value-free
                    # progress bridge on first auth.
                    global _BROWSER_PROGRESS_LOOP
                    _BROWSER_PROGRESS_LOOP = asyncio.get_running_loop()
                    try:
                        from osia.tools.browser_controller import set_browser_progress_callback
                        set_browser_progress_callback(_on_browser_progress)
                    except Exception:
                        pass
                    _bind_mobile_device(msg.get("device_id"))
                    durable_client_material = mobile_device_id or connection_id
                    durable_client_id = "client_" + hashlib.sha256(
                        durable_client_material.encode("utf-8")
                    ).hexdigest()[:32]
                    out.bind_durable_client(durable_client_id)
                    session_history = []
                    active_conv_id = None
                    # Track this client so server-initiated frames (e.g. a
                    # "new chat" reset) can reach it (WOR-10) — register the
                    # outbound CHANNEL, not the raw socket, so broadcasts go
                    # through this connection's writer.
                    _WS_CLIENTS.add(out)
                    # Send the first data frames the mobile app expects right
                    # after auth. Build the conversation list DEFENSIVELY: a
                    # failure here must not silently tear down a freshly
                    # authenticated socket (that looked like "open -> closed
                    # instantly even with a valid token").
                    #
                    # The post-auth burst (status/done + conversation LIST) is
                    # sent on ``ws`` DIRECTLY and unchanged: at this point no
                    # other coroutine sends (the heartbeat is started just
                    # below, the writer is idle), so there is no concurrency to
                    # funnel and the historically delicate auth handshake stays
                    # byte-for-byte as before.
                    await ws.send_json({"type": "status", "content": "done"})
                    print("[WS] auth OK, sending: status/done")
                    try:
                        snapshot = conversations.conversation_snapshot()
                    except Exception as e:
                        print(
                            "[WS] list_conversations() failed during initial "
                            f"send, falling back to empty list: {e!r}"
                        )
                        traceback.print_exc()
                        snapshot = {"type": "conversations", "items": []}
                    await ws.send_json(snapshot)
                    items = snapshot.get("items") or []
                    print(
                        f"[WS] auth OK, sending: conversations "
                        f"({len(items)} items)"
                    )
                    for _cron_frame in _pending_cron_notifications():
                        await ws.send_json(_cron_frame)
                    for _busy_id in _active_conversation_busy_ids():
                        if not await _send_recovered_turn_state(out, _busy_id):
                            await ws.send_json(
                                _conversation_busy_changed_frame(_busy_id, True)
                            )
                    supplied_cursors = msg.get("event_cursors")
                    if not isinstance(supplied_cursors, dict):
                        supplied_cursors = {}
                    server_cursors = await asyncio.to_thread(
                        _DURABLE_TRANSPORT.client_cursors,
                        durable_client_id,
                    )
                    await ws.send_json({
                        "type": "event_transport_context",
                        "schema_version": 1,
                        "client_id": durable_client_id,
                        "ack_mode": "contiguous_per_stream",
                        "cursors": server_cursors,
                    })
                    replay_frames = await asyncio.to_thread(
                        _DURABLE_TRANSPORT.replay_for_client,
                        durable_client_id,
                        supplied_cursors,
                    )
                    # A fresh mobile session hydrates completed conversations
                    # separately and has no delivery cursor for their old runs.
                    # Replay only streams this client can actually resume;
                    # otherwise a new device ID floods the relay with every
                    # historical projection in the store.
                    resume_streams = set(server_cursors) | set(supplied_cursors)
                    for replay_frame in replay_frames:
                        if replay_frame.get("stream_id") in resume_streams:
                            await out.send_durable_replay(replay_frame)
                    await out.activate_durable_delivery()
                    print("[OSIA Server] Mobile client authenticated.")
                    # Start the live system-metrics push for this client. The
                    # loop self-gates on heartbeat_enabled; guard against a
                    # stray second auth frame spawning a duplicate loop. It
                    # sends through ``out`` so it never races the writer.
                    if heartbeat_task is None:
                        heartbeat_task = asyncio.create_task(
                            _heartbeat_push_loop(out)
                        )
                else:
                    print("[WS] closing connection, reason: invalid token")
                    # Terminal pre-auth path: send directly then close, so the
                    # error frame is flushed before the socket is torn down.
                    await ws.send_json({"type": "status", "content": "error"})
                    await ws.close(code=4001, reason="Invalid token")
                    return
                continue

            # All other messages require authentication
            if not authenticated:
                print(
                    "[WS] closing connection, reason: not authenticated "
                    f"(first msg_type={msg_type!r}, expected 'auth')"
                )
                # Terminal pre-auth path: direct send then close (see above).
                await ws.send_json({"type": "status", "content": "error"})
                await ws.close(code=4001, reason="Not authenticated")
                return

            if msg_type == "event_ack":
                stream_id = str(msg.get("stream_id") or "")
                sequence = msg.get("sequence")
                if (
                    msg.get("schema_version") != 1
                    or not out._durable_client_id
                    or not isinstance(sequence, int)
                    or isinstance(sequence, bool)
                ):
                    continue
                try:
                    await asyncio.to_thread(
                        _DURABLE_TRANSPORT.ack,
                        client_id=out._durable_client_id,
                        stream_id=stream_id,
                        sequence=sequence,
                    )
                except (RuntimeError, TypeError, ValueError):
                    pass
                continue

            if msg_type == "event_replay_request":
                stream_id = str(msg.get("stream_id") or "")
                after_sequence = msg.get("after_sequence")
                if (
                    msg.get("schema_version") != 1
                    or not isinstance(after_sequence, int)
                    or isinstance(after_sequence, bool)
                    or after_sequence < 0
                ):
                    continue
                try:
                    replay_frames = await asyncio.to_thread(
                        _DURABLE_TRANSPORT.replay_stream,
                        stream_id,
                        after_sequence=after_sequence,
                    )
                except (RuntimeError, TypeError, ValueError):
                    replay_frames = []
                for replay_frame in replay_frames:
                    await out.send_durable_replay(replay_frame)
                continue

            durable_command = _durable_command_identity(msg_type, msg)
            if durable_command is not None:
                command_id, command_binding = durable_command
                try:
                    command_epoch = "epoch_" + secrets.token_urlsafe(24)
                    claim_task = asyncio.create_task(asyncio.to_thread(
                        _DURABLE_TRANSPORT.claim_command,
                        command_id=command_id,
                        command_type=msg_type,
                        binding=command_binding,
                        execution_epoch=command_epoch,
                        owner_id=connection_id if msg_type in {"text", "audio", "cancel_active_work"} else "",
                        owner_pid=os.getpid() if msg_type in {"text", "audio", "cancel_active_work"} else 0,
                        owner_process_identity=owner_process_identity if msg_type in {"text", "audio", "cancel_active_work"} else "",
                    ), name=f"ws-command-claim:{command_id}")
                    owner = {"command_id": command_id, "claim_task": claim_task,
                             "execution_epoch": command_epoch, "transferred": False}
                    if msg_type in {"text", "audio"}:
                        command_owners.append(owner)
                    command_claim = await asyncio.shield(claim_task)
                except (RuntimeError, TypeError, ValueError):
                    await out.send_json({
                        "type": "command_ack",
                        "command_id": command_id,
                        "state": "rejected",
                        "reason_code": "inbox_unavailable",
                    })
                    continue
                if command_claim.disposition is InboxDisposition.CONFLICT:
                    await out.send_json({
                        "type": "command_ack",
                        "command_id": command_id,
                        "state": "rejected",
                        "reason_code": "duplicate_command_conflict",
                    })
                    continue
                msg["_durable_command_id"] = command_id
                msg["_durable_command_epoch"] = command_claim.execution_epoch
                if (command_claim.disposition is InboxDisposition.CLAIMED
                    and msg_type in {"text", "audio"}):
                    msg["_durable_command_owner"] = owner
                if (
                    command_claim.disposition
                    is InboxDisposition.DUPLICATE_COMPLETED
                    and (msg_type not in {"text", "audio"}
                         or (command_claim.result or {}).get("accepted") is False
                         or (command_claim.result or {}).get("state") == "outcome_unknown")
                ):
                    await out.send_json({
                        "type": "command_ack",
                        "command_id": command_id,
                        "state": "duplicate_completed",
                        "result": command_claim.result or {},
                    })
                    continue
                if (
                    command_claim.disposition
                    is InboxDisposition.DUPLICATE_IN_PROGRESS
                ):
                    if msg_type in {"text", "audio"}:
                        if await _send_existing_turn_status_for_request(
                            out, command_binding.get("request_id"),
                        ):
                            continue
                    await out.send_json({
                        "type": "command_ack",
                        "command_id": command_id,
                        "state": "duplicate_in_progress",
                    })
                    continue

            if msg_type == "cancel_active_work":
                accepted = await _handle_correlated_chat_cancellation(
                    out,
                    msg,
                    streaming_tasks=streaming_tasks,
                )
                control_result = msg.get("_cancellation_command_result")
                if not isinstance(control_result, dict):
                    control_result = {
                        "accepted": bool(accepted),
                        "state": "cancelled" if accepted else "rejected",
                        "reason_code": (
                            "correlated_cancellation_accepted"
                            if accepted else "correlated_cancellation_rejected"
                        ),
                    }
                    _complete_durable_command_now(msg, control_result)
                await out.send_json({
                    "type": "command_ack",
                    "command_id": str(msg.get("_durable_command_id") or ""),
                    "state": "completed",
                    "result": control_result,
                })
                continue

            if msg_type == "get_browser_status":
                if msg.get("schema_version") != 1:
                    continue
                await out.send_json(_browser_progress_frame(snapshot=True))
                continue

            if msg_type == "get_document_status":
                if msg.get("schema_version") != 1:
                    continue
                if not _bind_mobile_device(msg.get("device_id")):
                    continue
                requested = msg.get("scopes")
                if not isinstance(requested, list) or len(requested) > 8:
                    continue
                for requested_scope in requested:
                    if (
                        not isinstance(requested_scope, dict)
                        or set(requested_scope) != {"scope_type", "scope_id"}
                    ):
                        continue
                    for frame in _document_progress_snapshot(
                        str(requested_scope.get("scope_type") or ""),
                        str(requested_scope.get("scope_id") or ""),
                        owner_device=mobile_device_id,
                    ):
                        await out.send_json(frame)
                continue

            # --- Conversation management ---
            if msg_type == "list_conversations":
                await out.send_json(conversations.conversation_snapshot())
                continue

            if msg_type == "get_conversations":
                # Compact id+title list for the mobile cron editor's "linked
                # conversation" picker (WOR-24). list_conversations() is already
                # sorted by updated_at descending; cap at the 50 most recent.
                recent = conversations.list_conversations()[:50]
                await out.send_json({
                    "type": "conversations_list",
                    "conversations": [
                        {"id": c.get("id"), "title": c.get("title")}
                        for c in recent
                    ],
                })
                continue

            if msg_type == "get_conversation_snapshot":
                # Project deletion confirmations on Mobile must use a freshly
                # read PC-canonical count, never a Mobile-only cached count.
                snapshot = conversations.conversation_snapshot()
                snapshot["operation_id"] = str(msg.get("operation_id") or "")
                await out.send_json(snapshot)
                continue

            if msg_type == "new_conversation_in_project":
                result = conversations.apply_folder_mutation(msg)
                await out.send_json({
                    "type": "conversation_folder_mutated",
                    "mutation": msg_type,
                    **result,
                })
                if result.get("success"):
                    session_history = []
                    conv = result.get("conversation") or {}
                    active_conv_id = str(conv.get("id") or "")
                    await _broadcast_canonical_conversation_change(
                        active_conv_id,
                        revision=int(conv.get("revision") or 0),
                        operation_id=str(result.get("operation_id") or ""),
                        exclude=out,
                    )
                    await out.send_json({
                        "type": "active_conversation",
                        "id": active_conv_id,
                        "title": conv.get("title") or conversations.DEFAULT_TITLE,
                        "operation_id": str(result.get("operation_id") or ""),
                    })
                snapshot = conversations.conversation_snapshot()
                snapshot["operation_id"] = result.get("operation_id", "")
                await out.send_json(snapshot)
                continue

            if msg_type == "new_conversation":
                # Reset session history, create a fresh conversation, and
                # promote it to active. Notify the client with BOTH the
                # active-conversation pointer AND the refreshed sidebar
                # list (so the new entry shows up immediately).
                session_history = []
                conv = conversations.create_conversation()
                active_conv_id = conv["id"]
                await _broadcast_canonical_conversation_change(
                    active_conv_id,
                    revision=int(conv.get("revision") or 0),
                    operation_id=str(conv.get("last_operation_id") or ""),
                    exclude=out,
                )
                print(
                    f"[OSIA Server] New conversation: {active_conv_id} "
                    f"(title={conv['title']!r})"
                )
                await out.send_json({
                    "type": "active_conversation",
                    "id": conv["id"],
                    "title": conv["title"],
                })
                await out.send_json(conversations.conversation_snapshot())
                continue

            if msg_type == "new_chat":
                # "Nouveau chat" voice command relayed from the mobile (WOR-10).
                # Reset this session's server-side state (the PC side) exactly
                # like new_conversation, then notify any OTHER connected devices
                # so every UI clears too.
                session_history = []
                conv = conversations.create_conversation()
                active_conv_id = conv["id"]
                await _broadcast_canonical_conversation_change(
                    active_conv_id,
                    revision=int(conv.get("revision") or 0),
                    operation_id=str(conv.get("last_operation_id") or ""),
                    exclude=out,
                )
                print(
                    f"[OSIA Server] new_chat from mobile → reset, "
                    f"new conv {active_conv_id}"
                )
                await out.send_json({
                    "type": "active_conversation",
                    "id": conv["id"],
                    "title": conv["title"],
                })
                await out.send_json(conversations.conversation_snapshot())
                await _broadcast_new_chat(exclude=out)
                continue

            if msg_type == "restart_server":
                # Self-restart the server process in place (WOR-28). Ack the
                # client first so the phone knows the request landed, then
                # re-exec the same interpreter + argv; os.execv replaces this
                # process image, so nothing after the call runs.
                print("[OSIA Server] restart_server requested → re-exec")
                await out.send_json({
                    "type": "status",
                    "status": "done",
                    "message": "restarting",
                })
                # Drain the writer so the ack actually leaves the socket before
                # os.execv replaces this process image (nothing after execv
                # runs). Best-effort: a dead socket just makes the writer exit.
                try:
                    await out.close()
                    await writer_task
                except Exception:
                    pass
                if sys.platform == "win32":
                    # os.execv on Windows does not reliably replace the process
                    # image; spawn a detached new process and exit instead.
                    subprocess.Popen(
                        [sys.executable, *sys.argv],
                        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    )
                    os._exit(0)
                os.execv(sys.executable, [sys.executable] + sys.argv)
                continue

            if msg_type == "load_conversation":
                conv_id = msg.get("id", "")
                # WOR-62 / WOR-53 (reconnect race, WITHOUT parking the receive
                # loop): if a turn for THIS conversation was DETACHED when a
                # previous connection dropped (phone slept mid-generation), it may
                # still be finishing — and it persists the reply only at END of
                # turn. Reading disk now would return an empty/stale history (the
                # original WOR-62 bug). So we must WAIT for that in-flight detached
                # turn (bounded) before reading disk — but the wait must NOT run on
                # this serial receive loop. Awaiting it inline here (the original
                # WOR-62 follow-up) parked the loop: every later frame — e.g. a
                # switch to ANOTHER conversation — sat UNREAD in the socket buffer
                # until the turn finished, breaking WOR-53 concurrency. So we
                # DEFER: switch the view now (a cheap local assignment) and hand
                # the await + disk-read + history-send to a background task, then
                # fall through to RETURN so the loop reads the next frame at once.
                #
                # The common case — no LIVE detached turn for this conv, i.e. ALL
                # normal switches (WOR-53) — skips this branch and uses the
                # UNCHANGED synchronous fast path below: no task, no await,
                # byte-for-byte the pre-WOR-62 inline read.
                _inflight = _DETACHED_BY_CONV.get(conv_id)
                if _inflight is not None and not _inflight.done():
                    # Re-point the VIEW to conv_id now so a follow-up turn the user
                    # types during recovery targets this conversation (not a
                    # freshly-created one via _ensure_active_conv). Also seed
                    # ``session_history`` synchronously from disk — a cheap sync
                    # file read, exactly what the fast path below does — so a
                    # follow-up turn typed during the (bounded) recovery window
                    # snapshots the PRIOR on-disk context instead of an empty
                    # list. The in-flight reply isn't persisted yet, so the
                    # deferred task REFRESHES session_history (and sends the
                    # user-facing history frame) once the detached turn finishes;
                    # this seed is internal state only and is NEVER sent, so the
                    # WOR-62 "show empty/stale history on wake" bug is not
                    # reintroduced. We do NOT await the turn here — awaiting it on
                    # this serial loop is exactly what broke WOR-53.
                    active_conv_id = conv_id
                    _seed = conversations.get_conversation(conv_id)
                    session_history = list(_seed.get("messages") or []) if _seed else []
                    _dload = asyncio.create_task(
                        _deferred_load_history(conv_id, _inflight)
                    )
                    # Strong ref so the loop's weak task-tracking can't GC it
                    # mid-flight; the done-callback keeps the set bounded (same
                    # pattern as _DETACHED_TURNS / _detached_done).
                    _DEFERRED_LOADS.add(_dload)
                    _dload.add_done_callback(_DEFERRED_LOADS.discard)
                    continue
                conv = conversations.get_conversation(conv_id)
                if conv is None:
                    await out.send_json({
                        "type": "status",
                        "content": "error",
                        "message": f"conversation {conv_id!r} not found",
                    })
                    continue
                # Re-point the VIEW only. Any in-flight stream keeps writing to
                # the conversation it captured at spawn — switching the view
                # here can't redirect or corrupt it. Served immediately even
                # mid-stream because the receive loop is no longer blocked.
                active_conv_id = conv_id
                session_history = list(conv.get("messages") or [])
                await out.send_json(_conversation_history_frame(conv))
                continue

            if msg_type == "delete_conversation":
                conv_id = str(msg.get("id") or "")
                operation_id = str(msg.get("operation_id") or "").strip()[:200]
                if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", operation_id):
                    operation_id = f"mobile_delete_{secrets.token_hex(16)}"
                print(
                    "[OSIA DeleteTrace] origin=Mobile phase=canonical_start "
                    f"conversation_id={conv_id!r} operation_id={operation_id!r} "
                    f"persistence_location={str(conversations.CONVERSATIONS_DIR)!r} "
                    f"started_at_unix_ns={time.time_ns()}",
                    flush=True,
                )
                if (
                    conv_id in streaming_tasks
                    or _is_conversation_busy(conv_id)
                ):
                    snapshot = conversations.conversation_snapshot()
                    snapshot_revision = int(
                        snapshot.get("snapshot_revision") or 0
                    )
                    print(
                        "[OSIA DeleteTrace] origin=Mobile phase=canonical_result "
                        f"conversation_id={conv_id!r} operation_id={operation_id!r} "
                        "success=false error=conversation_busy "
                        f"snapshot_revision={snapshot_revision} "
                        f"persistence_location={str(conversations.CONVERSATIONS_DIR)!r} "
                        f"completed_at_unix_ns={time.time_ns()}",
                        flush=True,
                    )
                    await out.send_json({
                        "type": "conversation_deleted",
                        "id": conv_id,
                        "success": False,
                        "error": "conversation_busy",
                        "operation_id": operation_id,
                        "snapshot_revision": snapshot_revision,
                    })
                    await out.send_json(snapshot)
                    await _send_recovered_turn_state(out, conv_id)
                    continue
                receipt = conversations.delete_conversation_with_receipt(
                    conv_id,
                    operation_id=operation_id,
                )
                ok = bool(receipt.get("success"))
                snapshot = conversations.conversation_snapshot()
                snapshot_revision = int(
                    snapshot.get("snapshot_revision")
                    or receipt.get("snapshot_revision")
                    or receipt.get("revision")
                    or 0
                )
                if ok and any(
                    str(item.get("id") or "") == conv_id
                    for item in snapshot.get("items") or []
                ):
                    ok = False
                    receipt = {
                        **receipt,
                        "error": "canonical_state_changed",
                    }
                print(
                    "[OSIA DeleteTrace] origin=Mobile phase=canonical_result "
                    f"conversation_id={conv_id!r} operation_id={operation_id!r} "
                    f"success={ok} error={str(receipt.get('error') or '')!r} "
                    f"snapshot_revision={snapshot_revision} "
                    f"persistence_location={str(conversations.CONVERSATIONS_DIR)!r} "
                    f"completed_at_unix_ns={time.time_ns()}",
                    flush=True,
                )
                if ok:
                    # The atomic store lease has now proved that no canonical
                    # turn owns this conversation. Clear only local projections
                    # for the file that was actually removed.
                    doomed = streaming_tasks.pop(conv_id, None)
                    if doomed is None:
                        doomed = _ACTIVE_TURNS_BY_CONV.get(conv_id)
                    if doomed is not None:
                        doomed.cancel()
                    media_task = _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
                    _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
                    if media_task is not None:
                        media_task.cancel()
                    dead_agent = conn_agents.pop(conv_id, None)
                    if dead_agent is not None and not getattr(
                        dead_agent, "_in_use", False,
                    ):
                        _close_agent_client(dead_agent)
                    if active_conv_id == conv_id:
                        active_conv_id = None
                        session_history = []
                await out.send_json({
                    "type": "conversation_deleted",
                    "id": conv_id,
                    "success": ok,
                    "operation_id": operation_id,
                    "snapshot_revision": snapshot_revision,
                    **({} if ok else {
                        "error": str(
                            receipt.get("error") or "conversation_delete_failed"
                        ),
                    }),
                })
                print(
                    "[OSIA DeleteTrace] origin=Mobile phase=ack_queued "
                    f"conversation_id={conv_id!r} operation_id={operation_id!r} "
                    f"success={ok} snapshot_revision={snapshot_revision}",
                    flush=True,
                )
                # Reconcile the initiating client from the same canonical disk
                # state used for this acknowledgement; no local tombstone is
                # needed to conceal an older full list.
                await out.send_json(snapshot)
                if ok:
                    await _broadcast_canonical_conversation_change(
                        conv_id,
                        revision=snapshot_revision,
                        operation_id=operation_id,
                        action="delete",
                        exclude=out,
                    )
                continue

            if msg_type == "rename_conversation":
                # WOR-76: rename a conversation. Validation (trim / non-empty /
                # TITLE_MAX_LEN cap / manual_title flag / atomic write) lives in
                # conversations.rename_conversation, so an empty title simply
                # acks success:false and never crashes the loop.
                conv_id = msg.get("id", "")
                new_title = msg.get("title", "")
                ok = conversations.rename_conversation(conv_id, new_title)
                await out.send_json({
                    "type": "conversation_renamed",
                    "id": conv_id,
                    "success": ok,
                })
                # Refresh the caller's sidebar, then fan the updated list out to
                # every OTHER connected device (NOT new_chat — that would wrongly
                # clear the chat view). All sends go through the outbound channel,
                # honouring the single-writer rule.
                await out.send_json(conversations.conversation_snapshot())
                if ok:
                    updated = conversations.get_conversation(conv_id) or {}
                    await _broadcast_canonical_conversation_change(
                        conv_id,
                        revision=int(updated.get("revision") or 0),
                        operation_id=str(
                            updated.get("last_operation_id") or ""
                        ),
                        action="rename",
                        exclude=out,
                    )
                else:
                    await _broadcast_conversations(exclude=out)
                continue

            if msg_type in {
                "create_conversation_folder",
                "rename_conversation_folder",
                "set_conversation_folder_state",
                "set_conversation_folder_context",
                "reorder_conversation_folders",
                "move_conversation_to_folder",
            }:
                result = conversations.apply_folder_mutation(msg)
                await out.send_json({
                    "type": "conversation_folder_mutated",
                    "mutation": msg_type,
                    **result,
                })
                if result.get("success"):
                    snapshot = conversations.conversation_snapshot()
                    snapshot["operation_id"] = result.get("operation_id", "")
                    await out.send_json(snapshot)
                    await _broadcast_conversations(exclude=out)
                continue

            if msg_type == "delete_conversation_project":
                folder_id = str(msg.get("folder_id") or "")
                snapshot_before_delete = conversations.conversation_snapshot()
                deleting_ids = {
                    str(item.get("id") or "")
                    for item in snapshot_before_delete.get("items") or []
                    if str(item.get("folder_id") or "") == folder_id
                }
                busy_delete_ids = {
                    conv_id for conv_id in deleting_ids
                    if conv_id in streaming_tasks
                    or _is_conversation_busy(conv_id)
                }
                result = (
                    {
                        "success": False,
                        "error": "conversation_busy",
                        "operation_id": str(msg.get("operation_id") or ""),
                        "deleted_ids": [],
                    }
                    if busy_delete_ids else
                    conversations.apply_folder_mutation(msg)
                )
                if busy_delete_ids:
                    for conv_id in sorted(busy_delete_ids):
                        await _send_recovered_turn_state(out, conv_id)
                if result.get("success"):
                    for conv_id in result.get("deleted_ids") or []:
                        doomed = streaming_tasks.pop(conv_id, None)
                        if doomed is None:
                            doomed = _ACTIVE_TURNS_BY_CONV.get(conv_id)
                        if doomed is not None:
                            doomed.cancel()
                        media_task = _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
                        _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
                        if media_task is not None:
                            media_task.cancel()
                        dead_agent = conn_agents.pop(conv_id, None)
                        if dead_agent is not None and not getattr(
                            dead_agent, "_in_use", False,
                        ):
                            _close_agent_client(dead_agent)
                        if active_conv_id == conv_id:
                            active_conv_id = None
                            session_history = []
                await out.send_json({
                    "type": "conversation_folder_mutated",
                    "mutation": msg_type,
                    **result,
                })
                snapshot = conversations.conversation_snapshot()
                snapshot["operation_id"] = result.get("operation_id", "")
                await out.send_json(snapshot)
                if result.get("success"):
                    await _broadcast_conversations(exclude=out)
                continue

            if msg_type == "bulk_delete_ungrouped_conversations":
                snapshot_before_delete = conversations.conversation_snapshot()
                deleting_ids = {
                    str(item.get("id") or "")
                    for item in snapshot_before_delete.get("items") or []
                    if not str(item.get("folder_id") or "")
                }
                busy_delete_ids = {
                    conv_id for conv_id in deleting_ids
                    if conv_id in streaming_tasks
                    or _is_conversation_busy(conv_id)
                }
                result = (
                    {
                        "success": False,
                        "error": "conversation_busy",
                        "operation_id": str(msg.get("operation_id") or ""),
                        "deleted_ids": [],
                    }
                    if busy_delete_ids else
                    conversations.delete_confirmed_ungrouped_conversations(
                        msg.get("expected_count"),
                        operation_id=str(msg.get("operation_id") or ""),
                    )
                )
                if busy_delete_ids:
                    for conv_id in sorted(busy_delete_ids):
                        await _send_recovered_turn_state(out, conv_id)
                if result.get("success"):
                    for conv_id in result.get("deleted_ids") or []:
                        doomed = streaming_tasks.pop(conv_id, None)
                        if doomed is None:
                            doomed = _ACTIVE_TURNS_BY_CONV.get(conv_id)
                        if doomed is not None:
                            doomed.cancel()
                        media_task = _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
                        _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
                        if media_task is not None:
                            media_task.cancel()
                        dead_agent = conn_agents.pop(conv_id, None)
                        if dead_agent is not None and not getattr(
                            dead_agent, "_in_use", False,
                        ):
                            _close_agent_client(dead_agent)
                        if active_conv_id == conv_id:
                            active_conv_id = None
                            session_history = []
                await out.send_json({
                    "type": "conversations_bulk_deleted",
                    **result,
                })
                snapshot = conversations.conversation_snapshot()
                snapshot["operation_id"] = result.get("operation_id", "")
                await out.send_json(snapshot)
                if result.get("success"):
                    await _broadcast_conversations(exclude=out)
                continue

            # --- Models ---
            if msg_type == "get_models":
                try:
                    await _send_model_catalogue()
                    _request_remote_catalogue_refresh()
                except Exception as e:
                    print(f"[OSIA Server] get_models failed: {e}")
                    await out.send_json({
                        "type": "models_list",
                        "catalogue": {"version": 1, "providers": []},
                        "models": [],
                        "current": None,
                        "current_provider": None,
                        "account": {},
                    })
                continue

            if msg_type == "set_model":
                provider_id = (msg.get("provider") or "").strip().lower()
                model_id = (msg.get("model") or "").strip()
                if model_id and not provider_id:
                    # Backward compatibility for older mobile builds. New
                    # clients always send the exact provider/model pair.
                    from osia.models import provider_for_model

                    provider_id = provider_for_model(model_id)
                if not provider_id or not model_id:
                    await out.send_json({"type": "status", "content": "error"})
                    continue
                try:
                    from osia.mobile_model_catalogue import validate_and_select

                    selected = await asyncio.to_thread(
                        validate_and_select, provider_id, model_id
                    )
                    if selected:
                        print(
                            "[OSIA Server] selected provider/model -> "
                            f"{provider_id!r}/{model_id!r}"
                        )
                        await out.send_json({"type": "status", "content": "done"})
                        await _send_model_catalogue(
                            refresh_local=False, refresh_account=False
                        )
                    else:
                        await out.send_json({"type": "status", "content": "error"})
                except Exception as e:
                    print(
                        "[OSIA Server] set_model failed for sanitized "
                        f"provider/model {provider_id!r}/{model_id!r}: {e}"
                    )
                    await out.send_json({"type": "status", "content": "error"})
                continue

            if msg_type == "set_model_reasoning":
                provider_id = (msg.get("provider") or "").strip().lower()
                model_id = (msg.get("model") or "").strip()
                option_id = (msg.get("reasoning") or "").strip().lower()
                try:
                    from osia.mobile_model_catalogue import validate_and_set_reasoning

                    saved = await asyncio.to_thread(
                        validate_and_set_reasoning,
                        provider_id,
                        model_id,
                        option_id,
                    )
                    await out.send_json({
                        "type": "status",
                        "content": "done" if saved else "error",
                    })
                    if saved:
                        await _send_model_catalogue(
                            refresh_local=False, refresh_account=False
                        )
                except Exception:
                    await out.send_json({"type": "status", "content": "error"})
                continue

            if msg_type == "get_chatgpt_account":
                try:
                    from osia.mobile_model_catalogue import account_snapshot

                    state = await asyncio.to_thread(account_snapshot)
                except Exception:
                    state = {
                        "providerId": "chatgpt_account",
                        "runtimeAvailable": False,
                        "connected": False,
                        "connecting": False,
                        "loginCancellable": False,
                        "available": False,
                        "error": "runtime_missing",
                        "models": [],
                    }
                await out.send_json({
                    "type": "chatgpt_account_state", "account": state
                })
                continue

            if msg_type == "chatgpt_account_login":
                chatgpt_login_started_here = True
                try:
                    from osia.mobile_model_catalogue import start_device_login

                    state = await asyncio.to_thread(
                        start_device_login, _push_chatgpt_account_state
                    )
                except Exception:
                    state = {
                        "providerId": "chatgpt_account",
                        "runtimeAvailable": False,
                        "connected": False,
                        "connecting": False,
                        "loginCancellable": False,
                        "available": False,
                        "error": "login_failed",
                        "models": [],
                    }
                await out.send_json({
                    "type": "chatgpt_account_state", "account": state
                })
                continue

            if msg_type == "chatgpt_account_cancel_login":
                from osia.mobile_model_catalogue import cancel_device_login

                state = await asyncio.to_thread(cancel_device_login)
                await out.send_json({
                    "type": "chatgpt_account_state", "account": state
                })
                await _send_model_catalogue(
                    refresh_local=False, refresh_account=False
                )
                continue

            if msg_type == "chatgpt_account_logout":
                from osia.mobile_model_catalogue import logout_account

                state = await asyncio.to_thread(logout_account)
                await out.send_json({
                    "type": "chatgpt_account_state", "account": state
                })
                await _send_model_catalogue(
                    refresh_local=False, refresh_account=False
                )
                continue


            # --- API keys & settings (mobile "Configure APIs & Providers") ---
            # set_api_key persists a provider's key to the canonical key store
            # (config/api_keys.json — the file get_api_key() reads, so the key
            # is immediately usable by the agent/TTS). set_setting writes a
            # single whitelisted key into config/settings.json.
            if msg_type == "set_api_key":
                provider = (msg.get("provider") or "").strip().lower()
                key = (msg.get("key") or "").strip()
                if not provider:
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia.config import set_api_key
                    set_api_key(provider, key)
                    if key:
                        print(
                            f"[OSIA Server] set_api_key provider={provider!r} "
                            f"(set, len={len(key)})"
                        )
                    else:
                        print(
                            f"[OSIA Server] set_api_key provider={provider!r} "
                            f"(empty -> deleted)"
                        )
                    # Reused agents must re-read this provider's key next turn.
                    _invalidate_cached_models(provider)
                    await out.send_json({"type": "status", "status": "done"})
                    _request_remote_catalogue_refresh()
                except Exception as e:
                    print(f"[OSIA Server] set_api_key({provider!r}) failed: {e}")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # delete_api_key removes a provider's key entirely from the canonical
            # key store (config/api_keys.json) — it does NOT leave behind an empty
            # string, so get_api_key()/load_api_keys() report the provider as unset.
            if msg_type == "delete_api_key":
                provider = (msg.get("provider") or "").strip().lower()
                if not provider:
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia.config import load_api_keys, set_api_key
                    keys = load_api_keys()
                    existed = provider in keys
                    set_api_key(provider, "")
                    print(
                        f"[OSIA Server] delete_api_key provider={provider!r} "
                        f"({'removed' if existed else 'absent'})"
                    )
                    # Reused agents must re-read this provider's key next turn.
                    _invalidate_cached_models(provider)
                    await out.send_json({"type": "status", "status": "done"})
                    _request_remote_catalogue_refresh()
                except Exception as e:
                    print(f"[OSIA Server] delete_api_key({provider!r}) failed: {e}")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- API fallback keys (mobile fallback-keys editor, WOR-29) ---
            # Each provider keeps an ORDERED list of backup keys under
            # settings.json:api_key_fallbacks[provider]; the agent rotates through
            # them when the primary key is rate-limited/exhausted. These two
            # handlers mirror the desktop GUI editor (main_popup._persist): set
            # replaces one slot (or appends), delete removes one slot.
            #
            # set_api_fallback_key sets/replaces the fallback key at ``index`` for
            # the provider; index == -1 appends a new key. An out-of-range index
            # (the client's view is stale) is rejected so we never punch holes in
            # the ordered list.
            if msg_type == "set_api_fallback_key":
                provider = (msg.get("provider") or "").strip().lower()
                index = msg.get("index")
                key = (msg.get("key") or "").strip()
                # bool is an int subclass: reject JSON true/false explicitly so it
                # can't slip through as index 1/0 and clobber the wrong slot.
                if (not provider or isinstance(index, bool)
                        or not isinstance(index, int) or not key):
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia.config import get_setting, set_setting
                    fallbacks = dict(get_setting("api_key_fallbacks", {}) or {})
                    chain = list(fallbacks.get(provider) or [])
                    if index == -1:
                        chain.append(key)
                    elif 0 <= index < len(chain):
                        chain[index] = key
                    else:
                        print(
                            f"[OSIA Server] set_api_fallback_key provider={provider!r} "
                            f"rejected out-of-range index={index} (len={len(chain)})"
                        )
                        await out.send_json({"type": "status", "status": "error"})
                        continue
                    fallbacks[provider] = chain
                    set_setting("api_key_fallbacks", fallbacks)
                    print(
                        f"[OSIA Server] set_api_fallback_key provider={provider!r} "
                        f"index={index} ({len(chain)} keys, len={len(key)})"
                    )
                    # Reused agents must re-read this provider's key chain next turn.
                    _invalidate_cached_models(provider)
                    await out.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(
                        f"[OSIA Server] set_api_fallback_key({provider!r}) failed: {e}"
                    )
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # delete_api_fallback_key removes the fallback key at ``index`` for the
            # provider. When the last key for a provider is removed the provider
            # entry is dropped entirely (same as the desktop GUI _persist), so the
            # dict never carries empty lists. Out-of-range index is rejected.
            if msg_type == "delete_api_fallback_key":
                provider = (msg.get("provider") or "").strip().lower()
                index = msg.get("index")
                # bool is an int subclass: reject JSON true/false explicitly so it
                # can't slip through as index 1/0 and delete the wrong slot.
                if not provider or isinstance(index, bool) or not isinstance(index, int):
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia.config import get_setting, set_setting
                    fallbacks = dict(get_setting("api_key_fallbacks", {}) or {})
                    chain = list(fallbacks.get(provider) or [])
                    if not (0 <= index < len(chain)):
                        print(
                            f"[OSIA Server] delete_api_fallback_key provider={provider!r} "
                            f"rejected out-of-range index={index} (len={len(chain)})"
                        )
                        await out.send_json({"type": "status", "status": "error"})
                        continue
                    chain.pop(index)
                    if chain:
                        fallbacks[provider] = chain
                    else:
                        fallbacks.pop(provider, None)
                    set_setting("api_key_fallbacks", fallbacks)
                    print(
                        f"[OSIA Server] delete_api_fallback_key provider={provider!r} "
                        f"index={index} ({len(chain)} keys left)"
                    )
                    # Reused agents must re-read this provider's key chain next turn.
                    _invalidate_cached_models(provider)
                    await out.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(
                        f"[OSIA Server] delete_api_fallback_key({provider!r}) failed: {e}"
                    )
                    await out.send_json({"type": "status", "status": "error"})
                continue

            if msg_type == "set_setting":
                setting_key = (msg.get("key") or "").strip()
                setting_value = msg.get("value")
                # Whitelist: only these settings.json keys are writable from the
                # mobile app. Anything else is rejected so a client can't poke
                # arbitrary server state.
                allowed_settings = {
                    "liveapi_enabled", "liveapi_provider", "liveapi_model",
                    "tts_provider", "tts_voice", "stt_provider",
                    "stt_fallback_provider", "stt_local_model", "stt_groq_model",
                    "ui_theme",
                    "wake_word_enabled",
                    "wake_greeting_enabled", "wake_greeting_text",
                    "cron_jobs", "ai_hierarchy_enabled",
                    # Flat aliases for the two AI-hierarchy fallback chains; the
                    # write path below folds them into ai_hierarchy.{heavy,medium}.
                    "heavy_chain", "medium_chain",
                    # Live system-metrics push: master flag (shared with the
                    # desktop monitor) + seconds-granularity push cadence.
                    "heartbeat_enabled", "heartbeat_interval_seconds",
                    "heartbeat_checks",
                    # Shared reasoning/thinking level settings (WOR-77).
                    "reasoning",
                    # Non-secret Claude Console workspace, shared with PC.
                    "anthropic_workspace_id",
                    # MCP / Connectors server list (WOR-44 Phase 4). The list is
                    # NON-SECRET (env is always {} — see the mcp_servers write
                    # branch below); per-server tokens live only in the
                    # gitignored api_keys.json under `mcp:<id>`.
                    "mcp_servers",
                }
                if setting_key not in allowed_settings:
                    print(
                        f"[OSIA Server] set_setting rejected non-whitelisted "
                        f"key={setting_key!r}"
                    )
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                if setting_key == "anthropic_workspace_id":
                    if (
                        not isinstance(setting_value, str)
                        or len(setting_value) > 256
                        or any(ord(char) < 32 or ord(char) == 127 for char in setting_value)
                    ):
                        await out.send_json({"type": "status", "status": "error"})
                        continue
                    setting_value = setting_value.strip()
                if setting_key == "stt_fallback_provider" and (
                    not isinstance(setting_value, str)
                    or setting_value not in {"local", "groq", "none"}
                ):
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                if setting_key == "stt_local_model":
                    try:
                        from osia.voice.stt import available_whisper_model_sizes

                        available = available_whisper_model_sizes()
                    except Exception:
                        available = set()
                    if (
                        not isinstance(setting_value, str)
                        or setting_value not in {"small", "medium"}
                        or setting_value not in available
                    ):
                        await out.send_json({"type": "status", "status": "error"})
                        continue
                if setting_key == "stt_groq_model" and (
                    not isinstance(setting_value, str)
                    or setting_value not in {
                        "whisper-large-v3-turbo", "whisper-large-v3",
                    }
                ):
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia.config import (
                        set_setting,
                        get_setting,
                        set_reasoning_settings,
                    )
                    logged_detail = "updated"
                    if setting_key in ("heavy_chain", "medium_chain"):
                        # heavy_chain/medium_chain are the flat names the mobile
                        # app uses for the two ordered fallback chains that live
                        # nested under ``ai_hierarchy`` in settings.json. Nothing
                        # reads a flat key, so fold them into the nested dict the
                        # desktop GUI and task router already consume.
                        category = (
                            "heavy" if setting_key == "heavy_chain" else "medium"
                        )
                        hierarchy = dict(get_setting("ai_hierarchy", {}) or {})
                        chain = setting_value if isinstance(setting_value, list) else []
                        hierarchy[category] = [
                            v for v in chain if isinstance(v, str) and v
                        ]
                        set_setting("ai_hierarchy", hierarchy)
                        logged_detail = f"{len(hierarchy[category])} item(s)"
                    elif setting_key == "ui_theme":
                        set_setting(setting_key, setting_value)
                        try:
                            from osia.gui.theme import get_theme_manager

                            get_theme_manager().set_theme(setting_value, persist=False)
                        except Exception:
                            # Headless/server-only mode still persists canonical state.
                            pass
                    elif setting_key == "mcp_servers":
                        # mcp_servers is a list of NON-SECRET server configs. A
                        # token must NEVER reach the git-tracked settings.json —
                        # it lives only in the gitignored api_keys.json under
                        # `mcp:<id>`. Defensively force env={} AND drop any
                        # `headers` key on every entry (WOR-44 Phase 5: a
                        # streamable-HTTP server's token rides an in-memory
                        # Authorization header at connect time — it must never be
                        # persisted) so a buggy/malicious client cannot persist a
                        # secret here even if it sends one (upholds the WOR-44
                        # secret-storage contract; mirrors the desktop
                        # _mcp_upsert_server, which writes env={} and no headers).
                        clean = []
                        if isinstance(setting_value, list):
                            for s in setting_value:
                                if isinstance(s, dict) and s.get("id"):
                                    e = dict(s)
                                    e["env"] = {}
                                    e.pop("headers", None)
                                    clean.append(e)
                        set_setting("mcp_servers", clean)
                        logged_detail = f"{len(clean)} server(s)"
                    elif setting_key == "reasoning":
                        clean_reasoning = _normalize_mobile_reasoning_setting(setting_value)
                        set_reasoning_settings(clean_reasoning)
                        logged_detail = "updated"
                    elif setting_key == "cron_jobs":
                        from osia import scheduled_tasks

                        result = scheduled_tasks.replace_scheduled_tasks_from_settings(
                            setting_value,
                            source_default="mobile_settings",
                        )
                        if not result.get("ok"):
                            print(
                                "[OSIA Server] set_setting cron_jobs rejected: "
                                f"{result.get('code')}: {result.get('message')}"
                            )
                            await out.send_json({"type": "status", "status": "error"})
                            continue
                        logged_detail = f"{len(result.get('jobs', []))} job(s)"
                    else:
                        set_setting(setting_key, setting_value)
                    # WOR-44 Phase 5: for mcp_servers log the SANITIZED `clean`
                    # list (env={} + headers stripped), never the raw value — a
                    # malicious/buggy client could embed a token in env/headers,
                    # and the raw value would otherwise leak it to the journal in
                    # plaintext. `clean` is only referenced on the mcp_servers
                    # branch, where it is always bound.
                    print(
                        f"[OSIA Server] set_setting {setting_key!r} -> "
                        f"{logged_detail}"
                    )
                    await out.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_setting({setting_key!r}) failed: {e}")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- Config snapshot (mobile "OSIA / Personality / Skills" screen) ---
            # get_config returns the safe PC config in one frame: settings and
            # presence/count metadata, hierarchy chains, personality, and skills.
            if msg_type == "get_config":
                try:
                    # Built by the shared _build_config_snapshot() so an
                    # unsolicited config PUSH (the MCP mirror, Fix 2) ships the
                    # EXACT same shape this request/response path does.
                    cfg = _build_config_snapshot()
                    await out.send_json({"type": "config", "config": cfg})
                    await _send_pending_cron_notifications(out)
                    print(
                        f"[OSIA Server] get_config sent "
                        f"({len(cfg.get('skills', []))} skills)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] get_config failed: {e}")
                    traceback.print_exc()
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- Memory management (WOR-89, additive WS API) ---
            if msg_type == "get_memory":
                try:
                    store = _memory_store()
                    memory = store.load()
                    await out.send_json({"type": "memory", "memory": memory})
                    print(
                        f"[OSIA Server] get_memory sent "
                        f"({len(memory.get('entries', []))} entries)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] get_memory failed: {e}")
                    traceback.print_exc()
                    await _send_memory_error(
                        out,
                        _memory_error_code(e),
                        str(e) or type(e).__name__,
                    )
                continue

            if msg_type == "upsert_memory":
                raw_entry = msg.get("entry")
                if not isinstance(raw_entry, dict):
                    await _send_memory_error(
                        out,
                        "validation_error",
                        "entry must be an object",
                    )
                    continue
                try:
                    store = _memory_store()
                    store.upsert_entry(raw_entry)
                    memory = store.export_snapshot()
                    print(
                        f"[OSIA Server] upsert_memory saved "
                        f"({len(memory.get('entries', []))} entries)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] upsert_memory failed: {e}")
                    await _send_memory_error(
                        out,
                        _memory_error_code(e),
                        str(e) or type(e).__name__,
                    )
                continue

            if msg_type == "delete_memory":
                raw_id = msg.get("id")
                entry_id = raw_id.strip() if isinstance(raw_id, str) else ""
                if not entry_id:
                    await _send_memory_error(
                        out,
                        "validation_error",
                        "id is required",
                    )
                    continue
                try:
                    store = _memory_store()
                    if not store.delete_entry(entry_id):
                        await _send_memory_error(
                            out,
                            "not_found",
                            f"memory entry {entry_id!r} not found",
                        )
                        continue
                    memory = store.export_snapshot()
                    print(
                        f"[OSIA Server] delete_memory removed {entry_id!r} "
                        f"({len(memory.get('entries', []))} entries)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] delete_memory failed: {e}")
                    await _send_memory_error(
                        out,
                        _memory_error_code(e),
                        str(e) or type(e).__name__,
                    )
                continue

            if msg_type == "clear_memory":
                try:
                    store = _memory_store()
                    store.clear()
                    memory = store.export_snapshot()
                    print("[OSIA Server] clear_memory cleared all entries")
                except Exception as e:
                    print(f"[OSIA Server] clear_memory failed: {e}")
                    await _send_memory_error(
                        out,
                        _memory_error_code(e),
                        str(e) or type(e).__name__,
                    )
                continue

            # --- API-key status (mobile "Configure APIs & Providers" editor) ---
            # Credential values stay on the PC. Keep the legacy response type
            # for older clients, but return empty values plus safe status only.
            if msg_type == "get_api_keys":
                try:
                    from osia.config import load_api_keys
                    from osia.models import API_PROVIDERS
                    stored = load_api_keys()
                    frame = _safe_api_key_status_frame(stored, API_PROVIDERS)
                    keys_set = frame["keys_set"]
                    await out.send_json(frame)
                    print(
                        f"[OSIA Server] get_api_keys sent "
                        f"({sum(1 for v in keys_set.values() if v)}/{len(keys_set)} set; values withheld)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] get_api_keys failed: {e}")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- MCP / Connectors: cancel an in-flight OAuth login (WOR-97) ---
            # Additive frame: abort an OAuth connector stuck "connecting…" (the
            # user went to create an account / cancelled / closed the browser).
            # Routes to MCPManager.cancel_pending — a no-op unless the connector
            # is CONNECTING, so an accidental cancel on a CONNECTED connector does
            # nothing, and it never touches ``enabled`` (a cancelled flow never
            # connected). Distinct from mcp_disconnect on purpose: that carries
            # revoke/forget_client semantics + the 10s graceful teardown — wrong
            # shape for aborting an in-flight login. cancel_pending BLOCKS (≤10s),
            # so offload to a daemon thread and broadcast the reset to every
            # client (PC + mobile pills) from that thread.
            if msg_type == "mcp_oauth_cancel":
                raw_id = msg.get("id")
                server_id = raw_id.strip() if isinstance(raw_id, str) else ""
                raw_request_id = msg.get("request_id")
                request_id = (
                    raw_request_id.strip()
                    if isinstance(raw_request_id, str)
                    else ""
                )
                if not server_id:
                    await out.send_json(
                        {"type": "status", "status": "error", "error": "missing id"}
                    )
                    continue
                if _is_native_gmail_rest_mobile_action(msg_type, server_id):
                    raw_attempt_id = msg.get("attempt_id")
                    attempt_id = (
                        raw_attempt_id.strip()
                        if isinstance(raw_attempt_id, str)
                        else ""
                    )
                    await _cancel_native_gmail_rest_mobile_connect(
                        attempt_id, owner=out
                    )
                    await out.send_json({
                        "type": "status",
                        "status": "done",
                        "id": server_id,
                        **({"request_id": request_id} if request_id else {}),
                    })
                    continue
                loop = asyncio.get_running_loop()

                def _cancel_worker(sid=server_id, loop=loop):
                    try:
                        from osia.mcp_client import get_manager
                        get_manager().cancel_pending(sid)
                    except Exception as e:
                        print(f"[OSIA Server] mcp_oauth_cancel worker {sid!r}: {e}")
                    try:
                        asyncio.run_coroutine_threadsafe(
                            _broadcast_mcp_status(_mcp_status_frame(sid)), loop
                        )
                    except Exception:
                        pass

                threading.Thread(
                    target=_cancel_worker,
                    name="osia-mcp-oauth-cancel", daemon=True,
                ).start()
                await out.send_json({"type": "status", "status": "done"})
                continue

            # --- MCP / Connectors: connect / disconnect / status (WOR-44 P4) ---
            # ACTIONS (not settings) that drive THIS process's MCPManager
            # singleton (osia.mcp.get_manager — the server's own; see the helpers
            # near _broadcast_mcp_status). The server LIST round-trips as the
            # `mcp_servers` setting (set_setting allowlist + get_config); the
            # per-server SECRET token lives only in api_keys.json under
            # `mcp:<id>` and is injected IN-MEMORY at connect time — never in a
            # frame, never in settings.json.
            #
            # IMPORTANT: MCPManager.connect()/disconnect() BLOCK the calling
            # thread until the handshake (or teardown) resolves — they schedule
            # work onto the manager's OWN loop but then wait on a future. Calling
            # them inline here would stall the server event loop for up to the
            # connect timeout, freezing every other client. So we offload to a
            # short-lived daemon thread (mirroring the desktop GUI's connect
            # worker), ack immediately, and broadcast the resulting status to
            # every client from that thread via run_coroutine_threadsafe.
            if msg_type in ("mcp_connect", "mcp_disconnect", "mcp_status"):
                raw_id = msg.get("id")
                server_id = raw_id.strip() if isinstance(raw_id, str) else ""
                raw_request_id = msg.get("request_id")
                request_id = (
                    raw_request_id.strip()
                    if isinstance(raw_request_id, str)
                    else ""
                )

                if msg_type == "mcp_status":
                    # Pure, non-blocking read of the manager snapshot (never
                    # leaks secrets — see _mcp_status_frame). One id, or all.
                    try:
                        await out.send_json(_mcp_status_frame(server_id or None))
                    except Exception as e:
                        print(f"[OSIA Server] mcp_status failed: {e}")
                        await out.send_json({"type": "status", "status": "error"})
                    continue

                # connect / disconnect require a known, saved server id, except
                # the native Gmail REST integration which reuses this action
                # channel but is intentionally not an MCP server.
                if not server_id:
                    await out.send_json(
                        {"type": "status", "status": "error", "error": "missing id"}
                    )
                    continue
                if _is_native_gmail_rest_mobile_action(msg_type, server_id):
                    if msg_type == "mcp_connect":
                        result = await _begin_native_gmail_rest_mobile_connect(
                            owner=out
                        )
                        if result.get("ok"):
                            await out.send_json(
                                _gmail_rest_oauth_url_frame(result, request_id)
                            )
                            await out.send_json({
                                "type": "status",
                                "status": "done",
                                "id": server_id,
                                **(
                                    {"request_id": request_id}
                                    if request_id
                                    else {}
                                ),
                            })
                        else:
                            await out.send_json({
                                "type": "status",
                                "status": "error",
                                "id": server_id,
                                "error_category": result.get("error_category")
                                or "authentication_failed",
                                "error": result.get("message")
                                or "Unable to start Google authorization.",
                                **(
                                    {"request_id": request_id}
                                    if request_id
                                    else {}
                                ),
                            })
                        continue
                    threading.Thread(
                        target=_disconnect_native_gmail_rest_and_broadcast,
                        args=(asyncio.get_running_loop(),),
                        name="osia-gmail-rest-disconnect",
                        daemon=True,
                    ).start()
                    await out.send_json({
                        "type": "status",
                        "status": "done",
                        "id": server_id,
                        **({"request_id": request_id} if request_id else {}),
                    })
                    continue
                saved = _mcp_find_saved_server(server_id)
                if saved is None:
                    print(
                        f"[OSIA Server] {msg_type} rejected unknown id={server_id!r}"
                    )
                    await out.send_json(
                        {"type": "status", "status": "error",
                         "error": "unknown server"}
                    )
                    continue

                loop = asyncio.get_running_loop()

                if msg_type == "mcp_connect":
                    if not _mcp_connectable(saved):
                        # Unsupported transport / url-less OAuth (mirrors the GUI,
                        # which hides the connect button otherwise). Clean error,
                        # no thread spawned, no FAILED entry left in the manager.
                        print(
                            f"[OSIA Server] mcp_connect id={server_id!r} not "
                            f"connectable (transport={saved.get('transport')!r}, "
                            f"auth={saved.get('auth_type')!r})"
                        )
                        await out.send_json(
                            {"type": "status", "status": "error",
                             "error": "not connectable yet"}
                        )
                        continue
                    if saved.get("auth_type") == "oauth":
                        # WOR-96 PART 2a: drive the PART-1 tunnel OAuth flow FROM
                        # the phone and push the authorize URL back over THIS
                        # connection — no PC browser. The flow runs entirely
                        # server-side (TunnelOAuthCallback + cross-loop registry +
                        # public /oauth/callback); the token is minted + stored
                        # server-side, so only the PUBLIC authorize URL (client_id /
                        # redirect / state / PKCE challenge — no secret) rides the
                        # WS. Run it as a detached task so the up-to-30s discovery
                        # wait never parks this connection's receive loop, and ack
                        # immediately (the URL arrives later as ``mcp_oauth_url``).
                        async def _drive_oauth(out=out, sid=server_id, saved=saved):
                            try:
                                res = await _begin_oauth_tunnel_connect(sid, saved)
                            except Exception as e:
                                res = {"ok": False,
                                       "error": f"{type(e).__name__}: {e}"}
                            # The phone may have dropped during the discovery wait —
                            # don't push onto an orphaned channel.
                            if out not in _WS_CLIENTS:
                                return
                            try:
                                if res.get("ok") and res.get("auth_url"):
                                    # Pushed to the REQUESTING connection only (the
                                    # auth_url is bound to one phone's browser
                                    # session) — never broadcast. Additive frame.
                                    await out.send_json(
                                        {"type": "mcp_oauth_url",
                                         "id": sid, "url": res["auth_url"]}
                                    )
                                else:
                                    await out.send_json(
                                        {"type": "status", "status": "error",
                                         "error": res.get("error")
                                         or "oauth start failed"}
                                    )
                            except Exception:
                                pass

                        task = asyncio.create_task(_drive_oauth())
                        _OAUTH_DRIVE_TASKS.add(task)
                        task.add_done_callback(_OAUTH_DRIVE_TASKS.discard)
                        await out.send_json({"type": "status", "status": "done"})
                        continue
                    connect_cfg = _mcp_build_connect_cfg(saved)

                    def _connect_worker(cfg=connect_cfg, sid=server_id, loop=loop):
                        status = None
                        try:
                            from osia.mcp_client import get_manager
                            status = get_manager().connect(cfg)
                        except Exception as e:
                            print(f"[OSIA Server] mcp_connect worker {sid!r}: {e}")
                        # WOR-44 Phase 2 (direction B): persist enabled=True on a
                        # SUCCESSFUL connect so the shared mcp_servers[].enabled flag
                        # is authoritative and the desktop reconcile daemon mirrors
                        # this onto the PC — even with no separate set_setting from
                        # the mobile. Only on success: a failed handshake must not
                        # mark the connector enabled (the desktop would then try to
                        # connect a connector the user never got working).
                        try:
                            from osia.mcp_client import STATUS_CONNECTED
                            if status == STATUS_CONNECTED:
                                _mcp_set_enabled(sid, True)
                        except Exception:
                            pass
                        # Broadcast the RESOLVED status (connected/failed) to all.
                        try:
                            asyncio.run_coroutine_threadsafe(
                                _broadcast_mcp_status(_mcp_status_frame(sid)), loop
                            )
                        except Exception:
                            pass

                    threading.Thread(
                        target=_connect_worker, name="osia-mcp-connect", daemon=True
                    ).start()
                    # Optimistic "connecting" so every client flips at once; the
                    # worker broadcasts the terminal status when the handshake
                    # resolves. Enqueued synchronously here (before the worker can
                    # finish the handshake), so it always precedes that frame.
                    try:
                        await _broadcast_mcp_status(
                            {"type": "mcp_status",
                             "servers": {server_id: {"status": "connecting",
                                                     "error": "", "tools": 0}}}
                        )
                    except Exception:
                        pass
                    await out.send_json({"type": "status", "status": "done"})
                    continue

                # mcp_disconnect
                # WOR-96 PART 2a: opt-in revocation for OAuth connectors. BOTH flags
                # default to False → today's behaviour is BYTE-IDENTICAL (session
                # torn down, token KEPT on disk, silent reconnect intact). revoke=
                # True performs RFC 7009 revocation then drops the stored token;
                # forget_client=True additionally drops the DCR client blob (full
                # account-switch / clean re-auth). Only meaningful for OAuth.
                _disc_revoke = (
                    bool(msg.get("revoke")) if isinstance(msg, dict) else False
                )
                _disc_forget = (
                    bool(msg.get("forget_client")) if isinstance(msg, dict) else False
                )
                _disc_is_oauth = (
                    str(saved.get("auth_type") or "").strip().lower() == "oauth"
                )
                _disc_url = str(saved.get("url") or "").strip()

                def _disconnect_worker(sid=server_id, loop=loop,
                                       revoke=_disc_revoke, forget=_disc_forget,
                                       is_oauth=_disc_is_oauth, url=_disc_url):
                    try:
                        from osia.mcp_client import get_manager
                        get_manager().disconnect(sid)
                    except Exception as e:
                        print(f"[OSIA Server] mcp_disconnect worker {sid!r}: {e}")
                    # WOR-96 PART 2a: with the session DOWN, optionally revoke the
                    # grant at the AS (RFC 7009, best-effort) and clear what the user
                    # asked to forget. Order: revoke → clear → enabled=False →
                    # broadcast. revoke_oauth_token NEVER raises and NEVER logs token
                    # values; an undiscoverable revocation_endpoint degrades to a
                    # local clear only (OSIA forgets the token regardless).
                    if is_oauth and revoke:
                        try:
                            from osia.mcp_client.oauth import revoke_oauth_token
                            asyncio.run(revoke_oauth_token(sid, url))
                        except Exception:
                            pass
                        try:
                            from osia.config import (
                                get_mcp_oauth, set_mcp_oauth, delete_mcp_oauth,
                            )
                            if forget:
                                # Switch account: drop the whole blob (token + DCR
                                # client) so the next connect re-registers + re-logs.
                                delete_mcp_oauth(sid)
                            else:
                                # Sign out: drop only the token, keep the DCR client.
                                blob = get_mcp_oauth(sid)
                                if isinstance(blob, dict) and blob:
                                    blob.pop("token", None)
                                    blob.pop("expires_at", None)
                                    set_mcp_oauth(sid, blob)
                        except Exception:
                            pass
                    # WOR-44 Phase 2 (direction B): persist enabled=False so the
                    # shared mcp_servers[].enabled flag is authoritative and the
                    # desktop reconcile daemon disconnects the PC manager to match.
                    # Unconditional (the user explicitly asked to disconnect);
                    # idempotent (a no-op write when already False).
                    try:
                        _mcp_set_enabled(sid, False)
                    except Exception:
                        pass
                    try:
                        asyncio.run_coroutine_threadsafe(
                            _broadcast_mcp_status(_mcp_status_frame(sid)), loop
                        )
                    except Exception:
                        pass

                threading.Thread(
                    target=_disconnect_worker,
                    name="osia-mcp-disconnect", daemon=True,
                ).start()
                await out.send_json({"type": "status", "status": "done"})
                continue

            # --- Heartbeat config (mobile live-metrics screen) ---
            # enable flag + the seconds-granularity push cadence used by the
            # heartbeat_data stream (distinct from the desktop monitor's
            # heartbeat_interval_minutes).
            if msg_type == "get_heartbeat_config":
                try:
                    from osia.config import load_settings
                    s = load_settings()
                    try:
                        interval = int(s.get("heartbeat_interval_seconds", 5) or 5)
                    except (TypeError, ValueError):
                        interval = 5
                    await out.send_json({
                        "type": "heartbeat_config",
                        "config": {
                            "enabled": bool(s.get("heartbeat_enabled", False)),
                            "interval_seconds": interval,
                        },
                    })
                    print("[OSIA Server] get_heartbeat_config sent")
                except Exception as e:
                    print(f"[OSIA Server] get_heartbeat_config failed: {e}")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- Personality document ---
            if msg_type == "set_personality":
                persona_text = msg.get("text")
                if not isinstance(persona_text, str):
                    persona_text = ""
                try:
                    from osia import personality
                    personality.save_personality(persona_text)
                    print(
                        f"[OSIA Server] set_personality "
                        f"({len(persona_text)} chars)"
                    )
                    await out.send_json({"type": "status", "status": "done"})
                    await _broadcast_personality_config()
                except Exception:
                    print("[OSIA Server] set_personality failed (details withheld)")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- Skills: update / create / delete ---
            # set_skill updates an EXISTING skill's enabled flag and/or its prose
            # (both fields optional, so a toggle and a text edit can each arrive
            # alone). create_skill seeds a new <name>.md from the template;
            # delete_skill removes the file and its enabled flag.
            if msg_type == "set_skill":
                skill_name = (msg.get("name") or "").strip()
                if not skill_name:
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia import personality
                    if "text" in msg:
                        personality.write_skill(skill_name, msg.get("text") or "")
                    if "enabled" in msg:
                        effective_enabled = personality.set_skill_enabled(
                            skill_name, bool(msg.get("enabled"))
                        )
                    else:
                        effective_enabled = personality.is_skill_enabled(skill_name)
                    skill_record = personality.get_skill_definition(skill_name)
                    skill_status = (
                        skill_record.status if skill_record is not None else "missing"
                    )
                    print(
                        f"[OSIA Server] set_skill "
                        f"text={'text' in msg} "
                        f"enabled={effective_enabled} status={skill_status}"
                    )
                    await out.send_json({
                        "type": "status",
                        "status": "done",
                        "skill_status": skill_status,
                    })
                    await _broadcast_personality_config()
                except Exception:
                    print("[OSIA Server] set_skill failed (details withheld)")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            if msg_type == "create_skill":
                skill_name = (msg.get("name") or "").strip()
                if not skill_name:
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia import personality
                    created = personality.create_skill(skill_name)
                    if not created:
                        await out.send_json({"type": "status", "status": "error"})
                        continue
                    print("[OSIA Server] create_skill completed")
                    await out.send_json({
                        "type": "status", "status": "done", "name": created,
                    })
                    await _broadcast_personality_config()
                except Exception:
                    print("[OSIA Server] create_skill failed (details withheld)")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            if msg_type == "delete_skill":
                skill_name = (msg.get("name") or "").strip()
                if not skill_name:
                    await out.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia import personality
                    personality.delete_skill(skill_name)
                    print("[OSIA Server] delete_skill completed")
                    await out.send_json({"type": "status", "status": "done"})
                    await _broadcast_personality_config()
                except Exception:
                    print("[OSIA Server] delete_skill failed (details withheld)")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- Speak on demand (read-aloud button on the phone) ---
            # Synthesize the text to MP3 and send it BACK to the phone as an
            # audio frame — the phone plays it. This is NOT PC-speaker
            # playback: the button lives on the phone, so the voice must
            # come out of the phone.
            if msg_type == "speak":
                coordinated = _application_transport_coordination_enabled()
                speak_meta = None
                speak_language = None
                if coordinated:
                    speak_conv_id = str(msg.get("conversation_id") or "").strip()
                    speak_request_id = _clean_client_request_id(
                        msg.get("request_id") or msg.get("client_request_id")
                    )
                    speak_turn_id = str(msg.get("turn_id") or "").strip()
                    speak_message_id = str(msg.get("message_id") or "").strip()
                    source_request_id = _clean_client_request_id(
                        msg.get("source_request_id")
                    )
                    source_turn_id = str(msg.get("source_turn_id") or "").strip()
                    if (
                        not speak_conv_id
                        or speak_request_id is None
                        or not speak_turn_id
                        or not speak_message_id
                        or source_request_id is None
                        or not source_turn_id
                    ):
                        print("[OSIA Server] speak rejected: missing correlation")
                        continue
                    persisted = conversations.find_correlated_turn(
                        speak_conv_id,
                        source_request_id,
                    )
                    if (
                        persisted is None
                        or persisted.get("turn_id") != source_turn_id
                        or persisted.get("assistant_message_id") != speak_message_id
                        or not isinstance(persisted.get("assistant_content"), str)
                    ):
                        print("[OSIA Server] speak rejected: correlation mismatch")
                        continue
                    source_binding = _persisted_turn_route_binding(persisted)
                    if source_binding is None:
                        print("[OSIA Server] speak rejected: route binding unavailable")
                        continue
                    speak_text = str(persisted["assistant_content"])
                    speak_language = normalize_locale(persisted.get("language"))
                    speak_meta = {
                        "conversation_id": speak_conv_id,
                        "request_id": speak_request_id,
                        "client_request_id": speak_request_id,
                        "turn_id": speak_turn_id,
                        "run_id": speak_turn_id,
                        "provider_id": source_binding["provider_id"],
                        "model_id": source_binding["model_id"],
                        "effective_reasoning": source_binding[
                            "effective_reasoning"
                        ],
                        "assistant_message_id": speak_message_id,
                        "_event_sequence": 0,
                    }
                else:
                    # Preserve the relay-only read-aloud path for existing
                    # history entries that predate canonical correlation.
                    speak_text = str(msg.get("text") or "").strip()
                    if not speak_text:
                        continue
                print(
                    f"[OSIA Server] speak request "
                    f"({len(speak_text)} chars)"
                )
                try:
                    # Off-thread so the event loop keeps serving while the
                    # TTS provider synthesizes.
                    mp3_bytes = await asyncio.to_thread(
                        synthesize_to_mp3_bytes, speak_text, speak_language,
                    )
                    mp3_b64 = base64.b64encode(mp3_bytes).decode("ascii")
                    # source="speak" tells the client this audio was
                    # explicitly requested, so it plays even when the
                    # auto-TTS preference is off.
                    if speak_meta is not None:
                        frame = _turn_event_frame(
                            speak_meta,
                            "audio",
                            message_id=str(speak_meta["assistant_message_id"]),
                            payload={
                                "format": "mp3",
                                "data": mp3_b64,
                                "source": "speak",
                                "phase": "media",
                                "media_terminal": True,
                            },
                        )
                    else:
                        frame = {
                            "type": "audio",
                            "format": "mp3",
                            "data": mp3_b64,
                            "source": "speak",
                        }
                    await out.send_json(frame)
                    print(
                        f"[OSIA Server] speak audio sent to mobile "
                        f"({len(mp3_bytes)} bytes)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] speak failed: {e}")
                    traceback.print_exc()
                    if speak_meta is not None:
                        try:
                            await out.send_json(
                                _turn_event_frame(
                                    speak_meta,
                                    "media_status",
                                    message_id=str(
                                        speak_meta["assistant_message_id"]
                                    ),
                                    payload={
                                        "phase": "media",
                                        "status": "error",
                                        "media_terminal": True,
                                    },
                                )
                            )
                        except Exception:
                            pass
                continue

            # --- Transactional attachment upload ---
            if msg_type in ATTACHMENT_EVENT_TYPES:
                responses = attachment_batches.handle(msg)
                for response in responses:
                    await out.send_json(response)
                continue

            # --- Legacy file upload (preserved for older mobile clients) ---
            if msg_type == "file":
                raw_filename = msg.get("filename", "upload.bin")
                safe_name = _sanitize_upload_filename(
                    raw_filename if isinstance(raw_filename, str) else ""
                )
                try:
                    upload = legacy_uploads.save_frame(msg)
                except LegacyUploadError as exc:
                    print(f"[OSIA Server] legacy file rejected ({exc.code})")
                    await out.send_json({
                        "type": "file_received",
                        "filename": safe_name,
                        "success": False,
                        "error": {"code": exc.code},
                    })
                    continue

                target = upload.path
                safe_name = upload.display_name
                ownership_transferred = False
                ack_sent = False
                created_chat_source_refs: list[str] = []
                chat_sources_accepted = False
                chat_source_owner_id = ""
                try:
                    image_mime = _image_mime_for(upload.path)
                    if image_mime is None:
                        # Non-image: queue for the next text/audio turn.
                        pending_files.append(str(target))
                        pending_file_display_names[str(target)] = safe_name
                        ownership_transferred = True
                    print(
                        f"[OSIA Server] file received "
                        f"({upload.byte_size} bytes)"
                    )
                    await out.send_json({
                        "type": "file_received",
                        "filename": safe_name,
                        "success": True,
                    })
                    ack_sent = True
                    if image_mime is not None:
                        # Legacy images use the same opaque local OCR source as
                        # transactional attachments. Raw bytes never enter the
                        # provider turn or a client-visible frame.
                        image_prompt = str(msg.get("prompt") or "").strip()
                        display_prompt = image_prompt or "Analyze the attached document image."
                        upload_digest = hashlib.sha256(target.read_bytes()).hexdigest()
                        image_identity = _turn_input_identity(
                            display_prompt,
                            kind="legacy-image",
                            manifest=_legacy_attachment_manifest(
                                [str(target)], {str(target): safe_name},
                            ),
                        )
                        legacy_image_hash = hashlib.sha256(
                            upload_digest.encode("ascii")
                        ).hexdigest()
                        # Run the OCR/document turn in its own task so the receive
                        # loop stays responsive while the model analyses the
                        # image (same head-of-line fix as text/audio). Resolve
                        # the conversation FIRST so the turn is keyed by a real
                        # id (no synthetic placeholder, no duplicate-conv race).
                        client_request_id = _clean_client_request_id(
                            msg.get("request_id") or msg.get("client_request_id"),
                        )
                        if client_request_id is None:
                            if _application_transport_coordination_enabled():
                                await out.send_json({
                                    "type": "status",
                                    "content": "error",
                                    "error": "missing_or_invalid_request_id",
                                })
                                continue
                            client_request_id = _legacy_request_id()
                        try:
                            if await _send_existing_turn_status_for_request(
                                out,
                                client_request_id,
                                expected_identity=image_identity,
                                legacy_client_content_hash=legacy_image_hash,
                                legacy_exact_user_content=display_prompt,
                            ):
                                continue
                        except ValueError:
                            await out.send_json({
                                "type": "status",
                                "content": "error",
                                "error": "request_id_conflict",
                            })
                            continue
                        _conv = await _resolve_turn_conversation(
                            msg, display_prompt, client_request_id,
                        )
                        if _conv is not None:
                            chat_source_owner_id = _conv
                            try:
                                if await _send_persisted_turn_for_request(
                                    out,
                                    _conv,
                                    client_request_id,
                                    expected_identity=image_identity,
                                    legacy_client_content_hash=legacy_image_hash,
                                    legacy_exact_user_content=display_prompt,
                                ):
                                    continue
                            except ValueError:
                                await out.send_json({
                                    "type": "status",
                                    "content": "error",
                                    "error": "request_id_conflict",
                                    "conversation_id": _conv,
                                })
                                continue
                            if _is_conversation_busy(_conv):
                                try:
                                    if await _send_existing_turn_status(
                                        out,
                                        _conv,
                                        client_request_id,
                                        expected_identity=image_identity,
                                        legacy_client_content_hash=legacy_image_hash,
                                        legacy_exact_user_content=display_prompt,
                                    ):
                                        continue
                                except ValueError:
                                    await out.send_json({
                                        "type": "status",
                                        "content": "error",
                                        "error": "request_id_conflict",
                                        "conversation_id": _conv,
                                    })
                                    continue
                            from osia.document_sources import get_source_store

                            capability = get_source_store().create(
                                target,
                                owner_id=_conv,
                                source_kind="legacy_mobile_attachment",
                                display_name=safe_name,
                                declared_mime=image_mime,
                                permission_epoch=_current_policy_epoch(),
                            )
                            created_chat_source_refs.append(
                                str(capability["source_ref"])
                            )
                            document_content = (
                                "[Document image available through read_document: "
                                f"source_ref={capability['source_ref']} "
                                f"type={capability['type_label']} state={capability['state']}]"
                                f"\n\n{display_prompt}"
                            )
                            turn_meta = _new_turn_meta(_conv, client_request_id)
                            turn_meta["_document_owner_device"] = mobile_device_id
                            turn_meta["_execution_origin_ref"] = (
                                authorization_origin_ref
                            )
                            turn_meta["client_locale"] = msg.get("locale")
                            turn_meta["_original_user_text"] = display_prompt
                            turn_meta["tts_enabled"] = (
                                msg.get("tts_enabled", True) is not False
                            )
                            _apply_turn_input_identity(turn_meta, image_identity)
                            turn_meta["_durable_agent_input_v1"] = document_content
                            turn_meta["_preaccept_source_refs"] = list(
                                created_chat_source_refs
                            )
                            turn_meta["persisted_user_content"] = display_prompt
                            if not await _accept_user_turn_before_spawn(
                                _conv,
                                display_prompt,
                                turn_meta,
                            ):
                                continue
                            chat_sources_accepted = True

                            async def _run_legacy_image_turn(
                                snap,
                                _vc=document_content,
                                _c=_conv,
                                _m=turn_meta,
                                _path=target,
                            ):
                                async def _operation():
                                    return await _run_agent_and_respond(
                                        out,
                                        _vc,
                                        snap,
                                        _c,
                                        get_agent=_get_conn_agent,
                                        turn_meta=_m,
                                    )

                                return await run_with_legacy_cleanup(
                                    legacy_uploads,
                                    [_path],
                                    _operation,
                                )

                            ownership_transferred = _spawn_turn(
                                _conv,
                                _run_legacy_image_turn,
                                turn_meta,
                            )
                except Exception as e:
                    print(
                        "[OSIA Server] legacy file processing failed "
                        f"({type(e).__name__})"
                    )
                    if ack_sent:
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "legacy_file_processing_failed",
                        })
                    else:
                        await out.send_json({
                            "type": "file_received",
                            "filename": safe_name,
                            "success": False,
                            "error": {"code": "legacy_file_processing_failed"},
                        })
                finally:
                    if created_chat_source_refs and not chat_sources_accepted:
                        _release_unaccepted_source_refs(
                            created_chat_source_refs,
                            owner_id=chat_source_owner_id,
                        )
                    if not ownership_transferred:
                        legacy_uploads.cleanup([target])
                continue

            # --- Text message ---
            if msg_type == "text":
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue
                raw_turn_directives = msg.get("turn_directives")
                turn_directives = None
                canonical_turn_directives = ""
                if raw_turn_directives is not None:
                    try:
                        from osia.slash_commands import SlashTurnDirectivesV1

                        turn_directives = SlashTurnDirectivesV1.from_wire(
                            raw_turn_directives
                        )
                        canonical_turn_directives = turn_directives.canonical_json()
                    except Exception as exc:
                        error_code = getattr(exc, "code", "invalid_directives")
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": error_code,
                        })
                        continue
                client_request_id = _clean_client_request_id(
                    msg.get("request_id") or msg.get("client_request_id"),
                )
                if client_request_id is None:
                    if _application_transport_coordination_enabled():
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "missing_or_invalid_request_id",
                        })
                        continue
                    client_request_id = _legacy_request_id()
                attachment_batch_id = str(msg.get("attachment_batch_id") or "")
                validated_attachment_batch = None
                attachment_validation_error = None
                requested_conversation_id = str(
                    msg.get("conversation_id") or ""
                ).strip()
                waiting_turn_command = msg.get("waiting_turn_command") is True
                legacy_content_identity = (
                    f"{user_text}\0{attachment_batch_id}"
                    if attachment_batch_id else user_text
                )
                if raw_turn_directives is not None:
                    legacy_content_identity += "\0" + canonical_turn_directives
                content_identity = json.dumps(
                    {
                        "attachment_batch_id": attachment_batch_id,
                        "text": user_text,
                        "turn_directives": (
                            canonical_turn_directives
                            if raw_turn_directives is not None else None
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                legacy_candidate_files = (
                    list(pending_files) if not attachment_batch_id else []
                )
                legacy_candidate_names = {
                    path: pending_file_display_names.get(path, "")
                    for path in legacy_candidate_files
                }
                if attachment_batch_id:
                    try:
                        validated_attachment_batch = (
                            attachment_batches.validate_turn_reference(
                                attachment_batch_id,
                                requested_conversation_id,
                                client_request_id,
                            )
                        )
                    except AttachmentProtocolError as exc:
                        if exc.code not in {
                            "attachment_batch_not_committed",
                            "attachment_batch_already_consumed",
                        }:
                            await out.send_json({
                                "type": "attachment_batch_failed",
                                "protocol_version": 1,
                                "batch_id": attachment_batch_id,
                                "conversation_id": requested_conversation_id,
                                "client_request_id": client_request_id,
                                "request_id": client_request_id,
                                "event_id": f"event_{secrets.token_hex(16)}",
                                "sequence": 0,
                                "category": exc.code,
                            })
                            continue
                        attachment_validation_error = exc
                input_manifest = (
                    _transactional_attachment_manifest(
                        validated_attachment_batch
                    )
                    if validated_attachment_batch is not None else
                    _legacy_attachment_manifest(
                        legacy_candidate_files, legacy_candidate_names,
                    )
                    if legacy_candidate_files else
                    None
                )
                input_identity = _turn_input_identity(
                    content_identity,
                    kind=(
                        "transactional-attachments"
                        if attachment_batch_id else
                        "legacy-attachments"
                        if legacy_candidate_files else
                        "text"
                    ),
                    manifest=input_manifest,
                )
                legacy_text_hash = (
                    ""
                    if input_manifest else
                    hashlib.sha256(
                        legacy_content_identity.encode("utf-8")
                    ).hexdigest()
                )

                def _discard_replayed_text_inputs() -> None:
                    if validated_attachment_batch is not None:
                        attachment_batches.release(attachment_batch_id)
                    if legacy_candidate_files:
                        legacy_uploads.cleanup(legacy_candidate_files)
                        for path in legacy_candidate_files:
                            try:
                                pending_files.remove(path)
                            except ValueError:
                                pass
                            pending_file_display_names.pop(path, None)

                try:
                    if await _send_existing_turn_status_for_request(
                        out,
                        client_request_id,
                        expected_identity=input_identity,
                        legacy_client_content_hash=legacy_text_hash,
                        legacy_exact_user_content=user_text,
                    ):
                        _discard_replayed_text_inputs()
                        continue
                except ValueError:
                    _discard_replayed_text_inputs()
                    await out.send_json({
                        "type": "status",
                        "content": "error",
                        "error": "request_id_conflict",
                    })
                    continue
                # A completed attachment turn has already released its
                # temporary batch.  Replay it from canonical persistence before
                # requiring the live batch, provided the client names the
                # conversation (which it learns from turn acceptance).
                if attachment_batch_id and requested_conversation_id:
                    try:
                        if await _send_persisted_turn_for_request(
                            out,
                            requested_conversation_id,
                            client_request_id,
                            expected_identity=input_identity,
                            legacy_client_content_hash=legacy_text_hash,
                            legacy_exact_user_content=user_text,
                        ):
                            _discard_replayed_text_inputs()
                            continue
                    except ValueError:
                        _discard_replayed_text_inputs()
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                            "conversation_id": requested_conversation_id,
                        })
                        continue
                if attachment_validation_error is not None:
                    await out.send_json({
                            "type": "attachment_batch_failed",
                            "protocol_version": 1,
                            "batch_id": attachment_batch_id,
                            "conversation_id": requested_conversation_id,
                            "client_request_id": client_request_id,
                            "request_id": client_request_id,
                            "event_id": f"event_{secrets.token_hex(16)}",
                            "sequence": 0,
                            "category": attachment_validation_error.code,
                        })
                    continue
                # Consume pending files synchronously (clears the shared list)
                # so this turn — and only this turn — carries them. Resolve the
                # conversation synchronously (creating it on the first message)
                # so the turn is keyed by a real id, then run the agent in its
                # own task so the receive loop stays free.
                _conv = await _resolve_turn_conversation(
                    msg, user_text, client_request_id,
                )
                if _conv is not None:
                    try:
                        if await _send_persisted_turn_for_request(
                            out,
                            _conv,
                            client_request_id,
                            expected_identity=input_identity,
                            legacy_client_content_hash=legacy_text_hash,
                            legacy_exact_user_content=user_text,
                        ):
                            _discard_replayed_text_inputs()
                            continue
                    except ValueError:
                        _discard_replayed_text_inputs()
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                            "conversation_id": _conv,
                        })
                        continue
                    if turn_directives is not None:
                        try:
                            from osia.slash_commands import validate_live

                            turn_directives.require_sendable(
                                body=user_text,
                                has_attachment=bool(attachment_batch_id),
                            )
                            validate_live(
                                turn_directives,
                                current_conversation_id=_conv,
                                has_attachment=bool(attachment_batch_id),
                                requires_image_attachment=bool(
                                    validated_attachment_batch is not None
                                    and any(
                                        item.detected_category == "image"
                                        for item in validated_attachment_batch.files.values()
                                    )
                                ),
                            )
                        except Exception as exc:
                            error_code = getattr(
                                exc, "code", "invalid_directives"
                            )
                            locale = normalize_locale(msg.get("locale")) or "en"
                            message_fn = getattr(exc, "message", None)
                            safe_message = (
                                message_fn(locale)
                                if callable(message_fn) else
                                "Invalid slash directives."
                            )
                            await out.send_json({
                                "type": "status",
                                "content": "error",
                                "error": error_code,
                                "message": safe_message,
                                "conversation_id": _conv,
                            })
                            continue
                    if _is_conversation_busy(_conv):
                        if await _handle_waiting_tool_command(
                            out, _conv, user_text,
                        ):
                            continue
                        try:
                            if await _send_existing_turn_status(
                                out,
                                _conv,
                                client_request_id,
                                expected_identity=input_identity,
                                legacy_client_content_hash=legacy_text_hash,
                                legacy_exact_user_content=user_text,
                            ):
                                _discard_replayed_text_inputs()
                                continue
                        except ValueError:
                            _discard_replayed_text_inputs()
                            await out.send_json({
                                "type": "status",
                                "content": "error",
                                "error": "request_id_conflict",
                                "conversation_id": _conv,
                            })
                            continue
                    if waiting_turn_command:
                        # The suspended turn may have reached terminal between
                        # Mobile's tap and this receive-loop iteration. Never
                        # reinterpret that bounded control command as a new
                        # provider turn; refresh canonical history instead.
                        settled = conversations.get_conversation(_conv)
                        if settled is not None:
                            await out.send_json(
                                _conversation_history_frame(settled)
                            )
                        continue
                    committed_attachments = []
                    created_source_refs: list[str] = []
                    turn_image_artifacts: list = []
                    if attachment_batch_id:
                        try:
                            committed_attachments = attachment_batches.consume_for_turn(
                                attachment_batch_id,
                                _conv,
                                client_request_id,
                            )
                        except AttachmentProtocolError as exc:
                            await out.send_json({
                                "type": "attachment_batch_failed",
                                "protocol_version": 1,
                                "batch_id": attachment_batch_id,
                                "conversation_id": _conv,
                                "client_request_id": client_request_id,
                                "request_id": client_request_id,
                                "event_id": f"event_{secrets.token_hex(16)}",
                                "sequence": 0,
                                "category": exc.code,
                            })
                            continue
                        try:
                            agent_input = build_agent_input(
                                user_text,
                                committed_attachments,
                                owner_id=_conv,
                                permission_epoch=_current_policy_epoch(),
                                source_refs_out=created_source_refs,
                                image_artifacts_out=turn_image_artifacts,
                            )
                        except BaseException:
                            _release_unaccepted_source_refs(
                                created_source_refs, owner_id=_conv,
                            )
                            raise
                        display_names = ", ".join(
                            item.display_name for item in committed_attachments
                        )
                        persisted_user_content = (
                            f"📎 {display_names}\n{user_text}".strip()
                        )
                    else:
                        # Render from a copy first.  Ownership remains in
                        # ``pending_files`` until the turn metadata is ready,
                        # so a handler exception before spawn is still covered
                        # by connection teardown.
                        try:
                            agent_input = _consume_pending_files(
                                user_text,
                                list(pending_files),
                                display_names=pending_file_display_names,
                                owner_id=_conv,
                                permission_epoch=_current_policy_epoch(),
                                source_refs_out=created_source_refs,
                                image_artifacts_out=turn_image_artifacts,
                            )
                        except BaseException:
                            _release_unaccepted_source_refs(
                                created_source_refs, owner_id=_conv,
                            )
                            raise
                        persisted_user_content = user_text
                    turn_meta = None
                    try:
                        turn_meta = _new_turn_meta(
                            _conv,
                            client_request_id,
                            turn_directives=turn_directives,
                        )
                        turn_meta["_document_owner_device"] = mobile_device_id
                        turn_meta["_execution_origin_ref"] = (
                            authorization_origin_ref
                        )
                        turn_meta["client_locale"] = msg.get("locale")
                        turn_meta["_original_user_text"] = user_text
                        turn_meta["tts_enabled"] = (
                            msg.get("tts_enabled", True) is not False
                        )
                        turn_meta["_durable_command_id"] = str(
                            msg.get("_durable_command_id") or ""
                        )
                        turn_meta["_durable_command_epoch"] = str(
                            msg.get("_durable_command_epoch") or ""
                        )
                        turn_meta["_durable_command_owner"] = msg.get(
                            "_durable_command_owner"
                        )
                        _apply_turn_input_identity(turn_meta, input_identity)
                        if created_source_refs:
                            turn_meta["_preaccept_source_refs"] = list(
                                created_source_refs
                            )
                        if (
                            input_identity.get("attachment_manifest_hash")
                            and isinstance(agent_input, str)
                        ):
                            turn_meta["_durable_agent_input_v1"] = agent_input
                        if turn_directives is not None:
                            turn_meta["turn_directives"] = turn_directives
                            turn_meta["_turn_directives_v1"] = (
                                turn_directives.to_wire()
                            )
                        if committed_attachments:
                            turn_meta["persisted_user_content"] = (
                                persisted_user_content
                            )
                            attachment_metadata = [
                                item.persistence_metadata()
                                for item in committed_attachments
                            ]
                            image_metadata = iter(
                                artifact.safe_metadata()
                                for artifact in turn_image_artifacts
                            )
                            for metadata_item in attachment_metadata:
                                if metadata_item.get("category") == "image":
                                    metadata_item.update(next(image_metadata, {}))
                            turn_meta["attachment_metadata"] = attachment_metadata
                        elif turn_image_artifacts and isinstance(input_manifest, list):
                            attachment_metadata = [
                                dict(item) for item in input_manifest
                                if isinstance(item, dict)
                            ]
                            image_metadata = iter(
                                artifact.safe_metadata()
                                for artifact in turn_image_artifacts
                            )
                            for metadata_item in attachment_metadata:
                                if metadata_item.get("category") == "image":
                                    metadata_item.update(next(image_metadata, {}))
                            turn_meta["attachment_metadata"] = attachment_metadata
                    except BaseException:
                        if isinstance(turn_meta, dict):
                            _release_unaccepted_turn_sources(
                                turn_meta, owner_id=_conv,
                            )
                        else:
                            _release_unaccepted_source_refs(
                                created_source_refs, owner_id=_conv,
                            )
                        raise
                    legacy_turn_files = (
                        list(pending_files) if not attachment_batch_id else []
                    )
                    try:
                        accepted_for_spawn = (
                            await _accept_user_turn_before_spawn(
                                _conv,
                                persisted_user_content,
                                turn_meta,
                            )
                        )
                    except BaseException:
                        _release_unaccepted_turn_sources(
                            turn_meta, owner_id=_conv,
                        )
                        raise
                    if not accepted_for_spawn:
                        legacy_uploads.cleanup(legacy_turn_files)
                        for path in legacy_turn_files:
                            try:
                                pending_files.remove(path)
                            except ValueError:
                                pass
                            pending_file_display_names.pop(path, None)
                        if attachment_batch_id:
                            attachment_batches.release(attachment_batch_id)
                        continue

                    async def _run_text_turn_with_attachment_cleanup(
                        snap,
                        _input=agent_input,
                        _c=_conv,
                        _m=turn_meta,
                        _batch_id=attachment_batch_id,
                        _legacy_paths=(
                            list(pending_files)
                            if not attachment_batch_id
                            else []
                        ),
                    ):
                        async def _operation():
                            try:
                                return await _run_agent_and_respond(
                                    out,
                                    _input,
                                    snap,
                                    _c,
                                    get_agent=_get_conn_agent,
                                    turn_meta=_m,
                                )
                            finally:
                                if _batch_id:
                                    attachment_batches.release(_batch_id)

                        return await run_with_legacy_cleanup(
                            legacy_uploads,
                            _legacy_paths,
                            _operation,
                        )

                    if legacy_turn_files:
                        pending_files.clear()
                        for path in legacy_turn_files:
                            pending_file_display_names.pop(path, None)

                    try:
                        spawned = _spawn_turn(
                            _conv,
                            _run_text_turn_with_attachment_cleanup,
                            turn_meta,
                        )
                    except BaseException:
                        legacy_uploads.cleanup(legacy_turn_files)
                        if attachment_batch_id:
                            attachment_batches.release(attachment_batch_id)
                        raise
                    if not spawned and attachment_batch_id:
                        attachment_batches.release(attachment_batch_id)
                    if not spawned:
                        legacy_uploads.cleanup(legacy_turn_files)

            # --- Audio message ---
            elif msg_type == "audio":
                b64_data = msg.get("data", "")
                audio_format = msg.get("format", "wav")
                if not b64_data:
                    continue
                client_request_id = _clean_client_request_id(
                    msg.get("request_id") or msg.get("client_request_id"),
                )
                if client_request_id is None:
                    if _application_transport_coordination_enabled():
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "missing_or_invalid_request_id",
                        })
                        continue
                    client_request_id = _legacy_request_id()
                audio_candidate_files = list(pending_files)
                audio_candidate_names = {
                    path: pending_file_display_names.get(path, "")
                    for path in audio_candidate_files
                }
                audio_manifest = (
                    _legacy_attachment_manifest(
                        audio_candidate_files, audio_candidate_names,
                    )
                    if audio_candidate_files else None
                )
                audio_identity = _turn_input_identity(
                    b64_data,
                    kind=(
                        "legacy-audio-attachments"
                        if audio_candidate_files else "audio"
                    ),
                    manifest=audio_manifest,
                )
                legacy_audio_hash = hashlib.sha256(
                    str(b64_data).encode("utf-8")
                ).hexdigest()
                active_legacy_audio_conv = str(
                    _ACTIVE_CONV_BY_REQUEST.get(client_request_id) or ""
                )
                active_legacy_audio_meta = _ACTIVE_TURN_META_BY_CONV.get(
                    active_legacy_audio_conv
                )
                active_legacy_audio_replay = bool(
                    not audio_candidate_files
                    and _legacy_audio_replay_record(
                        active_legacy_audio_meta,
                        legacy_audio_hash,
                        require_complete=False,
                    )
                )

                def _discard_replayed_audio_inputs() -> None:
                    legacy_uploads.cleanup(audio_candidate_files)
                    for path in audio_candidate_files:
                        try:
                            pending_files.remove(path)
                        except ValueError:
                            pass
                        pending_file_display_names.pop(path, None)

                if not active_legacy_audio_replay:
                    try:
                        if await _send_existing_turn_status_for_request(
                            out,
                            client_request_id,
                            expected_identity=audio_identity,
                        ):
                            _discard_replayed_audio_inputs()
                            continue
                    except ValueError:
                        _discard_replayed_audio_inputs()
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                        })
                        continue
                # Snapshot + clear pending files at spawn time so they attach
                # to THIS turn regardless of uploads that arrive during STT.
                # Resolve the conversation synchronously (default title; the
                # transcript upgrades it on first persist) so concurrent first
                # messages serialise into ONE conversation.
                # TRADE-OFF (accepted): a silent/blank first recording now
                # leaves an empty conversation, since the id is reserved before
                # STT runs. This is the cost of having a real id at spawn time
                # (which fixes the duplicate-conversation race); the stray conv
                # is harmless and reused by the next real message.
                _conv = await _resolve_turn_conversation(
                    msg, None, client_request_id,
                )
                if _conv is not None:
                    if (
                        active_legacy_audio_replay
                        and active_legacy_audio_conv != _conv
                    ):
                        _discard_replayed_audio_inputs()
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                            "conversation_id": _conv,
                        })
                        continue
                    try:
                        persisted_audio_record = (
                            conversations.find_correlated_turn(
                                _conv, client_request_id,
                            )
                        )
                    except ValueError:
                        persisted_audio_record = None
                        persisted_audio_conflict = True
                    else:
                        persisted_audio_conflict = False
                    if persisted_audio_conflict:
                        _discard_replayed_audio_inputs()
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                            "conversation_id": _conv,
                        })
                        continue
                    legacy_audio_shape = bool(
                        not audio_candidate_files
                        and _legacy_audio_replay_record(
                            persisted_audio_record,
                            legacy_audio_hash,
                            require_complete=False,
                        )
                    )
                    legacy_audio_complete = bool(
                        legacy_audio_shape
                        and _legacy_audio_replay_record(
                            persisted_audio_record,
                            legacy_audio_hash,
                            require_complete=True,
                        )
                    )
                    if not legacy_audio_shape:
                        try:
                            if await _send_persisted_turn_for_request(
                                out,
                                _conv,
                                client_request_id,
                                expected_identity=audio_identity,
                            ):
                                _discard_replayed_audio_inputs()
                                continue
                        except ValueError:
                            _discard_replayed_audio_inputs()
                            await out.send_json({
                                "type": "status",
                                "content": "error",
                                "error": "request_id_conflict",
                                "conversation_id": _conv,
                            })
                            continue
                    if _is_conversation_busy(_conv):
                        if not (
                            legacy_audio_shape or active_legacy_audio_replay
                        ):
                            try:
                                if await _send_existing_turn_status(
                                    out,
                                    _conv,
                                    client_request_id,
                                    expected_identity=audio_identity,
                                ):
                                    _discard_replayed_audio_inputs()
                                    continue
                            except ValueError:
                                _discard_replayed_audio_inputs()
                                await out.send_json({
                                    "type": "status",
                                    "content": "error",
                                    "error": "request_id_conflict",
                                    "conversation_id": _conv,
                                })
                                continue
                    if legacy_audio_shape and not legacy_audio_complete:
                        _discard_replayed_audio_inputs()
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                            "conversation_id": _conv,
                        })
                        continue
                    _pf = list(audio_candidate_files)
                    _pf_names = dict(audio_candidate_names)
                    turn_meta = _new_turn_meta(_conv, client_request_id)
                    turn_meta["_document_owner_device"] = mobile_device_id
                    turn_meta["_execution_origin_ref"] = (
                        authorization_origin_ref
                    )
                    turn_meta["client_locale"] = msg.get("locale")
                    turn_meta["tts_enabled"] = (
                        msg.get("tts_enabled", True) is not False
                    )
                    turn_meta["_durable_command_id"] = str(
                        msg.get("_durable_command_id") or ""
                    )
                    turn_meta["_durable_command_epoch"] = str(
                        msg.get("_durable_command_epoch") or ""
                    )
                    turn_meta["_durable_command_owner"] = msg.get(
                        "_durable_command_owner"
                    )
                    _apply_turn_input_identity(turn_meta, audio_identity)
                    if isinstance(audio_manifest, list):
                        turn_meta["_attachment_input_manifest_v1"] = [
                            dict(item) for item in audio_manifest
                            if isinstance(item, dict)
                        ]
                    if legacy_audio_complete:
                        turn_meta["_legacy_audio_replay_v1"] = {
                            "legacy_audio_hash": legacy_audio_hash,
                        }

                    async def _run_legacy_audio_turn(
                        snap,
                        _b=b64_data,
                        _f=audio_format,
                        _c=_conv,
                        _files=_pf,
                        _file_names=_pf_names,
                        _m=turn_meta,
                        _epoch=_current_policy_epoch(),
                    ):
                        async def _operation():
                            return await _handle_audio(
                                out,
                                _b,
                                _f,
                                snap,
                                _c,
                                pending_files=_files,
                                pending_file_display_names=_file_names,
                                get_agent=_get_conn_agent,
                                turn_meta=_m,
                                permission_epoch=_epoch,
                            )

                        return await run_with_legacy_cleanup(
                            legacy_uploads,
                            list(_files),
                            _operation,
                        )

                    pending_files.clear()
                    for path in _pf:
                        pending_file_display_names.pop(path, None)
                    try:
                        spawned = _spawn_turn(
                            _conv,
                            _run_legacy_audio_turn,
                            turn_meta,
                        )
                    except BaseException:
                        legacy_uploads.cleanup(_pf)
                        raise
                    if not spawned:
                        legacy_uploads.cleanup(_pf)

            else:
                await out.send_json({"type": "status", "content": "error"})

    except WebSocketDisconnect:
        print("[WS] closing connection, reason: client disconnected (WebSocketDisconnect)")
        print("[OSIA Server] Client disconnected. Resetting session state.")
    except Exception as e:
        print(f"[WS] closing connection, reason: unexpected exception: {e!r}")
        print(f"[OSIA Server] Unexpected error: {e}")
        traceback.print_exc()
        # Terminal teardown notice. Enqueue on the writer (NOT a direct ws
        # send) so it never races an in-flight streaming write on the same
        # socket; the finally block drains the writer, flushing this frame.
        try:
            await out.send_json({"type": "status", "content": "error"})
        except Exception:
            pass
    finally:
        for owner in command_owners:
            settlement = asyncio.create_task(_settle_websocket_command_owner(owner))
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                await settlement
            except Exception:
                # The bounded diagnostic is emitted by the settlement helper.
                # Still close uploads/subscriptions/writer on storage failure.
                pass
        command_owners.clear()
        legacy_uploads.cleanup(pending_files)
        attachment_batches.cleanup_disconnected()
        attachment_cleanup_task.cancel()
        try:
            await attachment_cleanup_task
        except (asyncio.CancelledError, Exception):
            pass
        # Drop this client from the broadcast registry (WOR-10). discard() is
        # a no-op if it was never added (e.g. auth failed).
        _WS_CLIENTS.discard(out)
        for unsubscribe in (
            unsubscribe_api_key_changes,
            unsubscribe_setting_changes,
            unsubscribe_memory_changes,
            unsubscribe_account_changes,
        ):
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception:
                    pass
        if chatgpt_login_started_here:
            try:
                from osia.mobile_model_catalogue import cancel_device_login

                await asyncio.to_thread(cancel_device_login)
            except Exception:
                pass
        if unsubscribe_scheduled_tasks is not None:
            try:
                unsubscribe_scheduled_tasks()
            except Exception:
                pass
        # WOR-62: DETACH any in-flight streaming turn for this connection
        # instead of cancelling it. Cancelling raised CancelledError inside the
        # turn (at the ``await asyncio.to_thread(...)`` agent run) BEFORE it
        # reached its single canonical assistant persist point, so a phone
        # that slept mid-generation lost BOTH the reply and the user message —
        # the conversation was left empty on disk. The agent's worker thread is
        # uncancellable and runs ``agent.run`` to completion EITHER way (the
        # compute is identical); detaching simply stops throwing the result
        # away. The turn finishes on the shared server event loop and persists
        # via its normal path. Its remaining frames target the now-dead socket,
        # but every send is enqueue-only on the _OutboundChannel and the writer
        # swallows the failed flush, so nothing raises before persistence.
        #
        # Move each task into the module-level set so a strong reference keeps
        # it alive (the loop only weakly references tasks; clearing
        # ``streaming_tasks`` below would otherwise make it GC-eligible
        # mid-run), and register a done-callback so it removes itself on
        # completion — the set stays bounded. Do NOT await them here: that would
        # block this receive-loop teardown until the agent finishes. They run
        # independently and persist on their own.
        for _cid, _task in list(streaming_tasks.items()):
            # Text generation stays detached so it can persist, but media has no
            # live recipient on this channel. Invalidate its per-turn worker now;
            # late provider bytes can no longer reach a later connection.
            _media_task = _POST_TURN_MEDIA_BY_CONV.get(_cid)
            _media_meta = _POST_TURN_MEDIA_META_BY_CONV.get(_cid)
            _release_task = (
                _media_meta.get("_post_turn_media_release_task")
                if isinstance(_media_meta, dict) else None
            )
            _release_inflight = bool(
                isinstance(_release_task, asyncio.Task)
                and _media_task is _release_task
                and not _release_task.done()
            )
            if not _release_inflight:
                if _POST_TURN_MEDIA_BY_CONV.get(_cid) is _media_task:
                    _POST_TURN_MEDIA_BY_CONV.pop(_cid, None)
                    _POST_TURN_MEDIA_META_BY_CONV.pop(_cid, None)
                if isinstance(_media_meta, dict):
                    _media_meta["_response_media_cancelled"] = True
                if _media_task is not None:
                    _media_task.cancel()
                if (
                    isinstance(_media_meta, dict)
                    and _media_meta.get(
                        "_post_turn_media_lease_committed"
                    ) is True
                ):
                    _schedule_response_media_lease_release(_media_meta)
            _DETACHED_TURNS.add(_task)
            # Also index by conversation id (WOR-62 follow-up) so a reconnecting
            # client's load_conversation can await THIS turn before reading disk.
            _DETACHED_BY_CONV[_cid] = _task

            def _detached_done(t, _cid=_cid):
                _DETACHED_TURNS.discard(t)
                # Clear the index only if it still points at THIS task: a later
                # reconnect may have detached a NEWER turn for the same
                # conversation — never drop that newer entry.
                if _DETACHED_BY_CONV.get(_cid) is t:
                    _DETACHED_BY_CONV.pop(_cid, None)

            _task.add_done_callback(_detached_done)
        streaming_tasks.clear()
        # Dispose this connection's reused agents (perf fix #3): close each
        # idle agent's OpenAI/httpx client so its sockets + SSL context are
        # released promptly, then drop the cache. Runs AFTER the streaming
        # turns above are cancelled/awaited. Agents still flagged ``_in_use``
        # (a cancelled turn whose uncancellable worker thread may still be
        # running) are SKIPPED here and reclaimed by GC once that thread ends —
        # closing a client mid-use would break it.
        for _ag in list(conn_agents.values()):
            if not getattr(_ag, "_in_use", False):
                _close_agent_client(_ag)
        conn_agents.clear()
        # Stop this client's live heartbeat push (no-op if never started).
        # Cancel then await so the task unwinds cleanly without a pending-task
        # warning; CancelledError on the await is expected.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        # Stop the single writer last: drain whatever is still queued, then
        # exit. On a dead socket the writer simply errors out and returns.
        try:
            await out.close()
        except Exception:
            pass
        try:
            await writer_task
        except (asyncio.CancelledError, Exception):
            pass
        # State is local to this function; on return it is garbage-collected.
        session_history = []
        active_conv_id = None


# ---------------------------------------------------------------------------
# Latency profiling helper
# ---------------------------------------------------------------------------
def _ts(t0: float, label: str, extra: str = "") -> None:
    """Log a single latency milestone relative to ``t0`` (perf_counter
    seconds). Lines are prefixed with ``[OSIA Latency]`` so they're
    easy to grep. Safe to call from a worker thread (``print`` is
    threadsafe in CPython).
    """
    elapsed_ms = (time.perf_counter() - t0) * 1000
    tail = f"  {extra}" if extra else ""
    print(f"[OSIA Latency] +{elapsed_ms:6.0f}ms  {label}{tail}", flush=True)


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------
def _displayable_history(history: list) -> list[dict]:
    """Project the stored OpenAI-format history into the shape the mobile
    client renders: only user / assistant turns with real text. Drops
    tool-call envelopes, tool-result echoes, empty assistant frames
    that exist only to carry ``tool_calls``, and any message that looks
    like leaked system-prompt content.
    """
    out: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        # WOR-83: belt-and-suspenders — never render an internal model-steering
        # frame as a chat bubble. The primary guard is exported_history() (these
        # are filtered before persistence, so the stored history fed here never
        # carries the marker), but drop it here too in case one ever reaches a
        # frame straight from in-memory history.
        if msg.get("_internal"):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        raw = msg.get("content")
        if isinstance(raw, list):
            # OpenAI vision-style content parts: pick text fragments.
            text = "".join(
                str(p.get("text", "")) for p in raw if isinstance(p, dict)
            )
        elif raw is None:
            text = ""
        else:
            text = str(raw)
        text = text.strip()
        if not text:
            continue
        if role == "user" and text.lstrip().startswith("<tool_result>"):
            continue
        # Strip raw <tool_call>…</tool_call> blocks (tags + JSON payload)
        # from assistant turns — intermediate tool-call rounds live in the
        # stored history but must never render on mobile.
        if role == "assistant":
            from osia.agent import strip_raw_tool_tags
            from osia.tool_attempts import is_internal_tool_control_text

            if is_internal_tool_control_text(text):
                continue
            text = strip_raw_tool_tags(text).strip()
            if not text:
                continue
        # Defence-in-depth: drop anything that looks like the system
        # prompt leaked into the conversation history.
        if "RÈGLES ABSOLUES" in text or "ABSOLUTE RULES" in text:
            continue
        display = {"role": role, "content": text}
        request_id = str(
            msg.get("_request_id")
            or msg.get("_client_request_id")
            or ""
        )
        turn_id = str(msg.get("_turn_id") or "")
        message_id = str(msg.get("_message_id") or "")
        if request_id and turn_id and message_id:
            display.update({
                "conversation_id": str(msg.get("_conversation_id") or ""),
                "request_id": request_id,
                "client_request_id": request_id,
                "turn_id": turn_id,
                "message_id": message_id,
            })
            route = {
                "run_id": str(msg.get("_run_id") or turn_id),
                "provider_id": str(msg.get("_provider_id") or ""),
                "model_id": str(msg.get("_model_id") or ""),
                "effective_reasoning": str(
                    msg.get("_effective_reasoning") or ""
                ),
            }
            if all(route.values()):
                display.update(route)
        if role == "assistant":
            from osia.execution_trace import validate_persisted_trace

            safe_trace = validate_persisted_trace(
                msg.get("_execution_trace_v1")
            )
            if safe_trace is not None:
                display["execution_trace"] = safe_trace
        out.append(display)
    return out


def _join_sent_chunks(chunks: list[str]) -> str:
    """Rebuild the text Mobile sees from stripped sentence chunks."""

    return " ".join(chunks)


class _LiveAssistantChunkGuard:
    """Defence-in-depth boundary for provisional assistant stream chunks."""

    def __init__(self, turn_context: TurnContext) -> None:
        self.turn_context = turn_context
        self._accepted: list[str] = []

    def accept(self, value) -> str:
        from osia.agent import clean_response, strip_raw_tool_tags
        from osia.tool_attempts import is_internal_tool_control_text

        if not isinstance(value, ValidatedLiveAssistantChunk):
            return ""
        raw = str(value or "").strip()
        if not raw or is_internal_tool_control_text(raw):
            return ""
        cleaned = clean_response(strip_raw_tool_tags(raw)).strip()
        if not cleaned or is_internal_tool_control_text(cleaned):
            return ""
        self._accepted.append(cleaned)
        return cleaned


def _tail_not_sent(full: str, sent: str) -> str:
    """Return the portion of ``full`` that hasn't been delivered yet.

    ``sent`` is the concatenation of every response_chunk we've already
    pushed to the client. Normalised whitespace comparison because chunk
    boundaries and the cleaned final text may differ in spacing.

    If the full response is already fully covered by what was sent we
    return ``""``. Otherwise we return the suffix of ``full`` starting
    from the first word that wasn't streamed.
    """
    if not full:
        return ""
    norm_sent = " ".join(sent.split()) if sent else ""
    norm_full = " ".join(full.split())
    if not norm_full or norm_full in norm_sent:
        return ""
    full_words = full.split()
    # Find the longest prefix of `full` that already appears in `sent`.
    matched = 0
    for i in range(1, len(full_words) + 1):
        prefix = " ".join(full_words[:i])
        if prefix in norm_sent:
            matched = i
        else:
            # Once a prefix stops matching, the rest is the tail. Walking
            # further would only shrink the candidate.
            break
    tail = " ".join(full_words[matched:]).strip()
    return tail


def _consume_pending_files(
    user_text: str,
    pending_files: list[str],
    *,
    display_names: dict[str, str] | None = None,
    owner_id: str | None = None,
    permission_epoch: int | None = None,
    source_refs_out: list[str] | None = None,
    image_artifacts_out: list | None = None,
) -> str | list[dict]:
    """Snapshot legacy images directly; keep opaque sources for documents."""
    if not pending_files:
        return user_text
    paths = list(pending_files)
    pending_files.clear()
    from osia.document_sources import get_source_store

    capabilities = []
    image_artifacts = []
    for raw_path in paths:
        path = Path(raw_path)
        display_name = str(
            (display_names or {}).get(str(path)) or path.name
        )
        declared_image_mime = _image_mime_for(path)
        try:
            with path.open("rb") as handle:
                prefix = handle.read(16)
        except OSError as exc:
            raise AttachmentProtocolError("file_read_failed") from exc
        from osia.attachments import detect_image_mime

        actual_image_mime = detect_image_mime(prefix)
        if declared_image_mime or actual_image_mime:
            from osia.image_inputs import (
                ImageInputError,
                ImageSourceType,
                create_image_artifact,
            )

            try:
                data = path.read_bytes()
            except OSError as exc:
                raise AttachmentProtocolError("file_read_failed") from exc
            if declared_image_mime and actual_image_mime != declared_image_mime:
                raise AttachmentProtocolError("invalid_image")
            try:
                artifact = create_image_artifact(
                    data,
                    str(actual_image_mime or declared_image_mime),
                    ImageSourceType.USER_UPLOAD,
                    maximum_bytes=MAX_ATTACHMENT_BYTES,
                )
            except ImageInputError as exc:
                raise AttachmentProtocolError("invalid_image") from exc
            image_artifacts.append(artifact)
            if image_artifacts_out is not None:
                image_artifacts_out.append(artifact)
            continue
        if owner_id:
            capability = get_source_store().create(
                path,
                owner_id=owner_id,
                source_kind="legacy_mobile_attachment",
                display_name=display_name,
                permission_epoch=permission_epoch,
            )
        else:
            capability = get_source_store().create_for_current_owner(
                path,
                source_kind="legacy_mobile_attachment",
                display_name=display_name,
                permission_epoch=permission_epoch,
            )
        capabilities.append(capability)
        if source_refs_out is not None:
            source_refs_out.append(str(capability["source_ref"]))
    bullets = "\n".join(
        f"- source_ref={item['source_ref']} type={item['type_label']} state={item['state']}"
        + (f" category={item['category']}" if item.get("category") else "")
        for item in capabilities
    )
    text = str(user_text or "").strip()
    if capabilities:
        text = (
            "[Non-image document attachments available through read_document "
            f"and query_document:\n{bullets}\n]\n\n{text}"
        ).strip()
    if not image_artifacts:
        return text
    if not text:
        text = "Analyze the attached image or images."
    from osia.image_inputs import content_with_images

    return content_with_images(
        text,
        image_artifacts,
        owner_id=str(owner_id or ""),
    )


def _release_unaccepted_source_refs(
    source_refs: list[str] | tuple[str, ...],
    *,
    owner_id: str,
) -> None:
    """Drop only pre-accept document copies owned by the losing turn."""

    refs = [str(value or "") for value in source_refs if str(value or "")]
    if not refs or not owner_id:
        return
    store = document_sources.get_source_store()
    try:
        pending_refs = list(store.release_owned_confirmed(
            refs, owner_id=owner_id,
        ))
    except document_sources.DocumentSourceError as exc:
        print(
            "[OSIA Server] unaccepted document source cleanup failed "
            f"category={exc.category}"
        )
        return
    except Exception as exc:
        pending_refs = list(refs)
        print(
            "[OSIA Server] unaccepted document source cleanup deferred "
            f"category={type(exc).__name__}"
        )
    if not pending_refs:
        return

    async def _retry_cleanup() -> None:
        remaining = list(pending_refs)
        delay = 0.25
        while remaining:
            await asyncio.sleep(delay)
            try:
                remaining = list(await asyncio.to_thread(
                    store.release_owned_confirmed,
                    remaining,
                    owner_id=owner_id,
                ))
            except asyncio.CancelledError:
                raise
            except document_sources.DocumentSourceError as exc:
                print(
                    "[OSIA Server] unaccepted document source cleanup stopped "
                    f"category={exc.category}"
                )
                return
            except Exception:
                pass
            delay = min(30.0, delay * 2.0)

    try:
        cleanup_loop = asyncio.get_running_loop()
    except RuntimeError:
        print(
            "[OSIA Server] unaccepted document source cleanup remains deferred"
        )
        return
    cleanup_task = cleanup_loop.create_task(_retry_cleanup())
    _PENDING_SOURCE_CLEANUP_TASKS.add(cleanup_task)
    cleanup_task.add_done_callback(_PENDING_SOURCE_CLEANUP_TASKS.discard)


def _release_unaccepted_turn_sources(
    turn_meta: dict | None,
    *,
    owner_id: str,
) -> None:
    if not isinstance(turn_meta, dict):
        return
    refs = turn_meta.pop("_preaccept_source_refs", [])
    if isinstance(refs, list):
        _release_unaccepted_source_refs(refs, owner_id=owner_id)


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
def _ensure_response_media_lease(turn_meta: dict | None) -> None:
    """Bind this process as the exact owner of the response-media phase."""

    if not isinstance(turn_meta, dict):
        return
    assistant_message_id = str(
        turn_meta.get("assistant_message_id") or ""
    ).strip()
    if not assistant_message_id:
        return
    from osia.turn_terminality import live_process_identity

    _owner_alive, owner_process_identity = live_process_identity(os.getpid())
    turn_meta.setdefault(
        "_post_turn_media_lease_id", f"media_{assistant_message_id}",
    )
    turn_meta["_post_turn_media_owner_pid"] = os.getpid()
    turn_meta["_post_turn_media_owner_process_identity"] = (
        owner_process_identity
    )


def _schedule_response_media_lease_release(
    turn_meta: dict | None,
) -> "asyncio.Task | None":
    """Retry the exact durable media-lease CAS until it is confirmed clear."""

    if not isinstance(turn_meta, dict):
        return None
    if turn_meta.get("_post_turn_media_lease_released") is True:
        return None
    if turn_meta.get("_post_turn_media_lease_committed") is not True:
        turn_meta["_post_turn_media_release_pending"] = True
        return None
    conv_id = str(turn_meta.get("conversation_id") or "")
    lease_id = str(turn_meta.get("_post_turn_media_lease_id") or "")
    if not conv_id or not lease_id:
        return None
    existing = turn_meta.get("_post_turn_media_release_task")
    if isinstance(existing, asyncio.Task) and not existing.done():
        return existing

    async def _retry_exact_release() -> None:
        delay = 0.25
        logged_category = ""
        while True:
            try:
                release_result = await asyncio.to_thread(
                    conversations.release_post_turn_media_cas,
                    conv_id,
                    lease_id,
                )
                if release_result in {
                    "released", "confirmed_absent_or_different",
                }:
                    turn_meta["_post_turn_media_lease_released"] = True
                    turn_meta.pop("_post_turn_media_release_pending", None)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category = type(exc).__name__
                if category != logged_category:
                    logged_category = category
                    print(
                        "[OSIA Server] response media lease release deferred "
                        f"category={category}"
                    )
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2.0)

    release_task = asyncio.create_task(_retry_exact_release())
    turn_meta["_post_turn_media_release_task"] = release_task
    _POST_TURN_MEDIA_TASKS.add(release_task)
    if _application_transport_coordination_enabled():
        _POST_TURN_MEDIA_BY_CONV[conv_id] = release_task
        _POST_TURN_MEDIA_META_BY_CONV[conv_id] = turn_meta

    def _release_done(done_task: "asyncio.Task") -> None:
        _POST_TURN_MEDIA_TASKS.discard(done_task)
        if turn_meta.get("_post_turn_media_release_task") is done_task:
            turn_meta.pop("_post_turn_media_release_task", None)
        if _POST_TURN_MEDIA_BY_CONV.get(conv_id) is done_task:
            _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
            _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
            if not _is_conversation_busy(conv_id):
                asyncio.create_task(
                    _broadcast_conversation_busy_changed(
                        conv_id, False, turn_meta,
                    )
                )

    release_task.add_done_callback(_release_done)
    return release_task


async def _release_response_media_lease(turn_meta: dict | None) -> None:
    release_task = _schedule_response_media_lease_release(turn_meta)
    if release_task is not None:
        await asyncio.shield(release_task)


async def _send_response_media_terminal(
    ws: "_OutboundChannel",
    turn_meta: dict,
    status: str,
) -> None:
    """Close one correlated media phase without changing the text lifecycle."""

    if turn_meta.get("_response_media_terminal_emitted") is True:
        await _release_response_media_lease(turn_meta)
        return
    if turn_meta.get("_response_media_terminal_claimed") is True:
        return
    turn_meta["_response_media_terminal_claimed"] = True
    try:
        await ws.send_json(
            _turn_event_frame(
                turn_meta,
                "media_status",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={
                    "phase": "media",
                    "status": status,
                    "media_terminal": True,
                },
            )
        )
    finally:
        turn_meta["_response_media_terminal_emitted"] = True
        await _release_response_media_lease(turn_meta)


class _TTSSettingsChanged(RuntimeError):
    """Raised inside the worker when a turn's provider/voice was changed."""


def _tts_settings_signature() -> tuple[str, str]:
    from osia.config import get_setting

    return (
        str(get_setting("tts_provider", "local") or "local"),
        str(get_setting("tts_voice") or ""),
    )


class _ResponseTTSSession:
    """One bounded, ordered progressive-TTS pipeline for one assistant turn."""

    def __init__(
        self,
        ws: "_OutboundChannel",
        turn_meta: dict,
        t0: float,
        *,
        synthesize=None,
        language: str | None = None,
        max_pending: int = _TTS_MAX_PENDING_SEGMENTS,
    ) -> None:
        self.ws = ws
        self.turn_meta = turn_meta
        self.t0 = t0
        self._uses_default_synthesize = synthesize is None
        self._synthesize = synthesize or synthesize_to_mp3_bytes
        synthesis_target = getattr(self._synthesize, "side_effect", None)
        if not callable(synthesis_target):
            synthesis_target = self._synthesize
        try:
            import inspect

            parameters = inspect.signature(synthesis_target).parameters.values()
            self._synthesize_accepts_language = any(
                parameter.name == "language"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except Exception:
            self._synthesize_accepts_language = False
        self.language = (
            normalize_locale(language)
            or normalize_locale(turn_meta.get("language"))
            or i18n.DEFAULT_LANG
        )
        self._max_pending = max(1, int(max_pending))
        self._pending: deque[tuple[int, int, str]] = deque()
        self._wake = asyncio.Event()
        self._text_final_sent = asyncio.Event()
        self._input_finished = False
        self._cancelled = False
        self._settings_invalidated = False
        self._settings_signature: tuple[str, str] | None = None
        self._next_sequence = 0
        self._task: asyncio.Task | None = None
        self._first_synthesis_logged = False
        self.queued: list[tuple[int, str]] = []
        self.synthesized: list[tuple[int, int, str]] = []
        self.delivered: list[tuple[int, int, str]] = []

    @property
    def task(self) -> "asyncio.Task | None":
        return self._task

    def start(self) -> "asyncio.Task":
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return self._task

    def enqueue(self, text: str) -> int | None:
        """Queue one speakable fragment without blocking streamed text."""

        cleaned = str(text or "").strip()
        if not cleaned or self._cancelled or self._input_finished:
            return None
        self._next_sequence += 1
        sequence = self._next_sequence
        self.queued.append((sequence, cleaned))
        if len(self._pending) >= self._max_pending:
            start, _end, previous = self._pending.pop()
            self._pending.append((start, sequence, f"{previous} {cleaned}"))
        else:
            self._pending.append((sequence, sequence, cleaned))
        self._wake.set()
        return sequence

    def finish_input(self, canonical_text: str) -> None:
        """Freeze input and append only the canonical tail not already queued."""

        if self._cancelled or self._input_finished:
            return
        queued_text = _join_sent_chunks([text for _seq, text in self.queued])
        tail = _tail_not_sent(str(canonical_text or "").strip(), queued_text)
        if tail:
            self.enqueue(tail)
        self._input_finished = True
        self._wake.set()

    def mark_text_final_sent(self) -> None:
        self._text_final_sent.set()
        self._wake.set()

    def cancel(self) -> None:
        """Invalidate pending and in-flight media; late synthesis is discarded."""

        self._cancelled = True
        self._pending.clear()
        self._wake.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def _synthesize_with_stable_settings(self, text: str) -> bytes:
        """Run off-loop and reject audio if provider/voice changed mid-turn."""

        current = _tts_settings_signature()
        if self._settings_signature is None:
            self._settings_signature = current
        elif current != self._settings_signature:
            raise _TTSSettingsChanged("TTS provider or voice changed")
        try:
            if self._uses_default_synthesize and self._synthesize_accepts_language:
                return self._synthesize(text, language=self.language)
            return self._synthesize(text)
        finally:
            if _tts_settings_signature() != self._settings_signature:
                raise _TTSSettingsChanged("TTS provider or voice changed")

    @staticmethod
    def _merge_segments(
        first: tuple[int, int, str],
        second: tuple[int, int, str],
    ) -> tuple[int, int, str]:
        return first[0], second[1], f"{first[2]} {second[2]}"

    async def _synthesize_segment(
        self,
        segment: tuple[int, int, str],
    ) -> bytes | None:
        if not self._first_synthesis_logged:
            self._first_synthesis_logged = True
            _ts(
                self.t0,
                "first TTS request started",
                f"(sequence={segment[0]}, {len(segment[2])} chars)",
            )
        try:
            mp3_bytes = await asyncio.to_thread(
                self._synthesize_with_stable_settings,
                segment[2],
            )
            if not mp3_bytes:
                raise RuntimeError("TTS produced no audio bytes")
            if not self.synthesized:
                _ts(
                    self.t0,
                    "first audio bytes ready",
                    f"({len(mp3_bytes)} bytes)",
                )
            self.synthesized.append(segment)
            return mp3_bytes
        except asyncio.CancelledError:
            raise
        except _TTSSettingsChanged as exc:
            self._settings_invalidated = True
            print(f"[OSIA Server] progressive TTS invalidated: {exc}")
            return None
        except Exception as exc:
            print(
                f"[OSIA Server] TTS segment {segment[0]}-{segment[1]} failed: {exc}"
            )
            return None

    async def _send_audio(
        self,
        segment: tuple[int, int, str],
        mp3_bytes: bytes,
        *,
        terminal: bool,
    ) -> bool:
        if self._cancelled:
            return False
        try:
            frame = _turn_event_frame(
                self.turn_meta,
                "audio",
                message_id=str(
                    self.turn_meta.get("assistant_message_id") or ""
                ),
                payload={
                    "format": "mp3",
                    "data": base64.b64encode(mp3_bytes).decode("ascii"),
                    "source": "response",
                    "phase": "media",
                    "media_terminal": terminal,
                },
            )
            await self.ws.send_json(frame)
        except Exception as exc:
            print(f"[OSIA Server] response audio send failed: {exc}")
            self._cancelled = True
            return False
        self.delivered.append(segment)
        if len(self.delivered) == 1:
            _ts(
                self.t0,
                "first response audio queued",
                f"({len(mp3_bytes)} bytes)",
            )
        if terminal:
            self.turn_meta["_response_media_terminal_emitted"] = True
            await _release_response_media_lease(self.turn_meta)
        return True

    async def _run(self) -> None:
        carry: tuple[int, int, str] | None = None
        final_retry_attempted = False
        try:
            while not self._cancelled:
                segment = None
                if self._pending:
                    segment = self._pending.popleft()
                    if carry is not None:
                        segment = self._merge_segments(carry, segment)
                        carry = None
                elif self._input_finished and carry is not None:
                    if final_retry_attempted:
                        await self._text_final_sent.wait()
                        if not self._cancelled:
                            await _send_response_media_terminal(
                                self.ws, self.turn_meta, "error"
                            )
                        return
                    final_retry_attempted = True
                    segment = carry
                    carry = None
                elif self._input_finished:
                    await self._text_final_sent.wait()
                    if not self._cancelled:
                        await _send_response_media_terminal(
                            self.ws, self.turn_meta, "done"
                        )
                    return
                else:
                    self._wake.clear()
                    if self._pending or self._input_finished:
                        continue
                    await self._wake.wait()
                    continue

                mp3_bytes = await self._synthesize_segment(segment)
                if mp3_bytes is None:
                    if self._settings_invalidated:
                        self._pending.clear()
                        await self._text_final_sent.wait()
                        if not self._cancelled:
                            await _send_response_media_terminal(
                                self.ws, self.turn_meta, "error"
                            )
                        return
                    carry = segment
                    continue
                if self._cancelled:
                    return
                terminal = (
                    self._input_finished
                    and not self._pending
                    and carry is None
                    and self._text_final_sent.is_set()
                )
                if not await self._send_audio(segment, mp3_bytes, terminal=terminal):
                    return
                if terminal:
                    return
        except asyncio.CancelledError:
            self._cancelled = True
            self._pending.clear()
            raise
        except Exception as exc:
            self._cancelled = True
            self._pending.clear()
            print(
                "[OSIA Server] response media worker failed "
                f"category={type(exc).__name__}"
            )


def _track_response_audio_task(
    task: "asyncio.Task",
    turn_meta: dict,
    ws: "_OutboundChannel | None" = None,
) -> bool:
    """Track one media worker and keep per-conversation completion ordered."""

    conv_id = str(turn_meta.get("conversation_id") or "")
    if not conv_id:
        task.cancel()
        return False
    _POST_TURN_MEDIA_TASKS.add(task)
    coordinated = _application_transport_coordination_enabled()
    if coordinated and _POST_TURN_MEDIA_BY_CONV.get(conv_id) is not None:
        task.cancel()
        _POST_TURN_MEDIA_TASKS.discard(task)
        return False
    if coordinated:
        _POST_TURN_MEDIA_BY_CONV[conv_id] = task
        _POST_TURN_MEDIA_META_BY_CONV[conv_id] = turn_meta

    def _media_done(done_task: "asyncio.Task") -> None:
        _POST_TURN_MEDIA_TASKS.discard(done_task)
        try:
            if not done_task.cancelled():
                done_task.exception()
        except Exception:
            pass
        if coordinated and _POST_TURN_MEDIA_BY_CONV.get(conv_id) is done_task:
            _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
            _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
        if turn_meta.get("_response_media_terminal_emitted") is not True:
            turn_meta["_response_media_worker_finished_early"] = True
            if (
                turn_meta.get("_post_turn_media_lease_committed") is True
                and ws is not None
                and turn_meta.get("_response_media_terminal_claimed") is not True
            ):
                close_task = asyncio.create_task(
                    _send_response_media_terminal(ws, turn_meta, "error")
                )
                _POST_TURN_MEDIA_TASKS.add(close_task)
                if coordinated:
                    _POST_TURN_MEDIA_BY_CONV[conv_id] = close_task
                    _POST_TURN_MEDIA_META_BY_CONV[conv_id] = turn_meta

                def _close_done(completed: "asyncio.Task") -> None:
                    _POST_TURN_MEDIA_TASKS.discard(completed)
                    if _POST_TURN_MEDIA_BY_CONV.get(conv_id) is completed:
                        _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
                        _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)

                close_task.add_done_callback(_close_done)
        elif not _is_conversation_busy(conv_id):
            asyncio.create_task(
                _broadcast_conversation_busy_changed(conv_id, False, turn_meta)
            )

    task.add_done_callback(_media_done)
    return True


async def _complete_response_media_after_text(
    ws: "_OutboundChannel",
    turn_meta: dict,
    tts_session: "_ResponseTTSSession | None",
    status: str,
) -> None:
    """Hand off committed text only to a still-live exact media worker."""

    if (
        tts_session is not None
        and turn_meta.get("_response_media_cancelled") is not True
        and turn_meta.get("_response_media_worker_finished_early") is not True
        and tts_session.task is not None
        and not tts_session.task.done()
    ):
        tts_session.mark_text_final_sent()
        return
    media_status = (
        "error"
        if tts_session is not None
        and turn_meta.get("_response_media_worker_finished_early") is True
        else status
    )
    if tts_session is not None:
        tts_session.cancel()
    await _send_response_media_terminal(ws, turn_meta, media_status)


async def _finalize_admitted_turn_after_worker_failure(
    ws: "_OutboundChannel",
    conv_id: str,
    turn_meta: dict | None,
    history: list,
) -> tuple[list, str] | None:
    """Close an accepted turn if setup failed outside the agent-run guard.

    This path never invokes a provider or a tool.  It retains the current
    conversation task as the live owner until the exact failure final is
    reserved and persisted, so the durable canonical lease cannot be cleared
    by a task callback while the accepted user message is still unterminated.
    """

    if not isinstance(turn_meta, dict) or turn_meta.get("persisted") is not True:
        return None
    receipt = turn_meta.get("_user_commit")
    if not (
        turn_meta.get("_accepted_user_already_persisted") is True
        or (isinstance(receipt, dict) and receipt.get("committed") is True)
    ):
        return None
    request_id = str(
        turn_meta.get("request_id")
        or turn_meta.get("client_request_id")
        or ""
    )
    objective = str(
        turn_meta.get("_terminality_objective")
        or turn_meta.get("_original_user_text")
        or turn_meta.get("persisted_user_content")
        or ""
    ).strip()[:32_000]
    if not conv_id or not request_id or not objective:
        return None
    language = normalize_locale(turn_meta.get("language")) or i18n.DEFAULT_LANG
    response_frame = None
    terminal_frame = None
    final_text = ""
    delay = 0.25

    while True:
        try:
            found = conversations.find_correlated_turn(conv_id, request_id)
        except Exception:
            found = None
        if isinstance(found, dict) and found.get("assistant_content") is not None:
            media_task = _POST_TURN_MEDIA_BY_CONV.get(conv_id)
            turn_meta["_response_media_cancelled"] = True
            if media_task is not None and not media_task.done():
                media_task.cancel()
                try:
                    await media_task
                except (asyncio.CancelledError, Exception):
                    pass
            if _POST_TURN_MEDIA_BY_CONV.get(conv_id) is media_task:
                _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
                _POST_TURN_MEDIA_META_BY_CONV.pop(conv_id, None)
            try:
                await _send_response_media_terminal(
                    ws,
                    turn_meta,
                    str(found.get("terminal_status") or "error"),
                )
            except Exception:
                pass
            stored = conversations.get_conversation(conv_id)
            return (
                list(stored.get("messages") or [])
                if isinstance(stored, dict) else list(history),
                conv_id,
            )

        try:
            terminal = await asyncio.to_thread(
                _reserve_failed_server_turn_terminal,
                object(),
                turn_meta,
                objective=objective,
                language=language,
            )
        except Exception as reserve_error:
            print(
                "[OSIA Server] admitted-turn terminal recovery deferred "
                f"category={type(reserve_error).__name__}"
            )
            terminal = None
        if isinstance(terminal, dict):
            try:
                if response_frame is None or terminal_frame is None:
                    from osia.agent import strip_tool_artifacts
                    from osia.execution_trace import validate_persisted_trace

                    final_text = strip_tool_artifacts(
                        str(terminal.get("final_text") or "")
                    ).strip() or localized_text("provider_failure", language)
                    safe_trace = validate_persisted_trace(
                        terminal.get("execution_trace")
                    )
                    if safe_trace is not None:
                        turn_meta["_execution_trace_v1"] = safe_trace
                    response_frame = _turn_event_frame(
                        turn_meta,
                        "response",
                        message_id=str(
                            turn_meta.get("assistant_message_id") or ""
                        ),
                        payload={"content": final_text},
                    )
                    terminal_frame = _turn_terminal_frame(
                        turn_meta, terminal,
                    )
                committed = _persist_completed_assistant_message(
                    conv_id,
                    final_text,
                    turn_meta,
                    response_frame,
                    terminal_frame,
                )
            except asyncio.CancelledError:
                raise
            except Exception as materialize_error:
                print(
                    "[OSIA Server] admitted-turn final materialization "
                    "deferred category="
                    f"{type(materialize_error).__name__}"
                )
                response_frame = None
                terminal_frame = None
                committed = False
            if committed:
                turn_meta["persisted"] = True
                terminal_frame["persisted"] = True
                try:
                    await _broadcast_committed_receipt(
                        conv_id, turn_meta.get("_assistant_commit"),
                    )
                except Exception:
                    pass
                try:
                    await ws.send_json(response_frame)
                    await ws.send_json(terminal_frame)
                except Exception:
                    pass
                try:
                    await _send_response_media_terminal(
                        ws, turn_meta, "error",
                    )
                except Exception:
                    pass
                stored = conversations.get_conversation(conv_id)
                return (
                    list(stored.get("messages") or [])
                    if isinstance(stored, dict) else list(history),
                    conv_id,
                )
        await asyncio.sleep(delay)
        delay = min(30.0, delay * 2.0)


async def _run_agent_and_respond(ws: "_OutboundChannel", text, history: list,
                                 active_conv_id: str | None,
                                 *, t0: float | None = None,
                                 get_agent=None,
                                 turn_meta: dict | None = None) -> tuple[list, str]:
    """Run the agent with the given conversation history, persist the
    new turn, then send one canonical reply through display and TTS.

    Returns ``(updated_history, active_conv_id)``. If no conversation
    was active a new one is created (with a smart keyword title seeded
    from ``text``) so every interaction lives in a persisted file.

    ``ws`` here is the per-connection OUTBOUND CHANNEL (``_OutboundChannel``),
    NOT the raw WebSocket: it exposes ``send_json``/``send_text`` and funnels
    every frame through the single writer task. The agent run itself is
    unchanged — only how its output frames reach the socket. Every outgoing
    frame carries ``conversation_id`` (the captured persistence target) so the
    client can route a backgrounded stream to the right conversation even while
    the user is viewing another one.

    ``t0`` is a ``time.perf_counter()`` reference for latency logging.
    The audio path passes its own t0 (set when the audio frame arrived)
    so the logs measure end-to-end latency including STT; the text path
    omits it and the function creates its own.
    """
    # Import lazily so the module loads quickly, but capture TOOL_MAP
    # here too so the debug log proves it's reachable from this context.
    from osia.agent import OSIAAgent, TOOL_MAP

    _ensure_response_media_lease(turn_meta)

    if t0 is None:
        t0 = time.perf_counter()
        if isinstance(text, str):
            _ts(t0, "text received", f"({len(text)} chars)")
        else:
            _ts(t0, "vision content received", f"({len(text)} parts)")

    # Auto-create a conversation on the FIRST message of the session,
    # seeded with a smart keyword title so the sidebar reads naturally
    # right away. Also push a refreshed conversation list so any
    # subscribed UI updates immediately.
    #
    # DEFENSIVE FALLBACK ONLY: the live receive-loop flow resolves the
    # conversation via ``_ensure_active_conv`` BEFORE spawning the turn, so
    # ``active_conv_id`` is never None here in normal operation. This block is
    # kept so the function stays correct if ever called with no conversation
    # (it is the SOLE other creation path — they do not both run for one turn).
    if active_conv_id is None:
        seed_title = conversations.smart_title(text)
        conv = conversations.create_conversation(title=seed_title)
        active_conv_id = conv["id"]
        await _broadcast_canonical_conversation_change(
            active_conv_id,
            revision=int(conv.get("revision") or 0),
            operation_id=str(conv.get("last_operation_id") or ""),
        )
        await ws.send_json({
            "type": "active_conversation",
            "id": conv["id"],
            "title": conv["title"],
        })
        await ws.send_json(conversations.conversation_snapshot())

    # Resolve from the original user-authored turn before attachment manifests
    # or other generated English metadata can enter the provider input. Keep
    # persisted ``_language`` metadata visible for this one decision, then strip
    # all transport metadata before the provider sees history.
    original_user_text = (
        turn_meta.get("_original_user_text")
        if isinstance(turn_meta, dict) else None
    )
    if not isinstance(original_user_text, str):
        original_user_text = (
            turn_meta.get("persisted_user_content")
            if isinstance(turn_meta, dict) else None
        )
    if not isinstance(original_user_text, str):
        original_user_text = text
    turn_context = resolve_turn_context(
        original_user_text,
        history=history,
        client_locale=(turn_meta or {}).get("client_locale"),
        saved_language=i18n.get_language(),
        reference=(turn_meta or {}).get("turn_id"),
    )
    if isinstance(turn_meta, dict):
        turn_meta["language"] = turn_context.language
        turn_meta["language_source"] = turn_context.source
        turn_meta["language_mode"] = turn_context.mode
        turn_meta["allowed_languages"] = list(turn_context.allowed_languages)
        turn_meta["turn_ref"] = turn_context.turn_ref

    # Keep correlation/artifact metadata until the agent's single provider
    # boundary. It is required for conversation isolation, stable receipts and
    # restart-safe document continuity; _wire_messages removes it from payloads.
    history = [item for item in history if isinstance(item, dict)]
    prev_len = len(history)
    persisted_user_content = (
        turn_meta.get("persisted_user_content", text)
        if isinstance(turn_meta, dict) else text
    )
    if isinstance(turn_meta, dict):
        stable_semantic_objective = turn_meta.get("_terminality_objective")
        if not isinstance(stable_semantic_objective, str) or not (
            stable_semantic_objective.strip()
        ):
            stable_semantic_objective = next((
                candidate
                for candidate in (
                    turn_meta.get("_original_user_text"),
                    persisted_user_content,
                    text,
                )
                if isinstance(candidate, str) and candidate.strip()
            ), "")
        if stable_semantic_objective:
            turn_meta["_terminality_objective"] = (
                stable_semantic_objective[:32_000]
            )
        _bind_pre_agent_cancellation_authority(turn_meta)
    accepted_user_persisted = _persist_accepted_user_message(
        active_conv_id,
        persisted_user_content,
        turn_meta,
    )
    if isinstance(turn_meta, dict):
        turn_meta["persisted"] = accepted_user_persisted
        if not accepted_user_persisted:
            _release_unaccepted_turn_sources(
                turn_meta, owner_id=str(active_conv_id or ""),
            )
            await ws.send_json({
                "type": "status",
                "content": "error",
                "pre_accept_rejection": True,
                "error": str(
                    turn_meta.get("_user_commit_error")
                    or "persistence_unavailable"
                ),
                "conversation_id": active_conv_id,
                "request_id": str(turn_meta.get("request_id") or ""),
                "client_request_id": str(
                    turn_meta.get("request_id") or ""
                ),
            })
            await _complete_durable_command(turn_meta, {
                "accepted": False,
                "state": "persistence_unavailable",
            })
            return history, active_conv_id
        await _complete_durable_command(turn_meta, {
            "accepted": True,
            "state": "turn_accepted",
        })
        user_receipt = turn_meta.get("_user_commit")
        if (
            isinstance(user_receipt, dict)
            and user_receipt.get("committed") is False
            and turn_meta.get("_accepted_user_already_persisted") is not True
        ):
            # Another process won the durable request/conversation acceptance
            # CAS. Reattach to that exact receipt; this worker must never call
            # the provider or a tool for the replay.
            _release_unaccepted_turn_sources(
                turn_meta, owner_id=str(active_conv_id or ""),
            )
            current_task = asyncio.current_task()
            if current_task is not None:
                _clear_conversation_turn(active_conv_id, current_task)
            replayed = await _send_persisted_turn_for_request(
                ws,
                active_conv_id,
                str(
                    turn_meta.get("request_id")
                    or turn_meta.get("client_request_id")
                    or ""
                ),
            )
            if not replayed:
                await ws.send_json({
                    "type": "status",
                    "content": "error",
                    "error": "persisted_turn_replay_unavailable",
                    "conversation_id": active_conv_id,
                    "request_id": str(turn_meta.get("request_id") or ""),
                    "client_request_id": str(
                        turn_meta.get("request_id") or ""
                    ),
                })
            return history, active_conv_id
        await _broadcast_committed_receipt(
            active_conv_id,
            user_receipt,
        )
        if "_accepted_frame" not in turn_meta:
            turn_meta["_turn_publication_started"] = True
            await ws.send_json(_turn_accepted_frame(turn_meta))
            busy_frame = _conversation_busy_changed_frame(
                active_conv_id, True, turn_meta,
            )
            await ws.send_json(busy_frame)
            await _broadcast_conversation_busy_changed(
                active_conv_id,
                True,
                turn_meta,
                frame=busy_frame,
                exclude=ws,
            )
        else:
            await ws.send_json(_turn_status_frame(turn_meta, "running"))

        pending_transcript = turn_meta.pop("_pending_transcript", None)
        if isinstance(pending_transcript, str) and pending_transcript:
            # Audio decoding/STT is speculative input preparation.  Publish its
            # result only after this exact request has won the durable user-turn
            # CAS above; a concurrent losing worker must not leak a second turn
            # identity or transcript before it reattaches to the winner.
            await ws.send_json(
                _turn_event_frame(
                    turn_meta,
                    "transcript",
                    message_id=str(turn_meta.get("user_message_id") or ""),
                    payload={"content": pending_transcript},
                )
            )

    # Status: thinking (single small send before the agent kicks off —
    # cheap, fire-and-forget effectively, mobile uses it for UI state).
    if isinstance(turn_meta, dict):
        await ws.send_json(
            _turn_event_frame(
                turn_meta,
                "status",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": "thinking"},
            )
        )

    # ── Canonical response plumbing ───────────────────────────────────
    # Provider callbacks are intentionally not used as a presentation transport:
    # they are sentence-normalized for speech and are not lossless Markdown.
    # Mobile progressively reveals the single verified canonical final instead.
    loop = asyncio.get_running_loop()
    chunk_queue: asyncio.Queue = asyncio.Queue()
    pending_puts: list = []
    _SENTINEL = object()
    # Single-element lists let the inner closures mutate the flag
    # without `nonlocal` gymnastics across threads.
    first_token_logged = [False]
    first_chunk_sent_logged = [False]
    # Track everything we've actually streamed to the client so we can
    # detect a missing tail at the end of the run.
    sent_chunks: list[str] = []
    provisional_chunks: list[str] = []
    live_chunk_guard = _LiveAssistantChunkGuard(turn_context)
    tts_session: _ResponseTTSSession | None = None
    tts_enabled = (
        not isinstance(turn_meta, dict)
        or (
            turn_meta.get("tts_enabled", True) is not False
            and turn_meta.get("_response_media_cancelled") is not True
        )
    )
    if tts_enabled and isinstance(turn_meta, dict):
        candidate = _ResponseTTSSession(ws, turn_meta, t0)
        if _track_response_audio_task(candidate.start(), turn_meta, ws):
            tts_session = candidate
        else:
            candidate.cancel()
    progress_task: asyncio.Task | None = (
        asyncio.create_task(_turn_progress_loop(ws, turn_meta))
        if isinstance(turn_meta, dict)
        else None
    )
    pending_turn_holder = [None]
    pending_terminal_recovery_task = [None]

    def on_first_token() -> None:
        # Called from the agent worker thread the moment the first
        # non-empty delta arrives back from the LLM.
        if not first_token_logged[0]:
            first_token_logged[0] = True
            _ts(t0, "first model token received")

    def on_sentence(sentence: str) -> None:
        del sentence

    async def sender() -> None:
        from osia.agent import strip_raw_tool_tags
        while True:
            item = await chunk_queue.get()
            if item is _SENTINEL:
                return
            # Last line of defence: strip any <tool_call>…</tool_call>
            # blocks (including JSON payload) before the chunk reaches
            # the mobile client.
            item = strip_raw_tool_tags(item).strip()
            if not item:
                continue
            if not first_chunk_sent_logged[0]:
                first_chunk_sent_logged[0] = True
                _ts(t0, "first response_chunk queued for mobile",
                    f"({len(item)} chars)")
            try:
                print(
                    f"[DEBUG] queueing response_chunk for mobile "
                    f"({len(item)} chars)",
                    flush=True,
                )
                if not isinstance(turn_meta, dict):
                    raise ValueError("response chunk has no turn correlation")
                await ws.send_json(
                    _turn_event_frame(
                        turn_meta,
                        "response_chunk",
                        message_id=str(
                            turn_meta.get("assistant_message_id") or ""
                        ),
                        payload={"content": item},
                    )
                )
                sent_chunks.append(item)
                # Yield to the event loop after every chunk. Enqueuing a frame
                # does NOT itself yield (unbounded queue put returns straight
                # away), so without this the sender could monopolise the loop
                # and starve the writer / receive loop — defeating the HOL fix.
                await asyncio.sleep(0)
            except Exception as e:
                print(f"[OSIA Server] response_chunk send failed: {e}")
                return

    sender_task = asyncio.create_task(sender())

    try:
        from osia.config import load_settings

        # Reuse this connection's per-conversation agent (perf fix #3) instead
        # of rebuilding it — and its SSL/certifi OpenAI client — on every
        # message. Fall back to a throwaway agent only when no accessor was
        # supplied (a direct/legacy call); the live text/audio/vision paths
        # always pass get_agent, keyed by this turn's conversation.
        turn_directives = (
            turn_meta.get("turn_directives")
            if isinstance(turn_meta, dict) else None
        )
        pinned_provider = str(
            (turn_meta or {}).get("_pinned_provider_id") or ""
        ).strip().lower()
        pinned_model = str(
            (turn_meta or {}).get("_pinned_model_id") or ""
        ).strip()
        pinned_reasoning = str(
            (turn_meta or {}).get("_pinned_effective_reasoning") or ""
        ).strip().lower()
        pinned_route = bool(
            pinned_provider or pinned_model or pinned_reasoning
        )
        route_bound_at_accept = bool(
            isinstance(turn_meta, dict)
            and turn_meta.get("_route_bound_at_accept") is True
        )
        if pinned_route and not all(
            (pinned_provider, pinned_model, pinned_reasoning)
        ):
            raise RuntimeError("incomplete persisted Chat route binding")
        if pinned_route and any(
            str((turn_meta or {}).get(public_key) or "").strip()
            != pinned_value
            for public_key, pinned_value in (
                ("provider_id", pinned_provider),
                ("model_id", pinned_model),
                ("effective_reasoning", pinned_reasoning),
            )
        ):
            raise RuntimeError("canonical Chat route binding changed after acceptance")
        # Directive-bearing turns use a throwaway agent so exact model and
        # reasoning choices can never leak into the next ordinary turn. A
        # recovered turn is also isolated: its immutable route/reasoning must
        # not inherit from, or leak into, the conversation agent cache.
        agent = (
            OSIAAgent()
            if turn_directives is not None or (
                pinned_route and not route_bound_at_accept
            )
            else (get_agent(active_conv_id) if get_agent is not None else OSIAAgent())
        )
        if isinstance(turn_meta, dict):
            configure_intent = getattr(
                agent, "configure_turn_terminality_intent", None,
            )
            if callable(configure_intent):
                configure_intent(
                    objective=str(
                        turn_meta.get("_terminality_objective")
                        or turn_meta.get("_original_user_text")
                        or turn_meta.get("persisted_user_content")
                        or text
                        or ""
                    ),
                    execution_binding={
                        key: str(turn_meta.get(key) or "")
                        for key in (
                            "conversation_id", "request_id", "turn_id", "run_id",
                            "provider_id", "model_id", "effective_reasoning",
                        )
                    },
                )
        selected_model = load_settings().get("selected_model")
        # Only (re)build the client when the agent isn't already on a clean
        # primary client for this model — the user switched models, a prior
        # turn's router/key/endpoint fallback drifted it, or it's this agent's
        # first turn. Otherwise skip the costly rebuild and reuse the client.
        if turn_directives is not None:
            from osia.slash_commands import apply_to_agent

            apply_to_agent(agent, turn_directives)
            if turn_directives.model_override is not None:
                agent._applied_selected_model = (
                    turn_directives.model_override.model_id
                )
        if pinned_route:
            from osia.models import PROVIDERS
            from osia.reasoning import resolve_reasoning_option_id

            if pinned_provider not in PROVIDERS:
                raise RuntimeError("persisted Chat provider is unavailable")
            if agent.provider != pinned_provider or agent.model != pinned_model:
                agent.set_model(pinned_model, provider=pinned_provider)
            if agent.provider != pinned_provider or agent.model != pinned_model:
                raise RuntimeError("persisted Chat route could not be restored")
            resolved_reasoning = resolve_reasoning_option_id(
                pinned_provider,
                pinned_model,
                settings={
                    "default": pinned_reasoning,
                    "providers": {},
                    "models": {},
                },
            )
            if resolved_reasoning != pinned_reasoning:
                raise RuntimeError(
                    "persisted Chat reasoning route is unavailable"
                )
            agent._reasoning_override_for_turn = pinned_reasoning
            agent._applied_selected_model = pinned_model
        elif (
            turn_directives is None
            and selected_model
            and _agent_needs_model_apply(agent, selected_model)
        ):
            try:
                agent.set_model(selected_model)
                agent._applied_selected_model = selected_model
            except Exception as e:
                print(
                    f"[OSIA Server] set_model({selected_model!r}) failed, "
                    f"falling back to default {agent.model!r}: {e}"
                )
        print(
            f"[OSIA Server] ▶ agent.run history check: "
            f"len(session_history)={len(history)} before agent.run()"
        )
        if history:
            tail_preview = [
                {
                    "role": m.get("role"),
                    "content": (str(m.get("content", ""))[:80] + "…")
                    if len(str(m.get("content", ""))) > 80
                    else str(m.get("content", "")),
                }
                for m in history[-2:]
            ]
            print(f"[OSIA Server]   last 2 history item count: {len(tail_preview)}")
        _ts(t0, "agent.run started",
            f"(model={agent.model}, provider={agent.provider}, "
            f"history={len(history)} msgs, TOOL_MAP={len(TOOL_MAP)} tools, "
            f"streaming=on, max_tokens=model-default)")
        # Mark the agent busy so LRU eviction / disconnect teardown never close
        # a client its (uncancellable) worker thread is still using. The flag is
        # cleared in the worker thread's OWN finally — i.e. exactly when
        # agent.run returns or raises and the client falls idle — so it resets
        # correctly on a normal return, on an exception (the thread is done by
        # then), AND after a cancelled turn whose orphan thread keeps running
        # until agent.run finishes. (Attribute get/set is atomic under the GIL,
        # so the event loop reading _in_use never sees a torn value.)
        agent._in_use = True
        document_loop = asyncio.get_running_loop()
        document_progress_futures = []
        execution_trace_futures = []
        execution_trace_binding = [None]

        def _execution_trace_event(frame: dict) -> None:
            if not isinstance(frame, dict) or frame.get("type") != "execution_trace":
                return
            if (
                frame.get("conversation_id") != active_conv_id
                or frame.get("request_id")
                != str((turn_meta or {}).get("request_id") or "")
                or frame.get("turn_id")
                != str((turn_meta or {}).get("turn_id") or "")
            ):
                return
            binding = execution_trace_binding[0]
            if isinstance(binding, dict) and any(
                frame.get(key) != binding.get(key)
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            ):
                return
            future = asyncio.run_coroutine_threadsafe(
                _broadcast_execution_trace(frame, primary=ws),
                document_loop,
            )
            # Track pending deliveries, including public-text revisions beyond
            # the activity budget, so completion drains the same ordered feed.
            execution_trace_futures[:] = [
                pending for pending in execution_trace_futures if not pending.done()
            ]
            execution_trace_futures.append(future)

        async def _drain_execution_trace() -> None:
            pending = list(execution_trace_futures)
            execution_trace_futures.clear()
            for future in pending:
                try:
                    await asyncio.wrap_future(future)
                except Exception:
                    pass

        async def _close_committed_cancelled_projection(
            terminal: dict | None,
        ) -> bool:
            """Abandon late output after the exact durable Stop tombstone."""

            if str((terminal or {}).get("terminal_state") or "") != (
                "user_cancelled"
            ):
                return False
            provisional_chunks.clear()
            turn_meta["_response_media_cancelled"] = True
            if tts_session is not None:
                tts_session.cancel()
            for future in list(pending_puts):
                try:
                    await asyncio.wrap_future(future)
                except Exception:
                    pass
            if not sender_task.done():
                await chunk_queue.put(_SENTINEL)
                try:
                    await sender_task
                except (asyncio.CancelledError, Exception):
                    pass
            await _drain_execution_trace()
            active_holder = _ACTIVE_TURNS_BY_CONV.get(active_conv_id)
            if (
                _ACTIVE_TURN_META_BY_CONV.get(active_conv_id) is turn_meta
                and active_holder is not None
            ):
                if (
                    active_holder is not asyncio.current_task()
                    and not active_holder.done()
                ):
                    active_holder.cancel()
                _clear_conversation_turn(active_conv_id, active_holder)
                if not _is_conversation_busy(active_conv_id):
                    try:
                        await _broadcast_conversation_busy_changed(
                            active_conv_id, False, turn_meta,
                        )
                    except Exception:
                        pass
            return True

        async def _finalize_pending_tool_attempt_once(
            attempt_id: str,
            final_text: str,
            error,
            *,
            terminal_override: dict | None = None,
        ) -> bool:
            terminal_result = getattr(agent, "terminality_result", None)
            terminal = (
                terminal_override
                if isinstance(terminal_override, dict) else
                terminal_result(str((turn_meta or {}).get("turn_id") or ""))
                if callable(terminal_result) else
                {"terminal_state": "success_verified", "final_text": final_text}
            )
            if not terminal:
                print(
                    "[TERMINALITY] pending Mobile final held without a "
                    "verified terminal reservation"
                )
                return False
            turn_meta["_pending_tool_finalizer_active"] = True
            if await _close_committed_cancelled_projection(terminal):
                turn_meta.pop("_pending_tool_finalizer_active", None)
                return True
            from osia.execution_trace import validate_persisted_trace

            safe_trace = validate_persisted_trace(terminal.get("execution_trace"))
            if safe_trace is not None:
                turn_meta["_execution_trace_v1"] = safe_trace
            final_text = str(terminal.get("final_text") or final_text or "")
            from osia.agent import clean_response, strip_tool_artifacts

            final_text = strip_tool_artifacts(final_text or "").strip()
            if not final_text:
                final_text = localized_text(
                    "empty_completion", turn_context.language,
                )
            for future in list(pending_puts):
                try:
                    await asyncio.wrap_future(future)
                except Exception:
                    pass
            tail = (
                final_text
                if not sent_chunks else
                _tail_not_sent(final_text, _join_sent_chunks(sent_chunks))
            )
            if tail:
                await chunk_queue.put(tail)
            await chunk_queue.put(_SENTINEL)
            await sender_task
            await _drain_execution_trace()
            provisional_chunks.clear()
            response_frame = _turn_event_frame(
                turn_meta,
                "response",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": final_text},
            )
            final_status = _terminal_status_from_result(terminal)
            terminal_frame = _turn_terminal_frame(turn_meta, terminal)
            if error is not None or final_status == "error":
                terminal_frame.update({
                    "stage": "tool_continuation",
                    "error_category": "same_provider_continuation_failed",
                    "reason_code": str(
                        terminal.get("reason_code")
                        or "same_provider_continuation_failed"
                    )[:160],
                })
            async def _deliver_persisted_final() -> None:
                turn_meta["persisted"] = True
                terminal_frame["persisted"] = True
                try:
                    await _broadcast_committed_receipt(
                        active_conv_id, turn_meta.get("_assistant_commit"),
                    )
                    try:
                        await ws.send_json(response_frame)
                        await ws.send_json(terminal_frame)
                    except Exception as send_error:
                        print(
                            "[OSIA Server] pending tool final delivery failed "
                            f"category={type(send_error).__name__}"
                        )
                    if (
                        turn_meta.get("tts_enabled", True) is not False
                        and turn_meta.get("_response_media_cancelled") is not True
                    ):
                        resumed_tts = _ResponseTTSSession(
                            ws, turn_meta, t0, language=turn_context.language,
                        )
                        if _track_response_audio_task(
                            resumed_tts.start(), turn_meta, ws,
                        ):
                            speech_text = clean_response(final_text)
                            resumed_tts.enqueue(speech_text)
                            resumed_tts.finish_input(speech_text)
                            resumed_tts.mark_text_final_sent()
                        else:
                            resumed_tts.cancel()
                            await _send_response_media_terminal(
                                ws, turn_meta, final_status,
                            )
                    else:
                        await _send_response_media_terminal(
                            ws, turn_meta, final_status,
                        )
                    log_final(turn_context, final_text)
                finally:
                    turn_meta.pop("_pending_tool_finalizer_active", None)
                    holder = pending_turn_holder[0]
                    if holder is not None:
                        _clear_conversation_turn(active_conv_id, holder)
                        if not holder.done():
                            holder.set_result(None)
                    if not _is_conversation_busy(active_conv_id):
                        try:
                            await _broadcast_conversation_busy_changed(
                                active_conv_id, False, turn_meta,
                            )
                        except Exception:
                            pass
                    clear_attempts = getattr(agent, "clear_tool_attempts", None)
                    if callable(clear_attempts):
                        try:
                            clear_attempts()
                        except Exception:
                            pass

            def _commit_final() -> bool:
                return _persist_completed_assistant_message(
                    active_conv_id,
                    final_text,
                    turn_meta,
                    response_frame,
                    terminal_frame,
                )

            if _commit_final():
                await _deliver_persisted_final()
                return True

            async def _retry_reserved_final_commit() -> None:
                delay = 0.25
                while (
                    _ACTIVE_TURN_META_BY_CONV.get(active_conv_id) is turn_meta
                    and _is_conversation_busy(active_conv_id)
                ):
                    await asyncio.sleep(delay)
                    if _commit_final():
                        await _deliver_persisted_final()
                        return
                    delay = min(30.0, delay * 2.0)

            retry_task = asyncio.create_task(_retry_reserved_final_commit())
            _PENDING_FINAL_PERSIST_TASKS.add(retry_task)
            retry_task.add_done_callback(_PENDING_FINAL_PERSIST_TASKS.discard)
            print(
                "[TERMINALITY] pending Mobile final reserved; canonical "
                "persistence retry scheduled"
            )
            # Provider/tool continuation is terminal and must release its claim;
            # only the exact assistant commit remains pending and keeps Chat busy.
            return True

        async def _pending_tool_terminal_reservation(
            attempt_id: str,
            final_text: str,
        ) -> dict | None:
            terminal_result = getattr(agent, "terminality_result", None)
            terminal = (
                terminal_result(str((turn_meta or {}).get("turn_id") or ""))
                if callable(terminal_result) else
                None
            )
            if isinstance(terminal, dict):
                return terminal
            receipt_recorder = getattr(
                agent, "record_failed_tool_continuation_receipt", None,
            )
            if callable(receipt_recorder):
                await asyncio.to_thread(receipt_recorder, attempt_id)
            return await asyncio.to_thread(
                _reserve_failed_server_turn_terminal,
                agent,
                turn_meta,
                objective=str(
                    (turn_meta or {}).get("_terminality_objective")
                    or (turn_meta or {}).get("persisted_user_content")
                    or text
                    or ""
                ),
                language=turn_context.language,
            )

        async def _finalize_pending_tool_attempt(
            attempt_id: str,
            final_text: str,
            error,
        ) -> bool:
            """Own terminal recovery until one persisted final is deliverable."""

            turn_meta["_pending_tool_finalizer_active"] = True

            async def _attempt_once() -> bool:
                terminal = await _pending_tool_terminal_reservation(
                    attempt_id, final_text,
                )
                if not isinstance(terminal, dict):
                    return False
                return await _finalize_pending_tool_attempt_once(
                    attempt_id,
                    final_text,
                    error,
                    terminal_override=terminal,
                )

            try:
                if await _attempt_once():
                    return True
            except Exception as finalize_error:
                print(
                    "[TERMINALITY] pending Mobile final recovery deferred "
                    f"category={type(finalize_error).__name__}"
                )

            current_retry = pending_terminal_recovery_task[0]
            if current_retry is None or current_retry.done():
                async def _retry_terminal_reservation() -> None:
                    delay = 0.25
                    try:
                        while (
                            _ACTIVE_TURN_META_BY_CONV.get(active_conv_id)
                            is turn_meta
                            and _is_conversation_busy(active_conv_id)
                        ):
                            await asyncio.sleep(delay)
                            try:
                                if await _attempt_once():
                                    return
                            except Exception:
                                pass
                            delay = min(30.0, delay * 2.0)
                    finally:
                        if pending_terminal_recovery_task[0] is asyncio.current_task():
                            pending_terminal_recovery_task[0] = None
                        if not _is_conversation_busy(active_conv_id):
                            turn_meta.pop(
                                "_pending_tool_finalizer_active", None,
                            )

                retry_task = asyncio.create_task(
                    _retry_terminal_reservation()
                )
                pending_terminal_recovery_task[0] = retry_task
                _PENDING_FINAL_PERSIST_TASKS.add(retry_task)
                retry_task.add_done_callback(
                    _PENDING_FINAL_PERSIST_TASKS.discard
                )
            print(
                "[TERMINALITY] pending Mobile continuation retained; "
                "durable terminal reservation retry scheduled"
            )
            # The coordinator may release its continuation claim. This exact
            # retry owns only terminal reservation/persistence and never calls
            # the provider or replays the tool.
            return True

        def _tool_attempt_status(status: str, attempt_id: str) -> None:
            turn_meta["_tool_attempt_id"] = str(attempt_id or "")
            from osia.chat_routing import project_chat_tool_attempt_status

            safe_status = project_chat_tool_attempt_status(status)
            if safe_status == "action_starting" and _apply_queued_tool_cancellation(
                turn_meta, attempt_id,
            ):
                try:
                    from osia.tool_attempts import get_tool_attempt_store

                    queued_attempt = get_tool_attempt_store().get_attempt(
                        attempt_id,
                    ) or {}
                except Exception:
                    queued_attempt = {}
                safe_status = (
                    "cancelled_proven"
                    if queued_attempt.get("status") == "cancelled_proven"
                    else "cancellation_requested"
                )
            turn_meta["_tool_attempt_status"] = safe_status

            async def _publish_status():
                frame = _turn_status_frame(turn_meta, safe_status)
                try:
                    await ws.send_json(frame)
                except Exception:
                    pass
                await _broadcast_turn_status(
                    turn_meta, safe_status, exclude=ws, frame=frame,
                )

            asyncio.run_coroutine_threadsafe(_publish_status(), document_loop)

        def _tool_attempt_final(attempt_id: str, final_text: str, error) -> bool:
            future = asyncio.run_coroutine_threadsafe(
                _finalize_pending_tool_attempt(
                    attempt_id, final_text, error,
                ),
                document_loop,
            )
            try:
                return bool(future.result(timeout=120.0))
            except Exception:
                return False

        def _chat_document_progress(payload: dict) -> None:
            frame = _document_progress_frame(
                payload,
                owner_device=str(
                    (turn_meta or {}).get("_document_owner_device") or ""
                ),
            )
            if frame is None:
                return
            future = asyncio.run_coroutine_threadsafe(
                ws.send_json(frame), document_loop,
            )
            if len(document_progress_futures) < 128:
                document_progress_futures.append(future)

        configure_attempts = getattr(agent, "configure_tool_attempts", None)
        if callable(configure_attempts):
            configure_attempts(
                conversation_id=active_conv_id,
                turn_id=str((turn_meta or {}).get("turn_id") or ""),
                request_id=str((turn_meta or {}).get("request_id") or ""),
                final_response_message_id=str(
                    (turn_meta or {}).get("assistant_message_id") or ""
                ),
                status_callback=_tool_attempt_status,
                final_callback=_tool_attempt_final,
                on_sentence=on_sentence,
                on_first_token=on_first_token,
                existing_attempt_id=str(
                    (turn_meta or {}).get("_existing_tool_attempt_id") or ""
                ),
            )
        configure_trace = getattr(agent, "configure_execution_trace", None)
        if callable(configure_trace):
            stable_trace_turn_id = str(
                (turn_meta or {}).get("turn_id") or ""
            )
            stable_trace_run_id = str(
                (turn_meta or {}).get("run_id") or stable_trace_turn_id
            )
            configure_trace(
                conversation_id=str(active_conv_id or ""),
                request_id=str((turn_meta or {}).get("request_id") or ""),
                turn_id=stable_trace_turn_id,
                run_id=stable_trace_run_id,
                provider_id=str(getattr(agent, "provider", "") or ""),
                model_id=str(getattr(agent, "model", "") or ""),
                effective_reasoning=pinned_reasoning,
                callback=_execution_trace_event,
            )
            trace_binding = getattr(agent, "execution_trace_binding", None)
            if callable(trace_binding):
                execution_trace_binding[0] = trace_binding(stable_trace_turn_id)
        if isinstance(turn_meta, dict):
            cancel_generation = getattr(
                agent, "_invalidate_execution_generation", None,
            )
            if callable(cancel_generation):
                turn_meta["_execution_generation_cancel"] = cancel_generation
            cancel_intent = getattr(
                agent, "request_turn_cancellation_intent", None,
            )
            cancel_turn = getattr(agent, "request_turn_cancellation", None)
            if callable(cancel_intent):
                turn_meta["_terminality_cancel_intent"] = cancel_intent
            if callable(cancel_turn):
                # Retain the pre-agent durable callback until the exact route,
                # tool receipt and trace bindings are installed. Then transfer
                # authority and replay any Stop that won during setup.
                turn_meta["_terminality_cancel"] = cancel_turn
                if turn_meta.get("_tool_cancel_requested") is True:
                    cancel_turn(
                        str(turn_meta.get("turn_id") or ""),
                        effect_possible=bool(
                            turn_meta.get("_tool_attempt_id")
                            or turn_meta.get("_existing_tool_attempt_id")
                        ),
                    )
                    exact_attempt_id = str(
                        turn_meta.get("_tool_attempt_id")
                        or turn_meta.get("_existing_tool_attempt_id")
                        or ""
                    )
                    consume_terminal_receipt = getattr(
                        agent,
                        "record_failed_tool_continuation_receipt",
                        None,
                    )
                    if exact_attempt_id and callable(consume_terminal_receipt):
                        consume_terminal_receipt(exact_attempt_id)

        def _run_agent_busy_tracked():
            try:
                with document_progress_scope(
                    "chat",
                    str((turn_meta or {}).get("turn_id") or "chat"),
                    sink=_chat_document_progress,
                ):
                    terminal_runner = getattr(agent, "run_terminal_turn", None)
                    if callable(terminal_runner):
                        return terminal_runner(
                            text,
                            turn_id=str((turn_meta or {}).get("turn_id") or ""),
                            semantic_objective=str(
                                (turn_meta or {}).get("_terminality_objective")
                                or ""
                            ),
                            history=history,
                            on_sentence=on_sentence,
                            on_first_token=on_first_token,
                            current_conversation_id=active_conv_id,
                            turn_context=turn_context,
                        )
                    return agent.run(
                        text,
                        history=history,
                        on_sentence=on_sentence,
                        on_first_token=on_first_token,
                        current_conversation_id=active_conv_id,
                        turn_context=turn_context,
                    )
            finally:
                agent._in_use = False

        # Off-thread so the event loop stays free to flush response_chunks.
        response_text = await asyncio.to_thread(_run_agent_busy_tracked)
        for future in document_progress_futures:
            try:
                await asyncio.wrap_future(future)
            except Exception:
                pass
        await _drain_execution_trace()
        await _cancel_turn_progress_task(progress_task)
        progress_task = None
        print(
            f"[DEBUG] agent.run() returned "
            f"({len(response_text or '')} chars)",
            flush=True,
        )
        from osia.tool_attempts import WAITING_TOOL_RESULT

        if response_text == WAITING_TOOL_RESULT:
            pending_probe = getattr(agent, "pending_tool_attempt_record", None)
            pending_attempt = (
                pending_probe()
                if callable(pending_probe) else
                None
            )
            if not isinstance(pending_attempt, dict):
                finalize_structural_wait = getattr(
                    agent, "finalize_wait_without_pending_tool", None,
                )
                response_text = (
                    finalize_structural_wait()
                    if callable(finalize_structural_wait) else
                    (
                        "L’exécution ne peut pas progresser de façon sûre avec "
                        "l’état actuel."
                        if turn_context.language == "fr" else
                        "Execution cannot safely progress from the current state."
                    )
                )
            else:
                await _cancel_turn_progress_task(progress_task)
                progress_task = None
                for future in pending_puts:
                    try:
                        await asyncio.wrap_future(future)
                    except Exception:
                        pass
                provisional_chunks.clear()
                if tts_session is not None:
                    tts_session.cancel()
                current_task = asyncio.current_task()
                holder = document_loop.create_future()
                if (
                    active_conv_id
                    and current_task is not None
                    and _ACTIVE_TURNS_BY_CONV.get(active_conv_id) is current_task
                ):
                    _ACTIVE_TURNS_BY_CONV[active_conv_id] = holder
                    pending_turn_holder[0] = holder
                waiting_frame = _turn_status_frame(
                    turn_meta, "waiting_tool_result",
                )
                await ws.send_json(waiting_frame)
                await _broadcast_turn_status(
                    turn_meta,
                    "waiting_tool_result",
                    exclude=ws,
                    frame=waiting_frame,
                )
                if not agent.activate_pending_tool_attempt():
                    if pending_turn_holder[0] is holder:
                        _clear_conversation_turn(active_conv_id, holder)
                        if not holder.done():
                            holder.set_result(None)
                    finalize_structural_wait = getattr(
                        agent, "finalize_wait_without_pending_tool", None,
                    )
                    response_text = (
                        finalize_structural_wait()
                        if callable(finalize_structural_wait) else
                        (
                            "L’exécution ne peut pas progresser de façon sûre "
                            "avec l’état actuel."
                            if turn_context.language == "fr" else
                            "Execution cannot safely progress from the current state."
                        )
                    )
                else:
                    return agent.exported_history(), active_conv_id
    except asyncio.CancelledError:
        # Turn cancelled (client disconnected, or the conversation was
        # deleted/superseded mid-stream). The agent worker thread is
        # uncancellable and keeps feeding chunk_queue, so reap the child
        # sender task explicitly here — `except Exception` below does NOT
        # catch CancelledError — then propagate the cancellation.
        await _cancel_turn_progress_task(progress_task)
        progress_task = None
        if tts_session is not None:
            tts_session.cancel()
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass
        raise
    except Exception as e:
        await _cancel_turn_progress_task(progress_task)
        progress_task = None
        if "_drain_execution_trace" in locals():
            await _drain_execution_trace()
        print(
            f"[OSIA Server] Agent error category={type(e).__name__}"
            + (
                f" attribute={getattr(e, 'name', '')}"
                if isinstance(e, AttributeError) else ""
            )
        )
        # Drain any in-flight puts and shut the sender down cleanly.
        for fut in pending_puts:
            try:
                await asyncio.wrap_future(fut)
            except Exception:
                pass
        await chunk_queue.put(_SENTINEL)
        await sender_task
        for future in locals().get("document_progress_futures", ()):
            try:
                await asyncio.wrap_future(future)
            except Exception:
                pass
        error_agent = locals().get("agent")
        terminality_result = getattr(error_agent, "terminality_result", None)
        verified_terminal = (
            terminality_result(str((turn_meta or {}).get("turn_id") or ""))
            if callable(terminality_result) else None
        )
        if (
            callable(terminality_result)
            and not verified_terminal
            and isinstance(turn_meta, dict)
        ):
            try:
                verified_terminal = await asyncio.to_thread(
                    _reserve_failed_server_turn_terminal,
                    error_agent,
                    turn_meta,
                    objective=str(
                        turn_meta.get("_terminality_objective")
                        or original_user_text
                        or text
                        or ""
                    ),
                    language=turn_context.language,
                )
            except Exception as reserve_error:
                print(
                    "[OSIA Server] failed terminal reservation deferred "
                    f"category={type(reserve_error).__name__}"
                )
        if isinstance(verified_terminal, dict) and isinstance(turn_meta, dict):
            from osia.execution_trace import validate_persisted_trace

            safe_trace = validate_persisted_trace(
                verified_terminal.get("execution_trace")
            )
            if safe_trace is not None:
                turn_meta["_execution_trace_v1"] = safe_trace
        close_cancelled_projection = locals().get(
            "_close_committed_cancelled_projection"
        )
        if (
            callable(close_cancelled_projection)
            and await close_cancelled_projection(verified_terminal)
        ):
            return history + [
                {"role": "user", "content": text},
            ], active_conv_id
        if callable(terminality_result) and not verified_terminal:
            provisional_chunks.clear()
            if tts_session is not None:
                tts_session.cancel()
                cancelled_media_task = tts_session.task
                if (
                    cancelled_media_task is not None
                    and cancelled_media_task is not asyncio.current_task()
                    and not cancelled_media_task.done()
                ):
                    try:
                        await cancelled_media_task
                    except (asyncio.CancelledError, Exception):
                        pass
            working_frame = None
            if isinstance(turn_meta, dict):
                working_frame = _turn_status_frame(
                    turn_meta, _active_turn_working_status(active_conv_id),
                )
                await ws.send_json(working_frame)
                await _broadcast_turn_status(
                    turn_meta,
                    _active_turn_working_status(active_conv_id),
                    exclude=ws,
                    frame=working_frame,
                )
            current_task = asyncio.current_task()
            holder = None
            if (
                active_conv_id
                and current_task is not None
                and _ACTIVE_TURNS_BY_CONV.get(active_conv_id) is current_task
            ):
                holder = document_loop.create_future()
                _ACTIVE_TURNS_BY_CONV[active_conv_id] = holder
                pending_turn_holder[0] = holder
            if holder is not None and isinstance(turn_meta, dict):
                retry_objective = str(
                    turn_meta.get("_terminality_objective")
                    or original_user_text
                    or text
                    or ""
                )
                retry_found = {
                    "conversation_id": str(active_conv_id or ""),
                    "request_id": str(turn_meta.get("request_id") or ""),
                    "turn_id": str(turn_meta.get("turn_id") or ""),
                }

                async def _retry_failed_terminal_reservation() -> None:
                    delay = 0.25
                    while (
                        _ACTIVE_TURNS_BY_CONV.get(active_conv_id) is holder
                        and _ACTIVE_TURN_META_BY_CONV.get(active_conv_id)
                        is turn_meta
                    ):
                        await asyncio.sleep(delay)
                        if turn_meta.get("_pending_tool_finalizer_active") is True:
                            return
                        try:
                            persisted_terminal = await asyncio.to_thread(
                                _reserve_failed_server_turn_terminal,
                                error_agent,
                                turn_meta,
                                objective=retry_objective,
                                language=turn_context.language,
                            )
                        except Exception:
                            persisted_terminal = None
                        if not isinstance(persisted_terminal, dict):
                            delay = min(30.0, delay * 2.0)
                            continue
                        try:
                            committed = await _resume_incomplete_persisted_chat_turn(
                                ws,
                                retry_found,
                                owner_turn_meta=turn_meta,
                            )
                        except Exception as recovery_error:
                            print(
                                "[OSIA Server] failed terminal recovery deferred "
                                f"category={type(recovery_error).__name__}"
                            )
                            committed = False
                        if not committed:
                            delay = min(30.0, delay * 2.0)
                            continue
                        final_text = str(
                            persisted_terminal.get("final_text") or ""
                        )
                        try:
                            _retire_document_scope(
                                "chat", str(turn_meta.get("turn_id") or ""),
                            )
                            turn_meta.pop(
                                "_response_media_worker_finished_early", None,
                            )
                            if (
                                turn_meta.get("tts_enabled", True) is not False
                                and turn_meta.get("_response_media_cancelled") is not True
                                and turn_meta.get(
                                    "_post_turn_media_lease_committed"
                                ) is True
                            ):
                                from osia.agent import clean_response

                                resumed_tts = _ResponseTTSSession(
                                    ws,
                                    turn_meta,
                                    t0,
                                    language=turn_context.language,
                                )
                                if _track_response_audio_task(
                                    resumed_tts.start(), turn_meta, ws,
                                ):
                                    speech_text = clean_response(final_text)
                                    resumed_tts.enqueue(speech_text)
                                    resumed_tts.finish_input(speech_text)
                                    resumed_tts.mark_text_final_sent()
                                else:
                                    resumed_tts.cancel()
                                    await _send_response_media_terminal(
                                        ws, turn_meta, "error",
                                    )
                            else:
                                requested_tts_without_lease = bool(
                                    turn_meta.get("tts_enabled", True) is not False
                                    and turn_meta.get(
                                        "_response_media_cancelled"
                                    ) is not True
                                    and turn_meta.get(
                                        "_post_turn_media_lease_committed"
                                    ) is not True
                                )
                                await _send_response_media_terminal(
                                    ws,
                                    turn_meta,
                                    (
                                        "error"
                                        if requested_tts_without_lease else
                                        "done"
                                    ),
                                )
                            log_final(turn_context, final_text)
                        finally:
                            _clear_conversation_turn(active_conv_id, holder)
                            if not holder.done():
                                holder.set_result(None)
                            if not _is_conversation_busy(active_conv_id):
                                try:
                                    await _broadcast_conversation_busy_changed(
                                        active_conv_id, False, turn_meta,
                                    )
                                except Exception:
                                    pass
                            clear_attempts = getattr(
                                error_agent, "clear_tool_attempts", None,
                            )
                            if callable(clear_attempts):
                                try:
                                    clear_attempts()
                                except Exception:
                                    pass
                        return

                retry_task = asyncio.create_task(
                    _retry_failed_terminal_reservation()
                )
                _PENDING_FINAL_PERSIST_TASKS.add(retry_task)
                retry_task.add_done_callback(
                    _PENDING_FINAL_PERSIST_TASKS.discard
                )
            return history + [{"role": "user", "content": text}], active_conv_id
        if isinstance(turn_meta, dict):
            provisional_chunks.clear()
            error_text = (
                str(verified_terminal.get("final_text") or "")
                if isinstance(verified_terminal, dict) else
                localized_text("provider_failure", turn_context.language)
            )
            from osia.agent import clean_response, strip_tool_artifacts

            error_text = strip_tool_artifacts(error_text).strip() or localized_text(
                "provider_failure", turn_context.language,
            )
            chunk_frame = _turn_event_frame(
                turn_meta,
                "response_chunk",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": error_text},
            )
            await ws.send_json(chunk_frame)
            if tts_session is not None:
                speech_text = clean_response(error_text)
                tts_session.enqueue(speech_text)
                tts_session.finish_input(speech_text)
            response_frame = _turn_event_frame(
                turn_meta,
                "response",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": error_text},
            )
            terminal_frame = _turn_terminal_frame(
                turn_meta,
                verified_terminal if isinstance(verified_terminal, dict) else None,
            )
            if not isinstance(verified_terminal, dict):
                terminal_frame.update({
                    "stage": "response_generation",
                    "error_category": "provider_failure",
                    "reason_code": "provider_failure",
                })
            try:
                from osia.document_artifacts import normalize_references

                artifact_refs = normalize_references(
                    getattr(locals().get("agent"), "last_run_artifacts", None)
                )
            except Exception:
                artifact_refs = []
            if artifact_refs:
                turn_meta["_artifact_refs"] = artifact_refs
            if accepted_user_persisted and _persist_completed_assistant_message(
                active_conv_id,
                error_text,
                turn_meta,
                response_frame,
                terminal_frame,
            ):
                turn_meta["persisted"] = True
                terminal_frame["persisted"] = True
                _retire_document_scope(
                    "chat", str(turn_meta.get("turn_id") or ""),
                )
                await _broadcast_committed_receipt(
                    active_conv_id,
                    turn_meta.get("_assistant_commit"),
                )
                await ws.send_json(response_frame)
                await ws.send_json(terminal_frame)
                await _complete_response_media_after_text(
                    ws, turn_meta, tts_session, "done",
                )
                log_final(turn_context, error_text)
                return history + [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": error_text},
                ], active_conv_id
            if accepted_user_persisted:
                if tts_session is not None:
                    tts_session.cancel()
                current_task = asyncio.current_task()
                holder = None
                if (
                    active_conv_id
                    and current_task is not None
                    and _ACTIVE_TURNS_BY_CONV.get(active_conv_id)
                    is current_task
                ):
                    holder = document_loop.create_future()
                    _ACTIVE_TURNS_BY_CONV[active_conv_id] = holder
                    pending_turn_holder[0] = holder

                if holder is not None:
                    async def _retry_error_final_commit() -> None:
                        delay = 0.25
                        while (
                            _ACTIVE_TURNS_BY_CONV.get(active_conv_id) is holder
                            and _ACTIVE_TURN_META_BY_CONV.get(active_conv_id)
                            is turn_meta
                        ):
                            await asyncio.sleep(delay)
                            if not _persist_completed_assistant_message(
                                active_conv_id,
                                error_text,
                                turn_meta,
                                response_frame,
                                terminal_frame,
                            ):
                                delay = min(30.0, delay * 2.0)
                                continue
                            turn_meta["persisted"] = True
                            terminal_frame["persisted"] = True
                            _retire_document_scope(
                                "chat", str(turn_meta.get("turn_id") or ""),
                            )
                            try:
                                await _broadcast_committed_receipt(
                                    active_conv_id,
                                    turn_meta.get("_assistant_commit"),
                                )
                                try:
                                    await ws.send_json(response_frame)
                                    await ws.send_json(terminal_frame)
                                except Exception as send_error:
                                    print(
                                        "[OSIA Server] retried error final "
                                        "delivery failed category="
                                        f"{type(send_error).__name__}"
                                    )
                                if (
                                    turn_meta.get("tts_enabled", True) is not False
                                    and turn_meta.get("_response_media_cancelled") is not True
                                ):
                                    resumed_tts = _ResponseTTSSession(
                                        ws,
                                        turn_meta,
                                        t0,
                                        language=turn_context.language,
                                    )
                                    if _track_response_audio_task(
                                        resumed_tts.start(), turn_meta, ws,
                                    ):
                                        speech_text = clean_response(error_text)
                                        resumed_tts.enqueue(speech_text)
                                        resumed_tts.finish_input(speech_text)
                                        resumed_tts.mark_text_final_sent()
                                    else:
                                        resumed_tts.cancel()
                                        await _send_response_media_terminal(
                                            ws, turn_meta, "error",
                                        )
                                else:
                                    await _send_response_media_terminal(
                                        ws, turn_meta, "done",
                                    )
                                log_final(turn_context, error_text)
                            finally:
                                _clear_conversation_turn(
                                    active_conv_id, holder,
                                )
                                if not holder.done():
                                    holder.set_result(None)
                                if not _is_conversation_busy(active_conv_id):
                                    try:
                                        await _broadcast_conversation_busy_changed(
                                            active_conv_id, False, turn_meta,
                                        )
                                    except Exception:
                                        pass
                                clear_attempts = getattr(
                                    error_agent,
                                    "clear_tool_attempts",
                                    None,
                                )
                                if callable(clear_attempts):
                                    try:
                                        clear_attempts()
                                    except Exception:
                                        pass
                            return

                    retry_task = asyncio.create_task(
                        _retry_error_final_commit()
                    )
                    _PENDING_FINAL_PERSIST_TASKS.add(retry_task)
                    retry_task.add_done_callback(
                        _PENDING_FINAL_PERSIST_TASKS.discard
                    )
                    _ts(t0, "error final persistence retry scheduled")
                return history + [
                    {"role": "user", "content": text},
                ], active_conv_id
            await ws.send_json(terminal_frame)
            if tts_session is not None:
                tts_session.cancel()
        if accepted_user_persisted:
            return history + [{"role": "user", "content": text}], active_conv_id
        return history, active_conv_id

    # Wait for every scheduled put to actually land in the queue before
    # signalling end-of-stream — otherwise the sentinel could overtake
    # the last sentence and the sender would close early.
    for fut in pending_puts:
        try:
            await asyncio.wrap_future(fut)
        except Exception:
            pass
    await chunk_queue.put(_SENTINEL)
    await sender_task
    provisional_chunks.clear()

    # Strip any raw <tool_call>…</tool_call> blocks (tags AND JSON payload)
    # that survived the agent's own cleaning. This runs BEFORE the tail
    # flush, the final `response` message, and TTS synthesis — tool markup
    # must never reach the mobile client or be spoken.
    raw_response_text = response_text or ""
    terminal_result = getattr(agent, "terminality_result", None)
    terminal = (
        terminal_result(str((turn_meta or {}).get("turn_id") or ""))
        if callable(terminal_result) else
        {"terminal_state": "success_verified", "final_text": response_text}
    )
    if not terminal:
        raise RuntimeError(
            "agent returned without a verified terminal reservation"
        )
    from osia.execution_trace import validate_persisted_trace

    safe_trace = validate_persisted_trace(terminal.get("execution_trace"))
    if safe_trace is not None:
        turn_meta["_execution_trace_v1"] = safe_trace
    if await _close_committed_cancelled_projection(terminal):
        return history + [
            {"role": "user", "content": text},
        ], active_conv_id
    response_text = str(terminal.get("final_text") or response_text or "")
    from osia.agent import clean_response, strip_tool_artifacts
    if response_text:
        response_text = strip_tool_artifacts(response_text).strip()
    print(
        f"[DEBUG] cleaned response ({len(response_text or '')} chars)",
        flush=True,
    )
    if not response_text and raw_response_text.strip():
        # The agent returned text but strip_raw_tool_tags() emptied it —
        # the reply was nothing but tool markup. Surface this loudly: it
        # means the agent-side cleaning/guards let markup through.
        print(
            "[WARN] agent reply stripped to empty by strip_raw_tool_tags() "
            f"(raw was {len(raw_response_text)} chars)",
            flush=True,
        )

    # Explicit tail flush: if the agent's final response_text has content
    # that wasn't streamed as a chunk (race in the streaming flush path,
    # short tail under the early-flush threshold, …), emit the missing
    # tail as one last response_chunk so the client never loses the end
    # of a reply.
    if response_text:
        sent_concat = _join_sent_chunks(sent_chunks)
        cleaned_full = response_text
        if cleaned_full:
            tail = (
                cleaned_full
                if not sent_chunks else
                _tail_not_sent(cleaned_full, sent_concat)
            )
            if tail:
                try:
                    print(
                        f"[DEBUG] queueing tail response_chunk for mobile "
                        f"({len(tail)} chars)",
                        flush=True,
                    )
                    if not isinstance(turn_meta, dict):
                        raise ValueError("response tail has no turn correlation")
                    await ws.send_json(
                        _turn_event_frame(
                            turn_meta,
                            "response_chunk",
                            message_id=str(
                                turn_meta.get("assistant_message_id") or ""
                            ),
                            payload={"content": tail},
                        )
                    )
                    sent_chunks.append(tail)
                    _ts(t0, "tail response_chunk flushed",
                        f"({len(tail)} chars)")
                except Exception as e:
                    print(f"[OSIA Server] tail response_chunk send failed: {e}")

    # Pull the updated history back out of the agent so the next request
    # keeps context. Use exported_history() — it strips the system prompt
    # at index 0 AND defensively filters any system-prompt-looking frame,
    # so the system prompt cannot leak into the conversation history.
    new_history = agent.exported_history()
    from osia.document_artifacts import normalize_references
    artifact_refs = normalize_references(
        getattr(agent, "last_run_artifacts", None)
    )
    if artifact_refs:
        turn_meta["_artifact_refs"] = artifact_refs
    run_diagnostic = getattr(agent, "last_run_diagnostic", None)
    if not isinstance(run_diagnostic, dict):
        run_diagnostic = None
    language_diagnostic = persisted_language_drift_diagnostic(run_diagnostic)
    if language_diagnostic is not None:
        turn_meta["_language_validation_diagnostic_v1"] = language_diagnostic
    from osia.tool_contracts import (
        persisted_computer_use_argument_diagnostic,
    )
    argument_diagnostic = persisted_computer_use_argument_diagnostic(
        getattr(agent, "last_tool_argument_diagnostic", None)
    )
    if (
        isinstance(turn_meta, dict)
        and argument_diagnostic is not None
        and argument_diagnostic["conversation_id"]
        == str(turn_meta.get("conversation_id") or "")
        and argument_diagnostic["request_id"]
        == str(turn_meta.get("request_id") or "")
        and argument_diagnostic["turn_id"]
        == str(turn_meta.get("turn_id") or "")
        and argument_diagnostic["provider_id"]
        == str(turn_meta.get("provider_id") or "")
        and argument_diagnostic["model_id"]
        == str(turn_meta.get("model_id") or "")
    ):
        turn_meta["_computer_use_argument_diagnostic_v1"] = (
            argument_diagnostic
        )

    # Tool-call counter for debug logging.
    new_msgs = new_history[prev_len:]
    tool_call_count = sum(
        str(m.get("content", "")).count("<tool_call>")
        for m in new_msgs if m.get("role") == "assistant"
    )
    _ts(t0, "agent.run done",
        f"(rounds={sum(1 for m in new_msgs if m.get('role') == 'assistant')}, "
        f"tool_calls={tool_call_count}, "
        f"reply_chars={len(response_text or '')})")

    # Provider-internal assistant/tool rounds remain in-memory for context.
    # Persistence is finalized once below from the canonical response text.
    if new_history:
        tail_preview = [
            {
                "role": m.get("role"),
                "content": (str(m.get("content", ""))[:80] + "…")
                if len(str(m.get("content", ""))) > 80
                else str(m.get("content", "")),
            }
            for m in new_history[-2:]
        ]
        print(f"[OSIA Server]   last 2 history item count: {len(tail_preview)}")

    if not response_text:
        provisional_chunks.clear()
        print(
            "[WARN] empty canonical response — using localized safe fallback",
            flush=True,
        )
        response_text = localized_text(
            "empty_completion", turn_context.language,
        )
        run_diagnostic = {
            "stage": "response_generation",
            "error_category": "empty_completion",
            "reason_code": "empty_completion",
        }
        try:
            if not isinstance(turn_meta, dict):
                raise ValueError("fallback response has no turn correlation")
            await ws.send_json(
                _turn_event_frame(
                    turn_meta,
                    "response_chunk",
                    message_id=str(
                        turn_meta.get("assistant_message_id") or ""
                    ),
                    payload={"content": response_text},
                )
            )
            sent_chunks.append(response_text)
            if tts_session is not None:
                tts_session.enqueue(response_text)
        except Exception as e:
            print(
                "[OSIA Server] fallback response_chunk send failed "
                f"category={type(e).__name__}"
            )

    if tts_session is not None:
        tts_session.finish_input(clean_response(response_text))

    # Send the canonical full text once (clients that don't track chunks
    # can rely on this; clients that do can ignore it).
    print(
        f"[DEBUG] sending response to mobile ({len(response_text)} chars)",
        flush=True,
    )
    if not isinstance(turn_meta, dict):
        raise ValueError("completed response has no turn correlation")
    response_frame = _turn_event_frame(
        turn_meta,
        "response",
        message_id=str(turn_meta.get("assistant_message_id") or ""),
        payload={"content": response_text},
    )
    terminal_frame = _turn_terminal_frame(turn_meta, terminal)
    if run_diagnostic is not None:
        terminal_frame.update({
            key: str(run_diagnostic.get(key) or "")
            for key in ("stage", "error_category", "reason_code")
            if run_diagnostic.get(key)
        })
    if not _persist_completed_assistant_message(
        active_conv_id,
        response_text,
        turn_meta,
        response_frame,
        terminal_frame,
    ):
        if tts_session is not None:
            tts_session.cancel()
        current_task = asyncio.current_task()
        holder = document_loop.create_future()
        if (
            active_conv_id
            and current_task is not None
            and _ACTIVE_TURNS_BY_CONV.get(active_conv_id) is current_task
        ):
            _ACTIVE_TURNS_BY_CONV[active_conv_id] = holder
            pending_turn_holder[0] = holder

        async def _retry_completed_final_commit() -> None:
            delay = 0.25
            while (
                _ACTIVE_TURNS_BY_CONV.get(active_conv_id) is holder
                and _ACTIVE_TURN_META_BY_CONV.get(active_conv_id) is turn_meta
            ):
                await asyncio.sleep(delay)
                if not _persist_completed_assistant_message(
                    active_conv_id,
                    response_text,
                    turn_meta,
                    response_frame,
                    terminal_frame,
                ):
                    delay = min(30.0, delay * 2.0)
                    continue
                turn_meta["persisted"] = True
                terminal_frame["persisted"] = True
                _retire_document_scope(
                    "chat", str(turn_meta.get("turn_id") or ""),
                )
                try:
                    await _broadcast_committed_receipt(
                        active_conv_id,
                        turn_meta.get("_assistant_commit"),
                    )
                    try:
                        await ws.send_json(response_frame)
                        await ws.send_json(terminal_frame)
                    except Exception as send_error:
                        print(
                            "[OSIA Server] retried final delivery failed "
                            f"category={type(send_error).__name__}"
                        )
                    if (
                        turn_meta.get("tts_enabled", True) is not False
                        and turn_meta.get("_response_media_cancelled") is not True
                    ):
                        resumed_tts = _ResponseTTSSession(
                            ws,
                            turn_meta,
                            t0,
                            language=turn_context.language,
                        )
                        if _track_response_audio_task(
                            resumed_tts.start(), turn_meta, ws,
                        ):
                            speech_text = clean_response(response_text)
                            resumed_tts.enqueue(speech_text)
                            resumed_tts.finish_input(speech_text)
                            resumed_tts.mark_text_final_sent()
                        else:
                            resumed_tts.cancel()
                            await _send_response_media_terminal(
                                ws, turn_meta, "error",
                            )
                    else:
                        await _send_response_media_terminal(
                            ws, turn_meta, "done",
                        )
                    log_final(turn_context, response_text)
                finally:
                    _clear_conversation_turn(active_conv_id, holder)
                    if not holder.done():
                        holder.set_result(None)
                    if not _is_conversation_busy(active_conv_id):
                        try:
                            await _broadcast_conversation_busy_changed(
                                active_conv_id,
                                False,
                                turn_meta,
                            )
                        except Exception:
                            pass
                    clear_attempts = getattr(agent, "clear_tool_attempts", None)
                    if callable(clear_attempts):
                        try:
                            clear_attempts()
                        except Exception:
                            pass
                return

        retry_task = asyncio.create_task(_retry_completed_final_commit())
        _PENDING_FINAL_PERSIST_TASKS.add(retry_task)
        retry_task.add_done_callback(_PENDING_FINAL_PERSIST_TASKS.discard)
        _ts(t0, "assistant persistence retry scheduled")
        return new_history, active_conv_id
    await _broadcast_committed_receipt(
        active_conv_id,
        turn_meta.get("_assistant_commit"),
    )
    turn_meta["persisted"] = True
    stored_conversation = conversations.get_conversation(active_conv_id)
    if isinstance(stored_conversation, dict):
        new_history = list(stored_conversation.get("messages") or [])
    terminal_frame["persisted"] = True
    _retire_document_scope(
        "chat", str(turn_meta.get("turn_id") or ""),
    )
    await ws.send_json(response_frame)

    # Text and persistence are complete. Media is tracked separately, but the
    # conversation remains unavailable for another turn until that phase emits
    # its correlated terminal event.
    await ws.send_json(terminal_frame)
    log_final(turn_context, response_text)
    await _complete_response_media_after_text(
        ws, turn_meta, tts_session, "done",
    )
    current_task = asyncio.current_task()
    if (
        active_conv_id
        and current_task is not None
        and _clear_conversation_turn(active_conv_id, current_task)
    ) and not _is_conversation_busy(active_conv_id):
        await _broadcast_conversation_busy_changed(
            active_conv_id,
            False,
            turn_meta,
        )
    _ts(t0, "turn final frame sent")

    _ts(t0, "request complete")
    clear_attempts = getattr(agent, "clear_tool_attempts", None)
    if callable(clear_attempts):
        try:
            clear_attempts()
        except Exception:
            pass
    return new_history, active_conv_id


async def _handle_audio(ws: "_OutboundChannel", b64_data: str, audio_format: str,
                        history: list,
                        active_conv_id: str | None,
                        *, pending_files: list[str] | None = None,
                        pending_file_display_names: dict[str, str] | None = None,
                        get_agent=None,
                        turn_meta: dict | None = None,
                        permission_epoch: int | None = None) -> tuple[list, str | None]:
    """Process an audio command: transcribe, then run the agent on the
    transcript using the SAME path as text commands (so tools execute
    identically and the transcript ends up in the same conversation).

    ``ws`` is the per-connection OUTBOUND CHANNEL (``_OutboundChannel``), not
    the raw WebSocket — see ``_run_agent_and_respond``. Lifecycle frames carry
    ``conversation_id`` for client-side routing (``None`` until a conversation
    exists, which is fine: there is nothing to route to yet).

    Latency-optimized:
      * Whisper inference runs off-thread (``asyncio.to_thread``) so
        the event loop keeps serving other clients during STT.
      * STT remains speculative input preparation, but its canonical
        ``transcript`` event is emitted only after the transcribed user turn
        wins the durable request CAS in ``_run_agent_and_respond``.
    """
    from osia.voice.stt import transcribe, WHISPER_SAMPLE_RATE, resample_audio

    t0 = time.perf_counter()
    _ts(t0, "audio received", f"({len(b64_data)} b64 chars, format={audio_format})")
    if not isinstance(turn_meta, dict):
        raise ValueError("audio command has no turn correlation")

    try:
        # Decode and resample
        audio, sr = decode_base64_wav(b64_data)
        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            await ws.send_json({
                "type": "status",
                "content": "error",
                "pre_accept_rejection": True,
                "error": "empty_transcript",
                "conversation_id": str(active_conv_id or ""),
                "request_id": str(turn_meta.get("request_id") or ""),
                "client_request_id": str(turn_meta.get("request_id") or ""),
            })
            _ts(t0, "silent audio — short-circuit done")
            return history, active_conv_id

        if sr != WHISPER_SAMPLE_RATE:
            audio = resample_audio(audio, sr, WHISPER_SAMPLE_RATE)
            sr = WHISPER_SAMPLE_RATE

        # Whisper inference off-thread (sync, CPU-bound).
        transcript = await asyncio.to_thread(transcribe, audio, sample_rate=sr)
        _ts(t0, "transcription done",
            f"({len(transcript)} chars)")

        if not transcript.strip():
            await ws.send_json({
                "type": "status",
                "content": "error",
                "pre_accept_rejection": True,
                "error": "empty_transcript",
                "conversation_id": str(active_conv_id or ""),
                "request_id": str(turn_meta.get("request_id") or ""),
                "client_request_id": str(turn_meta.get("request_id") or ""),
            })
            return history, active_conv_id

        legacy_replay = turn_meta.get("_legacy_audio_replay_v1")
        if isinstance(legacy_replay, dict):
            legacy_audio_hash = str(
                legacy_replay.get("legacy_audio_hash") or ""
            )
            request_id = str(
                turn_meta.get("request_id")
                or turn_meta.get("client_request_id")
                or ""
            )
            replay_record = None
            try:
                replay_record = conversations.find_correlated_turn(
                    str(active_conv_id or ""), request_id,
                )
            except Exception:
                replay_record = None
            stored_transcript = str(
                (replay_record or {}).get("user_content") or ""
            )
            exact_transcript = bool(
                transcript == stored_transcript
                and transcript != str(b64_data)
                and not transcript.startswith((
                    "📎 ",
                    "[Document/media attachments available",
                    "[Document image available",
                ))
            )
            exact_legacy_record = _legacy_audio_replay_record(
                replay_record,
                legacy_audio_hash,
                require_complete=True,
            )
            if not exact_transcript or not exact_legacy_record:
                await ws.send_json({
                    "type": "status",
                    "content": "error",
                    "pre_accept_rejection": True,
                    "error": "request_id_conflict",
                    "conversation_id": str(active_conv_id or ""),
                    "request_id": request_id,
                    "client_request_id": request_id,
                })
                return history, active_conv_id
            if _persisted_turn_route_binding(replay_record) is None:
                replay_conversation = conversations.get_conversation(
                    str(active_conv_id or "")
                )
                if not isinstance(replay_conversation, dict):
                    await ws.send_json({
                        "type": "status",
                        "content": "error",
                        "pre_accept_rejection": True,
                        "error": "request_id_conflict",
                        "conversation_id": str(active_conv_id or ""),
                        "request_id": request_id,
                        "client_request_id": request_id,
                    })
                    return history, active_conv_id
                history_frame = _conversation_history_frame(
                    replay_conversation
                )
                history_frame.update({
                    "replayed": True,
                    "legacy_route_unavailable": True,
                    "request_id": request_id,
                    "client_request_id": request_id,
                })
                await ws.send_json(history_frame)
                return history, active_conv_id
            expected_identity = {
                key: str(turn_meta.get(key) or "")
                for key in (
                    "client_content_hash",
                    "client_base_content_hash",
                    "attachment_manifest_hash",
                    "input_identity_version",
                    "input_kind",
                )
            }
            try:
                replayed = await _send_persisted_turn_for_request(
                    ws,
                    str(active_conv_id or ""),
                    request_id,
                    expected_identity=expected_identity,
                    legacy_client_content_hash=legacy_audio_hash,
                    legacy_exact_user_content=transcript,
                )
            except ValueError:
                replayed = False
            if not replayed:
                await ws.send_json({
                    "type": "status",
                    "content": "error",
                    "pre_accept_rejection": True,
                    "error": "request_id_conflict",
                    "conversation_id": str(active_conv_id or ""),
                    "request_id": request_id,
                    "client_request_id": request_id,
                })
            return history, active_conv_id

        agent_input = transcript
        turn_meta["_original_user_text"] = transcript
        turn_meta["persisted_user_content"] = transcript
        turn_meta["_pending_transcript"] = transcript
        if pending_files:
            created_source_refs: list[str] = []
            turn_image_artifacts: list = []
            try:
                agent_input = _consume_pending_files(
                    agent_input,
                    pending_files,
                    display_names=pending_file_display_names,
                    owner_id=active_conv_id,
                    permission_epoch=permission_epoch,
                    source_refs_out=created_source_refs,
                    image_artifacts_out=turn_image_artifacts,
                )
            except BaseException:
                _release_unaccepted_source_refs(
                    created_source_refs, owner_id=str(active_conv_id or ""),
                )
                raise
            if created_source_refs:
                turn_meta["_preaccept_source_refs"] = created_source_refs
            manifest = turn_meta.get("_attachment_input_manifest_v1")
            if turn_image_artifacts and isinstance(manifest, list):
                attachment_metadata = [
                    dict(item) for item in manifest
                    if isinstance(item, dict)
                ]
                image_metadata = iter(
                    artifact.safe_metadata()
                    for artifact in turn_image_artifacts
                )
                for metadata_item in attachment_metadata:
                    if metadata_item.get("category") == "image":
                        metadata_item.update(next(image_metadata, {}))
                turn_meta["attachment_metadata"] = attachment_metadata
        if (
            turn_meta.get("attachment_manifest_hash")
            and isinstance(agent_input, str)
        ):
            turn_meta["_durable_agent_input_v1"] = agent_input

        return await _run_agent_and_respond(
            ws, agent_input, history, active_conv_id, t0=t0,
            get_agent=get_agent,
            turn_meta=turn_meta,
        )

    except asyncio.CancelledError:
        if turn_meta.get("persisted") is not True:
            _release_unaccepted_turn_sources(
                turn_meta, owner_id=str(active_conv_id or ""),
            )
        raise
    except Exception as e:
        print(f"[OSIA Server] Audio processing error: {e}")
        traceback.print_exc()
        if turn_meta.get("persisted") is True:
            raise
        _release_unaccepted_turn_sources(
            turn_meta, owner_id=str(active_conv_id or ""),
        )
        await ws.send_json({
            "type": "status",
            "content": "error",
            "pre_accept_rejection": True,
            "error": "audio_processing_failed",
            "conversation_id": str(active_conv_id or ""),
            "request_id": str(turn_meta.get("request_id") or ""),
            "client_request_id": str(turn_meta.get("request_id") or ""),
        })
        return history, active_conv_id


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
