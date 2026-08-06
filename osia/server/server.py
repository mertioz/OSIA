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
    {"type": "get_config"}                                             # request the full config snapshot (settings incl. api_key_fallbacks, personality, skills)
    {"type": "set_personality",      "text": "..."}                    # save the personality document (~/.osia/personality.md)
    {"type": "set_skill",            "name": "...", "enabled": true, "text": "..."}  # update a skill's enabled flag and/or prose (both optional)
    {"type": "create_skill",         "name": "..."}                    # create a new skill from the template (starts enabled)
    {"type": "delete_skill",         "name": "..."}                    # delete a skill and drop its enabled flag
    {"type": "get_api_keys"}                                           # request the actual stored provider keys (mobile API-keys editor)
    {"type": "get_heartbeat_config"}                                   # request the live-heartbeat enable flag + push interval (seconds)
    {"type": "restart_server"}                                         # restart the server process in place via os.execv; acked with status "done" + "message": "restarting" first (WOR-28)
    {"type": "autonomous_start",     "objective": "...", "model": "..."}  # WOR-45 P5: begin ONE machine-global autonomous run (planner→orchestrator); refused with autonomous_progress{event:"busy"} if a run already holds the single-runner lock
    {"type": "cancel_active_work", "schema_version": 1, "scope": "chat|autonomous", ...correlated target...}  # WOR-231: exact idempotent cancellation control; never a user turn
    {"type": "autonomous_interrupt"}                                   # WOR-45 P5: pause the autonomous run — state stays in_progress (resumable)
    {"type": "autonomous_abort", "run_id": "..."}                    # WOR-45 P5 compatibility alias: exact run correlation is still required before terminalizing the workflow
    {"type": "get_autonomous_status"}                                  # WOR-45 P5: request the current autonomous snapshot (or {event:"idle"}); a paused run reports {event:"interrupted"}; lets a phone reconnecting mid-run render progress
    {"type": "autonomous_resume"}                                      # WOR-45: resume a PAUSED (interrupted) autonomous run from its cursor (re-acquires the single-runner lock; refused with {event:"busy"} if another run holds it, {event:"idle"} if nothing is resumable)
    {"type": "scheduled_task_authorization_request", "task_id": "..."}
    {"type": "scheduled_task_authorization_response", "request_id": "...", "task_id": "...", "task_revision": int, "definition_hash": "...", "authorization_revision": int, "policy_epoch": int, "expiration": number, "origin_ref": "...", "action": "approve|deny"}
    {"type": "scheduled_task_authorization_revoke", ...bound task fields...}
    {"type": "scheduled_task_disable", ...bound task fields...}
  Server → Client:
    {"type": "api_keys",             "keys": {provider: key, ...}}     # actual stored keys, one entry per models.API_PROVIDERS ("" if unset)
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
    {"type": "config",               "config": {...}}                  # full config snapshot (settings incl. api_key_fallbacks, personality, skills)
    {"type": "cron_notification",    "id": "...", "task_id": "...", "title": "...",
                                     "status": "completed|failed", "conversation_id": "...", "created_at": "..."}  # in-app fallback for scheduled task completion
    {"type": "status",               "status": "done|error"}           # ack for the config-family setters (create_skill adds "name", restart_server adds "message": "restarting")
    {"type": "autonomous_progress",  "event": "running|interrupted|aborted|plan_failed|busy|idle|task_done|task_failed|salvaged|done|failed",
                                     "tasks_done": int, "tasks_total": int, "phase_name": "...", "objective": "...", "status": "..."}
                                     # WOR-45 P5: autonomous-run progress. Broadcast to ALL clients on every engine/synthesized transition (the run is machine-global);
                                     # the host synthesizes running/interrupted/aborted/plan_failed/busy/idle and enriches every frame with the live task_state snapshot.
    {"type": "scheduled_task_authorization_context|scheduled_task_authorization_request|scheduled_task_authorization_state", ...sanitized revision-bound fields...}
"""

import asyncio
import base64
import binascii
import concurrent.futures
import contextvars
import hashlib
import io
import json
import math
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
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request

from osia import conversations, document_sources, i18n
from osia.attachments import (
    ATTACHMENT_EVENT_TYPES,
    AttachmentBatchManager,
    AttachmentProtocolError,
    build_agent_input,
    cleanup_orphan_batches,
)
from osia.config import CONFIG_DIR as _CONFIG_DIR, regenerate_mobile_token
from osia.document_progress import document_progress_scope
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
    fixed_turn_context,
    localized_text,
    log_final,
    normalize_locale,
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
_PERMISSION_SERVER_ID = hashlib.sha256(
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

_PERMISSION_REQUEST_CACHE_MAX = 512
_PERMISSION_REQUEST_RETENTION_SECONDS = 600.0
_MOBILE_FULL_ACCESS_REQUEST_TTL_SECONDS = 30.0
_PERMISSION_REQUESTS: "OrderedDict[str, dict]" = OrderedDict()
_PERMISSION_REQUESTS_LOCK = threading.Lock()
_PERMISSION_SESSIONS: set["_PermissionMobileSession"] = set()
_PERMISSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PERMISSION_REVOCATION_OPERATIONS = {
    "revoke_temporary_permission",
    "revoke_autonomous_run",
    "revoke_scheduled_task",
    "revoke_all_delegated_authority",
    "disable_full_access",
}
_PERMISSION_CHANGE_OPERATIONS = {
    "set_category",
    "apply_default_access",
    "request_full_access",
    "revoke_all_temporary_permissions",
}
_PERMISSION_REQUEST_FIELDS = {
    "type", "schema_version", "request_id", "device_id", "session_id",
    "expected_policy_epoch", "expected_snapshot_sequence", "operation",
    "target_id", "expires_at",
}
_PERMISSION_CHANGE_FIELDS = {
    "schema_version", "contract_schema_version", "request_id", "session_id",
    "expected_policy_epoch", "expected_snapshot_sequence", "operation",
    "category_id", "enabled", "auth_method", "auth_nonce", "expires_at",
}


def _permission_safe_id(value) -> str:
    candidate = str(value or "")
    return candidate if _PERMISSION_ID_RE.fullmatch(candidate) else ""


def _mobile_document_owner(device_id: str) -> str:
    """Stable private owner identity for one authenticated mobile device."""

    safe_device = _permission_safe_id(device_id)
    if not safe_device:
        return ""
    return "mobile-document:" + hashlib.sha256(
        f"{_PERMISSION_SERVER_ID}\0{safe_device}".encode("utf-8"),
    ).hexdigest()


def _purge_permission_requests(now: float) -> None:
    with _PERMISSION_REQUESTS_LOCK:
        expired = [
            request_id
            for request_id, record in _PERMISSION_REQUESTS.items()
            if float(record.get("retain_until", 0.0)) <= now
        ]
        for request_id in expired:
            _PERMISSION_REQUESTS.pop(request_id, None)
        while len(_PERMISSION_REQUESTS) > _PERMISSION_REQUEST_CACHE_MAX:
            _PERMISSION_REQUESTS.popitem(last=False)


def _permission_request_statuses(
    request_ids: tuple[str, ...], *, device_id: str,
) -> tuple[dict, ...]:
    now = time.time()
    _purge_permission_requests(now)
    statuses = []
    with _PERMISSION_REQUESTS_LOCK:
        for request_id in request_ids:
            record = _PERMISSION_REQUESTS.get(request_id)
            status = "unknown"
            if record is not None and record.get("device_id") == device_id:
                status = str(record.get("status") or "unknown")
            statuses.append({"request_id": request_id, "status": status})
    return tuple(statuses)


async def _refresh_revoked_permission_contexts(result) -> None:
    """Refresh loaded run/task state without changing dispatched outcomes."""

    operation = str(getattr(result, "operation", "") or "")
    epoch = int(getattr(result, "policy_epoch", 0) or 0)
    execution_type = str(getattr(result, "execution_type", "") or "")
    authority_ref = str(getattr(result, "authority_ref", "") or "")
    revoke_all = operation == "revoke_all_delegated_authority"
    revoke_run = operation == "revoke_autonomous_run" or (
        operation == "revoke_temporary_permission"
        and execution_type == "autonomous"
    )
    st = _autonomous.get("st")
    if isinstance(st, dict):
        same_run = revoke_all or (
            revoke_run and str(st.get("run_id") or "") == authority_ref
        )
        disable_full = operation == "disable_full_access"
        if same_run or disable_full:
            orch = _autonomous.get("orch")
            if orch is not None:
                try:
                    await asyncio.to_thread(orch.stop)
                except Exception:
                    pass
            if same_run:
                _aauthorization.apply_external_permission_revocation(
                    st,
                    policy_epoch=epoch,
                    grant_ref=str(getattr(result, "grant_ref", "") or ""),
                    revoke_all=(
                        operation in {
                            "revoke_autonomous_run",
                            "revoke_all_delegated_authority",
                        }
                    ),
                )
            else:
                _aauthorization.revalidate_loaded_authorization(
                    st, origin_ref="mobile_revocation",
                )
                _astate.save_task_state(st)

    try:
        from osia import scheduled_tasks

        scheduled_tasks.refresh_after_permission_epoch(
            epoch,
            revoked_task_id=(
                authority_ref
                if operation == "revoke_scheduled_task" or (
                    operation == "revoke_temporary_permission"
                    and execution_type == "scheduled"
                )
                else None
            ),
            revoke_all=revoke_all,
            full_access_disabled=(operation == "disable_full_access"),
        )
    except Exception:
        pass


class _PermissionMobileSession:
    """One authenticated connection's monotonic permission snapshot stream."""

    def __init__(self, out: "_OutboundChannel", loop, store) -> None:
        self.out = out
        self.loop = loop
        self.store = store
        self.server_id = _PERMISSION_SERVER_ID
        self.session_id = "permission-session:" + secrets.token_urlsafe(24)
        self.device_id = ""
        self.sequence = 0
        self.active = False
        self._suppress_epoch_snapshot = False
        self._epoch_listener = self._on_epoch

    def activate(self) -> None:
        if self.active:
            return
        self.active = True
        _PERMISSION_SESSIONS.add(self)
        self.store.add_epoch_listener(self._epoch_listener)

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        _PERMISSION_SESSIONS.discard(self)
        self.store.remove_epoch_listener(self._epoch_listener)

    def bind_device(self, value) -> bool:
        device_id = _permission_safe_id(value)
        if not device_id:
            return False
        if self.device_id and self.device_id != device_id:
            return False
        self.device_id = device_id
        return True

    def _on_epoch(self, _epoch: int) -> None:
        if (
            not self.active
            or not self.device_id
            or self._suppress_epoch_snapshot
        ):
            return
        try:
            asyncio.run_coroutine_threadsafe(self.send_snapshot(), self.loop)
        except Exception:
            pass

    async def send_snapshot(
        self, pending_request_ids: tuple[str, ...] = (),
    ) -> dict | None:
        if not self.active or not self.device_id:
            return None
        safe_ids = tuple(dict.fromkeys(
            request_id
            for request_id in (
                _permission_safe_id(item) for item in pending_request_ids[:64]
            )
            if request_id
        ))
        pending = _permission_request_statuses(
            safe_ids, device_id=self.device_id,
        )
        self.sequence += 1
        frame = self.store.mobile_permission_snapshot(
            server_id=self.server_id,
            session_id=self.session_id,
            sequence=self.sequence,
            pending_requests=pending,
        )
        await self.out.send_json(frame)
        return frame

    async def _send_ack(
        self,
        *,
        request_id: str,
        base_epoch: int,
        base_sequence: int,
        status: str,
        reason: str,
        policy_epoch: int | None = None,
    ) -> dict:
        if policy_epoch is None:
            policy_epoch = self.store.load_state().policy_epoch
        frame = {
            "type": "permission_revocation_ack",
            "schema_version": 1,
            "request_id": request_id,
            "server_id": self.server_id,
            "session_id": self.session_id,
            "base_policy_epoch": base_epoch,
            "base_snapshot_sequence": base_sequence,
            "status": status,
            "reason": reason,
            "policy_epoch": int(policy_epoch),
        }
        await self.out.send_json(frame)
        return frame

    async def handle_revocation(self, message: dict) -> dict:
        request_id = _permission_safe_id(message.get("request_id"))
        base_epoch = message.get("expected_policy_epoch")
        base_sequence = message.get("expected_snapshot_sequence")
        safe_base_epoch = base_epoch if type(base_epoch) is int else -1
        safe_base_sequence = base_sequence if type(base_sequence) is int else -1

        async def reject(reason: str) -> dict:
            return await self._send_ack(
                request_id=request_id,
                base_epoch=safe_base_epoch,
                base_sequence=safe_base_sequence,
                status="rejected",
                reason=reason,
            )

        if set(message) != _PERMISSION_REQUEST_FIELDS:
            return await reject("malformed_request")
        if message.get("type") != "permission_revocation_request":
            return await reject("malformed_request")
        if message.get("schema_version") != 1:
            return await reject("unsupported_schema")
        if not request_id:
            return await reject("malformed_request")
        if not self.bind_device(message.get("device_id")):
            return await reject("wrong_device")
        if message.get("session_id") != self.session_id:
            return await reject("wrong_session")
        operation = str(message.get("operation") or "")
        if operation not in _PERMISSION_REVOCATION_OPERATIONS:
            return await reject("unsupported_operation")
        target_id = str(message.get("target_id") or "")
        if target_id and not _permission_safe_id(target_id):
            return await reject("malformed_request")
        expires_at = message.get("expires_at")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= time.time()
            or float(expires_at) > time.time() + 120.0
        ):
            return await reject("expired_request")
        if safe_base_sequence != self.sequence:
            return await reject("stale_snapshot")
        state = self.store.load_state()
        if safe_base_epoch != state.policy_epoch:
            return await reject("stale_epoch")

        _purge_permission_requests(time.time())
        with _PERMISSION_REQUESTS_LOCK:
            duplicate = _PERMISSION_REQUESTS.get(request_id)
        if duplicate is not None:
            same = (
                duplicate.get("device_id") == self.device_id
                and duplicate.get("operation") == operation
                and duplicate.get("target_id") == target_id
            )
            if same and duplicate.get("status") in {"applied", "idempotent"}:
                ack = await self._send_ack(
                    request_id=request_id,
                    base_epoch=safe_base_epoch,
                    base_sequence=safe_base_sequence,
                    status="idempotent",
                    reason="already_applied",
                    policy_epoch=int(duplicate.get("policy_epoch", state.policy_epoch)),
                )
                await self.send_snapshot((request_id,))
                return ack
            return await reject("duplicate_request")

        record = {
            "device_id": self.device_id,
            "operation": operation,
            "target_id": target_id,
            "status": "pending",
            "reason": "pending",
            "policy_epoch": state.policy_epoch,
            "retain_until": time.time() + _PERMISSION_REQUEST_RETENTION_SECONDS,
        }
        with _PERMISSION_REQUESTS_LOCK:
            _PERMISSION_REQUESTS[request_id] = record
            _PERMISSION_REQUESTS.move_to_end(request_id)

        try:
            self._suppress_epoch_snapshot = True
            result = self.store.revoke_mobile_authority(
                operation=operation,
                target_id=target_id,
                expected_policy_epoch=safe_base_epoch,
            )
        except Exception as exc:
            from osia.permission_store import (
                PermissionStoreCorrupt,
                PermissionStoreError,
                PersistentPermissionUnavailable,
            )

            if isinstance(exc, PersistentPermissionUnavailable):
                reason = "changes_unavailable"
            elif isinstance(exc, PermissionStoreCorrupt):
                reason = "changes_unavailable"
            elif isinstance(exc, PermissionStoreError):
                reason = {
                    "stale_policy_epoch": "stale_epoch",
                    "unknown_mobile_target": "unknown_target",
                    "unsupported_mobile_revocation": "unsupported_operation",
                }.get(exc.code, "rejected")
            else:
                reason = "internal_failure"
            with _PERMISSION_REQUESTS_LOCK:
                record.update({
                    "status": "rejected",
                    "reason": reason,
                    "policy_epoch": self.store.load_state().policy_epoch,
                })
            return await reject(reason)
        finally:
            self._suppress_epoch_snapshot = False

        await _refresh_revoked_permission_contexts(result)
        status = "applied" if result.changed else "idempotent"
        reason = "applied" if result.changed else "already_revoked"
        with _PERMISSION_REQUESTS_LOCK:
            record.update({
                "status": status,
                "reason": reason,
                "policy_epoch": result.policy_epoch,
            })
        ack = await self._send_ack(
            request_id=request_id,
            base_epoch=safe_base_epoch,
            base_sequence=safe_base_sequence,
            status=status,
            reason=reason,
            policy_epoch=result.policy_epoch,
        )
        await self.send_snapshot((request_id,))
        return ack

    async def handle_change_request(self, message) -> None:
        """Apply one snapshot-bound Mobile request without optimistic grants."""

        async def reject() -> None:
            if self.device_id:
                await self.send_snapshot()

        if not isinstance(message, dict) or set(message) != _PERMISSION_CHANGE_FIELDS:
            await reject()
            return
        if (
            message.get("schema_version") != 1
            or message.get("contract_schema_version") != 2
        ):
            await reject()
            return
        request_id = _permission_safe_id(message.get("request_id"))
        operation = str(message.get("operation") or "")
        category_id = str(message.get("category_id") or "")
        enabled = message.get("enabled")
        auth_method = str(message.get("auth_method") or "")
        auth_nonce = _permission_safe_id(message.get("auth_nonce"))
        base_epoch = message.get("expected_policy_epoch")
        base_sequence = message.get("expected_snapshot_sequence")
        expires_at = message.get("expires_at")
        if (
            not request_id
            or not self.device_id
            or message.get("session_id") != self.session_id
            or operation not in _PERMISSION_CHANGE_OPERATIONS
            or type(base_epoch) is not int
            or type(base_sequence) is not int
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= time.time()
            or float(expires_at) > time.time() + 120.0
            or base_sequence != self.sequence
        ):
            await reject()
            return
        state = self.store.load_state()
        if base_epoch != state.policy_epoch:
            await reject()
            return

        from osia.action_policy import AUTOMATIC_PERMISSION_CATEGORIES

        category_request = operation == "set_category"
        full_access_request = operation == "request_full_access"
        if (
            category_request
            and (
                category_id not in AUTOMATIC_PERMISSION_CATEGORIES
                or not isinstance(enabled, bool)
            )
        ) or (
            not category_request and (category_id or enabled is not False)
        ) or (
            full_access_request
            and (
                auth_method != "osia_confirmation"
                or not auth_nonce
                or float(expires_at) > time.time() + _MOBILE_FULL_ACCESS_REQUEST_TTL_SECONDS
            )
        ) or (
            not full_access_request and (auth_method or message.get("auth_nonce"))
        ):
            await reject()
            return

        _purge_permission_requests(time.time())
        with _PERMISSION_REQUESTS_LOCK:
            duplicate = _PERMISSION_REQUESTS.get(request_id)
            replayed_auth = full_access_request and any(
                item.get("auth_nonce") == auth_nonce
                for item in _PERMISSION_REQUESTS.values()
            )
        if duplicate is not None or replayed_auth:
            await reject()
            return

        record = {
            "device_id": self.device_id,
            "operation": operation,
            "target_id": category_id,
            "enabled": enabled,
            "auth_nonce": auth_nonce,
            "status": "pending",
            "reason": "pending",
            "policy_epoch": state.policy_epoch,
            "retain_until": time.time() + _PERMISSION_REQUEST_RETENTION_SECONDS,
        }
        with _PERMISSION_REQUESTS_LOCK:
            _PERMISSION_REQUESTS[request_id] = record
            _PERMISSION_REQUESTS.move_to_end(request_id)

        try:
            self._suppress_epoch_snapshot = True
            if operation == "request_full_access":
                from osia.local_reauth import (
                    issue_mobile_full_access_activation_proof,
                )

                result = self.store.activate_full_access(
                    activation_proof=issue_mobile_full_access_activation_proof(),
                    expected_policy_epoch=base_epoch,
                )
                result_epoch = result.policy_epoch
            elif operation == "set_category":
                result = self.store.update_automatic_permissions(
                    {category_id: enabled},
                    expected_policy_epoch=base_epoch,
                )
                result_epoch = result.policy_epoch
            elif operation == "apply_default_access":
                result = self.store.apply_default_access(
                    expected_policy_epoch=base_epoch,
                )
                result_epoch = result.policy_epoch
            else:
                result = self.store.revoke_all_grants(
                    expected_policy_epoch=base_epoch,
                )
                result_epoch = result.policy_epoch
        except Exception as exc:
            from osia.permission_store import (
                PermissionStoreCorrupt,
                PermissionStoreError,
                PersistentPermissionUnavailable,
            )

            if isinstance(exc, (PersistentPermissionUnavailable, PermissionStoreCorrupt)):
                reason = "changes_unavailable"
            elif isinstance(exc, PermissionStoreError):
                reason = exc.code
            else:
                reason = "internal_failure"
            record.update({
                "status": "rejected",
                "reason": reason,
                "policy_epoch": self.store.load_state().policy_epoch,
            })
            await reject()
            return
        finally:
            self._suppress_epoch_snapshot = False

        changed = result_epoch != base_epoch
        record.update({
            "status": "applied" if changed else "idempotent",
            "reason": "applied" if changed else "already_applied",
            "policy_epoch": result_epoch,
        })
        await self.send_snapshot()

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
_TTS_MAX_PENDING_SEGMENTS = 8
_CONVERSATION_BUSY_MESSAGE = "OSIA is still working in this conversation."
_TURN_ID_PREFIX = "turn-"
_EVENT_ID_PREFIX = "event-"
_MESSAGE_ID_PREFIX = "message-"
_CLIENT_REQUEST_ID_MAX = 128
_TURN_PROGRESS_INTERVAL_SECONDS = 10.0


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

    __slots__ = ("_closed", "_queue", "_text_coalescer")

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._text_coalescer = AssistantTextCoalescer(
            lambda frame: self._queue.put_nowait(("json", frame)),
            loop=asyncio.get_running_loop(),
        )

    async def send_json(self, obj) -> None:
        if self._closed:
            return
        if self._text_coalescer.offer(obj):
            return
        await self._queue.put(("json", obj))

    def send_json_nowait(self, obj) -> None:
        if self._closed:
            return
        if self._text_coalescer.offer(obj):
            return
        self._queue.put_nowait(("json", obj))

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


async def _broadcast_conversations(exclude: "_OutboundChannel | None" = None) -> None:
    """Best-effort fan-out of the refreshed conversation LIST to every connected
    client (WOR-76). Used after a rename so all devices' sidebars update.

    Like ``_broadcast_new_chat``, each frame is enqueued onto the connection's
    outbound channel — the real send is performed by that connection's own
    writer task (never from here). Unlike ``new_chat``, this carries the
    ``conversations`` list and does NOT clear the chat view; it only refreshes
    the sidebar.
    """
    items = conversations.list_conversations()
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json({"type": "conversations", "items": items})
        except Exception:
            _WS_CLIENTS.discard(client)


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
            if conversations.get_conversation(conv_id) is not None:
                return False
            _LAST_BROADCAST_REVISIONS.pop(conv_id, None)
            await _send_conversation_frame(
                {
                    "type": "conversation_deleted",
                    "id": conv_id,
                    "success": True,
                    "operation_id": str(operation_id or ""),
                },
                exclude=exclude,
            )
            await _broadcast_conversations(exclude=exclude)
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


def _new_turn_meta(conv_id: str, client_request_id: str | None) -> dict:
    request_id = client_request_id or ""
    return {
        "conversation_id": conv_id,
        "request_id": request_id,
        "client_request_id": request_id,
        "turn_id": _new_turn_id(),
        "user_message_id": _MESSAGE_ID_PREFIX + secrets.token_hex(16),
        "assistant_message_id": _MESSAGE_ID_PREFIX + secrets.token_hex(16),
        "_event_sequence": 0,
        "persisted": False,
    }


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
    if req_id:
        fields["request_id"] = req_id
        fields["client_request_id"] = req_id
    if turn_id:
        fields["turn_id"] = turn_id
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
    if not conversation_id or not request_id or not turn_id:
        raise ValueError("turn event correlation is incomplete")
    sequence = int(turn_meta.get("_event_sequence") or 0) + 1
    turn_meta["_event_sequence"] = sequence
    frame = {
        "type": event_type,
        "conversation_id": conversation_id,
        "request_id": request_id,
        # Existing clients still read this additive compatibility alias.
        "client_request_id": request_id,
        "turn_id": turn_id,
        "event_id": _EVENT_ID_PREFIX + secrets.token_hex(16),
        "event_type": event_type,
        "sequence": sequence,
        "terminal": bool(terminal),
    }
    language = normalize_locale(turn_meta.get("language"))
    if language:
        frame["language"] = language
    if message_id:
        frame["message_id"] = message_id
    if payload:
        frame.update(payload)
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
    return _turn_event_frame(
        turn_meta,
        "turn_status",
        message_id=str(turn_meta.get("assistant_message_id") or ""),
        terminal=terminal,
        cache_key=f"_terminal_frame_{status}" if terminal else None,
        payload=payload,
    )


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


async def _send_existing_turn_status(
    channel: "_OutboundChannel",
    conv_id: str,
    client_request_id: str | None,
) -> bool:
    if not client_request_id or not _is_conversation_busy(conv_id):
        return False
    meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
    if not meta or meta.get("client_request_id") != client_request_id:
        return False
    await channel.send_json(_turn_accepted_frame(meta))
    await channel.send_json(
        _turn_status_frame(meta, _active_turn_working_status(conv_id))
    )
    return True


async def _send_existing_turn_status_for_request(
    channel: "_OutboundChannel",
    client_request_id: str | None,
) -> bool:
    if not client_request_id:
        return False
    conv_id = _ACTIVE_CONV_BY_REQUEST.get(client_request_id)
    if not conv_id:
        return False
    return await _send_existing_turn_status(channel, conv_id, client_request_id)


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

    meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id) or {}
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
        return remembered
    return "working"


async def _send_recovered_turn_state(
    channel: "_OutboundChannel", conv_id: str,
) -> bool:
    """Rebind a connected client to an existing turn with fresh event ids."""

    meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
    if not isinstance(meta, dict) or not _is_conversation_busy(conv_id):
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
    if coordinator.store.find_compatible_suspended_turn(conv_id) is None:
        meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        if (
            cancel_requested
            and isinstance(meta, dict)
            and _is_conversation_busy(conv_id)
        ):
            # The provider is still planning and no tool receipt exists yet.
            # Carry one bounded intent to the first accepted tool boundary;
            # this never creates a second turn or dispatches an action.
            meta["_tool_cancel_requested"] = True
            await channel.send_json(
                _turn_status_frame(meta, "cancellation_requested")
            )
            return True
        return False
    if cancel_requested:
        handled = coordinator.request_cancellation(conv_id)
        status = "cancellation_requested"
    elif _is_resume_reconciliation_command(text):
        handled = coordinator.request_reconciliation(conv_id)
        status = "verifying"
    elif tokens in _WAITING_STATUS_COMMANDS:
        handled = True
        status = _active_turn_working_status(conv_id)
    else:
        # The conversation is occupied by the suspended turn. Consume the
        # command on this bounded control path and report busy truthfully; it
        # must never fall through into a separate provider or Autonomous turn.
        await _send_conversation_busy(channel, conv_id)
        meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
        if isinstance(meta, dict):
            await channel.send_json(
                _turn_status_frame(meta, _active_turn_working_status(conv_id))
            )
        return True
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


async def _handle_correlated_chat_cancellation(
    channel: "_OutboundChannel", msg: dict,
) -> bool:
    """Request cancellation only for the exact active canonical chat turn."""

    conv_id = _clean_cancellation_target_id(msg.get("conversation_id"))
    request_id = _clean_client_request_id(
        msg.get("request_id") or msg.get("client_request_id")
    )
    turn_id = _clean_cancellation_target_id(msg.get("turn_id"))
    supplied_attempt_id = _clean_cancellation_target_id(
        msg.get("tool_attempt_id"), maximum=160,
    ) if msg.get("tool_attempt_id") else ""
    target = {
        "conversation_id": conv_id,
        "request_id": request_id or "",
        "client_request_id": request_id or "",
        "turn_id": turn_id,
        "tool_attempt_id": supplied_attempt_id,
    }
    if not conv_id or request_id is None or not turn_id:
        await _send_cancellation_rejection(
            channel, "chat", "incomplete_target", **target,
        )
        return False
    meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
    if not isinstance(meta, dict) or not _is_conversation_busy(conv_id):
        await _send_cancellation_rejection(
            channel, "chat", "target_not_active", **target,
        )
        return False
    if (
        str(meta.get("turn_id") or "") != turn_id
        or str(meta.get("request_id") or meta.get("client_request_id") or "")
        != request_id
    ):
        await _send_cancellation_rejection(
            channel, "chat", "target_mismatch", **target,
        )
        await _send_recovered_turn_state(channel, conv_id)
        return False

    from osia.tool_attempts import (
        TERMINAL_STATUSES,
        ToolAttemptStatus,
        get_tool_attempt_store,
    )

    store = get_tool_attempt_store()
    attempt = store.find_compatible_suspended_turn(conv_id)
    if isinstance(attempt, dict) and (
        str(attempt.get("turn_id") or "") != turn_id
        or str(attempt.get("request_id") or "") != request_id
    ):
        await _send_cancellation_rejection(
            channel, "chat", "active_receipt_mismatch", **target,
        )
        return False
    remembered_attempt_id = str(meta.get("_tool_attempt_id") or "")
    active_attempt_id = str(
        (attempt or {}).get("tool_attempt_id") or remembered_attempt_id
    )
    if supplied_attempt_id and supplied_attempt_id != active_attempt_id:
        await _send_cancellation_rejection(
            channel, "chat", "tool_attempt_mismatch", **target,
        )
        return False

    control_target = (conv_id, request_id, turn_id, active_attempt_id)
    if meta.get("_cancellation_control_target") == control_target:
        await channel.send_json(
            _turn_status_frame(meta, _active_turn_working_status(conv_id))
        )
        return True

    if attempt is None and remembered_attempt_id:
        remembered = store.get_attempt(remembered_attempt_id)
        if (
            isinstance(remembered, dict)
            and ToolAttemptStatus(remembered["status"]) in TERMINAL_STATUSES
        ):
            await _send_cancellation_rejection(
                channel, "chat", "target_not_cancellable", **target,
            )
            return False

    status = "cancellation_requested"
    if isinstance(attempt, dict):
        attempt_id = str(attempt.get("tool_attempt_id") or "")
        try:
            requested = store.request_cancellation(attempt_id)
        except Exception:
            await _send_cancellation_rejection(
                channel, "chat", "cancellation_unavailable", **target,
            )
            return False
        if ToolAttemptStatus(requested["status"]) is ToolAttemptStatus.CANCELLED_PROVEN:
            status = "cancelled_proven"
        meta["_tool_attempt_id"] = attempt_id
    else:
        # The provider is still planning. Carry the intent to the first exact
        # accepted tool boundary; it can then be proven cancelled pre-dispatch.
        meta["_tool_cancel_requested"] = True
    meta["_tool_attempt_status"] = status
    meta["_cancellation_control_target"] = control_target
    frame = _turn_status_frame(meta, status)
    await channel.send_json(frame)
    await _broadcast_turn_status(meta, status, exclude=channel, frame=frame)
    return True


async def _send_persisted_turn_for_request(
    channel: "_OutboundChannel",
    conv_id: str,
    request_id: str,
    *,
    expected_user_content=None,
) -> bool:
    """Replay one completed request or reject an incomplete persisted request."""

    found = conversations.find_correlated_turn(conv_id, request_id)
    if found is None:
        return False
    if (
        expected_user_content is not None
        and str(found.get("client_content_hash") or "")
        != hashlib.sha256(
            str(expected_user_content).encode("utf-8")
        ).hexdigest()
    ):
        raise ValueError("request id was already used with different content")
    turn_id = str(found.get("turn_id") or "")
    user_message_id = str(found.get("user_message_id") or "")
    assistant_message_id = str(found.get("assistant_message_id") or "")
    response_event_id = str(found.get("response_event_id") or "")
    response_sequence = int(found.get("response_sequence") or 0)
    terminal_event_id = str(found.get("terminal_event_id") or "")
    terminal_sequence = int(found.get("terminal_sequence") or 0)
    assistant_content = found.get("assistant_content")
    terminal_status = str(found.get("terminal_status") or "done")
    common = {
        "conversation_id": conv_id,
        "request_id": request_id,
        "client_request_id": request_id,
        "turn_id": turn_id,
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
    await channel.send_json({
        "type": "turn_status",
        **common,
        "message_id": assistant_message_id or user_message_id,
        "event_id": _EVENT_ID_PREFIX + secrets.token_hex(16),
        "event_type": "turn_status",
        "sequence": max(response_sequence, terminal_sequence, 0) + 1,
        "terminal": True,
        "status": "error",
        "error": "persisted_turn_incomplete",
        "persisted": True,
    })
    return True


def _persist_accepted_user_message(
    conv_id: str | None,
    content,
    turn_meta: dict | None,
) -> bool:
    if not conv_id or not isinstance(turn_meta, dict):
        return False
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
        metadata = {
            "_conversation_id": conv_id,
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": turn_id,
            "_message_id": message_id,
            "_client_content_hash": str(
                turn_meta.get("client_content_hash") or ""
            ),
        }
        language = normalize_locale(turn_meta.get("language"))
        if language:
            metadata["_language"] = language
        attachment_metadata = turn_meta.get("attachment_metadata")
        if isinstance(attachment_metadata, list) and attachment_metadata:
            metadata["_attachments"] = attachment_metadata
        receipt = conversations.commit_message(
            conv_id,
            "user",
            content,
            metadata=metadata,
        )
    except Exception as e:
        print(
            "[OSIA Server] failed to persist accepted user turn: "
            f"{type(e).__name__}"
        )
        return False
    turn_meta["_user_commit"] = receipt
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
    response_event_id = str(response_frame.get("event_id") or "")
    response_sequence = response_frame.get("sequence")
    event_id = str(terminal_frame.get("event_id") or "")
    sequence = terminal_frame.get("sequence")
    if (
        not request_id
        or not turn_id
        or not message_id
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
            "_conversation_id": conv_id,
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": turn_id,
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
        }
        from osia.document_artifacts import normalize_references
        artifact_refs = normalize_references(turn_meta.get("_artifact_refs"))
        if artifact_refs:
            metadata["_artifact_refs"] = artifact_refs
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
    turn_meta["_assistant_commit"] = receipt
    return receipt is not None


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


def _build_config_snapshot() -> dict:
    """Build the mobile config snapshot — the ``config`` payload of a
    ``{"type": "config", "config": {...}}`` frame. ONE source of truth shared by
    the ``get_config`` WS request/response handler and the unsolicited config
    broadcast (the MCP mirror, Fix 2), so a pushed snapshot is BYTE-FOR-BYTE the
    same shape the mobile already repaints from after a ``get_config`` — no new
    frame type, no partial frame that could clobber unrelated config state on the
    phone.

    Carries NO secret beyond what ``get_config`` already ships on the
    authenticated+TLS connection (the raw ``api_key_fallbacks`` keys, identical to
    the existing ``get_config`` path); the per-server MCP token is NOT here
    (``mcp_servers`` env is always ``{}``). Synchronous file reads — callers run
    it OFF the event loop (the mirror worker thread) where possible. May raise;
    callers handle (``get_config`` already wraps it)."""
    from osia.config import load_settings, load_api_keys, get_setting
    from osia import personality
    from osia.permission_contract import mobile_permission_contract
    cfg = load_settings()
    cfg["permission_contract"] = mobile_permission_contract()
    try:
        from osia import scheduled_tasks

        cfg["cron_jobs"] = scheduled_tasks.sanitized_mobile_jobs(
            cfg.get("cron_jobs")
        )
    except Exception:
        cfg["cron_jobs"] = []
    cfg["reasoning"] = _normalize_mobile_reasoning_setting(cfg.get("reasoning"))
    cfg["mcp_servers"] = _sanitize_mobile_mcp_servers(cfg.get("mcp_servers"))
    cfg["gmail_rest"] = _gmail_rest_config_snapshot()
    # api_key_fallbacks holds the raw ordered fallback keys per provider (WOR-29).
    # The mobile fallback-keys editor needs the actual keys, so ship them as-is —
    # the connection is already authenticated + TLS, identical to get_api_keys.
    cfg["api_key_fallbacks"] = get_setting("api_key_fallbacks", {})
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
    # FIX 3 — MCP token parity (BUG 2). Provider keys already travel raw to the
    # mobile editor over this authenticated+TLS WS (api_keys_set above is only a
    # boolean; api_key_fallbacks ships raw values). MCP Bearer/stdio tokens live in
    # the SAME api_keys.json (namespaced mcp:<id>) but were stripped here — ship
    # them too, raw, ADDITIVELY in this existing config frame, so mobile can render
    # them half-masked + copyable exactly like provider/fallback keys (same path,
    # same posture — no new WS message type). ONLY token-auth connectors that have
    # a stored token; OAuth blobs are NEVER shipped (SDK-managed, not OSIA keys).
    try:
        from osia.config import get_mcp_token
        mcp_tokens: dict = {}
        for s in (get_setting("mcp_servers", []) or []):
            if not isinstance(s, dict):
                continue
            if str(s.get("auth_type") or "").strip().lower() != "token":
                continue
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            tok = get_mcp_token(sid)
            if tok:
                mcp_tokens[sid] = tok
        cfg["mcp_tokens"] = mcp_tokens
    except Exception:
        cfg["mcp_tokens"] = {}
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


async def _broadcast_permission_snapshots() -> int:
    """Push fresh PC-canonical state to every bound Mobile session."""

    sent = 0
    for session in list(_PERMISSION_SESSIONS):
        if not session.active or not session.device_id:
            continue
        try:
            if await session.send_snapshot() is not None:
                sent += 1
        except Exception:
            continue
    return sent


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


@app.post("/internal/permission_changed")
async def internal_permission_changed(request: Request):
    """Loopback-only invalidation after a canonical PC permission change."""

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    sent = await _broadcast_permission_snapshots()
    return {"ok": True, "sessions": sent}


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
    if action not in {"upsert", "rename", "delete"}:
        action = "upsert"
    try:
        revision = max(0, int(body.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    broadcast = await _broadcast_canonical_conversation_change(
        conv_id,
        revision=revision,
        operation_id=str(body.get("operation_id") or ""),
        action=action,
    )
    return {
        "ok": bool(conv_id),
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


# ── MCP write-tool confirmation provider (WOR-44 Phase 5, 5-conf-c) ──────────
# The headless gate (osia.mcp.registry, 5-conf-a) calls a registered provider
# ``provider(server_id, tool_name, summary) -> bool`` before every write/
# destructive MCP tool. THIS server registers a provider that does a WS round-
# trip to the mobile client that ORIGINATED the current turn: send an
# ``mcp_confirm_request``, block the agent's worker thread on a Future, then
# resolve it when the matching ``mcp_confirm_response`` arrives (or deny on
# timeout / disconnect / no client / send failure).
#
# Correlating the worker-thread provider call back to the originating connection:
# ``_turn_target`` is a ContextVar holding ``(channel, loop)``, set ONCE per
# connection in the WebSocket endpoint. asyncio.create_task (the turn task),
# asyncio.to_thread (the agent run) and the tool executor's
# ``contextvars.copy_context`` (osia.agent._execute_tool_once) all PROPAGATE the
# context, so the provider reads the right ``(channel, loop)`` even three
# thread-hops away — and a confirm is always routed to the device that asked.
# No secret rides these frames: only the server id, tool name, and an
# arg-NAMES-only summary (osia.mcp.registry._confirm_summary).
#
# Process isolation: the DESKTOP process registers a Qt provider in osia.main;
# THIS provider is registered only in the server process (the startup hook
# below). Different processes => different registry singletons => no conflict.
_turn_target: "contextvars.ContextVar" = contextvars.ContextVar(
    "osia_mcp_turn_target", default=None
)

# request_id -> (concurrent.futures.Future, originating _OutboundChannel).
_pending_confirms: dict = {}
_pending_confirms_lock = threading.Lock()

# Confirmation wait budget. Kept UNDER the per-call MCP tool budget granted in
# 5-conf-a (osia.agent._MCP_CONFIRM_TOOL_TIMEOUT == 150 s) so the worker returns
# (deny) BEFORE the tool executor would kill the call.
_MCP_CONFIRM_WS_TIMEOUT = 120.0


def _resolve_confirm(request_id: str, approved: bool) -> bool:
    """Resolve a pending confirmation Future by id and remove it. Returns True if
    a pending entry was found and resolved, False if the id was unknown/expired
    (a late / duplicate / foreign response — ignored cleanly). Thread-safe; never
    raises."""
    with _pending_confirms_lock:
        entry = _pending_confirms.pop(request_id, None)
    if entry is None:
        return False
    fut, _channel = entry
    if not fut.done():
        try:
            fut.set_result(bool(approved))
        except Exception:
            pass
    return True


def _deny_confirms_for_channel(channel) -> None:
    """Deny (resolve False) + drop every pending confirmation targeting
    ``channel`` — called when that connection disconnects so a blocked worker
    thread wakes immediately (deny) instead of hanging until the timeout.
    Thread-safe; never raises."""
    with _pending_confirms_lock:
        victims = [
            rid for rid, (_f, ch) in _pending_confirms.items() if ch is channel
        ]
        for rid in victims:
            fut, _ch = _pending_confirms.pop(rid)
            if not fut.done():
                try:
                    fut.set_result(False)
                except Exception:
                    pass


def _ws_confirm_provider(server_id: str, tool_name: str, summary: str) -> bool | None:
    """MCP confirmation provider for the SERVER process. CALLED ON THE AGENT'S
    WORKER THREAD. Sends an ``mcp_confirm_request`` to the turn's originating
    client and blocks until the user answers or the timeout elapses. Returns True
    ONLY on an explicit approve, False on decline / timeout / disconnect, and
    None when there is no active target to ask. Never raises (the provider
    contract)."""
    try:
        target = _turn_target.get()
        if not target:
            return None  # no originating channel; let unattended policy decide
        channel, loop = target
        if channel is None or loop is None or not loop.is_running():
            return None
        request_id = secrets.token_hex(8)
        fut: "concurrent.futures.Future" = concurrent.futures.Future()
        with _pending_confirms_lock:
            _pending_confirms[request_id] = (fut, channel)
        frame = {
            "type": "mcp_confirm_request",
            "id": request_id,
            "server": str(server_id),
            "tool": str(tool_name),
            "summary": str(summary or ""),
        }
        try:
            # Enqueue the request onto the originating connection's single-writer
            # channel from this worker thread (the established bridge).
            asyncio.run_coroutine_threadsafe(channel.send_json(frame), loop)
        except Exception:
            with _pending_confirms_lock:
                _pending_confirms.pop(request_id, None)
            return None
        try:
            approved = fut.result(timeout=_MCP_CONFIRM_WS_TIMEOUT)
        except Exception:  # TimeoutError or any -> deny
            approved = False
        finally:
            # Idempotent drop: a resolve already popped it; a timeout pops it here
            # so a late response finds nothing and is ignored (no double-resolve).
            with _pending_confirms_lock:
                _pending_confirms.pop(request_id, None)
        return bool(approved)
    except Exception:
        return False  # any failure -> deny (safe)


def _ws_reinforced_deletion_provider(plan) -> bool:
    """Notify mobile with sanitized PC-review state, but never accept approval."""

    try:
        from osia.deletion_plan import DeletionPlan

        if not isinstance(plan, DeletionPlan) or not plan.colossal_deletion:
            return False
        target = _turn_target.get()
        if not target:
            return False
        channel, loop = target
        if channel is None or loop is None or not loop.is_running():
            return False
        summary = plan.safe_public_summary()
        if float(summary["expires_at"]) <= time.time():
            return False
        frame = {
            "type": "mcp_confirm_request",
            "id": str(summary["request_id"]),
            "server": "osia",
            "tool": "colossal_deletion",
            "summary": "Complete review required locally on the PC",
            "confirmation_kind": "colossal_deletion_pc_only",
            "can_approve": False,
            "files_count": int(summary["files_count"]),
            "folders_count": int(summary["folders_count"]),
            "total_bytes": int(summary["total_bytes"]),
            "root_count": int(summary["root_count"]),
            "safe_reason": "colossal_deletion",
            "expires_at": float(summary["expires_at"]),
        }
        future = asyncio.run_coroutine_threadsafe(channel.send_json(frame), loop)
        future.result(timeout=2.0)
    except Exception:
        pass
    # Phase 2 mobile is never an authorization owner.  The request id is not
    # entered in _pending_confirms, so old clients also fail closed.
    return False


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
        from osia.tool_attempts import recover_orphaned_tool_attempts

        recovered = await asyncio.to_thread(recover_orphaned_tool_attempts)
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
async def _recover_autonomous_checkpoint_at_startup() -> None:
    """Reconnect a durable mobile tool checkpoint after attempt reconciliation."""

    try:
        st = _astate.load_task_state()
    except Exception:
        st = None
    if not isinstance(st, dict):
        return
    loop = asyncio.get_running_loop()
    scheduled = _schedule_durable_autonomous_recovery(
        st,
        origin_ref=str(st.get("authorization_origin_ref") or "mobile_legacy"),
        loop=loop,
    )
    if not scheduled and (
        st.get("status") == "in_progress"
        and st.get("run_state") in _DURABLE_AUTONOMOUS_RECOVERY_STATES
    ):
        asyncio.create_task(_retry_durable_autonomous_recovery(loop))


async def _retry_durable_autonomous_recovery(loop) -> None:
    """Wait boundedly for an outgoing server owner to release its live lock."""

    for _ in range(30):
        await asyncio.sleep(1.0)
        try:
            st = _astate.load_task_state()
        except Exception:
            return
        if (
            not isinstance(st, dict)
            or st.get("status") != "in_progress"
            or st.get("run_state") not in _DURABLE_AUTONOMOUS_RECOVERY_STATES
        ):
            return
        if _schedule_durable_autonomous_recovery(
            st,
            origin_ref=str(
                st.get("authorization_origin_ref") or "mobile_legacy"
            ),
            loop=loop,
        ):
            return


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
async def _register_mcp_confirm_provider() -> None:
    """WOR-44 Phase 5 (5-conf-c): register the SERVER's WS confirmation provider
    so a write/destructive MCP tool prompts the ORIGINATING mobile client before
    it runs. Server-process only (this module's app); the desktop process
    registers its OWN Qt provider in osia.main, and the two never share a
    registry. Without this, server-side writes are hard-denied (5-conf-a)."""
    try:
        from osia.mcp_client import registry as _mcp_registry
        _mcp_registry.set_confirmation_provider(_ws_confirm_provider)
        from osia.action_policy import (
            set_reinforced_deletion_confirmation_provider,
        )
        set_reinforced_deletion_confirmation_provider(
            _ws_reinforced_deletion_provider
        )
        print("[OSIA Server] MCP write-tool confirmation provider registered (WS).")
    except Exception as e:
        print(f"[OSIA Server] MCP confirm provider not registered: {e}")


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


@app.get("/")
async def root():
    return {"status": "ok", "service": "osia-mobile-server"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


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
                    history=history,
                    current_conversation_id=conversation_id,
                )
            else:
                reply = agent.run(
                    task_text,
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
                )
            else:
                reply = agent.run(task_text)
            display = clean_response(reply or "")
        if conversation_stale:
            return {
                "status": "completed",
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
        return {"status": "completed"}
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


# ---------------------------------------------------------------------------
# Autonomous-agent support (WOR-45 Phase 5 — SERVER side only)
# ---------------------------------------------------------------------------
# A mobile client can start / interrupt / abort / observe ONE long-running
# autonomous task over the existing WebSocket. The planner + orchestrator +
# state layer (osia.autonomous.*) are consumed strictly as BLACK BOXES — there
# are ZERO edits to the engine. Everything below is purely additive and lives
# off the chat streaming path, the per-connection conn_agents and the
# single-writer _OutboundChannel/_writer model.
import osia.autonomous.state as _astate
from osia.autonomous import authorization as _aauthorization
from osia.autonomous import diagnostics as _adiagnostics
from osia.autonomous import planner as _aplanner
from osia.autonomous.orchestrator import Orchestrator as _Orchestrator

# Layer 1 — STATE ISOLATION (collision fix). Redirect the engine's persistent
# task-state file process-locally so the server never collides with the desktop
# GUI's task_state.json. The engine resolves TASK_STATE_FILE lazily (every
# load/save/clear reads this module global at call time), so reassigning it here
# redirects ALL server-side state I/O with zero engine edits. CONFIG_DIR is the
# same dir the GUI uses, resolved via osia.config (never recomputed).
_astate.TASK_STATE_FILE = CONFIG_DIR / "task_state_mobile.json"

# Layer 2 — SINGLE-RUNNER LOCK (collision fix). A machine-wide lockfile so at
# most ONE autonomous run (GUI or mobile) exists at a time — they share the
# browser, filesystem and LLM spend. Holds {owner, pid, task_id}. A stale lock
# whose pid is dead is reclaimable.
_AUTONOMOUS_LOCK_FILE = CONFIG_DIR / "autonomous.lock"

# THE one server-side autonomous run. The dedicated agent + orchestrator are
# built lazily on first start (a server that never runs an autonomous task pays
# nothing) and are NEVER shared with the per-connection chat agents. `loop` is
# the running event loop captured at start, used to hop progress frames from the
# orchestrator's daemon thread back onto each connection's single writer.
_autonomous: dict = {
    "agent": None,    # dedicated OSIAAgent (lazy)
    "orch": None,     # Orchestrator over `agent` (lazy)
    "st": None,       # current task_state dict (the engine mutates it in place)
    "loop": None,     # asyncio loop for run_coroutine_threadsafe broadcasts
    "active": False,  # True while THIS process holds the single-runner slot
    "gen": 0,         # acquisition generation — bumped on every successful
                      # acquire so a start worker can tell whether ITS slot is
                      # still the current one (a cancel-then-reacquire during the
                      # multi-second planning window invalidates a stale worker).
    "interrupting": False,  # ORPHAN-LOOP RACE gate (WOR-45). Set True by
                      # autonomous_interrupt; cleared ONLY once the (possibly
                      # still-draining) orchestrator loop is confirmed dead. While
                      # set, a terminal event from that paused/superseded loop is
                      # neither broadcast as the run outcome (R1) nor allowed to
                      # stop()/release a newer run (R2) — see
                      # _on_autonomous_progress and _autonomous_resume_worker.
    "origin_ref": "",
    "document_owner_device": "",
}
# Guards `_autonomous` lifecycle mutations + the lockfile across the event-loop
# thread and the orchestrator/worker daemon threads. Non-reentrant — the helpers
# below NEVER nest acquisitions of it.
_autonomous_lock = threading.Lock()
_CHAT_ROUTE_REQUESTS_LOCK = threading.Lock()
_CHAT_ROUTE_REQUESTS: OrderedDict[str, dict] = OrderedDict()

# Sentinel so _autonomous_frame() can tell "snapshot the current run" (default)
# apart from an explicit "no run → empty snapshot" (st=None).
_AUTO_UNSET = object()
_AUTONOMOUS_PROTOCOL_LOCK = threading.Lock()
_AUTONOMOUS_PROTOCOL_SESSION_ID = "autonomous-session:" + secrets.token_urlsafe(24)
_AUTONOMOUS_PROTOCOL_STATE = {
    "sequences": {},
    "terminals": {},
}
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
_AUTONOMOUS_SAFE_EVENTS = frozenset({
    "action_in_progress", "action_starting",
    "aborted", "authorization_denied", "authorization_pending",
    "blocked_outcome_unknown", "blocked_unknown_effect", "busy", "cancelled",
    "cancelled_proven", "cancellation_requested", "done", "failed", "idle",
    "interrupted", "plan_failed", "running", "salvaged",
    "resuming", "verifying_tool", "waiting_tool",
    "scope_extension_denied", "state_updated",
    "suspended_authorization_revoked", "suspended_browser_intervention",
    "suspended_browser_state", "suspended_scope_extension",
    "task_done", "task_failed",
})
_AUTONOMOUS_DURABLE_TOOL_EVENTS = frozenset({
    "action_starting", "action_in_progress", "waiting_tool",
    "verifying_tool", "resuming", "cancellation_requested",
    "blocked_unknown_effect", "cancelled_proven",
})
_AUTONOMOUS_ABANDON_STATES = frozenset({
    "abandon_requested", "abandoned_proven",
})
_AUTONOMOUS_ABANDON_EFFECT_STATES = frozenset({
    "no_current_effect", "no_effect_proven", "effect_confirmed",
    "effect_possible_or_unknown",
})


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
        scope_type not in {"chat", "autonomous"}
        or not _DOCUMENT_SAFE_ID_RE.fullmatch(scope_id)
        or not _DOCUMENT_ITEM_RE.fullmatch(item_id)
        or phase not in {*_DOCUMENT_PHASE_RANK, "failed"}
    ):
        return None
    if scope_type == "autonomous":
        with _AUTONOMOUS_PROTOCOL_LOCK:
            if scope_id in _AUTONOMOUS_PROTOCOL_STATE["terminals"]:
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
        "server_id": _PERMISSION_SERVER_ID,
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
                "server_id": _PERMISSION_SERVER_ID,
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


def _on_autonomous_document_progress(payload: dict) -> None:
    frame = _document_progress_frame(
        payload,
        owner_device=str(_autonomous.get("document_owner_device") or ""),
    )
    if frame is not None:
        _broadcast_autonomous(frame)


def _autonomous_snapshot(st) -> dict:
    """WS-contract snapshot (tasks_done/tasks_total + current phase/status)
    computed from a task_state dict. Pure reads — safe on any thread. Mirrors
    main.py's ``_orch_snapshot`` but emits the mobile WS field names."""
    if not isinstance(st, dict):
        return {}
    done, total, _phase_name = _astate.count_progress(st)
    browser_checkpoint = _astate.normalize_browser_checkpoint(
        st.get("browser_checkpoint")
    )
    receipt = browser_checkpoint.get("last_action_receipt") or {}
    safe_site = ""
    for item in reversed(receipt.get("evidence") or []):
        if not isinstance(item, dict):
            continue
        candidate = item.get("safe_url_after") or item.get("safe_url_before")
        if isinstance(candidate, str):
            from osia.tools.browser_state import safe_origin

            safe_site = safe_origin(candidate)[:512]
            break
    pending = browser_checkpoint.get("pending_operation") or {}
    _phase_index, _task_index, current_task = _astate.current_task(st)
    current_task = current_task or {}
    run_id = str(st.get("run_id") or "")
    abandon_state = str(st.get("abandon_state") or "")
    if abandon_state not in _AUTONOMOUS_ABANDON_STATES:
        abandon_state = ""
    abandon_effect_state = str(st.get("abandon_effect_state") or "")
    if abandon_effect_state not in _AUTONOMOUS_ABANDON_EFFECT_STATES:
        abandon_effect_state = ""
    return {
        "task_id": str(current_task.get("id") or "")[:160],
        "operation_id": str(current_task.get("tool_operation_id") or "")[:160],
        "tool_attempt_id": str(current_task.get("tool_attempt_id") or "")[:160],
        "tasks_done": done,
        "tasks_total": total,
        # Free-form objective/plan text may repeat a private basename, message
        # body or secret even after path/URL redaction. Mobile needs the plan
        # structure and safe capability summaries, not model-authored strings.
        "phase_name": "",
        "objective": "",
        "status": st.get("status", "") or "",
        "run_state": str(st.get("run_state") or "")[:64],
        "abandon_state": abandon_state,
        "abandon_effect_state": abandon_effect_state,
        "provider": str(st.get("provider") or ""),
        "model": str(st.get("model") or ""),
        "language": normalize_locale(st.get("language")) or i18n.DEFAULT_LANG,
        "plan_summary": [
            {
                "name": f"Phase {phase_index + 1}",
                "tasks": [
                    f"Task {phase_index + 1}.{task_index + 1}"
                    for task_index, task in enumerate(phase.get("tasks") or [])
                    if isinstance(task, dict)
                ],
            }
            for phase_index, phase in enumerate(st.get("phases") or [])
            if isinstance(phase, dict)
        ],
        "browser_progress": {
            "state": (
                "manual_intervention"
                if browser_checkpoint.get("manual_intervention")
                else "pending" if pending else "current"
            ),
            "safe_operation_phase": str(pending.get("kind") or "")[:32],
            "safe_site_display": safe_site,
            "manual_intervention": str(
                browser_checkpoint.get("manual_intervention") or ""
            )[:64],
            "artifact_summary": browser_checkpoint.get("last_artifact_summary"),
        },
        "document_progress": _document_progress_snapshot(
            "autonomous",
            run_id,
            owner_device=str(_autonomous.get("document_owner_device") or ""),
        ),
        **_aauthorization.mobile_projection(st),
        **_adiagnostics.projection(st),
    }


def _autonomous_failure_state(*, stage, category, reason_code,
                              provider="", model="",
                              language: str | None = None,
                              emit_log: bool = True) -> dict:
    """Build an objective-free terminal state carrying one safe diagnostic."""
    st = _astate.new_task_state(
        "", provider, model, [], language=language,
    )
    st["status"] = "failed"
    st["run_state"] = "failed"
    st["authorization_status"] = "corrupt"
    diagnostic = _adiagnostics.make_diagnostic(
        st,
        stage=stage,
        category=category,
        reason_code=reason_code,
        provider=provider,
        model=model,
        resumable=False,
        checkpoint_present=False,
    )
    _adiagnostics.record_diagnostic(st, diagnostic, emit=emit_log)
    return st


def _establish_autonomous_failure(st: dict, my_gen: int) -> bool:
    """Persist a failure only while ``my_gen`` still owns the runner slot."""
    with _autonomous_lock:
        if (not _autonomous.get("active")
                or int(_autonomous.get("gen", 0)) != my_gen):
            return False
        _autonomous["st"] = st
        _write_autonomous_lock(st.get("task_id"))
    _adiagnostics.set_checkpoint_present(st, True)
    try:
        _astate.save_task_state(st)
    except Exception:
        _adiagnostics.set_checkpoint_present(st, False)
    _adiagnostics.emit_diagnostic_log(st.get("failure_diagnostic"))
    return True


def _autonomous_frame(payload: dict, st=_AUTO_UNSET) -> dict:
    """Build an outbound ``autonomous_progress`` frame: the event payload
    enriched with the run snapshot. The engine only emits
    task_done/task_failed/salvaged/done/failed; the host synthesizes
    running/interrupted/aborted/plan_failed/busy/idle. Pass ``st=None`` to force
    an empty snapshot (no run); omit ``st`` to snapshot the current run."""
    if st is _AUTO_UNSET:
        st = _autonomous.get("st")
    snapshot = _autonomous_snapshot(st)
    requested_event = str(payload.get("event") or "idle")[:64]
    if requested_event not in _AUTONOMOUS_SAFE_EVENTS:
        requested_event = "state_updated"
    terminal_state = bool(
        isinstance(st, dict)
        and (
            st.get("status") in {"done", "failed", "aborted", "cancelled"}
            or st.get("run_state") in {
                "completed", "failed", "cancelled", "cancelled_proven",
            }
        )
    )
    durable_state = (
        ""
        if terminal_state
        else requested_event
        if requested_event in _AUTONOMOUS_DURABLE_TOOL_EVENTS
        else str(snapshot.get("run_state") or "")[:64]
    )
    if durable_state not in _AUTONOMOUS_DURABLE_TOOL_EVENTS:
        durable_state = ""
    # Keep the established event vocabulary for older strict clients.  The
    # exact new lifecycle is an additive safe field on the same frame type.
    event = (
        "cancelled" if requested_event == "cancelled_proven"
        else "blocked_outcome_unknown"
        if requested_event == "blocked_unknown_effect"
        else "state_updated"
        if requested_event in _AUTONOMOUS_DURABLE_TOOL_EVENTS
        else requested_event
    )
    run_id = str(st.get("run_id") or "")[:256] if isinstance(st, dict) else "idle"
    terminal = event in {
        "done", "failed", "aborted", "plan_failed", "cancelled",
        "cancelled_proven",
    }
    with _AUTONOMOUS_PROTOCOL_LOCK:
        existing_terminal = _AUTONOMOUS_PROTOCOL_STATE["terminals"].get(run_id)
        if existing_terminal is not None:
            if terminal:
                return dict(existing_terminal)
            return {"_drop_after_terminal": True, "run_id": run_id}
        sequence = int(
            _AUTONOMOUS_PROTOCOL_STATE["sequences"].get(run_id, 0)
        ) + 1
        _AUTONOMOUS_PROTOCOL_STATE["sequences"][run_id] = sequence
        while len(_AUTONOMOUS_PROTOCOL_STATE["sequences"]) > 256:
            retired_run = next(iter(_AUTONOMOUS_PROTOCOL_STATE["sequences"]))
            if retired_run == run_id:
                break
            _AUTONOMOUS_PROTOCOL_STATE["sequences"].pop(retired_run, None)
            _AUTONOMOUS_PROTOCOL_STATE["terminals"].pop(retired_run, None)
    frame = {
        "type": "autonomous_progress",
        "schema_version": 1,
        "event_id": "autonomous-event:" + secrets.token_urlsafe(24),
        "sequence": sequence,
        "server_id": _PERMISSION_SERVER_ID,
        "session_id": _AUTONOMOUS_PROTOCOL_SESSION_ID,
        "run_id": run_id,
        "plan_revision": int(st.get("plan_revision", 0) or 0) if isinstance(st, dict) else 0,
        "authorization_revision": int(
            st.get("authorization_revision", 0) or 0
        ) if isinstance(st, dict) else 0,
        "event": event,
        "durable_state": durable_state,
        "safe_operation_phase": event,
        "safe_site_display": str(
            (snapshot.get("browser_progress") or {}).get("safe_site_display") or ""
        )[:512],
        "safe_progress_text": {
            "running": "Autonomous work is running",
            "action_starting": "Autonomous action is starting",
            "action_in_progress": "Autonomous action is in progress",
            "waiting_tool": "Autonomous is waiting to verify an action",
            "verifying_tool": "Autonomous is verifying an action",
            "resuming": "Autonomous is resuming the mission",
            "cancellation_requested": "Cancellation was requested",
            "blocked_unknown_effect": "The action outcome could not be established",
            "cancelled_proven": "The action was cancelled before effect",
            "task_done": "One planned step completed",
            "task_failed": "One planned step needs attention",
            "done": "Autonomous work completed",
            "failed": "Autonomous work stopped with an error",
            "plan_failed": "Autonomous work stopped with an error",
            "interrupted": "Autonomous work is paused",
            "aborted": "Autonomous work was cancelled",
            "cancelled": "Autonomous work was cancelled",
            "idle": "No Autonomous work is active",
        }.get(event, "Autonomous state updated"),
        "artifact_safe_summary": (
            (snapshot.get("browser_progress") or {}).get("artifact_summary")
        ),
        "terminal": terminal,
        **snapshot,
    }
    final_answer = payload.get("final_answer")
    if (
        event == "done"
        and terminal
        and isinstance(final_answer, str)
        and final_answer.strip()
    ):
        frame["final_answer"] = _astate.sanitize_autonomous_text(
            final_answer,
        ).strip()[:8_000]
    if terminal and terminal_state:
        with _AUTONOMOUS_PROTOCOL_LOCK:
            _AUTONOMOUS_PROTOCOL_STATE["terminals"][run_id] = dict(frame)
    return frame


def _autonomous_cancellation_correlation(st: dict) -> dict:
    _phase_index, _task_index, current_task = _astate.current_task(st)
    current_task = current_task or {}
    return {
        "run_id": str(st.get("run_id") or "")[:256],
        "task_id": str(current_task.get("id") or "")[:160],
        "operation_id": str(current_task.get("tool_operation_id") or "")[:160],
        "tool_attempt_id": str(current_task.get("tool_attempt_id") or "")[:160],
    }


def _autonomous_abandon_effect_state(st: dict) -> str:
    """Classify the current exact tool receipt without changing or replaying it."""

    existing = str(st.get("abandon_effect_state") or "")
    if existing in _AUTONOMOUS_ABANDON_EFFECT_STATES:
        return existing
    _phase_index, _task_index, current_task = _astate.current_task(st)
    current_task = current_task or {}
    attempt_id = str(current_task.get("tool_attempt_id") or "")
    if not attempt_id:
        if str(st.get("run_state") or "") in {
            "blocked_outcome_unknown", "blocked_unknown_effect",
        }:
            return "effect_possible_or_unknown"
        return "no_current_effect"
    try:
        from osia.tool_attempts import get_tool_attempt_store

        attempt = get_tool_attempt_store().get_attempt(attempt_id) or {}
    except Exception:
        return "effect_possible_or_unknown"
    if (
        attempt.get("owner_scope") != "autonomous"
        or str(attempt.get("autonomous_run_id") or "")
        != str(st.get("run_id") or "")
        or str(attempt.get("autonomous_task_id") or "")
        != str(current_task.get("id") or "")
    ):
        return "effect_possible_or_unknown"
    status = str(attempt.get("status") or "")
    dispatch = str(attempt.get("dispatch_state") or "")
    outcome = str(attempt.get("terminal_outcome") or "")
    if (
        status == "cancelled_proven"
        or dispatch in {"not_dispatched", "cancelled_before_effect"}
        or outcome in {
            "cancelled_proven", "confirmed_failure_before_effect",
            "rejected_before_execution",
        }
    ):
        return "no_effect_proven"
    if dispatch == "effect_confirmed" or outcome in {
        "success_verified", "confirmed_success", "failed_after_effect",
    }:
        return "effect_confirmed"
    return "effect_possible_or_unknown"


def _request_exact_autonomous_attempt_abandon(st: dict) -> str:
    """Request cancellation of this run's receipt once, without resuming it."""

    _phase_index, _task_index, current_task = _astate.current_task(st)
    current_task = current_task or {}
    attempt_id = str(current_task.get("tool_attempt_id") or "")
    if not attempt_id:
        return _autonomous_abandon_effect_state(st)
    try:
        from osia.tool_attempts import TERMINAL_STATUSES, ToolAttemptStatus
        from osia.tool_attempts import get_tool_attempt_store

        store = get_tool_attempt_store()
        attempt = store.get_attempt(attempt_id) or {}
        if (
            attempt.get("owner_scope") != "autonomous"
            or str(attempt.get("autonomous_run_id") or "")
            != str(st.get("run_id") or "")
            or str(attempt.get("autonomous_task_id") or "")
            != str(current_task.get("id") or "")
        ):
            return "effect_possible_or_unknown"
        status = ToolAttemptStatus(attempt.get("status"))
        if status not in TERMINAL_STATUSES:
            store.request_cancellation(attempt_id)
    except Exception:
        return _autonomous_abandon_effect_state(st)
    st.pop("abandon_effect_state", None)
    return _autonomous_abandon_effect_state(st)


def _terminalize_autonomous_abandon(st: dict, orch) -> bool:
    """Prove workflow abandonment while preserving the external-effect truth."""

    if not isinstance(st, dict):
        return False
    if st.get("abandon_state") == "abandoned_proven":
        return False
    original_status = str(
        st.get("abandon_origin_status") or st.get("status") or ""
    )
    original_run_state = str(
        st.get("abandon_origin_run_state") or st.get("run_state") or ""
    )
    effect_state = _request_exact_autonomous_attempt_abandon(st)
    preserve_terminal = (
        original_status in {"done", "failed"}
        or original_run_state in {"completed", "failed"}
    )
    if not preserve_terminal:
        st["status"] = "aborted"
        st["run_state"] = "cancelled"
        try:
            _aauthorization.revoke_run_authorization(
                st, save=lambda _state: None,
            )
        except Exception:
            pass
        st["status"] = "aborted"
        st["run_state"] = "cancelled"
        agent = _autonomous.get("agent")
        provider_now, model_now = _adiagnostics.actual_provider_model(
            agent, st.get("provider", ""), st.get("model", ""),
        )
        diagnostic = _adiagnostics.make_diagnostic(
            st,
            stage=_adiagnostics.AutonomousStage.CONTROL,
            category=_adiagnostics.FailureCategory.ABORTED,
            reason_code="user_aborted",
            provider=provider_now,
            model=model_now,
            resumable=False,
            checkpoint_present=True,
        )
        _adiagnostics.record_diagnostic(st, diagnostic)
    st["abandon_effect_state"] = effect_state
    st["abandon_state"] = "abandoned_proven"
    if orch is not None and not preserve_terminal:
        try:
            orch.stop()
        except Exception:
            _adiagnostics.emit_diagnostic_log(
                _adiagnostics.make_diagnostic(
                    st,
                    stage=_adiagnostics.AutonomousStage.CONTROL,
                    category=_adiagnostics.FailureCategory.ORCHESTRATOR_FAILURE,
                    reason_code="abort_stop_failure",
                    resumable=False,
                    checkpoint_present=True,
                )
            )
    try:
        _astate.save_task_state(st)
    except Exception:
        _adiagnostics.set_checkpoint_present(st, False)
    return not preserve_terminal


async def _send_autonomous_abandon_ack(
    channel: "_OutboundChannel", st: dict,
) -> None:
    await channel.send_json({
        "type": "cancellation_control",
        "schema_version": 1,
        "scope": "autonomous",
        "state": "accepted",
        **_autonomous_cancellation_correlation(st),
        "abandon_state": str(st.get("abandon_state") or ""),
        "abandon_effect_state": str(st.get("abandon_effect_state") or ""),
    })


async def _prepare_correlated_autonomous_cancellation(
    channel: "_OutboundChannel", msg: dict,
) -> bool:
    """Validate and durably mark one exact Autonomous cancellation target."""

    supplied = {
        "run_id": _clean_cancellation_target_id(msg.get("run_id")),
        "task_id": _clean_cancellation_target_id(
            msg.get("task_id"), maximum=160,
        ) if msg.get("task_id") else "",
        "operation_id": _clean_cancellation_target_id(
            msg.get("operation_id"), maximum=160,
        ) if msg.get("operation_id") else "",
        "tool_attempt_id": _clean_cancellation_target_id(
            msg.get("tool_attempt_id"), maximum=160,
        ) if msg.get("tool_attempt_id") else "",
    }
    if not supplied["run_id"]:
        await _send_cancellation_rejection(
            channel, "autonomous", "incomplete_target", **supplied,
        )
        return False
    st = _autonomous.get("st")
    if not isinstance(st, dict):
        await _send_cancellation_rejection(
            channel, "autonomous", "target_not_active", **supplied,
        )
        return False
    current = _autonomous_cancellation_correlation(st)
    if supplied["run_id"] != current["run_id"]:
        await _send_cancellation_rejection(
            channel, "autonomous", "target_mismatch", **supplied,
        )
        await channel.send_json(_autonomous_frame({"event": "state_updated"}, st))
        return False
    for field in ("task_id", "operation_id", "tool_attempt_id"):
        if supplied[field] and supplied[field] != current[field]:
            await _send_cancellation_rejection(
                channel, "autonomous", f"{field}_mismatch", **supplied,
            )
            await channel.send_json(
                _autonomous_frame({"event": "state_updated"}, st)
            )
            return False
    if st.get("abandon_state") == "abandoned_proven":
        await _send_autonomous_abandon_ack(channel, st)
        return False

    control_target = tuple(current[field] for field in (
        "run_id", "task_id", "operation_id", "tool_attempt_id",
    ))
    if (
        _autonomous.get("cancellation_control_target") == control_target
        or st.get("abandon_state") == "abandon_requested"
    ):
        await _send_autonomous_abandon_ack(channel, st)
        await channel.send_json(
            _autonomous_frame({"event": "cancellation_requested"}, st)
        )
        return False

    _autonomous["cancellation_control_target"] = control_target
    _autonomous["correlated_cancellation_published"] = True
    st["abandon_origin_status"] = str(st.get("status") or "")[:32]
    st["abandon_origin_run_state"] = str(st.get("run_state") or "")[:64]
    st["abandon_state"] = "abandon_requested"
    st["abandon_effect_state"] = _autonomous_abandon_effect_state(st)
    if (
        st.get("status") in {"done", "failed", "aborted", "cancelled"}
        or st.get("run_state") in {
            "completed", "failed", "cancelled", "cancelled_proven",
        }
    ):
        _terminalize_autonomous_abandon(st, _autonomous.get("orch"))
        await _send_autonomous_abandon_ack(channel, st)
        _release_single_runner()
        return False
    st["run_state"] = "cancellation_requested"
    try:
        _astate.save_task_state(st)
    except Exception:
        pass
    _broadcast_autonomous(
        _autonomous_frame({"event": "cancellation_requested"}, st)
    )
    await _send_autonomous_abandon_ack(channel, st)
    return True


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


def _broadcast_autonomous(frame: dict) -> None:
    """Fan ``frame`` out to every connected client from a NON-loop thread. Never
    touches ``ws.send_*`` directly (that corrupts Starlette's writer state — see
    ``_OutboundChannel``); instead hops onto the captured event loop so each
    connection's single writer task enqueues it (the same thread→loop pattern
    the chat stream uses). Best-effort — a dead client is skipped, never fatal."""
    loop = _autonomous.get("loop")
    if loop is None or frame.get("_drop_after_terminal") is True:
        return
    for ch in _snapshot_ws_clients():
        try:
            asyncio.run_coroutine_threadsafe(ch.send_json(frame), loop)
        except Exception:
            pass


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
    st = _autonomous.get("st")
    run_id = (
        str(st.get("run_id") or "")[:256]
        if isinstance(st, dict) else "chat"
    ) or "chat"
    safe_site = ""
    browser_session_id = ""
    try:
        from osia.tools.browser import _browser_owner_snapshot
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
    with _AUTONOMOUS_PROTOCOL_LOCK:
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
        "server_id": _PERMISSION_SERVER_ID,
        "session_id": _BROWSER_PROTOCOL_SESSION_ID,
        "browser_session_id": browser_session_id,
        "run_id": run_id,
        "plan_revision": int(st.get("plan_revision", 0) or 0) if isinstance(st, dict) else 0,
        "authorization_revision": int(
            st.get("authorization_revision", 0) or 0
        ) if isinstance(st, dict) else 0,
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
        loop = _autonomous.get("loop")
        if loop is None:
            return
        for channel in _snapshot_ws_clients():
            try:
                asyncio.run_coroutine_threadsafe(channel.send_json(frame), loop)
            except Exception:
                pass
    except Exception:
        print("[WOR-186] browser progress callback failed")


def _read_autonomous_lock():
    """Parse the single-runner lockfile, or ``None`` when absent/unreadable/
    corrupt (e.g. a torn cross-process write) — graceful, never raises."""
    try:
        if not _AUTONOMOUS_LOCK_FILE.exists():
            return None
        return json.loads(_AUTONOMOUS_LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_autonomous_lock(task_id=None) -> None:
    """Write OUR ownership record atomically (temp + ``os.replace``, like
    state.py) so a concurrent cross-process reader never sees a torn file.
    Best-effort — a write failure must not abort a start (the in-process
    ``active`` flag still guards same-process re-entry). Callers hold
    ``_autonomous_lock``."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _AUTONOMOUS_LOCK_FILE.with_name(_AUTONOMOUS_LOCK_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps({"owner": "mobile", "pid": os.getpid(),
                        "task_id": task_id}),
            encoding="utf-8",
        )
        os.replace(tmp, _AUTONOMOUS_LOCK_FILE)
    except OSError:
        print("[WOR-45] could not write autonomous lockfile")


def _acquire_single_runner(task_id=None):
    """Reserve the machine-wide single autonomous-run slot. Returns the new
    acquisition GENERATION (int ≥ 1) on success, or ``None`` if a run is already
    active in THIS process OR a valid lock from another LIVE owner exists. A
    stale lock whose pid is dead is reclaimed. Atomic under ``_autonomous_lock``
    so two concurrent starts can't both win. The generation lets a start worker
    detect that its slot was released-and-reacquired during planning (so a stale
    worker never establishes/starts over a newer run)."""
    with _autonomous_lock:
        if _autonomous.get("active"):
            return None  # this process already has a run in flight
        existing = _read_autonomous_lock()
        if isinstance(existing, dict):
            pid = existing.get("pid")
            if isinstance(pid, int) and pid != os.getpid():
                try:
                    alive = psutil.pid_exists(pid)
                except Exception:
                    alive = False
                if alive:
                    return None  # another live owner holds the slot
        # No active run here and no foreign live owner ⇒ claim it.
        gen = int(_autonomous.get("gen", 0)) + 1
        _autonomous["gen"] = gen
        _write_autonomous_lock(task_id)
        _autonomous["active"] = True
        return gen


def _do_release_locked() -> None:
    """Clear the in-process flag and delete OUR lockfile — never a foreign live
    owner's (the pid guard). MUST be called holding ``_autonomous_lock``."""
    _autonomous["active"] = False
    _autonomous.pop("cancellation_control_target", None)
    _autonomous.pop("correlated_cancellation_published", None)
    existing = _read_autonomous_lock()
    if isinstance(existing, dict) and existing.get("pid") != os.getpid():
        return  # not ours — leave the other owner's lock intact
    try:
        if _AUTONOMOUS_LOCK_FILE.exists():
            _AUTONOMOUS_LOCK_FILE.unlink()
    except OSError:
        pass


def _release_single_runner() -> None:
    """Release the CURRENT run's slot unconditionally. Used by the terminal,
    interrupt, abort and shutdown paths, which all act on the established run (no
    new acquisition can race them — ``active`` stays True until they release)."""
    with _autonomous_lock:
        _do_release_locked()


def _release_single_runner_if_gen(my_gen: int) -> None:
    """Release the slot ONLY if generation ``my_gen`` is still the current one.
    Used by a start worker's failure paths so a stale worker (whose slot was
    already released and reacquired by a newer start during planning) can never
    free the newer run's slot."""
    with _autonomous_lock:
        if int(_autonomous.get("gen", 0)) != my_gen:
            return  # a newer acquisition owns the slot now — don't touch it
        _do_release_locked()


def _finalize_terminal_release(orch, term_gen: int) -> None:
    """Release the slot for a run that reached a TERMINAL engine event, on a
    SEPARATE thread that JOINS the (now-finishing) orchestrator thread FIRST.

    The terminal event fires synchronously on the orchestrator's own daemon
    thread, which is therefore still ``is_alive()`` for a brief window. If we
    cleared ``active`` from inside that callback, a back-to-back ``autonomous_start``
    could acquire the freed slot and call ``orch.start()`` while the old thread is
    still alive — ``Orchestrator._begin`` would silently no-op (its
    ``is_alive()`` early-return), wedging the new run with the slot held but no
    loop. ``orch.stop()`` joins the finishing thread (idempotent, ≤2 s), so by
    the time we release, the loop thread is truly dead and the next start spawns
    cleanly. A self-join can't happen — this always runs on a different thread.

    GENERATION GUARD (WOR-45) — ``term_gen`` is the acquisition generation that was
    current when this terminal was OBSERVED (captured by the progress callback).
    A stale/orphan run's LATE terminal must NEVER ``stop()`` or free a NEWER
    generation's run: the engine checks its stop flag only ONCE per step, so a step
    already past that check (inside the verifier ``agent.run()`` or a replan) can
    still run through to a terminal emit AFTER a newer run took over the slot. So
    BOTH the join and the release are gated on the generation still being
    ``term_gen``: if a newer run has since acquired, this terminal belongs to a
    superseded run ⇒ no-op (touching ``orch``/the slot would stop/free the newer
    run). The release itself also goes through ``_release_single_runner_if_gen`` so
    the gate is re-checked atomically under the lock at release time."""
    with _autonomous_lock:
        if int(_autonomous.get("gen", 0)) != term_gen:
            return  # a newer generation owns the slot — this terminal is stale
    try:
        if orch is not None:
            orch.stop()  # joins the finishing loop thread; idempotent
    except Exception:
        print("[WOR-45] terminal orchestrator.stop failed")
    _release_single_runner_if_gen(term_gen)


def _on_autonomous_progress(payload: dict) -> None:
    """Orchestrator ``progress_cb`` — runs on the orchestrator's daemon thread.
    ONLY builds a plain frame and broadcasts it (never a ws/Qt touch). On a
    TERMINAL engine event (done/failed) the run is over, so release the
    single-runner slot — but OFF this (still-alive) thread, joining it first (see
    ``_finalize_terminal_release``). Wrapped so a callback error never kills the
    loop. For a live run ``active`` stays True until that release, so no start can
    race in; the generation captured below makes a STALE orphan's late terminal a
    no-op so it can never stop/free a newer run (see ``_finalize_terminal_release``).

    ORPHAN-LOOP RACE GATE (WOR-45) — a timed-out ``autonomous_interrupt`` stop()
    leaves the old loop draining: the engine checks its stop flag only ONCE per
    step (loop top + after ``agent.run``), NEVER inside ``_verify``/``_llm_judge``/
    ``_replan``, so an interrupted step already past that check still runs through
    to a terminal ``done``/``failed`` emit on THIS thread — AFTER the slot was
    freed (interrupt) or re-acquired by a resume. Such a terminal is NOT the
    outcome of a live, owned run. Gate it: when we are mid-interrupt
    (``interrupting``) OR no run owns the slot (``active`` False), DROP it — do
    NOT broadcast it (closes R1: the spurious "failed" reaching the phone) and do
    NOT spawn the finalizer (closes R2: stopping/freeing a newer run). The
    drain/resume logic (:func:`_autonomous_resume_worker`) decides the real state
    from disk. A genuinely live run keeps ``active`` True and ``interrupting``
    False, so its terminal still broadcasts + finalizes exactly as before."""
    try:
        event = payload.get("event")
        current_state = _autonomous.get("st")
        if (
            isinstance(current_state, dict)
            and current_state.get("abandon_state") in {
                "abandon_requested", "abandoned_proven",
            }
            and event != "cancellation_requested"
        ):
            return
        if event in ("done", "failed", "cancelled_proven"):
            # ── Terminal: gate a paused/superseded orphan's late terminal ──
            if _autonomous.get("interrupting") or not _autonomous.get("active"):
                print(f"[WOR-45] gating stale autonomous terminal {event!r} "
                      f"(interrupting={_autonomous.get('interrupting')}, "
                      f"active={_autonomous.get('active')}) — orphan-loop race; "
                      "drain/resume decides the real state from disk")
                return
            frame = _autonomous_frame(dict(payload), _autonomous.get("st"))
            _retire_document_scope(
                "autonomous", str(frame.get("run_id") or ""),
            )
            _broadcast_autonomous(frame)
            # Capture the generation NOW (terminal-observation time) so the
            # release/stop can be gated to it — a superseded orphan's late
            # terminal must not free/stomp whatever generation owns the slot later.
            term_gen = int(_autonomous.get("gen", 0))
            threading.Thread(
                target=_finalize_terminal_release,
                args=(_autonomous.get("orch"), term_gen),
                daemon=True,
            ).start()
        else:
            # Non-terminal progress (running/task_done/task_failed/salvaged/…) is
            # never the run OUTCOME — broadcast it unchanged, as before.
            frame = _autonomous_frame(dict(payload), _autonomous.get("st"))
            _broadcast_autonomous(frame)
    except Exception:
        print("[WOR-45] autonomous progress callback failed")


def _ensure_autonomous_engine():
    """Lazily build (exactly once) the dedicated server-side autonomous agent
    and its Orchestrator — NEVER shared with conn_agents. Returns
    ``(agent, orchestrator)``."""
    with _autonomous_lock:
        if _autonomous.get("orch") is None:
            from osia.agent import OSIAAgent
            agent = OSIAAgent()
            # A persisted Autonomous run owns one exact provider/model pair.
            # Normal chat hierarchy routing must never replace it mid-run.
            agent._disable_hierarchy_for_turn = True
            _autonomous["agent"] = agent
            _autonomous["orch"] = _Orchestrator(
                agent,
                progress_cb=_on_autonomous_progress,
                document_progress_cb=_on_autonomous_document_progress,
            )
        return _autonomous["agent"], _autonomous["orch"]


def _autonomous_start_worker(
    objective: str,
    model: str,
    my_gen: int,
    turn_context: TurnContext,
    origin_ref: str = "mobile_legacy",
    document_inventory=(),
    document_session_owner: str = "",
    document_permission_epoch: int = 0,
    document_owner_device: str = "",
    strict_provider: str = "",
    chat_route: dict | None = None,
) -> None:
    """Plan + launch OFF the event loop (planning blocks on an LLM run). The
    single-runner slot (generation ``my_gen``) was already acquired by the
    handler. Mirrors main.py's ``handle_autonomous_start``: validate the exact
    inventory identity → provider-aware set_model → create_plan_state → save →
    start → emit running.

    Two hardening rules around the long planning gap and the held slot:

    * Wrap the WHOLE body so ANY failure (engine build, set_model, planning,
      start) RELEASES the slot and tells clients — otherwise a raise would leave
      ``active`` True forever and wedge every later start at ``busy``.
    * Before starting, ESTABLISH atomically and only if generation ``my_gen`` is
      still current. A cancel (abort/interrupt) during planning releases the slot
      (and a later start may reacquire it, bumping the generation); a stale worker
      must NOT persist or ``orch.start()`` the run the user cancelled — doing so
      would run it UNLOCKED (single-runner invariant broken) and/or stomp a newer
      run. ``orch.start()`` clears the stop signal, so this gate is load-bearing.

    ORPHAN-LOOP RACE (WOR-45) — like the resume worker, DRAIN any orphan loop
    BEFORE spawning. A timed-out ``autonomous_interrupt`` can leave the previous
    loop draining (the engine never checks its stop flag inside ``_verify``/
    ``_llm_judge``/``_replan``); ``orch.start()`` would then spawn a SECOND loop on
    the shared agent (``Orchestrator._begin``'s ``is_alive`` guard is blind once
    ``stop()`` nulled ``_thread``), AND this fresh run's terminal would be dropped
    by the ``interrupting`` gate still up for the orphan (a WEDGE). So drain first,
    then reopen the gate — exactly as the resume path does."""
    try:
        agent, orch = _ensure_autonomous_engine()
        autonomous_context = (
            turn_context
            if isinstance(turn_context, TurnContext)
            else fixed_turn_context(
                str(turn_context or ""),
                source="autonomous_start",
                reference=f"autonomous-{my_gen}",
            )
        )
        language = autonomous_context.language
        agent.set_turn_context(autonomous_context)

        # ── DRAIN before spawning (never two loops on the shared agent) ──
        # Wait (GENEROUS bound) until no orchestrator loop remains, so start
        # happens only against a dead/idle engine. If a prior interrupt's orphan
        # is wedged past the bound, do NOT spawn a second loop: report the start
        # failed and defer the slot release until the orphan truly dies.
        waited = 0.0
        while _orch_loop_alive() and waited < 120.0:
            time.sleep(0.2)
            waited += 0.2
        if _orch_loop_alive():
            print("[WOR-45] autonomous start: orchestrator loop wedged past the "
                  "drain bound — not spawning a second loop; reporting plan_failed")
            failed_state = _autonomous_failure_state(
                stage=_adiagnostics.AutonomousStage.ORCHESTRATOR,
                category=_adiagnostics.FailureCategory.ORCHESTRATOR_FAILURE,
                reason_code="orchestrator_unavailable",
                language=language,
            )
            _broadcast_autonomous(
                _autonomous_frame({"event": "plan_failed"}, failed_state)
            )
            threading.Thread(
                target=_release_after_orphan_drained,
                args=(my_gen,),
                daemon=True,
            ).start()
            return
        # Orphan drained (engine dead/idle) — reopen the gate (self-heal: a prior
        # interrupt's bounded poller may have given up while the orphan was alive).
        # Safe: no loop is alive, so no terminal can fire in between.
        _autonomous["interrupting"] = False

        from osia.mobile_model_catalogue import (
            apply_autonomous_agent_model,
            resolve_current_autonomous_selection,
        )
        resolution = resolve_current_autonomous_selection(
            provider_id=strict_provider if strict_provider else "",
            model_id=model if strict_provider else "",
            wire_value=None if strict_provider else model,
            refresh_account=True,
            strict=bool(strict_provider),
            persist=not bool(strict_provider),
        )
        provider = resolution.get("provider_id") or ""
        model = resolution.get("model_id") or ""
        if resolution.get("fallback_occurred"):
            print(
                "[WOR-182] Mobile Autonomous model fallback: "
                f"{resolution.get('reason') or 'unavailable'}"
            )
        model_setup_failed = False
        if provider and model:
            try:
                apply_autonomous_agent_model(agent, provider, model)
            except Exception:
                model_setup_failed = True
                _adiagnostics.emit_diagnostic_log(
                    _adiagnostics.make_diagnostic(
                        None,
                        stage=_adiagnostics.AutonomousStage.PLANNING,
                        category=(
                            _adiagnostics.FailureCategory.PLANNING_PROVIDER_FAILURE
                        ),
                        reason_code="model_setup_failure",
                        provider=provider,
                        model=model,
                        resumable=False,
                        checkpoint_present=False,
                    )
                )
        if provider and model and not model_setup_failed:
            provider, model = _adiagnostics.actual_provider_model(
                agent, provider, model,
            )
        planning_failures = []
        planned_run_id = secrets.token_hex(16)
        if provider and model and not model_setup_failed:
            try:
                st = _aplanner.create_plan_state(
                    objective, provider, model, agent,
                    failure_cb=planning_failures.append,
                    language=language,
                    turn_context=autonomous_context,
                    origin_ref=origin_ref,
                    run_id=planned_run_id,
                    document_sources=document_inventory,
                )
            except Exception:
                st = None
        else:
            st = None
        if not st:
            if planning_failures:
                failure = planning_failures[-1]
                category = failure.error_category
                reason_code = failure.reason
            else:
                category = _adiagnostics.FailureCategory.PLANNING_PROVIDER_FAILURE
                if model_setup_failed:
                    reason_code = "model_setup_failure"
                elif not provider or not model:
                    reason_code = "no_configured_model"
                else:
                    reason_code = "planning_internal_failure"
            failed_state = _autonomous_failure_state(
                stage=_adiagnostics.AutonomousStage.PLANNING,
                category=category,
                reason_code=reason_code,
                provider=provider,
                model=model,
                language=language,
                emit_log=False,
            )
            if _establish_autonomous_failure(failed_state, my_gen):
                _broadcast_autonomous(
                    _autonomous_frame({"event": "plan_failed"}, failed_state)
                )
            _release_single_runner_if_gen(my_gen)
            return

        if isinstance(chat_route, dict):
            request_id = str(chat_route.get("request_id") or "")[:160]
            reason_code = str(chat_route.get("reason_code") or "")[:48]
            if request_id:
                st["chat_route_request_id"] = request_id
            if reason_code:
                st["chat_route_reason_code"] = reason_code
            confidence = chat_route.get("confidence")
            if isinstance(confidence, (int, float)) and not isinstance(
                confidence, bool
            ):
                st["chat_route_confidence"] = max(
                    0.0, min(1.0, float(confidence))
                )
            st["chat_route_type"] = "autonomous"
            st["source_conversation_id"] = str(
                chat_route.get("conversation_id") or ""
            )[:160]
            st["source_turn_id"] = str(
                chat_route.get("source_turn_id") or request_id
            )[:160]
            st["source_request_id"] = str(
                chat_route.get("source_request_id") or request_id
            )[:160]
            st["source_permission_epoch"] = int(
                chat_route.get("permission_epoch") or 0
            )
            st["attached_tool_attempt_id"] = str(
                chat_route.get("tool_attempt_id") or ""
            )[:160]

        # ── Establish the run atomically — only if our slot is still current ──
        established = False
        source_binding_failed = False
        with _autonomous_lock:
            if _autonomous.get("active") and int(_autonomous.get("gen", 0)) == my_gen:
                try:
                    if document_inventory:
                        from osia.action_policy import get_default_permission_policy

                        current_epoch = int(
                            get_default_permission_policy().store.load_state().policy_epoch
                        )
                        if (
                            current_epoch != int(document_permission_epoch)
                            or int(st.get("policy_epoch", -1)) != current_epoch
                        ):
                            raise document_sources.DocumentSourceError(
                                "stale_permission_epoch"
                            )
                        bound = document_sources.get_source_store().bind_for_run(
                            [item["source_ref"] for item in document_inventory],
                            owner_id=document_session_owner,
                            run_owner_id=str(st.get("task_id") or ""),
                            run_id=str(st.get("run_id") or ""),
                            permission_epoch=current_epoch,
                        )
                        st["document_sources"] = _astate.normalize_document_sources(
                            bound
                        )
                    _autonomous["st"] = st
                    _autonomous["document_owner_device"] = str(
                        document_owner_device or ""
                    )[:128]
                    _write_autonomous_lock(st.get("task_id"))  # stamp the real id
                    established = True
                except document_sources.DocumentSourceError:
                    source_binding_failed = True
        if source_binding_failed:
            failed_state = _autonomous_failure_state(
                stage=_adiagnostics.AutonomousStage.PLANNING,
                category=_adiagnostics.FailureCategory.INVALID_PLANNING_OUTPUT,
                reason_code="invalid_document_sources",
                provider=provider,
                model=model,
                language=language,
                emit_log=False,
            )
            if _establish_autonomous_failure(failed_state, my_gen):
                _broadcast_autonomous(
                    _autonomous_frame({"event": "plan_failed"}, failed_state)
                )
            _release_single_runner_if_gen(my_gen)
            return
        if not established:
            # A cancel landed during planning (and the cancel handler already
            # released the slot, cleared any checkpoint and broadcast its
            # outcome) — or a newer start reacquired. Do NOT persist or start the
            # run the user cancelled; the slot is correctly owned by neither us
            # nor a stale generation.
            print("[WOR-45] autonomous start cancelled during planning — not starting")
            return

        try:
            _astate.save_task_state(st)
        except Exception:
            diagnostic = _adiagnostics.make_diagnostic(
                st,
                stage=_adiagnostics.AutonomousStage.PERSISTENCE,
                category=_adiagnostics.FailureCategory.CHECKPOINT_FAILURE,
                reason_code="checkpoint_failure",
                provider=provider,
                model=model,
                resumable=False,
                checkpoint_present=False,
            )
            _adiagnostics.record_diagnostic(st, diagnostic)
        if st.get("run_state") != "ready":
            _broadcast_autonomous(_autonomous_frame({
                "type": "autonomous_plan_ready",
                "event": "authorization_pending",
            }, st))
            return
        attached_attempt_id = str(st.get("attached_tool_attempt_id") or "")
        if attached_attempt_id:
            if orch.attach_existing_tool_attempt(st, attached_attempt_id):
                return
            phase_index, task_index, task = _astate.current_task(st)
            if task is not None:
                _astate.set_task_status(
                    st,
                    phase_index,
                    task_index,
                    "blocked_unknown_effect",
                    last_error="suspended_attempt_attachment_unsafe",
                    tool_durable_state="blocked_unknown_effect",
                    tool_terminal_reason="suspended_attempt_attachment_unsafe",
                )
            st["run_state"] = "blocked_unknown_effect"
            st["authorization_status"] = "outcome_unknown"
            _astate.save_task_state(st)
            _broadcast_autonomous(_autonomous_frame({
                "event": "blocked_unknown_effect",
                "reason": "suspended_attempt_attachment_unsafe",
            }, st))
            _release_single_runner_if_gen(my_gen)
            return
        orch.start(st)
        # Flip "planning" → "running" only AFTER start() spawned the loop. The
        # engine emits events only on task transitions, never on start, so this
        # won't double-fire.
        _broadcast_autonomous(_autonomous_frame({"event": "running"}, st))
    except Exception:
        # Any unexpected failure (e.g. OSIAAgent build error, or thread
        # exhaustion in orch.start) must NOT wedge the slot at busy forever:
        # release it (only if still ours) and tell clients the start failed.
        failed_state = _autonomous_failure_state(
            stage=_adiagnostics.AutonomousStage.ORCHESTRATOR,
            category=_adiagnostics.FailureCategory.ORCHESTRATOR_FAILURE,
            reason_code="start_worker_failure",
            language=language,
            emit_log=False,
        )
        if _establish_autonomous_failure(failed_state, my_gen):
            _broadcast_autonomous(
                _autonomous_frame({"event": "plan_failed"}, failed_state)
            )
        _release_single_runner_if_gen(my_gen)
        # ORPHAN-LOOP RACE (WOR-45): a crash BEFORE the drain reopened the gate
        # (e.g. _ensure_autonomous_engine raised) must not leave it stuck — but
        # only reopen if no orphan is alive (else its terminal must stay gated;
        # the interrupt backstop / next drain reopens it once the orphan dies).
        if not _orch_loop_alive():
            _autonomous["interrupting"] = False


def _chat_route_result_from_state(st: dict | None, *, active: bool) -> dict:
    if not isinstance(st, dict):
        return {"status": "failed", "error_code": "start_state_unavailable"}
    base = {
        "task_id": str(st.get("task_id") or "")[:160],
        "run_id": str(st.get("run_id") or "")[:160],
    }
    provider = str(st.get("provider") or "")[:48]
    model = str(st.get("model") or "")[:160]
    if provider and model:
        base.update({"provider_id": provider, "model_id": model})
    run_state = str(st.get("run_state") or "")
    status = str(st.get("status") or "")
    if status in {"failed", "aborted"} or st.get("failure_diagnostic"):
        diagnostic = st.get("failure_diagnostic")
        code = (
            str(diagnostic.get("reason_code") or "")[:80]
            if isinstance(diagnostic, dict) else "start_failed"
        )
        return {**base, "status": "failed", "error_code": code or "start_failed"}
    if run_state == "blocked_outcome_unknown":
        return {**base, "status": "outcome_unknown"}
    if run_state in {
        "waiting_tool", "verifying_tool", "resuming",
        "cancellation_requested",
    }:
        return {**base, "status": "running"}
    if run_state == "blocked_unknown_effect":
        return {**base, "status": "outcome_unknown"}
    if run_state in {
        "authorization_pending",
        "suspended_scope_extension",
        "suspended_authorization_revoked",
    }:
        return {**base, "status": "authorization_pending"}
    if active:
        return {**base, "status": "running"}
    if status == "in_progress":
        return {**base, "status": "outcome_unknown"}
    return {**base, "status": "failed", "error_code": "start_not_active"}


def _remember_chat_route_result(
    request_id: str,
    fingerprint: str,
    result: dict,
) -> dict:
    safe = dict(result)
    with _CHAT_ROUTE_REQUESTS_LOCK:
        _CHAT_ROUTE_REQUESTS[request_id] = {
            "fingerprint": fingerprint,
            "result": safe,
        }
        _CHAT_ROUTE_REQUESTS.move_to_end(request_id)
        while len(_CHAT_ROUTE_REQUESTS) > 256:
            _CHAT_ROUTE_REQUESTS.popitem(last=False)
    return safe


def _launch_autonomous_from_chat(request: dict) -> dict:
    """Synchronously launch one validated chat handoff on the canonical runner."""

    if not isinstance(request, dict):
        return {"status": "failed", "error_code": "invalid_request"}
    objective = str(request.get("objective") or "").strip()
    request_id = str(request.get("request_id") or "").strip()
    source_request_id = str(
        request.get("source_request_id") or request_id
    ).strip()
    source_turn_id = str(request.get("source_turn_id") or request_id).strip()
    conversation_id = str(request.get("conversation_id") or "").strip()
    origin_ref = str(request.get("origin_ref") or "desktop").strip()[:160]
    device_id = str(request.get("device_id") or "").strip()[:128]
    provider = str(request.get("provider_id") or "").strip().lower()
    model = str(request.get("model_id") or "").strip()
    source_refs = request.get("source_refs") or []
    if not provider or not model:
        return {
            "status": "failed",
            "error_code": "chat_model_identity_missing",
        }
    if (
        not objective
        or len(objective) > 16_000
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", request_id)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", source_request_id)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", source_turn_id)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", conversation_id)
        or not re.fullmatch(r"[a-z0-9_-]{1,48}", provider)
        or len(model) > 160
        or not isinstance(source_refs, list)
        or len(source_refs) > 10
        or any(
            not isinstance(ref, str)
            or not re.fullmatch(r"(?:src|doc|web)_[0-9a-f]{32}", ref)
            for ref in source_refs
        )
    ):
        return {"status": "failed", "error_code": "invalid_request"}
    fingerprint = hashlib.sha256(json.dumps(
        {
            "conversation_id": conversation_id,
            "source_request_id": source_request_id,
            "source_turn_id": source_turn_id,
            "objective": objective,
            "provider_id": provider,
            "model_id": model,
            "source_refs": source_refs,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    with _CHAT_ROUTE_REQUESTS_LOCK:
        existing = _CHAT_ROUTE_REQUESTS.get(request_id)
        if isinstance(existing, dict):
            if existing.get("fingerprint") != fingerprint:
                return {
                    "status": "failed",
                    "error_code": "duplicate_request_conflict",
                }
            result = existing.get("result")
            return dict(result) if isinstance(result, dict) else {
                "status": "starting",
            }
        _CHAT_ROUTE_REQUESTS[request_id] = {
            "fingerprint": fingerprint,
            "result": {"status": "starting"},
        }

    # Restart/reconnect idempotency: never create a second mission for a chat
    # request already bound into the persisted Autonomous state.
    with _autonomous_lock:
        current_state = _autonomous.get("st")
        current_active = bool(_autonomous.get("active"))
    if not isinstance(current_state, dict):
        try:
            current_state = _astate.load_task_state()
        except Exception:
            current_state = None
    if (
        isinstance(current_state, dict)
        and current_state.get("chat_route_request_id") == request_id
    ):
        return _remember_chat_route_result(
            request_id,
            fingerprint,
            _chat_route_result_from_state(current_state, active=current_active),
        )

    from osia.mobile_model_catalogue import resolve_current_autonomous_selection

    requested_provider, requested_model = provider, model
    resolution = resolve_current_autonomous_selection(
        provider_id=provider,
        model_id=model,
        refresh_account=True,
        strict=True,
        persist=False,
    )
    provider = str(resolution.get("provider_id") or "")
    model = str(resolution.get("model_id") or "")
    if (
        not provider
        or not model
        or resolution.get("fallback_occurred")
        or provider != requested_provider
        or model != requested_model
    ):
        return _remember_chat_route_result(request_id, fingerprint, {
            "status": "failed",
            "error_code": "chat_model_unavailable",
        })

    try:
        from osia.action_policy import get_default_permission_policy

        permission_epoch = int(
            get_default_permission_policy().store.load_state().policy_epoch
        )
        document_inventory = (
            document_sources.get_source_store().inspect_for_binding(
                source_refs,
                owner_id=conversation_id,
                permission_epoch=permission_epoch,
            )
            if source_refs else []
        )
    except document_sources.DocumentSourceError:
        return _remember_chat_route_result(request_id, fingerprint, {
            "status": "failed",
            "error_code": "invalid_document_sources",
        })

    compatible_attempt = None
    try:
        from osia.tool_attempts import get_tool_attempt_store

        suspended = get_tool_attempt_store().find_compatible_suspended_turn(
            conversation_id
        )
        if isinstance(suspended, dict):
            if (
                str(suspended.get("turn_id") or "") == source_turn_id
                and str(suspended.get("provider_id") or "") == provider
                and str(suspended.get("model_id") or "") == model
            ):
                compatible_attempt = suspended
            else:
                return _remember_chat_route_result(request_id, fingerprint, {
                    "status": "failed",
                    "error_code": "suspended_attempt_attachment_unsafe",
                })
    except Exception:
        return _remember_chat_route_result(request_id, fingerprint, {
            "status": "failed",
            "error_code": "suspended_attempt_attachment_unavailable",
        })

    autonomous_context = request.get("turn_context")
    if not isinstance(autonomous_context, TurnContext):
        autonomous_context = resolve_turn_context(
            objective,
            client_locale=request.get("language"),
            saved_language=i18n.get_language(),
            reference=request_id,
        )
    generation = _acquire_single_runner()
    if generation is None:
        return _remember_chat_route_result(request_id, fingerprint, {
            "status": "busy",
            "error_code": "single_runner_busy",
        })

    _autonomous["origin_ref"] = origin_ref or "desktop"
    _autonomous["document_owner_device"] = device_id
    try:
        _autonomous_start_worker(
            objective,
            model,
            generation,
            autonomous_context,
            origin_ref or "desktop",
            document_inventory,
            conversation_id,
            permission_epoch,
            device_id,
            provider,
            {
                "request_id": request_id,
                "source_request_id": source_request_id,
                "source_turn_id": source_turn_id,
                "conversation_id": conversation_id,
                "permission_epoch": permission_epoch,
                "tool_attempt_id": str(
                    (compatible_attempt or {}).get("tool_attempt_id") or ""
                ),
                "reason_code": str(request.get("reason_code") or "")[:48],
                "confidence": request.get("confidence"),
            },
        )
    except Exception:
        _release_single_runner_if_gen(generation)
        return _remember_chat_route_result(request_id, fingerprint, {
            "status": "failed",
            "error_code": "start_worker_failure",
        })
    with _autonomous_lock:
        state = _autonomous.get("st")
        active = bool(_autonomous.get("active"))
    result = _chat_route_result_from_state(state, active=active)
    return _remember_chat_route_result(request_id, fingerprint, result)


def _orch_loop_alive() -> bool:
    """True iff an orchestrator execution loop is still running IN THIS PROCESS.

    Detected by THREAD NAME — the engine names its loop thread
    ``"osia-orchestrator"`` (orchestrator.py). This is the only externally
    observable handle on it: ``Orchestrator.stop()`` (called by
    ``autonomous_interrupt``) nulls the engine's private ``_thread`` even when its
    2 s join TIMES OUT mid ``agent.run()``, so the old loop can still be alive
    while ``_begin``'s ``is_alive()`` idempotency guard is blind to it. The resume
    worker uses this to avoid EVER spawning a second loop over the same agent/state
    (see WOR-45 race note there). Read-only introspection — ZERO engine edits."""
    return any(getattr(t, "name", "") == "osia-orchestrator" and t.is_alive()
               for t in threading.enumerate())


def _release_after_orphan_drained(my_gen: int, timeout: float = 120.0) -> None:
    """Hold the single-runner slot until a still-draining orphan orchestrator loop
    is truly dead, THEN release it — only if generation ``my_gen`` still owns it.

    Used by the resume guard's "loop still alive after the bounded wait" branch.
    PRIMARY defence: releasing the slot WHILE the orphan is alive would let a newer
    run acquire it and then be stopped+freed by the orphan's LATE terminal event
    (the engine checks ``_stop`` only ONCE per step, so an interrupted step already
    past that check — inside the verifier ``agent.run()`` or a replan — still runs
    through to a terminal ``done``/``failed`` emit). Keeping the slot ``busy`` until
    the orphan drains means a newer run can NEVER coexist with it, so the question
    of attributing that terminal never arises; it also honours the single-runner
    invariant (the orphan is still using the shared agent). Bounded so a wedged
    orphan can never hold the slot forever; runs on a daemon thread so it never
    blocks the event loop. If the orphan instead drains via a terminal event,
    :func:`_finalize_terminal_release` (now generation-gated — the second line of
    defence) already released the slot under our generation, so the ``_if_gen``
    release here is then a safe no-op (a newer generation owns it) or a harmless
    idempotent re-release.

    ORPHAN-LOOP RACE (WOR-45) — this is one of the drain-completion sites, so it
    also CLEARS the ``interrupting`` gate, but ONLY once the orphan is truly DEAD
    (see below): clearing it while the orphan is still alive would let its late
    terminal breach the gate and be broadcast as a newer run's outcome."""
    waited = 0.0
    while _orch_loop_alive() and waited < timeout:
        time.sleep(0.2)
        waited += 0.2
    # Reopen terminals ONLY if the orphan is truly DEAD. If the bound elapsed with
    # it still alive (pathological), leave the gate UP — its late terminal stays
    # gated, and the next start/resume drain (or the orphan's eventual death)
    # reopens it (self-heal). The SLOT release below stays bounded so a wedged
    # orphan can never hold the slot forever; a still-draining orphan still can't
    # spawn a 2nd loop because BOTH start and resume drain before spawning.
    if not _orch_loop_alive():
        _autonomous["interrupting"] = False
    _release_single_runner_if_gen(my_gen)


def _clear_interrupting_after_orphan_drained(timeout: float = 120.0) -> None:
    """Clear the ``interrupting`` gate once a still-draining orphan orchestrator
    loop is dead — WITHOUT releasing the slot (the caller already released it).

    ORPHAN-LOOP RACE (WOR-45) — used by ``autonomous_interrupt`` for the
    NO-RESUME case: a timed-out stop() can leave the old loop draining, so the
    gate must stay set until that loop's late terminal has been observed (and
    dropped). The wait is BOUNDED (daemon thread, never blocks the loop), but the
    flag is cleared ONLY if the orphan is truly DEAD — clearing it while the orphan
    is still alive (bound exceeded) would let its late terminal breach the gate.
    If the bound elapses with the orphan still alive, the gate is left UP; it is a
    harmless stuck-True (there is no live owned run whose terminal it could
    swallow) and the NEXT start/resume drain reopens it once the orphan dies
    (self-heal). Idempotent: the resume worker may clear the same flag concurrently."""
    waited = 0.0
    while _orch_loop_alive() and waited < timeout:
        time.sleep(0.2)
        waited += 0.2
    if not _orch_loop_alive():
        _autonomous["interrupting"] = False


def _autonomous_resume_worker(
    my_gen: int, origin_ref: str = "mobile_legacy",
) -> None:
    """Resume a PAUSED autonomous run OFF the event loop, converging BYTE-FOR-BYTE
    onto the PROVEN GUI resume flow (``main.py``: ``load_task_state()`` → exact
    provider/model validation → provider-aware ``set_model`` →
    ``orchestrator.resume(state)``): resume ONLY
    against a dead/idle engine, against the on-disk state. The single-runner slot
    (generation ``my_gen``) was re-acquired by the handler — interrupt freed it.
    There is NO planning step: the plan already lives in the persisted task_state,
    so resume picks up at the cursor (the engine re-checks the interrupted task's
    success_criterion before re-executing — crash-salvage).

    ORPHAN-LOOP RACE FIX (WOR-45). A timed-out ``autonomous_interrupt`` stop()
    can leave the old orchestrator loop alive (the engine never checks its stop
    flag inside ``_verify``/``_llm_judge``/``_replan``); the GUI never hits this
    because it resumes on a FRESH process where no loop exists. So we make the
    server path the same by DRAINING the orphan first:

    * Fix #1 — DRAIN BEFORE the resumable generation acts. Before any
      generation-sensitive establish/resume, wait (GENEROUS bound, not the old
      3 s cap) until no live ``osia-orchestrator`` thread remains. This
      guarantees the orphan's terminal is observed (and dropped by the
      ``interrupting`` gate in :func:`_on_autonomous_progress`) while the loop is
      unambiguously its own — never a SECOND concurrent loop on the shared agent.
      If the orphan is wedged past the bound, step back: report ``interrupted``
      (resumable, as ``get_autonomous_status`` reports) and defer the slot
      release until it truly dies (:func:`_release_after_orphan_drained`).
    * Fix #2 — re-read the (mobile-isolated) task state FROM DISK and branch on
      its REAL status, mirroring the GUI exactly: ``in_progress`` ⇒
      ``orch.resume(st)`` (the proven idle-engine + disk-state path); a TERMINAL
      status (done/failed/aborted) ⇒ surface it TRUTHFULLY (broadcast it) and do
      NOT start a loop. This is the orphan's genuine result when it really ran the
      task to completion before draining — NOT the mis-attributed terminal the
      old code wrongly broadcast onto the just-resumed run.

    Hardening, exactly like the start worker:

    * Wrap the WHOLE body so ANY failure RELEASES the slot (only if generation
      ``my_gen`` is still current) — a raise must never wedge ``active`` True
      forever and refuse every later start/resume at ``busy``.
    * ESTABLISH atomically, only if generation ``my_gen`` is still current — a
      newer start/resume that reacquired during this window must not be stomped
      (the same load-bearing gate the start worker uses)."""
    try:
        agent, orch = _ensure_autonomous_engine()

        # ── Fix #1: DRAIN the orphan BEFORE the resumable generation acts ──
        # Resume must run only against a dead/idle engine (the GUI flow). Wait,
        # with a GENEROUS bound, until no orchestrator loop remains in-process so
        # the orphan's late terminal lands while the loop is still unambiguously
        # its own (and is dropped by the interrupting gate) — never a 2nd loop.
        waited = 0.0
        while _orch_loop_alive() and waited < 120.0:
            time.sleep(0.2)
            waited += 0.2
        if _orch_loop_alive():
            # Orphan wedged past the drain bound — do NOT spawn a second loop.
            # Report INTERRUPTED (the run stays paused/resumable, matching
            # get_autonomous_status) and release our slot only AFTER the orphan is
            # truly dead: freeing it now would let a newer run acquire the slot and
            # then be stopped/freed by the orphan's late terminal. That poller also
            # clears the interrupting gate when the drain completes.
            print("[WOR-45] autonomous resume: orchestrator loop wedged past the "
                  "drain bound — reporting interrupted; deferring slot release "
                  "until it exits")
            _broadcast_autonomous(
                _autonomous_frame({"event": "interrupted"}, _autonomous.get("st")))
            threading.Thread(
                target=_release_after_orphan_drained,
                args=(my_gen,),
                daemon=True,
            ).start()
            return

        # ── Orphan drained: the engine is dead/idle. This is a drain-completion
        # site, so reopen terminals (clear the interrupting gate) — the resumed
        # run's OWN terminals must broadcast normally. Safe here: no loop is alive
        # (orphan dead, resume not started), so no terminal can fire in between. ──
        _autonomous["interrupting"] = False

        # ── Fix #2: re-read the persisted (mobile-isolated) state from DISK and
        # branch on its REAL status — mirror the GUI's _offer_resume_if_pending. ──
        try:
            st = _astate.load_task_state()
        except Exception:
            st = None
        if not isinstance(st, dict):
            failed_state = _autonomous_failure_state(
                stage=_adiagnostics.AutonomousStage.RESUME,
                category=_adiagnostics.FailureCategory.INVALID_RESUME_STATE,
                reason_code="resume_state_unavailable",
                emit_log=False,
            )
            if _establish_autonomous_failure(failed_state, my_gen):
                _broadcast_autonomous(
                    _autonomous_frame({"event": "idle"}, failed_state)
                )
            _release_single_runner_if_gen(my_gen)
            return
        _aauthorization.revalidate_loaded_authorization(
            st, origin_ref=origin_ref,
        )
        _astate.save_task_state(st)
        if st.get("run_state") in {
            "authorization_pending",
            "suspended_scope_extension",
            "suspended_authorization_revoked",
            "blocked_outcome_unknown",
        }:
            established = False
            with _autonomous_lock:
                if (
                    _autonomous.get("active")
                    and int(_autonomous.get("gen", 0)) == my_gen
                ):
                    _autonomous["st"] = st
                    _write_autonomous_lock(st.get("task_id"))
                    established = True
            if established:
                pending_review = st.get("run_state") == "authorization_pending"
                _broadcast_autonomous(_autonomous_frame({
                    "type": (
                        "autonomous_plan_ready" if pending_review
                        else "autonomous_authorization_state"
                    ),
                    "event": (
                        "authorization_pending" if pending_review
                        else st.get("run_state")
                    ),
                }, st))
            return
        status = st.get("status", "")
        if status == "in_progress" and not _astate.resume_state_is_valid(st):
            st["status"] = "failed"
            diagnostic = _adiagnostics.make_diagnostic(
                st,
                stage=_adiagnostics.AutonomousStage.RESUME,
                category=_adiagnostics.FailureCategory.INVALID_RESUME_STATE,
                reason_code="invalid_resume_state",
                resumable=False,
                checkpoint_present=True,
            )
            _adiagnostics.record_diagnostic(st, diagnostic, emit=False)
            if _establish_autonomous_failure(st, my_gen):
                _broadcast_autonomous(
                    _autonomous_frame({"event": "failed"}, st)
                )
            _release_single_runner_if_gen(my_gen)
            return
        if status != "in_progress":
            # Disk shows a TERMINAL outcome — surface it truthfully and start NO
            # loop (the GUI only resumes an in_progress run). When the drained
            # orphan genuinely failed/finished the task, this is its REAL result.
            ev = {"done": "done", "failed": "failed",
                  "aborted": "aborted"}.get(status, "idle")
            _broadcast_autonomous(_autonomous_frame({"event": ev}, st))
            _release_single_runner_if_gen(my_gen)
            return

        # ── status == in_progress ⇒ resume EXACTLY like the GUI ──
        resume_context = fixed_turn_context(
            st.get("language"),
            source="autonomous_resume",
            reference=str(st.get("task_id") or "autonomous"),
            mode=str(st.get("language_mode") or "single-language"),
            allowed_languages=st.get("allowed_languages") or (),
            source_language=st.get("source_language"),
            target_language=st.get("target_language"),
        )
        agent.set_turn_context(resume_context)
        from osia.mobile_model_catalogue import (
            apply_autonomous_agent_model,
            resolve_current_autonomous_selection,
        )
        resolution = resolve_current_autonomous_selection(
            provider_id=st.get("provider", ""),
            model_id=st.get("model", ""),
            refresh_account=True,
            strict=True,
            persist=False,
        )
        provider = resolution.get("provider_id") or ""
        model = resolution.get("model_id") or ""
        durable_tool_resume = st.get("run_state") in {
            "waiting_tool", "verifying_tool", "resuming",
            "cancellation_requested",
        }
        exact_recovery_identity = (
            provider == str(st.get("provider") or "")
            and model == str(st.get("model") or "")
            and not resolution.get("fallback_occurred")
        )
        if durable_tool_resume and not exact_recovery_identity:
            established = False
            with _autonomous_lock:
                if (
                    _autonomous.get("active")
                    and int(_autonomous.get("gen", 0)) == my_gen
                ):
                    _autonomous["st"] = st
                    _write_autonomous_lock(st.get("task_id"))
                    established = True
            if established:
                orch.block_tool_recovery(
                    st, "tool_recovery_model_mismatch",
                )
            return
        if resolution.get("fallback_occurred"):
            print(
                "[WOR-182] Mobile Autonomous resume model fallback: "
                f"{resolution.get('reason') or 'unavailable'}"
            )
        if not provider or not model:
            st["status"] = "failed"
            diagnostic = _adiagnostics.make_diagnostic(
                st,
                stage=_adiagnostics.AutonomousStage.RESUME,
                category=_adiagnostics.FailureCategory.PLANNING_PROVIDER_FAILURE,
                reason_code="no_configured_model",
                resumable=False,
                checkpoint_present=True,
            )
            _adiagnostics.record_diagnostic(st, diagnostic)
            try:
                _astate.save_task_state(st)
            except Exception:
                _adiagnostics.set_checkpoint_present(st, False)
            if _establish_autonomous_failure(st, my_gen):
                _broadcast_autonomous(
                    _autonomous_frame({"event": "failed"}, st)
                )
            _release_single_runner_if_gen(my_gen)
            return
        if model:
            try:
                apply_autonomous_agent_model(agent, provider, model)
            except Exception:
                st["status"] = "failed"
                diagnostic = _adiagnostics.make_diagnostic(
                    st,
                    stage=_adiagnostics.AutonomousStage.RESUME,
                    category=(
                        _adiagnostics.FailureCategory.PLANNING_PROVIDER_FAILURE
                    ),
                    reason_code="model_setup_failure",
                    provider=provider,
                    model=model,
                    resumable=False,
                    checkpoint_present=True,
                )
                _adiagnostics.record_diagnostic(st, diagnostic)
                try:
                    _astate.save_task_state(st)
                except Exception:
                    _adiagnostics.set_checkpoint_present(st, False)
                if _establish_autonomous_failure(st, my_gen):
                    _broadcast_autonomous(
                        _autonomous_frame({"event": "failed"}, st)
                    )
                _release_single_runner_if_gen(my_gen)
                return
        provider_now, model_now = _adiagnostics.actual_provider_model(
            agent, provider, model,
        )
        if durable_tool_resume and (
            provider_now != str(st.get("provider") or "")
            or model_now != str(st.get("model") or "")
        ):
            established = False
            with _autonomous_lock:
                if (
                    _autonomous.get("active")
                    and int(_autonomous.get("gen", 0)) == my_gen
                ):
                    _autonomous["st"] = st
                    _write_autonomous_lock(st.get("task_id"))
                    established = True
            if established:
                orch.block_tool_recovery(
                    st, "tool_recovery_model_mismatch",
                )
            return
        st["provider"] = provider_now
        st["model"] = model_now
        try:
            _astate.save_task_state(st)
        except Exception:
            _adiagnostics.set_checkpoint_present(st, False)

        # ── Establish the run atomically — only if our slot is still current ──
        established = False
        with _autonomous_lock:
            if _autonomous.get("active") and int(_autonomous.get("gen", 0)) == my_gen:
                _autonomous["st"] = st
                _write_autonomous_lock(st.get("task_id"))  # stamp the real id
                established = True
        if not established:
            # A cancel landed during this window (its handler already released
            # the slot and broadcast its outcome) or a newer start/resume
            # reacquired — do NOT resume over the run the user cancelled / a
            # newer run; the slot is owned by neither us nor a stale generation.
            print("[WOR-45] autonomous resume superseded/cancelled — not resuming")
            return

        orch.resume(st)
        # Flip to "running" only AFTER resume() spawned the loop. The engine emits
        # events only on task transitions, never on resume, so this won't double-fire.
        _broadcast_autonomous(_autonomous_frame({"event": "running"}, st))
    except Exception:
        # Any unexpected failure (engine build, orch.resume thread exhaustion)
        # must NOT wedge the slot at busy forever: release it (only if still ours)
        # and tell clients the resume did not take.
        failed_state = _autonomous_failure_state(
            stage=_adiagnostics.AutonomousStage.RESUME,
            category=_adiagnostics.FailureCategory.ORCHESTRATOR_FAILURE,
            reason_code="resume_worker_failure",
            emit_log=False,
        )
        if _establish_autonomous_failure(failed_state, my_gen):
            _broadcast_autonomous(
                _autonomous_frame({"event": "plan_failed"}, failed_state)
            )
        _release_single_runner_if_gen(my_gen)
        # ORPHAN-LOOP RACE (WOR-45): a crash BEFORE the drain reopened the gate
        # (engine build / disk load) must not leave it stuck. Reopen only if no
        # orphan is alive; otherwise the interrupt backstop / next drain reopens
        # it once the orphan dies (its terminal must stay gated until then).
        if not _orch_loop_alive():
            _autonomous["interrupting"] = False


_DURABLE_AUTONOMOUS_RECOVERY_STATES = frozenset({
    "waiting_tool", "verifying_tool", "resuming", "cancellation_requested",
})


def _schedule_durable_autonomous_recovery(
    st: dict,
    *,
    origin_ref: str,
    loop,
    document_owner_device: str = "",
) -> bool:
    """Resume a persisted in-flight tool checkpoint once on fresh-process status."""

    if (
        not isinstance(st, dict)
        or st.get("status") != "in_progress"
        or st.get("run_state") not in _DURABLE_AUTONOMOUS_RECOVERY_STATES
    ):
        return False
    generation = _acquire_single_runner(task_id=st.get("task_id"))
    if generation is None:
        return False
    _autonomous["loop"] = loop
    _autonomous["origin_ref"] = origin_ref
    _autonomous["document_owner_device"] = document_owner_device
    threading.Thread(
        target=_autonomous_resume_worker,
        args=(generation, origin_ref),
        daemon=True,
    ).start()
    return True


def _autonomous_pc_snapshot(st: dict | None) -> dict:
    """Local-PC projection for reviewing the server-owned mobile run."""

    if not isinstance(st, dict):
        return {}
    checkpoint = _astate._checkpoint_projection(st)
    pc = _aauthorization.pc_projection(st)
    checkpoint.update(pc)
    checkpoint["_runtime_pc_capability_details"] = dict(
        pc.get("private_scope_details") or {}
    )
    checkpoint["_server_mobile_run"] = True
    return checkpoint


def _current_autonomous_state() -> dict | None:
    """Return the in-memory run, recovering its persisted checkpoint if needed."""

    st = _autonomous.get("st")
    if isinstance(st, dict):
        return st
    try:
        loaded = _astate.load_task_state()
    except Exception:
        loaded = None
    if not isinstance(loaded, dict):
        return None
    _aauthorization.restore_planned_runtime_descriptors(loaded)
    # A relay reconnect can recreate the canonical mobile checkpoint before the
    # local PC endpoint observes the process-owned in-memory reference. Publish
    # that same dict atomically; never replace a run established meanwhile.
    with _autonomous_lock:
        current = _autonomous.get("st")
        if isinstance(current, dict):
            return current
        _autonomous["st"] = loaded
        return loaded


def _internal_autonomous_origin(st: dict) -> str:
    request = st.get("authorization_request") or st.get(
        "pending_scope_extension"
    )
    if not isinstance(request, dict):
        return ""
    # The loopback-only endpoint is the authenticated PC authority. Atomically
    # transfer this still-pending request away from its mobile observer before
    # validating the local decision; any stale mobile response then fails its
    # origin binding.
    request["origin_ref"] = "desktop"
    return "desktop"


def _apply_internal_autonomous_model(agent, st: dict) -> bool:
    """Restore a persisted mobile run's model before local-PC execution."""

    saved_provider = str(st.get("provider") or "").strip()
    saved_model = str(st.get("model") or "").strip()
    if not saved_provider or not saved_model:
        # Compatibility for old checkpoints that predate the persisted pair.
        return True
    try:
        from osia.mobile_model_catalogue import (
            apply_autonomous_agent_model,
            resolve_current_autonomous_selection,
        )

        resolution = resolve_current_autonomous_selection(
            provider_id=saved_provider,
            model_id=saved_model,
            refresh_account=True,
            strict=True,
            persist=False,
        )
        provider = resolution.get("provider_id") or ""
        model = resolution.get("model_id") or ""
        if resolution.get("fallback_occurred"):
            print(
                "[WOR-188] Autonomous authorization model fallback: "
                f"{resolution.get('reason') or 'unavailable'}"
            )
        if not provider or not model:
            return False
        apply_autonomous_agent_model(agent, provider, model)
        return True
    except Exception:
        return False


@app.post("/internal/chat_autonomous_start")
async def internal_chat_autonomous_start(request: Request):
    """Loopback-only PC handoff into the canonical server-owned runner."""

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"status": "failed", "error_code": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return {"status": "failed", "error_code": "invalid_request"}
    allowed = {
        "objective", "source_refs", "request_id", "conversation_id",
        "source_request_id", "source_turn_id",
        "provider_id", "model_id", "reason_code", "confidence",
        "explicit_request", "language",
    }
    if set(body) - allowed:
        return {"status": "failed", "error_code": "invalid_request"}
    payload = {key: body.get(key) for key in allowed}
    payload["origin_ref"] = "desktop"
    payload["device_id"] = ""
    _autonomous["loop"] = asyncio.get_running_loop()
    return await asyncio.to_thread(_launch_autonomous_from_chat, payload)


@app.get("/internal/autonomous_authorization_state")
async def internal_autonomous_authorization_state(request: Request):
    """Expose the canonical mobile run only to the local PC review panel."""

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    return {
        "ok": True,
        "state": _autonomous_pc_snapshot(_current_autonomous_state()),
    }


@app.post("/internal/autonomous_authorization_action")
async def internal_autonomous_authorization_action(request: Request):
    """Apply one explicit local-PC decision to the canonical mobile run."""

    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "forbidden"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = str(body.get("action") or "") if isinstance(body, dict) else ""
    if action not in {
        "authorize", "scope_approve", "scope_deny", "revoke", "cancel",
    }:
        return {"ok": False, "error": "invalid_action"}
    st = _current_autonomous_state()
    if not isinstance(st, dict):
        return {"ok": False, "error": "run_missing"}
    try:
        prepared_engine = None
        if action in {"authorize", "scope_approve"}:
            prepared_engine = _ensure_autonomous_engine()
            if not _apply_internal_autonomous_model(prepared_engine[0], st):
                return {
                    "ok": False,
                    "error": "model_setup_failure",
                    "state": _autonomous_pc_snapshot(st),
                }
        if action == "authorize":
            origin_ref = _internal_autonomous_origin(st)
            response = _aauthorization.response_for_current_request(
                st, action="approve", origin_ref=origin_ref,
            )
            _aauthorization.authorize_and_start(
                st, response, origin_ref=origin_ref,
            )
            _astate.save_task_state(st)
            _agent, orch = prepared_engine
            orch.start(st)
            event = "running"
        elif action in {"scope_approve", "scope_deny"}:
            origin_ref = _internal_autonomous_origin(st)
            response_action = (
                "approve_exact" if action == "scope_approve" else "deny"
            )
            response = _aauthorization.response_for_current_request(
                st, action=response_action, origin_ref=origin_ref,
            )
            _aauthorization.approve_scope_extension(
                st, response, origin_ref=origin_ref,
            )
            _astate.save_task_state(st)
            if action == "scope_approve":
                _agent, orch = prepared_engine
                orch.resume(st)
                event = "running"
            else:
                event = "scope_extension_denied"
        elif action == "revoke":
            _aauthorization.revoke_run_authorization(
                st, save=_astate.save_task_state,
            )
            orch = _autonomous.get("orch")
            if orch is not None:
                await asyncio.to_thread(orch.stop)
            _astate.save_task_state(st)
            event = "suspended_authorization_revoked"
        else:
            run_state = str(st.get("run_state") or "")
            origin_ref = _internal_autonomous_origin(st)
            pending_request = (
                st.get("authorization_request")
                if run_state == "authorization_pending"
                else st.get("pending_scope_extension")
            )
            already_responded = bool(
                isinstance(pending_request, dict)
                and pending_request.get("responded") is True
            )
            if run_state == "authorization_pending" and not already_responded:
                response = _aauthorization.response_for_current_request(
                    st, action="cancel", origin_ref=origin_ref,
                )
                _aauthorization.authorize_and_start(
                    st, response, origin_ref=origin_ref,
                )
            elif run_state == "suspended_scope_extension" and not already_responded:
                response = _aauthorization.response_for_current_request(
                    st, action="cancel", origin_ref=origin_ref,
                )
                _aauthorization.approve_scope_extension(
                    st, response, origin_ref=origin_ref,
                )
            _aauthorization.revoke_run_authorization(
                st, save=_astate.save_task_state,
            )
            st["status"] = "aborted"
            st["run_state"] = "cancelled"
            orch = _autonomous.get("orch")
            if orch is not None:
                await asyncio.to_thread(orch.stop)
            _astate.clear_task_state()
            _release_single_runner()
            event = "aborted"
    except _aauthorization.AuthorizationError as exc:
        return {
            "ok": False,
            "error": exc.code,
            "state": _autonomous_pc_snapshot(st),
        }
    except Exception:
        return {
            "ok": False,
            "error": "authorization_internal_failure",
            "state": _autonomous_pc_snapshot(st),
        }
    frame = _autonomous_frame({"event": event}, st)
    _broadcast_autonomous(frame)
    return {
        "ok": True,
        "event": event,
        "state": _autonomous_pc_snapshot(st),
    }


@app.on_event("shutdown")
async def _stop_autonomous_on_shutdown() -> None:
    """Server cleanup: stop the autonomous loop (safe if never started) and
    release the single-runner slot so a restart isn't wrongly seen as busy."""
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
    orch = _autonomous.get("orch")
    if orch is not None:
        try:
            orch.stop()
        except Exception:
            print("[WOR-45] orchestrator.stop on shutdown failed")
    _release_single_runner()
    media_tasks = list(_POST_TURN_MEDIA_TASKS)
    for task in media_tasks:
        task.cancel()
    if media_tasks:
        await asyncio.gather(*media_tasks, return_exceptions=True)
    _POST_TURN_MEDIA_TASKS.clear()
    _POST_TURN_MEDIA_BY_CONV.clear()


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
    # WOR-44 Phase 5 (5-conf-c): bind THIS connection's outbound channel + loop to
    # the turn-target ContextVar so a write/destructive MCP tool's confirmation
    # provider (_ws_confirm_provider) routes its prompt to THIS originating client.
    # Set ONCE per connection, in the endpoint's own context: every turn task
    # (asyncio.create_task) copies this context, and it propagates onward through
    # asyncio.to_thread (agent run) and the tool executor's copy_context, so the
    # worker-thread provider reads the right (channel, loop). Each connection has
    # its own context, so a confirm never reaches the wrong device.
    _turn_target.set((out, asyncio.get_running_loop()))
    loop = asyncio.get_running_loop()
    from osia.action_policy import get_default_permission_policy

    permission_session = _PermissionMobileSession(
        out,
        loop,
        get_default_permission_policy().store,
    )
    unsubscribe_scheduled_tasks = None
    unsubscribe_api_key_changes = None
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
    legacy_uploads = LegacyUploadManager(UPLOAD_DIR)
    attachment_batches = AttachmentBatchManager(ATTACHMENT_UPLOAD_DIR)
    autonomous_attachment_acks: dict[str, dict] = {}

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
    catalogue_change_again = False
    account_change_again = False

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

        if not authenticated or provider_id not in API_PROVIDERS:
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
        from osia.config import add_api_key_change_listener
        from osia.chatgpt_account.provider import add_account_change_listener

        unsubscribe_api_key_changes = add_api_key_change_listener(_api_key_changed)
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
    # One OSIAAgent per CONVERSATION this connection has touched, reused across
    # that conversation's (serialized) turns instead of rebuilt — with its
    # SSL/certifi OpenAI client — on every message. Keyed by conv_id because
    # turns on the SAME conversation are serialized (see _run_turn), so a
    # per-conversation agent never has concurrent turns, while turns on
    # DIFFERENT conversations of this one connection CAN overlap (a backgrounded
    # stream): giving them distinct agents prevents a cross-thread race on
    # self.messages/self.client. The cache is local to this handler, so two
    # connections (users) NEVER share an agent. LRU-bounded here, disposed in
    # the finally block.
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
        if agent is not None:
            conn_agents.move_to_end(conv_id)
            return agent
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

    async def _ensure_active_conv(seed_text):
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
            conv = conversations.create_conversation(title=seed_title)
        except Exception as e:
            print(
                "[OSIA Server] create_conversation failed: "
                f"{type(e).__name__}"
            )
            await out.send_json({"type": "status", "content": "error"})
            return None
        active_conv_id = conv["id"]
        session_history = []
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
            items = conversations.list_conversations()
        except Exception as e:
            print(f"[OSIA Server] list_conversations() failed post-create: {e}")
            items = []
        await out.send_json({
            "type": "conversations",
            "items": items,
        })
        return active_conv_id

    async def _resolve_turn_conversation(msg: dict, seed_text):
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
        return await _ensure_active_conv(seed_text)

    async def _run_turn(conv_id, prev, make_coro):
        nonlocal session_history, active_conv_id
        # Serialise turns on the SAME conversation: wait for an earlier
        # in-flight turn to finish so this one sees its result and the two
        # never append to that conversation concurrently.
        if prev is not None and not prev.done():
            try:
                await prev
            except Exception:
                pass
        # Snapshot the working history for THIS conversation right before the
        # turn runs — independent of whatever conversation is being viewed.
        if active_conv_id == conv_id:
            snapshot = list(session_history)
        else:
            conv = conversations.get_conversation(conv_id)
            snapshot = list(conv.get("messages") or []) if conv else []
        try:
            new_history, result_conv_id = await make_coro(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[OSIA Server] streaming turn failed: {e}")
            traceback.print_exc()
            return
        # Keep the in-memory pointer coherent ONLY when the user is still
        # viewing this conversation. Otherwise the new messages live on disk
        # and are reloaded on demand — the currently-viewed conversation is
        # never clobbered by a background stream completing.
        if result_conv_id is not None and active_conv_id == result_conv_id:
            session_history = new_history

    def _spawn_turn(conv_id, make_coro, turn_meta: dict | None = None):
        # conv_id is always a real id here (resolved via _ensure_active_conv
        # before spawning), so the task is keyed by it: a follow-up turn on
        # the same conversation is rejected before this function is called, and
        # delete/disconnect can find and cancel it.
        prev = streaming_tasks.get(conv_id)
        task = asyncio.create_task(_run_turn(conv_id, prev, make_coro))
        if not _claim_conversation_turn(conv_id, task, turn_meta):
            task.cancel()
            return False
        streaming_tasks[conv_id] = task
        if isinstance(turn_meta, dict):
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
        else:
            asyncio.create_task(
                _broadcast_conversation_busy_changed(conv_id, True, turn_meta)
            )

        def _clear(t):
            if streaming_tasks.get(conv_id) is t:
                streaming_tasks.pop(conv_id, None)
            meta = _ACTIVE_TURN_META_BY_CONV.get(conv_id)
            if _clear_conversation_turn(conv_id, t):
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
    try:
        async for raw_msg in ws.iter_text():
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
            correlated_autonomous_abandon = False

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
                    # Browser work can originate from ordinary Chat as well as
                    # Autonomous. Capture the authenticated server loop and
                    # install the value-free progress bridge on first auth.
                    _autonomous["loop"] = asyncio.get_running_loop()
                    try:
                        from osia.tools.browser import set_browser_progress_callback
                        set_browser_progress_callback(_on_browser_progress)
                    except Exception:
                        pass
                    permission_session.bind_device(msg.get("device_id"))
                    permission_session.activate()
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
                    await ws.send_json({
                        "type": "autonomous_authorization_context",
                        "protocol_version": (
                            _aauthorization.AUTHORIZATION_PROTOCOL_VERSION
                        ),
                        "origin_ref": authorization_origin_ref,
                    })
                    await ws.send_json({
                        "type": "scheduled_task_authorization_context",
                        "protocol_version": 1,
                        "origin_ref": authorization_origin_ref,
                    })
                    print("[WS] auth OK, sending: status/done")
                    try:
                        items = conversations.list_conversations()
                    except Exception as e:
                        print(
                            "[WS] list_conversations() failed during initial "
                            f"send, falling back to empty list: {e!r}"
                        )
                        traceback.print_exc()
                        items = []
                    await ws.send_json({
                        "type": "conversations",
                        "items": items,
                    })
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

            if msg_type == "get_permission_snapshot":
                if msg.get("schema_version") != 1:
                    continue
                if not permission_session.bind_device(msg.get("device_id")):
                    continue
                pending_ids = msg.get("pending_request_ids", [])
                if not isinstance(pending_ids, list) or any(
                    not isinstance(item, str) for item in pending_ids
                ):
                    pending_ids = []
                await permission_session.send_snapshot(tuple(pending_ids[:64]))
                continue

            if msg_type == "get_browser_status":
                if msg.get("schema_version") != 1:
                    continue
                await out.send_json(_browser_progress_frame(snapshot=True))
                continue

            if msg_type == "get_document_status":
                if msg.get("schema_version") != 1:
                    continue
                if not permission_session.bind_device(msg.get("device_id")):
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
                        owner_device=permission_session.device_id,
                    ):
                        await out.send_json(frame)
                continue

            if msg_type == "permission_revocation_request":
                await permission_session.handle_revocation(msg)
                continue

            # --- Conversation management ---
            if msg_type == "list_conversations":
                await out.send_json({
                    "type": "conversations",
                    "items": conversations.list_conversations(),
                })
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
                await out.send_json({
                    "type": "conversations",
                    "items": conversations.list_conversations(),
                })
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
                await out.send_json({
                    "type": "conversations",
                    "items": conversations.list_conversations(),
                })
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
                conv_id = msg.get("id", "")
                # Cancel an in-flight stream for this conversation (if any) so
                # it stops persisting to a file we're about to remove.
                doomed = streaming_tasks.pop(conv_id, None)
                if doomed is None:
                    doomed = _ACTIVE_TURNS_BY_CONV.get(conv_id)
                if doomed is not None:
                    doomed.cancel()
                media_task = _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
                if media_task is not None:
                    media_task.cancel()
                # Drop this conversation's reused agent (perf fix #3) so the
                # deleted conv can't pin a slot in the LRU cache for the rest of
                # the connection. Its random UUID is never reused, so nothing
                # will look it up again. Close the client only when idle — if a
                # just-cancelled turn's uncancellable worker thread may still be
                # using it (_in_use), leave the close to GC once that thread ends.
                dead_agent = conn_agents.pop(conv_id, None)
                if dead_agent is not None and not getattr(dead_agent, "_in_use", False):
                    _close_agent_client(dead_agent)
                ok = conversations.delete_conversation(conv_id)
                if active_conv_id == conv_id:
                    active_conv_id = None
                    session_history = []
                await out.send_json({
                    "type": "conversation_deleted",
                    "id": conv_id,
                    "success": ok,
                })
                if ok:
                    await _broadcast_canonical_conversation_change(
                        conv_id,
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
                await out.send_json({
                    "type": "conversations",
                    "items": conversations.list_conversations(),
                })
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

            # --- Autonomous agent (WOR-45 Phase 5) ---
            # Start / interrupt / abort / observe ONE machine-global autonomous
            # run over this WebSocket. The engine (planner/orchestrator/state) is
            # consumed as a black box; progress frames are BROADCAST to every
            # connected client (the run is machine-global, not per-connection).
            if msg_type == "autonomous_release_sources":
                raw_refs = msg.get("document_sources")
                owner_id = _mobile_document_owner(permission_session.device_id)
                released = False
                if owner_id and isinstance(raw_refs, list) and len(raw_refs) <= 10:
                    try:
                        document_sources.get_source_store().release_owned(
                            raw_refs, owner_id=owner_id,
                        )
                        released = True
                    except document_sources.DocumentSourceError:
                        released = False
                await out.send_json({
                    "type": "autonomous_sources_released",
                    "schema_version": 1,
                    "released": released,
                })
                continue

            if msg_type == "autonomous_start":
                objective = (msg.get("objective") or "").strip()
                model = (msg.get("model") or "").strip()
                autonomous_context = resolve_turn_context(
                    objective,
                    client_locale=msg.get("locale"),
                    saved_language=i18n.get_language(),
                    reference=_new_turn_id(),
                )
                if not objective:
                    failed_state = _autonomous_failure_state(
                        stage=_adiagnostics.AutonomousStage.PLANNING,
                        category=(
                            _adiagnostics.FailureCategory.INVALID_PLANNING_OUTPUT
                        ),
                        reason_code="blank_objective",
                        language=autonomous_context.language,
                    )
                    # Malformed request from this client — reject it directly.
                    await out.send_json(
                        _autonomous_frame({"event": "plan_failed"}, failed_state)
                    )
                    continue
                raw_document_sources = msg.get("document_sources", [])
                document_owner = _mobile_document_owner(
                    permission_session.device_id
                )
                document_epoch = int(
                    permission_session.store.load_state().policy_epoch
                )
                try:
                    if (
                        not isinstance(raw_document_sources, list)
                        or len(raw_document_sources) > 10
                        or any(
                            not isinstance(item, str)
                            for item in raw_document_sources
                        )
                        or (raw_document_sources and not document_owner)
                    ):
                        raise document_sources.DocumentSourceError(
                            "invalid_source_reference"
                        )
                    document_inventory = (
                        document_sources.get_source_store().inspect_for_binding(
                            raw_document_sources,
                            owner_id=document_owner,
                            permission_epoch=document_epoch,
                        )
                        if raw_document_sources else []
                    )
                except document_sources.DocumentSourceError:
                    failed_state = _autonomous_failure_state(
                        stage=_adiagnostics.AutonomousStage.PLANNING,
                        category=(
                            _adiagnostics.FailureCategory.INVALID_PLANNING_OUTPUT
                        ),
                        reason_code="invalid_document_sources",
                        language=autonomous_context.language,
                    )
                    await out.send_json(
                        _autonomous_frame({"event": "plan_failed"}, failed_state)
                    )
                    continue
                # Acquire the single-runner slot BEFORE spending LLM tokens on
                # planning; refuse (busy) if another run already owns it. The
                # returned generation lets the worker detect a cancel-then-
                # reacquire during the (long) planning window.
                _gen = _acquire_single_runner()
                if _gen is None:
                    await out.send_json(_autonomous_frame({"event": "busy"}))
                    continue
                # Capture THIS event loop so the orchestrator's daemon thread can
                # hop progress frames back onto each connection's writer.
                _autonomous["loop"] = asyncio.get_running_loop()
                _autonomous["origin_ref"] = authorization_origin_ref
                _autonomous["document_owner_device"] = permission_session.device_id
                # Plan + start OFF the event loop (planning blocks on an LLM run).
                threading.Thread(
                    target=_autonomous_start_worker,
                    args=(
                        objective, model, _gen, autonomous_context,
                        authorization_origin_ref,
                        document_inventory,
                        document_owner,
                        document_epoch,
                        permission_session.device_id,
                    ),
                    daemon=True,
                ).start()
                continue

            if msg_type == "scheduled_task_authorization_request":
                from osia import scheduled_tasks

                result = scheduled_tasks.begin_scheduled_task_authorization(
                    str(msg.get("task_id") or ""),
                    origin_ref=authorization_origin_ref,
                    mobile=True,
                    ttl_seconds=msg.get("ttl_seconds", 30 * 24 * 60 * 60),
                    max_executions=msg.get("max_executions"),
                )
                if result.get("ok") and isinstance(result.get("request"), dict):
                    await out.send_json(result["request"])
                else:
                    await out.send_json({
                        "type": "scheduled_task_authorization_state",
                        "task_id": str(msg.get("task_id") or ""),
                        "ok": bool(result.get("ok")),
                        "code": str(result.get("code") or ""),
                        "authorization_status": (
                            "pc_local_review_required"
                            if result.get("code") == "pc_local_review_required"
                            else "authorization_required"
                        ),
                    })
                continue

            if msg_type == "scheduled_task_authorization_response":
                from osia import scheduled_tasks

                response = {
                    key: msg.get(key) for key in (
                        "protocol_version", "request_id", "task_id",
                        "task_revision", "definition_hash",
                        "authorization_revision", "policy_epoch", "expiration",
                        "origin_ref", "action",
                    )
                }
                result = scheduled_tasks.respond_scheduled_task_authorization(
                    response,
                    authenticated_origin=authorization_origin_ref,
                )
                await out.send_json({
                    "type": "scheduled_task_authorization_state",
                    "task_id": str(msg.get("task_id") or ""),
                    **result,
                })
                continue

            if msg_type in {
                "scheduled_task_authorization_revoke",
                "scheduled_task_disable",
            }:
                from osia import scheduled_tasks

                envelope = {
                    key: msg.get(key) for key in (
                        "protocol_version", "task_id", "task_revision",
                        "definition_hash", "authorization_revision",
                        "policy_epoch", "expiration", "origin_ref",
                    )
                }
                if msg_type == "scheduled_task_authorization_revoke":
                    result = scheduled_tasks.revoke_scheduled_task_authorization_bound(
                        envelope,
                        authenticated_origin=authorization_origin_ref,
                    )
                else:
                    result = scheduled_tasks.disable_scheduled_task_bound(
                        envelope,
                        authenticated_origin=authorization_origin_ref,
                    )
                await out.send_json({
                    "type": "scheduled_task_authorization_state",
                    "task_id": str(msg.get("task_id") or ""),
                    **result,
                })
                continue

            if msg_type == "autonomous_authorization_response":
                st = _autonomous.get("st")
                if not isinstance(st, dict):
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected", "reason_code": "run_missing",
                    })
                    continue
                response = {
                    key: msg.get(key) for key in (
                        "protocol_version", "request_id", "run_id",
                        "plan_revision", "authorization_revision",
                        "expected_policy_epoch", "origin_ref", "action",
                    )
                }
                selected = msg.get("selected_capability_ids")
                if not isinstance(selected, list) or any(
                    not isinstance(item, str) for item in selected
                ):
                    selected = None
                try:
                    _aauthorization.authorize_and_start(
                        st,
                        response,
                        selected_capability_ids=selected,
                        origin_ref=authorization_origin_ref,
                    )
                    _astate.save_task_state(st)
                    action = str(response.get("action") or "")
                    if action == "approve" and st.get("run_state") == "ready":
                        orch = _autonomous.get("orch")
                        if orch is None:
                            _ensure_autonomous_engine()
                            orch = _autonomous.get("orch")
                        orch.start(st)
                        event = "running"
                    elif action == "cancel":
                        _aauthorization.revoke_run_authorization(st)
                        st["status"] = "aborted"
                        st["run_state"] = "cancelled"
                        _astate.clear_task_state()
                        _release_single_runner()
                        event = "aborted"
                    else:
                        event = "authorization_denied"
                except _aauthorization.AuthorizationError as exc:
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected", "reason_code": exc.code,
                        **_autonomous_snapshot(st),
                    })
                    continue
                except Exception:
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected",
                        "reason_code": "authorization_internal_failure",
                        **_autonomous_snapshot(st),
                    })
                    continue
                _broadcast_autonomous(_autonomous_frame({
                    "type": "autonomous_authorization_state",
                    "event": event,
                }, st))
                continue

            if msg_type == "autonomous_scope_extension_response":
                st = _autonomous.get("st")
                if not isinstance(st, dict):
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected", "reason_code": "run_missing",
                    })
                    continue
                response = {
                    key: msg.get(key) for key in (
                        "protocol_version", "request_id", "run_id",
                        "plan_revision", "authorization_revision",
                        "expected_policy_epoch", "origin_ref", "action",
                    )
                }
                try:
                    _aauthorization.approve_scope_extension(
                        st, response, origin_ref=authorization_origin_ref,
                    )
                    _astate.save_task_state(st)
                    action = str(response.get("action") or "")
                    if action == "approve_exact":
                        orch = _autonomous.get("orch")
                        if orch is None:
                            _ensure_autonomous_engine()
                            orch = _autonomous.get("orch")
                        orch.resume(st)
                        event = "running"
                    elif action == "cancel":
                        _aauthorization.revoke_run_authorization(st)
                        st["status"] = "aborted"
                        st["run_state"] = "cancelled"
                        _astate.clear_task_state()
                        _release_single_runner()
                        event = "aborted"
                    else:
                        event = "scope_extension_denied"
                except _aauthorization.AuthorizationError as exc:
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected", "reason_code": exc.code,
                        **_autonomous_snapshot(st),
                    })
                    continue
                except Exception:
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected",
                        "reason_code": "scope_extension_internal_failure",
                        **_autonomous_snapshot(st),
                    })
                    continue
                _broadcast_autonomous(_autonomous_frame({
                    "type": "autonomous_authorization_state",
                    "event": event,
                }, st))
                continue

            if msg_type == "autonomous_authorization_revoke":
                st = _autonomous.get("st")
                try:
                    if not isinstance(st, dict):
                        raise _aauthorization.AuthorizationError("run_missing")
                    if msg.get("protocol_version") != (
                        _aauthorization.AUTHORIZATION_PROTOCOL_VERSION
                    ):
                        raise _aauthorization.AuthorizationError(
                            "wrong_protocol_version"
                        )
                    if msg.get("origin_ref") != authorization_origin_ref:
                        raise _aauthorization.AuthorizationError("wrong_origin")
                    if msg.get("run_id") != st.get("run_id"):
                        raise _aauthorization.AuthorizationError("wrong_run_id")
                    if msg.get("plan_revision") != st.get("plan_revision"):
                        raise _aauthorization.AuthorizationError(
                            "wrong_plan_revision"
                        )
                    if msg.get("authorization_revision") != st.get(
                        "authorization_revision"
                    ):
                        raise _aauthorization.AuthorizationError(
                            "wrong_authorization_revision"
                        )
                    from osia.action_policy import get_default_permission_policy

                    current_epoch = (
                        get_default_permission_policy().store.load_state().policy_epoch
                    )
                    if (
                        msg.get("expected_policy_epoch") != current_epoch
                        or st.get("policy_epoch") != current_epoch
                    ):
                        raise _aauthorization.AuthorizationError("wrong_policy_epoch")
                    if st.get("run_state") in {"completed", "failed", "cancelled"}:
                        raise _aauthorization.AuthorizationError("run_terminal")
                    _aauthorization.revoke_run_authorization(st)
                    orch = _autonomous.get("orch")
                    if orch is not None:
                        await asyncio.to_thread(orch.stop)
                    _astate.save_task_state(st)
                except _aauthorization.AuthorizationError as exc:
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected", "reason_code": exc.code,
                        **_autonomous_snapshot(st),
                    })
                    continue
                except Exception:
                    await out.send_json({
                        "type": "autonomous_authorization_state",
                        "event": "rejected", "reason_code": "revocation_failure",
                        **_autonomous_snapshot(st),
                    })
                    continue
                _broadcast_autonomous(_autonomous_frame({
                    "type": "autonomous_authorization_revoked",
                    "event": "suspended_authorization_revoked",
                }, st))
                continue

            if msg_type == "cancel_active_work":
                scope = str(msg.get("scope") or "").strip().lower()
                if msg.get("schema_version") != 1:
                    await _send_cancellation_rejection(
                        out, scope or "unknown", "wrong_schema_version",
                    )
                    continue
                if scope == "chat":
                    await _handle_correlated_chat_cancellation(out, msg)
                    continue
                if scope == "autonomous":
                    if not await _prepare_correlated_autonomous_cancellation(
                        out, msg,
                    ):
                        continue
                    # Reuse the established abort worker after exact target
                    # validation. The legacy name remains compatible below,
                    # but it must now carry the same exact run correlation.
                    correlated_autonomous_abandon = True
                    msg_type = "autonomous_abort"
                else:
                    await _send_cancellation_rejection(
                        out, scope or "unknown", "unsupported_scope",
                    )
                    continue

            if msg_type == "autonomous_resume":
                # Resume a PAUSED (interrupted) autonomous run from its cursor.
                # interrupt already released the single-runner slot, so this
                # re-acquire normally succeeds; if a DIFFERENT run now holds it,
                # refuse (busy) — unicast to the requester, exactly like
                # autonomous_start (NOT a broadcast: only this client asked).
                _gen = _acquire_single_runner()
                if _gen is None:
                    await out.send_json(_autonomous_frame({"event": "busy"}))
                    continue
                # Capture THIS event loop so the orchestrator's daemon thread can
                # hop progress frames back onto each connection's writer.
                _autonomous["loop"] = asyncio.get_running_loop()
                _autonomous["origin_ref"] = authorization_origin_ref
                _autonomous["document_owner_device"] = permission_session.device_id
                # Resume OFF the event loop (resume() spawns the loop thread and
                # joins ≤2 s on its first checkpoint save — never block the loop).
                threading.Thread(
                    target=_autonomous_resume_worker,
                    args=(_gen, authorization_origin_ref),
                    daemon=True,
                ).start()
                continue

            if msg_type == "autonomous_interrupt":
                # Pause (resumable): stop() joins ≤2 s, so run it off the loop.
                # The persisted state stays in_progress ⇒ resumable on relaunch.
                def _interrupt_worker():
                    # ORPHAN-LOOP RACE (WOR-45) — RAISE the gate BEFORE stop():
                    # stop()'s 2 s join can TIME OUT mid _verify/_replan, leaving
                    # the old loop draining to a terminal emit. While the gate is
                    # up, that terminal is dropped (not broadcast as the outcome,
                    # not allowed to stop/free a newer run) — see
                    # _on_autonomous_progress.
                    _autonomous["interrupting"] = True
                    try:
                        orch = _autonomous.get("orch")
                        if orch is not None:
                            try:
                                orch.stop()
                            except Exception:
                                _adiagnostics.emit_diagnostic_log(
                                    _adiagnostics.make_diagnostic(
                                        _autonomous.get("st"),
                                        stage=_adiagnostics.AutonomousStage.CONTROL,
                                        category=(
                                            _adiagnostics.FailureCategory
                                            .ORCHESTRATOR_FAILURE
                                        ),
                                        reason_code="interrupt_stop_failure",
                                        resumable=True,
                                        checkpoint_present=True,
                                    )
                                )
                        st = _autonomous.get("st")
                        if isinstance(st, dict):
                            agent = _autonomous.get("agent")
                            provider_now, model_now = (
                                _adiagnostics.actual_provider_model(
                                    agent,
                                    st.get("provider", ""),
                                    st.get("model", ""),
                                )
                            )
                            diagnostic = _adiagnostics.make_diagnostic(
                                st,
                                stage=_adiagnostics.AutonomousStage.CONTROL,
                                category=_adiagnostics.FailureCategory.INTERRUPTED,
                                reason_code="user_interrupted",
                                provider=provider_now,
                                model=model_now,
                                resumable=_astate.resume_state_is_valid(st),
                                checkpoint_present=True,
                            )
                            _adiagnostics.record_diagnostic(st, diagnostic)
                            try:
                                _astate.save_task_state(st)
                            except Exception:
                                _adiagnostics.set_checkpoint_present(st, False)
                        _broadcast_autonomous(
                            _autonomous_frame({"event": "interrupted"},
                                              st)
                        )
                        _release_single_runner()
                    finally:
                        # ALWAYS arm the backstop that clears the gate once the
                        # (possibly still-draining) orphan is confirmed dead — even
                        # if stop/broadcast/release raised. Otherwise the gate could
                        # stick True and swallow a FUTURE run's terminal (the
                        # NO-RESUME case; a resume/start would also clear it via its
                        # own drain). The clear is gated on the orphan being dead;
                        # the poller is bounded so it never blocks forever.
                        threading.Thread(
                            target=_clear_interrupting_after_orphan_drained,
                            daemon=True,
                        ).start()

                threading.Thread(target=_interrupt_worker, daemon=True).start()
                continue

            if msg_type == "autonomous_abort":
                if not correlated_autonomous_abandon:
                    if not await _prepare_correlated_autonomous_cancellation(
                        out, msg,
                    ):
                        continue
                # Terminalize the workflow while retaining the exact durable
                # receipt. A late engine callback cannot revive this run, and
                # an external effect is never replayed to prove abandonment.
                def _abort_worker():
                    st = _autonomous.get("st")
                    orch = _autonomous.get("orch")
                    _autonomous.pop("correlated_cancellation_published", None)
                    if isinstance(st, dict) and not st.get("abandon_state"):
                        st["abandon_origin_status"] = str(
                            st.get("status") or ""
                        )[:32]
                        st["abandon_origin_run_state"] = str(
                            st.get("run_state") or ""
                        )[:64]
                        st["abandon_state"] = "abandon_requested"
                    terminalized = _terminalize_autonomous_abandon(st, orch)
                    if terminalized:
                        _broadcast_autonomous(
                            _autonomous_frame({"event": "aborted"}, st)
                        )
                    _release_single_runner()

                threading.Thread(target=_abort_worker, daemon=True).start()
                continue

            if msg_type == "get_autonomous_status":
                # Let a phone that (re)connects mid-run render current progress.
                # Prefer the in-memory state; fall back to the persisted mobile
                # file so a fresh process still reports a known run. Unicast.
                st = _autonomous.get("st")
                if not isinstance(st, dict):
                    try:
                        st = _astate.load_task_state()
                    except Exception:
                        st = None
                if isinstance(st, dict):
                    _schedule_durable_autonomous_recovery(
                        st,
                        origin_ref=authorization_origin_ref,
                        loop=asyncio.get_running_loop(),
                        document_owner_device=permission_session.device_id,
                    )
                    status = st.get("status", "")
                    run_state = st.get("run_state", "")
                    if status in {"done", "failed", "aborted"}:
                        ev = {
                            "done": "done",
                            "failed": "failed",
                            "aborted": "aborted",
                        }[status]
                    elif run_state in {
                        "authorization_pending", "suspended_scope_extension",
                        "suspended_authorization_revoked", "blocked_outcome_unknown",
                        "waiting_tool", "verifying_tool", "resuming",
                        "blocked_unknown_effect", "cancellation_requested",
                        "cancelled_proven",
                    }:
                        ev = run_state
                    elif status == "in_progress":
                        # A LIVE run keeps active=True; interrupt cleared it (the
                        # run is PAUSED but state stays in_progress ⇒ resumable),
                        # and a fresh process recovering st from disk has no
                        # in-memory active run either. Report "interrupted" in
                        # both cases so a reconnecting phone can offer resume
                        # (consistent with the GUI offering resume on launch);
                        # only a genuinely live run (active True) is "running".
                        ev = "running" if _autonomous.get("active") else "interrupted"
                    else:
                        ev = "running"
                    frame_type = (
                        "autonomous_plan_ready"
                        if ev == "authorization_pending"
                        else "autonomous_authorization_state"
                        if ev in {
                            "suspended_scope_extension",
                            "suspended_authorization_revoked",
                            "blocked_outcome_unknown",
                        }
                        else "autonomous_progress"
                    )
                    await out.send_json(_autonomous_frame({
                        "type": frame_type, "event": ev,
                    }, st))
                    if st.get("abandon_state") in _AUTONOMOUS_ABANDON_STATES:
                        await _send_autonomous_abandon_ack(out, st)
                else:
                    await out.send_json(_autonomous_frame({"event": "idle"}, None))
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
                if setting_key == "permission_change_request":
                    await permission_session.handle_change_request(setting_value)
                    continue
                # Whitelist: only these settings.json keys are writable from the
                # mobile app. Anything else is rejected so a client can't poke
                # arbitrary server state.
                allowed_settings = {
                    "liveapi_enabled", "liveapi_provider", "liveapi_model",
                    "tts_provider", "tts_voice", "stt_provider",
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
                    # Cooperation IA vision provider/model (WOR-25).
                    "vision_provider", "vision_model",
                    # Shared reasoning/thinking level settings (WOR-77).
                    "reasoning",
                    # MCP / Connectors server list (WOR-44 Phase 4). The list is
                    # NON-SECRET (env is always {} — see the mcp_servers write
                    # branch below); per-server tokens live only in the
                    # gitignored api_keys.json under `mcp:<id>`.
                    "mcp_servers",
                    # WOR-191 downgrade-compatible keys. The authoritative
                    # config layer forces these writes to True/False, so legacy
                    # mobile clients cannot reactivate auto-approval.
                    "mcp_confirm_writes",
                    "mcp_allow_unattended_writes",
                }
                if setting_key not in allowed_settings:
                    print(
                        f"[OSIA Server] set_setting rejected non-whitelisted "
                        f"key={setting_key!r}"
                    )
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
            # get_config returns everything the mobile config screens need in one
            # frame: the settings.json contents (incl. api_key_fallbacks), the two
            # hierarchy chains exposed under their flat mobile names, the
            # personality document, and the full skill list (name/enabled/prose).
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
                    await _broadcast_memory_updated(memory)
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
                    await _broadcast_memory_updated(memory)
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
                    await _broadcast_memory_updated(memory)
                    print("[OSIA Server] clear_memory cleared all entries")
                except Exception as e:
                    print(f"[OSIA Server] clear_memory failed: {e}")
                    await _send_memory_error(
                        out,
                        _memory_error_code(e),
                        str(e) or type(e).__name__,
                    )
                continue

            # --- API keys (mobile "Configure APIs & Providers" editor) ---
            # get_config's api_keys_set field reveals only WHICH providers have a
            # primary key; get_api_keys returns the actual stored primary key per
            # provider so the mobile editor can display/prefill them. (Fallback
            # keys travel separately via get_config's api_key_fallbacks.)
            # Authenticated-only.
            if msg_type == "get_api_keys":
                try:
                    from osia.config import get_api_key
                    from osia.models import API_PROVIDERS
                    keys = {p: get_api_key(p) for p in API_PROVIDERS}
                    await out.send_json({"type": "api_keys", "keys": keys})
                    print(
                        f"[OSIA Server] get_api_keys sent "
                        f"({sum(1 for v in keys.values() if v)}/{len(keys)} set)"
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

            # --- MCP write-tool confirmation response (WOR-44 Phase 5, 5-conf-c) ---
            # The mobile user tapped Approve/Decline on an mcp_confirm_request we
            # sent during a write/destructive MCP tool. Resolve the matching pending
            # Future so the blocked agent worker thread proceeds (approve) or gets a
            # deny. Non-blocking (just resolves a Future), so the receive loop keeps
            # serving other frames while a confirm is pending. An unknown/expired id
            # is ignored cleanly. Additive — mirrors the Phase-4 mcp_* handlers; the
            # frame carries no secret.
            if msg_type == "mcp_confirm_response":
                raw_cid = msg.get("id")
                cid = raw_cid.strip() if isinstance(raw_cid, str) else ""
                approved = bool(msg.get("approved", False))
                if cid:
                    _resolve_confirm(cid, approved)
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
                    speak_text = str(persisted["assistant_content"])
                    speak_language = normalize_locale(persisted.get("language"))
                    speak_meta = {
                        "conversation_id": speak_conv_id,
                        "request_id": speak_request_id,
                        "client_request_id": speak_request_id,
                        "turn_id": speak_turn_id,
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
                projected_responses = []
                for response in responses:
                    if (
                        response.get("type") == "attachment_batch_commit_ack"
                        and response.get("attachment_purpose") == "autonomous"
                    ):
                        batch_id = str(response.get("batch_id") or "")
                        cached = autonomous_attachment_acks.get(batch_id)
                        if cached is not None:
                            projected_responses.append(dict(cached))
                            continue
                        owner_id = _mobile_document_owner(
                            permission_session.device_id
                        )
                        created_refs = []
                        try:
                            if not owner_id:
                                raise document_sources.DocumentSourceError(
                                    "unauthorized_source"
                                )
                            committed = attachment_batches.consume_for_autonomous(
                                batch_id,
                                str(response.get("request_id") or ""),
                            )
                            permission_epoch = int(
                                permission_session.store.load_state().policy_epoch
                            )
                            safe_sources = []
                            for item in committed:
                                source = document_sources.get_source_store().create(
                                    item.path,
                                    owner_id=owner_id,
                                    source_kind="autonomous_mobile_attachment",
                                    display_name=item.display_name,
                                    declared_mime=item.detected_mime,
                                    permission_epoch=permission_epoch,
                                )
                                created_refs.append(source["source_ref"])
                                if source.get("state") != "ready":
                                    raise document_sources.DocumentSourceError(
                                        "failed_source"
                                    )
                                safe_sources.append({
                                    "source_ref": source["source_ref"],
                                    "type_label": source["type_label"],
                                    "document_family": source["document_family"],
                                    "state": source["state"],
                                    "warnings": list(source.get("warnings") or []),
                                    "logical_order": item.logical_order,
                                })
                            enriched = dict(response)
                            enriched["autonomous_sources"] = safe_sources
                            autonomous_attachment_acks[batch_id] = dict(enriched)
                            projected_responses.append(enriched)
                        except (
                            AttachmentProtocolError,
                            document_sources.DocumentSourceError,
                        ):
                            if created_refs and owner_id:
                                try:
                                    document_sources.get_source_store().release_owned(
                                        created_refs, owner_id=owner_id,
                                    )
                                except document_sources.DocumentSourceError:
                                    pass
                            projected_responses.append({
                                "type": "attachment_batch_failed",
                                "protocol_version": response.get(
                                    "protocol_version", 1
                                ),
                                "batch_id": batch_id,
                                "conversation_id": "",
                                "client_request_id": str(
                                    response.get("client_request_id") or ""
                                ),
                                "request_id": str(
                                    response.get("request_id") or ""
                                ),
                                "event_id": "event_" + secrets.token_hex(16),
                                "sequence": int(response.get("sequence", 0)),
                                "category": "document_validation_failed",
                            })
                        finally:
                            attachment_batches.release(batch_id)
                        continue
                    projected_responses.append(response)
                for response in projected_responses:
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
                try:
                    image_mime = _image_mime_for(upload.path)
                    if image_mime is None:
                        # Non-image: queue for the next text/audio turn.
                        pending_files.append(str(target))
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
                        _conv = await _resolve_turn_conversation(msg, display_prompt)
                        if _conv is not None:
                            from osia.document_sources import get_source_store

                            capability = get_source_store().create(
                                target,
                                owner_id=_conv,
                                source_kind="legacy_mobile_attachment",
                                display_name=safe_name,
                                declared_mime=image_mime,
                                permission_epoch=int(
                                    permission_session.store.load_state().policy_epoch
                                ),
                            )
                            document_content = (
                                "[Document image available through read_document: "
                                f"source_ref={capability['source_ref']} "
                                f"type={capability['type_label']} state={capability['state']}]"
                                f"\n\n{display_prompt}"
                            )
                            if await _send_persisted_turn_for_request(
                                out,
                                _conv,
                                client_request_id,
                                expected_user_content=upload_digest,
                            ):
                                continue
                            if _is_conversation_busy(_conv):
                                await _send_conversation_busy(out, _conv)
                                continue
                            turn_meta = _new_turn_meta(_conv, client_request_id)
                            turn_meta["_document_owner_device"] = (
                                permission_session.device_id
                            )
                            turn_meta["_autonomous_origin_ref"] = (
                                authorization_origin_ref
                            )
                            turn_meta["client_locale"] = msg.get("locale")
                            turn_meta["_original_user_text"] = display_prompt
                            turn_meta["tts_enabled"] = (
                                msg.get("tts_enabled", True) is not False
                            )
                            turn_meta["client_content_hash"] = hashlib.sha256(
                                upload_digest.encode("ascii")
                            ).hexdigest()
                            turn_meta["persisted_user_content"] = display_prompt

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
                    if not ownership_transferred:
                        legacy_uploads.cleanup([target])
                continue

            # --- Text message ---
            if msg_type == "text":
                user_text = msg.get("content", "").strip()
                if not user_text:
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
                requested_conversation_id = str(
                    msg.get("conversation_id") or ""
                ).strip()
                waiting_turn_command = msg.get("waiting_turn_command") is True
                content_identity = (
                    f"{user_text}\0{attachment_batch_id}"
                    if attachment_batch_id else user_text
                )
                if await _send_existing_turn_status_for_request(
                    out,
                    client_request_id,
                ):
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
                            expected_user_content=content_identity,
                        ):
                            continue
                    except ValueError:
                        await out.send_json({
                            "type": "status",
                            "content": "error",
                            "error": "request_id_conflict",
                            "conversation_id": requested_conversation_id,
                        })
                        continue
                if attachment_batch_id:
                    try:
                        attachment_batches.validate_turn_reference(
                            attachment_batch_id,
                            requested_conversation_id,
                            client_request_id,
                        )
                    except AttachmentProtocolError as exc:
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
                # Consume pending files synchronously (clears the shared list)
                # so this turn — and only this turn — carries them. Resolve the
                # conversation synchronously (creating it on the first message)
                # so the turn is keyed by a real id, then run the agent in its
                # own task so the receive loop stays free.
                _conv = await _resolve_turn_conversation(msg, user_text)
                if _conv is not None:
                    try:
                        if await _send_persisted_turn_for_request(
                            out,
                            _conv,
                            client_request_id,
                            expected_user_content=content_identity,
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
                        if await _handle_waiting_tool_command(
                            out, _conv, user_text,
                        ):
                            continue
                        if await _send_existing_turn_status(
                            out,
                            _conv,
                            client_request_id,
                        ):
                            continue
                        await _send_conversation_busy(out, _conv)
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
                        agent_input = build_agent_input(
                            user_text,
                            committed_attachments,
                            owner_id=_conv,
                            permission_epoch=int(
                                permission_session.store.load_state().policy_epoch
                            ),
                        )
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
                        agent_input = _consume_pending_files(
                            user_text,
                            list(pending_files),
                            owner_id=_conv,
                            permission_epoch=int(
                                permission_session.store.load_state().policy_epoch
                            ),
                        )
                        persisted_user_content = user_text
                    turn_meta = _new_turn_meta(_conv, client_request_id)
                    turn_meta["_document_owner_device"] = (
                        permission_session.device_id
                    )
                    turn_meta["_autonomous_origin_ref"] = (
                        authorization_origin_ref
                    )
                    turn_meta["client_locale"] = msg.get("locale")
                    turn_meta["_original_user_text"] = user_text
                    turn_meta["tts_enabled"] = (
                        msg.get("tts_enabled", True) is not False
                    )
                    turn_meta["client_content_hash"] = hashlib.sha256(
                        content_identity.encode("utf-8")
                    ).hexdigest()
                    if committed_attachments:
                        turn_meta["persisted_user_content"] = persisted_user_content
                        turn_meta["attachment_metadata"] = [
                            item.persistence_metadata()
                            for item in committed_attachments
                        ]

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

                    legacy_turn_files = (
                        list(pending_files) if not attachment_batch_id else []
                    )
                    if legacy_turn_files:
                        pending_files.clear()

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
                if await _send_existing_turn_status_for_request(
                    out,
                    client_request_id,
                ):
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
                _conv = await _resolve_turn_conversation(msg, None)
                if _conv is not None:
                    if await _send_persisted_turn_for_request(
                        out,
                        _conv,
                        client_request_id,
                        expected_user_content=b64_data,
                    ):
                        continue
                    if _is_conversation_busy(_conv):
                        if await _send_existing_turn_status(
                            out,
                            _conv,
                            client_request_id,
                        ):
                            continue
                        await _send_conversation_busy(out, _conv)
                        continue
                    _pf = list(pending_files)
                    turn_meta = _new_turn_meta(_conv, client_request_id)
                    turn_meta["_document_owner_device"] = (
                        permission_session.device_id
                    )
                    turn_meta["_autonomous_origin_ref"] = (
                        authorization_origin_ref
                    )
                    turn_meta["client_locale"] = msg.get("locale")
                    turn_meta["tts_enabled"] = (
                        msg.get("tts_enabled", True) is not False
                    )
                    turn_meta["client_content_hash"] = hashlib.sha256(
                        b64_data.encode("ascii")
                    ).hexdigest()

                    async def _run_legacy_audio_turn(
                        snap,
                        _b=b64_data,
                        _f=audio_format,
                        _c=_conv,
                        _files=_pf,
                        _m=turn_meta,
                        _epoch=int(
                            permission_session.store.load_state().policy_epoch
                        ),
                    ):
                        async def _operation():
                            return await _handle_audio(
                                out,
                                _b,
                                _f,
                                snap,
                                _c,
                                pending_files=_files,
                                get_agent=_get_conn_agent,
                                turn_meta=_m,
                                permission_epoch=_epoch,
                            )

                        return await run_with_legacy_cleanup(
                            legacy_uploads,
                            _files,
                            _operation,
                        )

                    pending_files.clear()
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
        permission_session.close()
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
        # WOR-44 Phase 5 (5-conf-c): deny + drop any MCP confirmation still pending
        # for THIS connection so a blocked agent worker thread (a detached turn's
        # write tool awaiting approval) wakes immediately with a deny instead of
        # hanging until the timeout. No-op if none are pending.
        _deny_confirms_for_channel(out)
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
            _media_task = _POST_TURN_MEDIA_BY_CONV.pop(_cid, None)
            if _media_task is not None:
                _media_task.cancel()
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
            text = strip_raw_tool_tags(text).strip()
            if not text:
                continue
        # Defence-in-depth: drop anything that looks like the system
        # prompt leaked into the conversation history.
        if "RÈGLES ABSOLUES" in text or "ABSOLUTE RULES" in text:
            continue
        if text.lstrip().startswith("##"):
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
        out.append(display)
    return out


def _join_sent_chunks(chunks: list[str]) -> str:
    """Rebuild the text Mobile sees from stripped sentence chunks."""

    return " ".join(chunks)


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
    owner_id: str | None = None,
    permission_epoch: int | None = None,
) -> str:
    """Replace legacy pending paths with opaque sources before model access."""
    if not pending_files:
        return user_text
    paths = list(pending_files)
    pending_files.clear()
    from osia.document_sources import get_source_store

    capabilities = []
    for path in paths:
        if owner_id:
            capability = get_source_store().create(
                path,
                owner_id=owner_id,
                source_kind="legacy_mobile_attachment",
                display_name=Path(path).name,
                permission_epoch=permission_epoch,
            )
        else:
            capability = get_source_store().create_for_current_owner(
                path,
                source_kind="legacy_mobile_attachment",
                display_name=Path(path).name,
                permission_epoch=permission_epoch,
            )
        capabilities.append(capability)
    bullets = "\n".join(
        f"- source_ref={item['source_ref']} type={item['type_label']} state={item['state']}"
        + (f" category={item['category']}" if item.get("category") else "")
        for item in capabilities
    )
    return (
        "[Document/media attachments available through read_document and "
        f"query_document:\n{bullets}\n]\n\n{user_text}"
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
async def _send_response_media_terminal(
    ws: "_OutboundChannel",
    turn_meta: dict,
    status: str,
) -> None:
    """Close one correlated media phase without changing the text lifecycle."""

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
        frame = _turn_event_frame(
            self.turn_meta,
            "audio",
            message_id=str(self.turn_meta.get("assistant_message_id") or ""),
            payload={
                "format": "mp3",
                "data": base64.b64encode(mp3_bytes).decode("ascii"),
                "source": "response",
                "phase": "media",
                "media_terminal": terminal,
            },
        )
        try:
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


def _track_response_audio_task(
    task: "asyncio.Task",
    turn_meta: dict,
) -> bool:
    """Track one media worker and keep per-conversation completion ordered."""

    conv_id = str(turn_meta.get("conversation_id") or "")
    if not conv_id:
        task.cancel()
        return False
    _POST_TURN_MEDIA_TASKS.add(task)
    if not _application_transport_coordination_enabled():
        task.add_done_callback(_POST_TURN_MEDIA_TASKS.discard)
        return True
    if _POST_TURN_MEDIA_BY_CONV.get(conv_id) is not None:
        task.cancel()
        _POST_TURN_MEDIA_TASKS.discard(task)
        return False
    _POST_TURN_MEDIA_BY_CONV[conv_id] = task

    def _media_done(done_task: "asyncio.Task") -> None:
        _POST_TURN_MEDIA_TASKS.discard(done_task)
        if _POST_TURN_MEDIA_BY_CONV.get(conv_id) is done_task:
            _POST_TURN_MEDIA_BY_CONV.pop(conv_id, None)
            if not _is_conversation_busy(conv_id):
                asyncio.create_task(
                    _broadcast_conversation_busy_changed(conv_id, False, turn_meta)
                )

    task.add_done_callback(_media_done)
    return True


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
        await ws.send_json({
            "type": "conversations",
            "items": conversations.list_conversations(),
        })

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
    accepted_user_persisted = _persist_accepted_user_message(
        active_conv_id,
        persisted_user_content,
        turn_meta,
    )
    if accepted_user_persisted and isinstance(turn_meta, dict):
        await _broadcast_committed_receipt(
            active_conv_id,
            turn_meta.get("_user_commit"),
        )
    if isinstance(turn_meta, dict):
        turn_meta["persisted"] = accepted_user_persisted
        if "_accepted_frame" not in turn_meta:
            await ws.send_json(_turn_accepted_frame(turn_meta))
        else:
            await ws.send_json(_turn_status_frame(turn_meta, "running"))
        if not accepted_user_persisted:
            await ws.send_json(_turn_status_frame(turn_meta, "error"))
            return history, active_conv_id

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
    # Agent callbacks are buffered until agent.run returns successfully.
    # Only the cleaned canonical return value is then sent to WebSocket/TTS.
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
    tts_session: _ResponseTTSSession | None = None
    tts_enabled = (
        not isinstance(turn_meta, dict)
        or turn_meta.get("tts_enabled", True) is not False
    )
    if tts_enabled and isinstance(turn_meta, dict):
        candidate = _ResponseTTSSession(ws, turn_meta, t0)
        if _track_response_audio_task(candidate.start(), turn_meta):
            tts_session = candidate
        else:
            candidate.cancel()
    progress_task: asyncio.Task | None = (
        asyncio.create_task(_turn_progress_loop(ws, turn_meta))
        if isinstance(turn_meta, dict)
        else None
    )
    pending_turn_holder = [None]

    def on_first_token() -> None:
        # Called from the agent worker thread the moment the first
        # non-empty delta arrives back from the LLM.
        if not first_token_logged[0]:
            first_token_logged[0] = True
            _ts(t0, "first model token received")

    def on_sentence(sentence: str) -> None:
        if not sentence:
            return
        provisional_chunks.append(sentence)

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
                if tts_session is not None:
                    tts_session.enqueue(item)
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
        agent = get_agent(active_conv_id) if get_agent is not None else OSIAAgent()
        selected_model = load_settings().get("selected_model")
        # Only (re)build the client when the agent isn't already on a clean
        # primary client for this model — the user switched models, a prior
        # turn's router/key/endpoint fallback drifted it, or it's this agent's
        # first turn. Otherwise skip the costly rebuild and reuse the client.
        if selected_model and _agent_needs_model_apply(agent, selected_model):
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

        async def _finalize_pending_tool_attempt(
            attempt_id: str,
            final_text: str,
            error,
        ) -> bool:
            del attempt_id
            if error is not None:
                final_text = (
                    "Le r\u00e9sultat de l'action a \u00e9t\u00e9 conserv\u00e9, mais la "
                    "reprise du m\u00eame fournisseur a \u00e9chou\u00e9. L'action "
                    "n'a pas \u00e9t\u00e9 relanc\u00e9e."
                    if turn_context.language == "fr" else
                    "The action result was preserved, but continuation with "
                    "the same provider failed. The action was not repeated."
                )
                agent.messages.append({"role": "assistant", "content": final_text})
            from osia.agent import clean_response, strip_raw_tool_tags

            final_text = clean_response(
                strip_raw_tool_tags(final_text or "")
            ).strip()
            if not final_text:
                final_text = localized_text(
                    "empty_completion", turn_context.language,
                )
            response_frame = _turn_event_frame(
                turn_meta,
                "response",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": final_text},
            )
            terminal_frame = _turn_status_frame(
                turn_meta, "done" if error is None else "error",
            )
            if error is not None:
                terminal_frame.update({
                    "stage": "tool_continuation",
                    "error_category": "same_provider_continuation_failed",
                    "reason_code": "same_provider_continuation_failed",
                })
            persisted = _persist_completed_assistant_message(
                active_conv_id,
                final_text,
                turn_meta,
                response_frame,
                terminal_frame,
            )
            if persisted:
                await _broadcast_committed_receipt(
                    active_conv_id, turn_meta.get("_assistant_commit"),
                )
                turn_meta["persisted"] = True
                terminal_frame["persisted"] = True
            try:
                await ws.send_json(
                    _turn_event_frame(
                        turn_meta,
                        "response_chunk",
                        message_id=str(
                            turn_meta.get("assistant_message_id") or ""
                        ),
                        payload={"content": final_text},
                    )
                )
                await ws.send_json(response_frame)
                await ws.send_json(terminal_frame)
            except Exception as send_error:
                print(
                    "[OSIA Server] pending tool final delivery failed "
                    f"category={type(send_error).__name__}"
                )
            if turn_meta.get("tts_enabled", True) is not False:
                resumed_tts = _ResponseTTSSession(
                    ws, turn_meta, t0, language=turn_context.language,
                )
                if _track_response_audio_task(resumed_tts.start(), turn_meta):
                    resumed_tts.enqueue(final_text)
                    resumed_tts.finish_input(final_text)
                    resumed_tts.mark_text_final_sent()
                else:
                    resumed_tts.cancel()
            else:
                await _send_response_media_terminal(ws, turn_meta, "done")
            holder = pending_turn_holder[0]
            if holder is not None:
                _clear_conversation_turn(active_conv_id, holder)
                if not holder.done():
                    holder.set_result(None)
            await _broadcast_conversation_busy_changed(
                active_conv_id, False, turn_meta,
            )
            clear_attempts = getattr(agent, "clear_tool_attempts", None)
            if callable(clear_attempts):
                clear_attempts()
            log_final(turn_context, final_text)
            return bool(persisted)

        def _tool_attempt_status(status: str, attempt_id: str) -> None:
            turn_meta["_tool_attempt_id"] = str(attempt_id or "")
            safe_status = {
                "action_starting": "action_starting",
                "action_in_progress": "action_in_progress",
                "waiting_tool_result": "waiting_tool_result",
                "verifying_tool_result": "verifying",
                "resuming_tool_turn": "resuming",
                "blocked_unknown_effect": "blocked_unknown_effect",
                "cancellation_requested": "cancellation_requested",
                "cancelled_proven": "cancelled_proven",
            }.get(str(status), "action_in_progress")
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

        _autonomous["loop"] = document_loop
        agent._chat_autonomous_context = {
            "launcher": _launch_autonomous_from_chat,
            "original_user_text": str(original_user_text or ""),
            "provider_input": text,
            "history": history,
            "conversation_id": active_conv_id,
            "request_id": str((turn_meta or {}).get("turn_id") or ""),
            "source_request_id": str(
                (turn_meta or {}).get("request_id") or ""
            ),
            "source_turn_id": str((turn_meta or {}).get("turn_id") or ""),
            "origin_ref": str(
                (turn_meta or {}).get("_autonomous_origin_ref") or "desktop"
            ),
            "device_id": str(
                (turn_meta or {}).get("_document_owner_device") or ""
            ),
            "turn_context": turn_context,
        }
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
            )

        def _run_agent_busy_tracked():
            try:
                with document_progress_scope(
                    "chat",
                    str((turn_meta or {}).get("turn_id") or "chat"),
                    sink=_chat_document_progress,
                ):
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
                agent._chat_autonomous_context = None

        # Off-thread so the event loop stays free to flush response_chunks.
        response_text = await asyncio.to_thread(_run_agent_busy_tracked)
        for future in document_progress_futures:
            try:
                await asyncio.wrap_future(future)
            except Exception:
                pass
        await _cancel_turn_progress_task(progress_task)
        progress_task = None
        print(
            f"[DEBUG] agent.run() returned "
            f"({len(response_text or '')} chars)",
            flush=True,
        )
        from osia.tool_attempts import WAITING_TOOL_RESULT

        if response_text == WAITING_TOOL_RESULT:
            await _cancel_turn_progress_task(progress_task)
            progress_task = None
            for future in pending_puts:
                try:
                    await asyncio.wrap_future(future)
                except Exception:
                    pass
            await chunk_queue.put(_SENTINEL)
            await sender_task
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
                raise RuntimeError("pending tool attempt could not be activated")
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
        print(f"[OSIA Server] Agent error category={type(e).__name__}")
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
        if isinstance(turn_meta, dict):
            _retire_document_scope(
                "chat", str(turn_meta.get("turn_id") or ""),
            )
            provisional_chunks.clear()
            error_text = localized_text("provider_failure", turn_context.language)
            chunk_frame = _turn_event_frame(
                turn_meta,
                "response_chunk",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": error_text},
            )
            await ws.send_json(chunk_frame)
            if tts_session is not None:
                tts_session.enqueue(error_text)
                tts_session.finish_input(error_text)
            response_frame = _turn_event_frame(
                turn_meta,
                "response",
                message_id=str(turn_meta.get("assistant_message_id") or ""),
                payload={"content": error_text},
            )
            terminal_frame = _turn_status_frame(turn_meta, "error")
            terminal_frame.update({
                "stage": "response_generation",
                "error_category": "provider_failure",
                "reason_code": "provider_failure",
            })
            if accepted_user_persisted and _persist_completed_assistant_message(
                active_conv_id,
                error_text,
                turn_meta,
                response_frame,
                terminal_frame,
            ):
                await _broadcast_committed_receipt(
                    active_conv_id,
                    turn_meta.get("_assistant_commit"),
                )
                turn_meta["persisted"] = True
                terminal_frame["persisted"] = True
                await ws.send_json(response_frame)
                await ws.send_json(terminal_frame)
                if tts_session is not None:
                    tts_session.mark_text_final_sent()
                else:
                    await _send_response_media_terminal(ws, turn_meta, "done")
                log_final(turn_context, error_text)
                return history + [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": error_text},
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
    if response_text:
        from osia.agent import clean_response, strip_raw_tool_tags
        response_text = clean_response(strip_raw_tool_tags(response_text)).strip()
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
            tail = _tail_not_sent(cleaned_full, sent_concat)
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
                    if tts_session is not None:
                        tts_session.enqueue(tail)
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
        tts_session.finish_input(response_text)

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
    terminal_frame = _turn_status_frame(turn_meta, "done")
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
        _retire_document_scope(
            "chat", str(turn_meta.get("turn_id") or ""),
        )
        if tts_session is not None:
            tts_session.cancel()
        await ws.send_json(_turn_status_frame(turn_meta, "error"))
        current_task = asyncio.current_task()
        if (
            active_conv_id
            and current_task is not None
            and _clear_conversation_turn(active_conv_id, current_task)
        ):
            await _broadcast_conversation_busy_changed(
                active_conv_id,
                False,
                turn_meta,
            )
        _ts(t0, "assistant persistence failed")
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
    if tts_session is not None:
        tts_session.mark_text_final_sent()
    else:
        await _send_response_media_terminal(ws, turn_meta, "done")
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
        clear_attempts()
    return new_history, active_conv_id


async def _handle_audio(ws: "_OutboundChannel", b64_data: str, audio_format: str,
                        history: list,
                        active_conv_id: str | None,
                        *, pending_files: list[str] | None = None,
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
      * The ``transcript`` message is fired with ``asyncio.create_task``
        WITHOUT awaiting, so the agent starts in parallel with the
        WebSocket send — mobile receives the transcript while the LLM
        is already warming up.
    """
    from osia.voice.stt import transcribe, WHISPER_SAMPLE_RATE, resample_audio

    t0 = time.perf_counter()
    _ts(t0, "audio received", f"({len(b64_data)} b64 chars, format={audio_format})")
    if not isinstance(turn_meta, dict):
        raise ValueError("audio command has no turn correlation")

    # Status: thinking
    await ws.send_json(_turn_status_frame(turn_meta, "thinking"))

    try:
        # Decode and resample
        audio, sr = decode_base64_wav(b64_data)
        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            await ws.send_json(
                _turn_event_frame(
                    turn_meta,
                    "transcript",
                    message_id=str(turn_meta.get("user_message_id") or ""),
                    payload={"content": ""},
                )
            )
            await ws.send_json(_turn_status_frame(turn_meta, "rejected"))
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
            await ws.send_json(
                _turn_event_frame(
                    turn_meta,
                    "transcript",
                    message_id=str(turn_meta.get("user_message_id") or ""),
                    payload={"content": ""},
                )
            )
            await ws.send_json(_turn_status_frame(turn_meta, "rejected"))
            return history, active_conv_id

        # Fire transcript send in parallel with the agent start — don't
        # await it. The send is just an enqueue onto the outbound channel,
        # so it lands long before the agent's first response_chunk, but
        # starting the agent immediately saves the ~1-50 ms a real-network
        # round-trip would otherwise cost.
        transcript_send = asyncio.create_task(
            ws.send_json(
                _turn_event_frame(
                    turn_meta,
                    "transcript",
                    message_id=str(turn_meta.get("user_message_id") or ""),
                    payload={"content": transcript},
                )
            )
        )

        agent_input = transcript
        turn_meta["_original_user_text"] = transcript
        if pending_files:
            agent_input = _consume_pending_files(
                agent_input,
                pending_files,
                owner_id=active_conv_id,
                permission_epoch=permission_epoch,
            )

        try:
            return await _run_agent_and_respond(
                ws, agent_input, history, active_conv_id, t0=t0,
                get_agent=get_agent,
                turn_meta=turn_meta,
            )
        finally:
            # Make sure the transcript frame finished going out before
            # this handler returns (almost certainly already done by now).
            try:
                await transcript_send
            except Exception as e:
                print(f"[OSIA Server] transcript send failed: {e}")

    except Exception as e:
        print(f"[OSIA Server] Audio processing error: {e}")
        traceback.print_exc()
        await ws.send_json(_turn_status_frame(turn_meta, "error"))
        return history, active_conv_id


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
