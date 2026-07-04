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
    {"type": "set_model",            "model": "..."}
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
    {"type": "autonomous_interrupt"}                                   # WOR-45 P5: pause the autonomous run — state stays in_progress (resumable)
    {"type": "autonomous_abort"}                                       # WOR-45 P5: discard the autonomous run — marks state aborted then deletes the checkpoint
    {"type": "get_autonomous_status"}                                  # WOR-45 P5: request the current autonomous snapshot (or {event:"idle"}); a paused run reports {event:"interrupted"}; lets a phone reconnecting mid-run render progress
    {"type": "autonomous_resume"}                                      # WOR-45: resume a PAUSED (interrupted) autonomous run from its cursor (re-acquires the single-runner lock; refused with {event:"busy"} if another run holds it, {event:"idle"} if nothing is resumable)
  Server → Client:
    {"type": "api_keys",             "keys": {provider: key, ...}}     # actual stored keys, one entry per models.API_PROVIDERS ("" if unset)
    {"type": "heartbeat_config",     "config": {"enabled": bool, "interval_seconds": int}}
    {"type": "heartbeat_data",       "cpu_percent": float, "ram_mb": int,
                                     "ram_percent": float, "disk_percent": float,
                                     "cpu_temp": float|null, "gpu_temp": float|null,
                                     "timestamp": "<ISO>"}              # pushed every heartbeat_interval_seconds while heartbeat_enabled
    {"type": "transcript",           "content": "...", "conversation_id": "..."}
    {"type": "response_chunk",       "content": "...", "conversation_id": "..."}  # streamed sentence-by-sentence
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
    {"type": "models_list",          "models": [{id,label,provider,tier}, ...], "current": "..."}
    {"type": "file_received",        "filename": "...", "path": "...", "success": true|false}
    {"type": "config",               "config": {...}}                  # full config snapshot (settings incl. api_key_fallbacks, personality, skills)
    {"type": "status",               "status": "done|error"}           # ack for the config-family setters (create_skill adds "name", restart_server adds "message": "restarting")
    {"type": "autonomous_progress",  "event": "running|interrupted|aborted|plan_failed|busy|idle|task_done|task_failed|salvaged|done|failed",
                                     "tasks_done": int, "tasks_total": int, "phase_name": "...", "objective": "...", "status": "..."}
                                     # WOR-45 P5: autonomous-run progress. Broadcast to ALL clients on every engine/synthesized transition (the run is machine-global);
                                     # the host synthesizes running/interrupted/aborted/plan_failed/busy/idle and enriches every frame with the live task_state snapshot.
"""

import asyncio
import base64
import concurrent.futures
import contextvars
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request

from osia import conversations
from osia.config import CONFIG_DIR as _CONFIG_DIR, regenerate_mobile_token

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


# Windows reserved device basenames — invalid as filenames (with or without an
# extension, case-insensitive) on Windows. Sanitized on every OS so uploads are
# portable.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize_upload_filename(filename: str) -> str:
    """Return a safe basename for a client-supplied upload filename.

    Applied on all platforms (important on Windows): keep the basename only,
    drop the chars ``<>:"/\\|?*`` and control chars, strip leading/trailing
    dots and spaces, and rename Windows reserved basenames. Always returns a
    non-empty name (falls back to ``upload.bin``).
    """
    name = os.path.basename(filename or "")
    name = "".join(
        ch for ch in name
        if ch not in '<>:"/\\|?*' and ord(ch) >= 32
    )
    name = name.strip(" .")
    if not name:
        return "upload.bin"
    stem = name.split(".", 1)[0]
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name

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
            try:
                from osia.models import provider_for_model
                provider = provider_for_model(selected)
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
def _kokoro_to_mp3_bytes(text: str) -> bytes:
    """Synthesize text with Kokoro and return mp3 bytes."""
    from kokoro import KPipeline
    from osia.voice.tts import detect_language, KOKORO_VOICE_FR, KOKORO_VOICE_EN, KOKORO_SPEED, KOKORO_SR

    lang = detect_language(text)
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


def _openai_to_mp3_bytes(text: str) -> bytes:
    """Synthesize text with OpenAI TTS and return mp3 bytes.

    Rotates through the user's OpenAI key chain on a 429/5xx/auth/timeout error
    (shared rotation via :func:`osia.utils.key_chain.with_key_rotation`) before
    the caller falls back to Kokoro. WAV→MP3 conversion stays outside the
    rotation so an ffmpeg failure never burns API keys.
    """
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


def _google_to_mp3_bytes(text: str) -> bytes:
    """Synthesize text with Gemini TTS (REST generateContent) and return mp3 bytes.

    Mirrors ``osia.voice.tts.speak_google``: Google *Cloud* TTS rejects simple
    Gemini API keys (401, OAuth2 required), so we call the Gemini
    generateContent endpoint, which accepts the ``x-goog-api-key`` header.
    Audio comes back as base64 24kHz 16-bit mono PCM, which we wrap as WAV and
    transcode to MP3 for the phone (the WebSocket protocol still ships mp3).
    """
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
    """Convert WAV audio bytes to MP3 via ffmpeg pipe."""
    # Resolve ffmpeg robustly: prefer one on PATH; on Windows fall back to the
    # bundled <project_root>/windows/ffmpeg.exe if present.
    ffmpeg_exe = shutil.which("ffmpeg")
    if ffmpeg_exe is None:
        bundled = PROJECT_DIR / "windows" / "ffmpeg.exe"
        if bundled.exists():
            ffmpeg_exe = str(bundled)
    if ffmpeg_exe is None:
        msg = (
            "ffmpeg not found: install ffmpeg and ensure it is on PATH "
            "(or provide windows/ffmpeg.exe on Windows)."
        )
        print(f"[OSIA Server] {msg}", flush=True)
        raise RuntimeError(msg)
    proc = subprocess.run(
        [ffmpeg_exe, "-y", "-i", "pipe:0", "-f", "mp3", "-b:a", bitrate, "pipe:1"],
        input=wav_bytes,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg conversion failed: {proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc.stdout


def synthesize_to_mp3_bytes(text: str) -> bytes:
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
            mp3 = _openai_to_mp3_bytes(text)
            print(
                f"[TTS] synthesize_to_mp3_bytes: done via openai ({len(mp3)} bytes)",
                flush=True,
            )
            return mp3
        except Exception as e:
            print(f"⚠️  OpenAI TTS failed, falling back to Kokoro: {e}")
    elif provider == "google" and has_key("google"):
        try:
            mp3 = _google_to_mp3_bytes(text)
            print(
                f"[TTS] synthesize_to_mp3_bytes: done via google ({len(mp3)} bytes)",
                flush=True,
            )
            return mp3
        except Exception as e:
            print(f"⚠️  Google TTS failed, falling back to Kokoro: {e}")

    mp3 = _kokoro_to_mp3_bytes(text)
    print(
        f"[TTS] synthesize_to_mp3_bytes: done via kokoro ({len(mp3)} bytes)",
        flush=True,
    )
    return mp3


# ---------------------------------------------------------------------------
# Audio decoding (base64 WAV → numpy array)
# ---------------------------------------------------------------------------
def decode_base64_wav(b64_data: str) -> tuple[np.ndarray, int]:
    """Decode a base64-encoded WAV string into (samples, sample_rate)."""
    raw = base64.b64decode(b64_data)
    buf = io.BytesIO(raw)
    audio, sr = sf.read(buf, dtype="float32", always_2d=False)
    return audio, sr


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="OSIA Mobile Server", version="1.0.0")


# Registry of authenticated mobile clients (WOR-10). Holds each connection's
# OUTBOUND CHANNEL (``_OutboundChannel``) — NOT the raw WebSocket — so a
# server-initiated fan-out frame (e.g. the "new chat" reset) is funnelled
# through that connection's single writer task instead of being sent from a
# foreign coroutine (which would race the connection's own writer and corrupt
# Starlette's send state). Populated after a successful auth, pruned on
# disconnect.
_WS_CLIENTS: set["_OutboundChannel"] = set()

# WOR-96 PART 2a: strong references to in-flight "drive OAuth" background tasks
# spawned by the mobile mcp_connect handler. asyncio keeps only a WEAK reference
# to a bare create_task() result, so a fire-and-forget task can be garbage
# collected mid-await ("Task was destroyed but it is pending"). Holding the task
# here until it completes prevents that; the done-callback prunes it.
_OAUTH_DRIVE_TASKS: set = set()


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
    change. ``put`` on an unbounded queue never blocks, so
    ``await channel.send_json(...)`` returns immediately — it does NOT wait for
    the network. That is exactly what keeps the receive loop free while a
    response streams.
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send_json(self, obj) -> None:
        await self._queue.put(("json", obj))

    async def send_text(self, text: str) -> None:
        await self._queue.put(("text", text))

    async def close(self) -> None:
        """Signal the writer task to drain what's queued, then stop."""
        await self._queue.put((_WRITER_STOP, None))


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
    try:
        from osia.tools.gmail import gmail_connection_status

        raw = gmail_connection_status(validate=False)
        status = raw if isinstance(raw, dict) else {}
    except Exception:
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
    reason = ""
    if not connected:
        error = status.get("error") or status.get("reason") or ""
        reason = str(error).strip()[:500] if error else ""
    return {
        "connected": connected,
        "status": state,
        "emailAddress": email,
        "capabilities": {
            "readonly": bool(
                caps.get("readOnly")
                or caps.get("readonly")
                or status.get("readOnlyConnected")
            ),
            "compose": bool(caps.get("compose") or status.get("composeEnabled")),
            "modify": bool(caps.get("modify") or status.get("modifyEnabled")),
        },
        "reason": reason,
    }


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
    cfg = load_settings()
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
    cfg["personality"] = personality.load_personality()
    cfg["skills"] = [
        {
            "name": n,
            "enabled": personality.is_skill_enabled(n),
            "text": personality.read_skill(n),
        }
        for n in personality.list_skills()
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
# An OAuth MCP connect can complete via https://osia.space/oauth/callback (routed
# by the Cloudflare named tunnel to THIS server on :8765) IN ADDITION to the PC
# loopback (127.0.0.1:8975). The flow runs ENTIRELY in this server process: the
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

    Reached through the Cloudflare named tunnel (osia.space → this server). An
    OAuth provider redirects the user's browser here with ``?code=&state=`` after
    an interactive login that an MCP connect flow started IN THIS PROCESS (the
    tunnel branch of ``MCPManager._serve``). We match ``state`` to the in-flight
    flow and hand ``(code, state)`` to the waiting provider's ``callback_handler``
    — across the FastAPI ↔ MCP-manager event loops via a
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
    # WOR-100 Phase 2: a non-DCR (pre-registered-client) connector — Gmail/Drive —
    # is wired for forced OAuth ONLY on the PC loopback branch (manager._serve
    # gated on static_client). The tunnel branch does NOT force the flow nor seed
    # the static client, so a mobile/tunnel attempt would 200-handshake into a
    # FALSE CONNECTED with no token (Google 200s unauthenticated). Refuse it here
    # with a clear message until a later phase wires forced auth onto the tunnel;
    # the user authorizes these on the PC (loopback) for now. DCR connectors are
    # unaffected (oauth_no_dcr falsy).
    if saved.get("oauth_no_dcr"):
        return {
            "ok": False,
            "error": "authorize this connector on the PC (loopback) for now",
        }

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
            # WOR-98: use the rendezvous Worker as the OAuth redirect target so the
            # mobile/tunnel flow works for ANY user via their quick tunnel — not
            # only the owner's named osia.space tunnel. The redirect_uri becomes the
            # STABLE per-user ``<worker>/oauth/cb/<rendezvous_key>``; the Worker
            # forwards the callback to THIS PC's current trycloudflare
            # ``/oauth/callback`` (host-agnostic, unchanged). Gated on a mobile
            # token existing: with no token, leave cfg untouched so _serve falls
            # back to today's [osia.space, loopback] behavior (no regression).
            try:
                from osia.config import load_mobile_token, rendezvous_key
                from osia.rendezvous import WORKER_URL
                _tok = load_mobile_token()
                if _tok:
                    cfg["oauth_redirect_uri"] = (
                        WORKER_URL.rstrip("/") + "/oauth/cb/" + rendezvous_key(_tok)
                    )
            except Exception:
                pass
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
            print(f"[OSIA Server] mcp_oauth_tunnel_connect worker {sid!r}: {e}")
            try:
                fail_oauth_trigger_flow(fid, f"{type(e).__name__}: {e}")
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
}
# Guards `_autonomous` lifecycle mutations + the lockfile across the event-loop
# thread and the orchestrator/worker daemon threads. Non-reentrant — the helpers
# below NEVER nest acquisitions of it.
_autonomous_lock = threading.Lock()

# Sentinel so _autonomous_frame() can tell "snapshot the current run" (default)
# apart from an explicit "no run → empty snapshot" (st=None).
_AUTO_UNSET = object()


def _autonomous_snapshot(st) -> dict:
    """WS-contract snapshot (tasks_done/tasks_total + current phase/status)
    computed from a task_state dict. Pure reads — safe on any thread. Mirrors
    main.py's ``_orch_snapshot`` but emits the mobile WS field names."""
    if not isinstance(st, dict):
        return {}
    done, total, phase_name = _astate.count_progress(st)
    return {
        "tasks_done": done,
        "tasks_total": total,
        "phase_name": phase_name,
        "objective": st.get("objective", "") or "",
        "status": st.get("status", "") or "",
    }


def _autonomous_frame(payload: dict, st=_AUTO_UNSET) -> dict:
    """Build an outbound ``autonomous_progress`` frame: the event payload
    enriched with the run snapshot. The engine only emits
    task_done/task_failed/salvaged/done/failed; the host synthesizes
    running/interrupted/aborted/plan_failed/busy/idle. Pass ``st=None`` to force
    an empty snapshot (no run); omit ``st`` to snapshot the current run."""
    if st is _AUTO_UNSET:
        st = _autonomous.get("st")
    return {
        "type": "autonomous_progress",
        **dict(payload),
        **_autonomous_snapshot(st),
    }


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
    if loop is None:
        return
    for ch in _snapshot_ws_clients():
        try:
            asyncio.run_coroutine_threadsafe(ch.send_json(frame), loop)
        except Exception:
            pass


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
    except OSError as e:
        print(f"[WOR-45] could not write autonomous lockfile: {e}")


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
    except Exception as e:
        print(f"[WOR-45] terminal orchestrator.stop failed: {e}")
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
        if event in ("done", "failed"):
            # ── Terminal: gate a paused/superseded orphan's late terminal ──
            if _autonomous.get("interrupting") or not _autonomous.get("active"):
                print(f"[WOR-45] gating stale autonomous terminal {event!r} "
                      f"(interrupting={_autonomous.get('interrupting')}, "
                      f"active={_autonomous.get('active')}) — orphan-loop race; "
                      "drain/resume decides the real state from disk")
                return
            frame = _autonomous_frame(dict(payload), _autonomous.get("st"))
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
    except Exception as e:
        print(f"[WOR-45] autonomous progress_cb error: {e}")


def _ensure_autonomous_engine():
    """Lazily build (exactly once) the dedicated server-side autonomous agent
    and its Orchestrator — NEVER shared with conn_agents. Returns
    ``(agent, orchestrator)``."""
    with _autonomous_lock:
        if _autonomous.get("orch") is None:
            from osia.agent import OSIAAgent
            agent = OSIAAgent()
            _autonomous["agent"] = agent
            _autonomous["orch"] = _Orchestrator(
                agent, progress_cb=_on_autonomous_progress
            )
        return _autonomous["agent"], _autonomous["orch"]


def _autonomous_start_worker(objective: str, model: str, my_gen: int) -> None:
    """Plan + launch OFF the event loop (planning blocks on an LLM run). The
    single-runner slot (generation ``my_gen``) was already acquired by the
    handler. Mirrors main.py's ``handle_autonomous_start``: set_model → resolve
    provider → create_plan_state → save → start → emit running.

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
            _broadcast_autonomous(_autonomous_frame({"event": "plan_failed"}, None))
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

        # Honour the requested model; fall back to the configured one so the run
        # uses the user's selected model rather than the bare OSIAAgent() default.
        if not model:
            try:
                from osia.config import load_settings
                model = (load_settings().get("selected_model") or "").strip()
            except Exception:
                model = ""
        if model:
            try:
                agent.set_model(model)
            except Exception as e:
                print(f"[WOR-45] autonomous set_model({model!r}) failed: {e}")
        # Resolve the provider from the model id (osia.models), like the GUI passes.
        try:
            from osia.models import provider_for_model
            provider = provider_for_model(model) if model else ""
        except Exception:
            provider = ""
        try:
            st = _aplanner.create_plan_state(objective, provider, model, agent)
        except Exception as e:
            print(f"[WOR-45] autonomous planning crashed: {e}")
            st = None
        if not st:
            _broadcast_autonomous(_autonomous_frame({"event": "plan_failed"}, None))
            _release_single_runner_if_gen(my_gen)
            return

        # ── Establish the run atomically — only if our slot is still current ──
        established = False
        with _autonomous_lock:
            if _autonomous.get("active") and int(_autonomous.get("gen", 0)) == my_gen:
                _autonomous["st"] = st
                _write_autonomous_lock(st.get("task_id"))  # stamp the real id
                established = True
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
        except Exception as e:
            print(f"[WOR-45] save_task_state failed: {e}")
        orch.start(st)
        # Flip "planning" → "running" only AFTER start() spawned the loop. The
        # engine emits events only on task transitions, never on start, so this
        # won't double-fire.
        _broadcast_autonomous(_autonomous_frame({"event": "running"}, st))
    except Exception as e:
        # Any unexpected failure (e.g. OSIAAgent build error, or thread
        # exhaustion in orch.start) must NOT wedge the slot at busy forever:
        # release it (only if still ours) and tell clients the start failed.
        print(f"[WOR-45] autonomous start worker crashed: {e}")
        _broadcast_autonomous(_autonomous_frame({"event": "plan_failed"}, None))
        _release_single_runner_if_gen(my_gen)
        # ORPHAN-LOOP RACE (WOR-45): a crash BEFORE the drain reopened the gate
        # (e.g. _ensure_autonomous_engine raised) must not leave it stuck — but
        # only reopen if no orphan is alive (else its terminal must stay gated;
        # the interrupt backstop / next drain reopens it once the orphan dies).
        if not _orch_loop_alive():
            _autonomous["interrupting"] = False


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


def _autonomous_resume_worker(my_gen: int) -> None:
    """Resume a PAUSED autonomous run OFF the event loop, converging BYTE-FOR-BYTE
    onto the PROVEN GUI resume flow (``main.py``: ``load_task_state()`` →
    ``set_model(state["model"])`` → ``orchestrator.resume(state)``): resume ONLY
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
            # Nothing on disk to resume (no pending run).
            _broadcast_autonomous(_autonomous_frame({"event": "idle"}, None))
            _release_single_runner_if_gen(my_gen)
            return
        status = st.get("status", "")
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
        # Honour the model the run was planned with (main.py: set_model(state["model"])).
        model = st.get("model")
        if model:
            try:
                agent.set_model(model)
            except Exception as e:
                print(f"[WOR-45] autonomous resume set_model({model!r}) failed: {e}")

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
    except Exception as e:
        # Any unexpected failure (engine build, orch.resume thread exhaustion)
        # must NOT wedge the slot at busy forever: release it (only if still ours)
        # and tell clients the resume did not take.
        print(f"[WOR-45] autonomous resume worker crashed: {e}")
        _broadcast_autonomous(_autonomous_frame({"event": "plan_failed"}, None))
        _release_single_runner_if_gen(my_gen)
        # ORPHAN-LOOP RACE (WOR-45): a crash BEFORE the drain reopened the gate
        # (engine build / disk load) must not leave it stuck. Reopen only if no
        # orphan is alive; otherwise the interrupt backstop / next drain reopens
        # it once the orphan dies (its terminal must stay gated until then).
        if not _orch_loop_alive():
            _autonomous["interrupting"] = False


@app.on_event("shutdown")
async def _stop_autonomous_on_shutdown() -> None:
    """Server cleanup: stop the autonomous loop (safe if never started) and
    release the single-runner slot so a restart isn't wrongly seen as busy."""
    orch = _autonomous.get("orch")
    if orch is not None:
        try:
            orch.stop()
        except Exception as e:
            print(f"[WOR-45] orchestrator.stop on shutdown failed: {e}")
    _release_single_runner()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    authenticated = False
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
    # Live system-metrics push task (heartbeat_data frames). Created once after
    # a successful auth, cancelled in the finally block on disconnect.
    heartbeat_task: asyncio.Task | None = None

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
            print(f"[OSIA Server] create_conversation failed: {e}")
            traceback.print_exc()
            await out.send_json({"type": "status", "content": "error"})
            return None
        active_conv_id = conv["id"]
        session_history = []
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

    def _spawn_turn(conv_id, make_coro):
        # conv_id is always a real id here (resolved via _ensure_active_conv
        # before spawning), so the task is keyed by it: a follow-up turn on
        # the same conversation chains after this one, and delete/disconnect
        # can find and cancel it.
        prev = streaming_tasks.get(conv_id)
        task = asyncio.create_task(_run_turn(conv_id, prev, make_coro))
        streaming_tasks[conv_id] = task

        def _clear(t):
            if streaming_tasks.get(conv_id) is t:
                streaming_tasks.pop(conv_id, None)

        task.add_done_callback(_clear)

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
            await out.send_json({
                "type": "conversation_history",
                "id": conv_id,
                "conversation_id": conv_id,
                "title": conv.get("title"),
                "messages": _displayable_history(messages),
            })
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
                print(f"[WS] first message: {repr(raw_msg)[:200]}")
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                print(f"[WS] bad JSON frame (not closing): {repr(raw_msg)[:120]}")
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
                masked = (
                    f"{token[:4]}…{token[-4:]} (len={len(token)})"
                    if token else "<empty>"
                )
                print(
                    f"[WS] auth attempt: token={masked} "
                    f"expected_len={len(MOBILE_TOKEN)} matches={matched}"
                )
                if matched:
                    authenticated = True
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
                await out.send_json({
                    "type": "conversation_history",
                    "id": conv_id,
                    # Additive routing field (mirrors response_chunk/done) so a
                    # client can match this history to a conversation tab even
                    # while another conversation is streaming. Old clients that
                    # only read "id" keep working.
                    "conversation_id": conv_id,
                    "title": conv.get("title"),
                    "messages": _displayable_history(session_history),
                })
                continue

            if msg_type == "delete_conversation":
                conv_id = msg.get("id", "")
                # Cancel an in-flight stream for this conversation (if any) so
                # it stops persisting to a file we're about to remove.
                doomed = streaming_tasks.pop(conv_id, None)
                if doomed is not None:
                    doomed.cancel()
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
                await _broadcast_conversations(exclude=out)
                continue

            # --- Models ---
            if msg_type == "get_models":
                try:
                    from osia.models import get_all_models
                    from osia.config import load_settings
                    models = get_all_models()
                    current = load_settings().get("selected_model")
                except Exception as e:
                    print(f"[OSIA Server] get_models failed: {e}")
                    models, current = [], None
                await out.send_json({
                    "type": "models_list",
                    "models": models,
                    "current": current,
                })
                continue

            if msg_type == "set_model":
                model_id = (msg.get("model") or "").strip()
                if not model_id:
                    await out.send_json({"type": "status", "content": "error"})
                    continue
                try:
                    from osia.config import set_setting
                    set_setting("selected_model", model_id)
                    print(f"[OSIA Server] selected_model -> {model_id!r}")
                    await out.send_json({"type": "status", "content": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_model({model_id!r}) failed: {e}")
                    await out.send_json({"type": "status", "content": "error"})
                continue

            # --- Autonomous agent (WOR-45 Phase 5) ---
            # Start / interrupt / abort / observe ONE machine-global autonomous
            # run over this WebSocket. The engine (planner/orchestrator/state) is
            # consumed as a black box; progress frames are BROADCAST to every
            # connected client (the run is machine-global, not per-connection).
            if msg_type == "autonomous_start":
                objective = (msg.get("objective") or "").strip()
                model = (msg.get("model") or "").strip()
                if not objective:
                    # Malformed request from this client — reject it directly.
                    await out.send_json(
                        _autonomous_frame({"event": "plan_failed"}, None)
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
                # Plan + start OFF the event loop (planning blocks on an LLM run).
                threading.Thread(
                    target=_autonomous_start_worker,
                    args=(objective, model, _gen),
                    daemon=True,
                ).start()
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
                # Resume OFF the event loop (resume() spawns the loop thread and
                # joins ≤2 s on its first checkpoint save — never block the loop).
                threading.Thread(
                    target=_autonomous_resume_worker,
                    args=(_gen,),
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
                            except Exception as e:
                                print(f"[WOR-45] orchestrator.stop (interrupt) failed: {e}")
                        _broadcast_autonomous(
                            _autonomous_frame({"event": "interrupted"},
                                              _autonomous.get("st"))
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
                # Discard. Mark the SHARED state dict aborted FIRST — before
                # stop() — so a late daemon checkpoint on its way out persists
                # status="aborted" (never in_progress), which a resume ignores.
                # Then stop the loop, delete the checkpoint, emit, release.
                def _abort_worker():
                    st = _autonomous.get("st")
                    if isinstance(st, dict):
                        st["status"] = "aborted"
                    orch = _autonomous.get("orch")
                    if orch is not None:
                        try:
                            orch.stop()
                        except Exception as e:
                            print(f"[WOR-45] orchestrator.stop (abort) failed: {e}")
                    try:
                        _astate.clear_task_state()
                    except Exception as e:
                        print(f"[WOR-45] clear_task_state failed: {e}")
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
                    status = st.get("status", "")
                    if status == "in_progress":
                        # A LIVE run keeps active=True; interrupt cleared it (the
                        # run is PAUSED but state stays in_progress ⇒ resumable),
                        # and a fresh process recovering st from disk has no
                        # in-memory active run either. Report "interrupted" in
                        # both cases so a reconnecting phone can offer resume
                        # (consistent with the GUI offering resume on launch);
                        # only a genuinely live run (active True) is "running".
                        ev = "running" if _autonomous.get("active") else "interrupted"
                    else:
                        ev = {
                            "done": "done",
                            "failed": "failed",
                            "aborted": "aborted",
                        }.get(status, "running")
                    await out.send_json(_autonomous_frame({"event": ev}, st))
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
                    from osia.config import (
                        set_api_key, load_api_keys, save_api_keys,
                    )
                    if key:
                        set_api_key(provider, key)
                        print(
                            f"[OSIA Server] set_api_key provider={provider!r} "
                            f"(set, len={len(key)})"
                        )
                    else:
                        # An empty value means "delete": drop the entry from
                        # api_keys.json entirely rather than storing "". Same
                        # behavior as the delete_api_key handler below.
                        keys = load_api_keys()
                        keys.pop(provider, None)
                        save_api_keys(keys)
                        print(
                            f"[OSIA Server] set_api_key provider={provider!r} "
                            f"(empty -> deleted)"
                        )
                    # Reused agents must re-read this provider's key next turn.
                    _invalidate_cached_models(provider)
                    await out.send_json({"type": "status", "status": "done"})
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
                    from osia.config import load_api_keys, save_api_keys
                    keys = load_api_keys()
                    existed = keys.pop(provider, None) is not None
                    save_api_keys(keys)
                    print(
                        f"[OSIA Server] delete_api_key provider={provider!r} "
                        f"({'removed' if existed else 'absent'})"
                    )
                    # Reused agents must re-read this provider's key next turn.
                    _invalidate_cached_models(provider)
                    await out.send_json({"type": "status", "status": "done"})
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
                    "wake_word_enabled",
                    "wake_greeting_enabled", "wake_greeting_text",
                    "cron_jobs", "ai_hierarchy_enabled",
                    # Flat aliases for the two AI-hierarchy fallback chains; the
                    # write path below folds them into ai_hierarchy.{heavy,medium}.
                    "heavy_chain", "medium_chain",
                    # Live system-metrics push: master flag (shared with the
                    # desktop monitor) + seconds-granularity push cadence.
                    "heartbeat_enabled", "heartbeat_interval_seconds",
                    # Cooperation IA vision provider/model (WOR-25).
                    "vision_provider", "vision_model",
                    # MCP / Connectors server list (WOR-44 Phase 4). The list is
                    # NON-SECRET (env is always {} — see the mcp_servers write
                    # branch below); per-server tokens live only in the
                    # gitignored api_keys.json under `mcp:<id>`.
                    "mcp_servers",
                    # MCP write-tool confirmation gate (WOR-44 Phase 5, 5-conf-a):
                    # NON-secret global bool toggles. mcp_confirm_writes gates
                    # write/destructive MCP tools; mcp_allow_unattended_writes
                    # governs unattended runs. Both flow through the generic
                    # set_setting path (no special handling).
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
                    from osia.config import set_setting, get_setting
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
                    else:
                        set_setting(setting_key, setting_value)
                    # WOR-44 Phase 5: for mcp_servers log the SANITIZED `clean`
                    # list (env={} + headers stripped), never the raw value — a
                    # malicious/buggy client could embed a token in env/headers,
                    # and the raw value would otherwise leak it to the journal in
                    # plaintext. `clean` is only referenced on the mcp_servers
                    # branch, where it is always bound.
                    logged_value = clean if setting_key == "mcp_servers" else setting_value
                    print(
                        f"[OSIA Server] set_setting {setting_key!r} -> "
                        f"{logged_value!r}"
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
                    print(
                        f"[OSIA Server] get_config sent "
                        f"({len(cfg.get('skills', []))} skills)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] get_config failed: {e}")
                    traceback.print_exc()
                    await out.send_json({"type": "status", "status": "error"})
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
                if not server_id:
                    await out.send_json(
                        {"type": "status", "status": "error", "error": "missing id"}
                    )
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

                if msg_type == "mcp_status":
                    # Pure, non-blocking read of the manager snapshot (never
                    # leaks secrets — see _mcp_status_frame). One id, or all.
                    try:
                        await out.send_json(_mcp_status_frame(server_id or None))
                    except Exception as e:
                        print(f"[OSIA Server] mcp_status failed: {e}")
                        await out.send_json({"type": "status", "status": "error"})
                    continue

                # connect / disconnect require a known, saved server id.
                if not server_id:
                    await out.send_json(
                        {"type": "status", "status": "error", "error": "missing id"}
                    )
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
                except Exception as e:
                    print(f"[OSIA Server] set_personality failed: {e}")
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
                        personality.set_skill_enabled(
                            skill_name, bool(msg.get("enabled"))
                        )
                    print(
                        f"[OSIA Server] set_skill name={skill_name!r} "
                        f"text={'text' in msg} "
                        f"enabled={msg.get('enabled') if 'enabled' in msg else '—'}"
                    )
                    await out.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_skill({skill_name!r}) failed: {e}")
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
                    print(f"[OSIA Server] create_skill -> {created!r}")
                    await out.send_json({
                        "type": "status", "status": "done", "name": created,
                    })
                except Exception as e:
                    print(f"[OSIA Server] create_skill({skill_name!r}) failed: {e}")
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
                    print(f"[OSIA Server] delete_skill name={skill_name!r}")
                    await out.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] delete_skill({skill_name!r}) failed: {e}")
                    await out.send_json({"type": "status", "status": "error"})
                continue

            # --- Speak on demand (read-aloud button on the phone) ---
            # Synthesize the text to MP3 and send it BACK to the phone as an
            # audio frame — the phone plays it. This is NOT PC-speaker
            # playback: the button lives on the phone, so the voice must
            # come out of the phone.
            if msg_type == "speak":
                speak_text = (msg.get("text") or "").strip()
                if not speak_text:
                    continue
                print(
                    f"[OSIA Server] speak request "
                    f"({len(speak_text)} chars: {speak_text[:60]!r})"
                )
                try:
                    # Off-thread so the event loop keeps serving while the
                    # TTS provider synthesizes.
                    mp3_bytes = await asyncio.to_thread(
                        synthesize_to_mp3_bytes, speak_text,
                    )
                    mp3_b64 = base64.b64encode(mp3_bytes).decode("ascii")
                    # source="speak" tells the client this audio was
                    # explicitly requested, so it plays even when the
                    # auto-TTS preference is off.
                    await out.send_json({
                        "type": "audio",
                        "format": "mp3",
                        "data": mp3_b64,
                        "source": "speak",
                    })
                    print(
                        f"[OSIA Server] speak audio sent to mobile "
                        f"({len(mp3_bytes)} bytes)"
                    )
                except Exception as e:
                    # No status frame here: speak is not a turn, and a
                    # status error/done would wrongly tear down the
                    # streaming UI of any in-flight response.
                    print(f"[OSIA Server] speak failed: {e}")
                    traceback.print_exc()
                continue

            # --- File upload ---
            if msg_type == "file":
                filename = (msg.get("filename") or "upload.bin").strip()
                b64_data = msg.get("data", "")
                if not b64_data:
                    await out.send_json({
                        "type": "file_received",
                        "filename": filename,
                        "path": "",
                        "success": False,
                    })
                    continue
                try:
                    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    # Sanitize the client-supplied name (basename only, strip
                    # illegal/reserved chars & Windows device names), prefix
                    # with a timestamp to avoid clobbering.
                    safe_name = _sanitize_upload_filename(filename)
                    target = UPLOAD_DIR / f"{int(time.time() * 1000)}_{safe_name}"
                    target.write_bytes(base64.b64decode(b64_data))
                    image_mime = _image_mime_for(target)
                    if image_mime is None:
                        # Non-image: queue for the next text/audio turn.
                        pending_files.append(str(target))
                    print(
                        f"[OSIA Server] file received: {safe_name!r} -> {target} "
                        f"({target.stat().st_size} bytes)"
                    )
                    await out.send_json({
                        "type": "file_received",
                        "filename": safe_name,
                        "path": str(target),
                        "success": True,
                    })
                    if image_mime is not None:
                        # Image: re-read from disk, base64-encode, and trigger
                        # the agent immediately with a DeepSeek Vision message
                        # so the model can actually see and analyze the image.
                        img_b64 = base64.b64encode(target.read_bytes()).decode("ascii")
                        vision_content = [{
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_mime};base64,{img_b64}",
                            },
                        }]
                        print(
                            f"[OSIA Server] image upload — triggering vision agent "
                            f"({image_mime}, {len(img_b64)} b64 chars)"
                        )
                        # Run the vision turn in its own task so the receive
                        # loop stays responsive while the model analyses the
                        # image (same head-of-line fix as text/audio). Resolve
                        # the conversation FIRST so the turn is keyed by a real
                        # id (no synthetic placeholder, no duplicate-conv race).
                        _conv = await _ensure_active_conv(vision_content)
                        if _conv is not None:
                            _spawn_turn(
                                _conv,
                                lambda snap, _vc=vision_content, _c=_conv:
                                    _run_agent_and_respond(
                                        out, _vc, snap, _c,
                                        get_agent=_get_conn_agent),
                            )
                except Exception as e:
                    print(f"[OSIA Server] file upload failed: {e}")
                    await out.send_json({
                        "type": "file_received",
                        "filename": filename,
                        "path": "",
                        "success": False,
                    })
                continue

            # --- Text message ---
            if msg_type == "text":
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue
                # Consume pending files synchronously (clears the shared list)
                # so this turn — and only this turn — carries them. Resolve the
                # conversation synchronously (creating it on the first message)
                # so the turn is keyed by a real id, then run the agent in its
                # own task so the receive loop stays free.
                user_text = _consume_pending_files(user_text, pending_files)
                _conv = await _ensure_active_conv(user_text)
                if _conv is not None:
                    _spawn_turn(
                        _conv,
                        lambda snap, _t=user_text, _c=_conv:
                            _run_agent_and_respond(
                                out, _t, snap, _c,
                                get_agent=_get_conn_agent),
                    )

            # --- Audio message ---
            elif msg_type == "audio":
                b64_data = msg.get("data", "")
                audio_format = msg.get("format", "wav")
                if not b64_data:
                    continue
                # Snapshot + clear pending files at spawn time so they attach
                # to THIS turn regardless of uploads that arrive during STT.
                _pf = list(pending_files)
                pending_files.clear()
                # Resolve the conversation synchronously (default title; the
                # transcript upgrades it on first persist) so concurrent first
                # messages serialise into ONE conversation.
                # TRADE-OFF (accepted): a silent/blank first recording now
                # leaves an empty conversation, since the id is reserved before
                # STT runs. This is the cost of having a real id at spawn time
                # (which fixes the duplicate-conversation race); the stray conv
                # is harmless and reused by the next real message.
                _conv = await _ensure_active_conv(None)
                if _conv is not None:
                    _spawn_turn(
                        _conv,
                        lambda snap, _b=b64_data, _f=audio_format, _c=_conv, _files=_pf:
                            _handle_audio(out, _b, _f, snap, _c,
                                          pending_files=_files,
                                          get_agent=_get_conn_agent),
                    )

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
        # Drop this client from the broadcast registry (WOR-10). discard() is
        # a no-op if it was never added (e.g. auth failed).
        _WS_CLIENTS.discard(out)
        # WOR-44 Phase 5 (5-conf-c): deny + drop any MCP confirmation still pending
        # for THIS connection so a blocked agent worker thread (a detached turn's
        # write tool awaiting approval) wakes immediately with a deny instead of
        # hanging until the timeout. No-op if none are pending.
        _deny_confirms_for_channel(out)
        # WOR-62: DETACH any in-flight streaming turn for this connection
        # instead of cancelling it. Cancelling raised CancelledError inside the
        # turn (at the ``await asyncio.to_thread(...)`` agent run) BEFORE it
        # reached its single persist point (_persist_new_messages), so a phone
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
        out.append({"role": role, "content": text})
    return out


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


def _consume_pending_files(user_text: str, pending_files: list[str]) -> str:
    """Prepend any pending uploaded file paths to ``user_text`` so the agent
    sees them as part of the next user turn, then clear the list.

    The injected block is plain text — the agent can read those files with
    its existing filesystem tools.
    """
    if not pending_files:
        return user_text
    paths = list(pending_files)
    pending_files.clear()
    bullets = "\n".join(f"- {p}" for p in paths)
    return f"[Fichiers joints disponibles localement :\n{bullets}\n]\n\n{user_text}"


def _persist_new_messages(conv_id: str | None, full_history: list,
                          prev_len: int) -> None:
    """Append messages added since the last turn to the conversation file.

    ``full_history`` is the agent's post-run message list (sans system
    prompt), ``prev_len`` is its length before the turn started.
    """
    if conv_id is None:
        return
    for msg in full_history[prev_len:]:
        try:
            conversations.add_message(
                conv_id, msg.get("role"), msg.get("content", ""),
            )
        except Exception as e:
            print(f"[OSIA Server] failed to persist message: {e}")


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
async def _run_agent_and_respond(ws: "_OutboundChannel", text, history: list,
                                 active_conv_id: str | None,
                                 *, t0: float | None = None,
                                 get_agent=None) -> tuple[list, str]:
    """Run the agent with the given conversation history, persist the
    new turn, stream the spoken response sentence-by-sentence, then send
    the assembled reply + TTS audio.

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
            _ts(t0, "text received", f"({len(text)} chars: {text[:60]!r})")
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
        await ws.send_json({
            "type": "active_conversation",
            "id": conv["id"],
            "title": conv["title"],
        })
        await ws.send_json({
            "type": "conversations",
            "items": conversations.list_conversations(),
        })

    prev_len = len(history)

    # Status: thinking (single small send before the agent kicks off —
    # cheap, fire-and-forget effectively, mobile uses it for UI state).
    await ws.send_json({
        "type": "status", "content": "thinking",
        "conversation_id": active_conv_id,
    })

    # ── Streaming plumbing ────────────────────────────────────────────
    # The agent runs in a worker thread (so it doesn't block the event
    # loop). Each completed sentence fires `on_sentence`, which schedules
    # an asyncio.Queue.put on the event loop. A local sender task drains
    # the queue and pushes each sentence to the WebSocket as a
    # `response_chunk` message — the first chunk lands as soon as the
    # model produces its first sentence.
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

    def on_first_token() -> None:
        # Called from the agent worker thread the moment the first
        # non-empty delta arrives back from the LLM.
        if not first_token_logged[0]:
            first_token_logged[0] = True
            _ts(t0, "first model token received")

    def on_sentence(sentence: str) -> None:
        if not sentence:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                chunk_queue.put(sentence), loop,
            )
            pending_puts.append(fut)
        except Exception as e:
            print(f"[OSIA Server] on_sentence dispatch failed: {e}")

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
                _ts(t0, "first response_chunk sent to mobile",
                    f"({len(item)} chars: {item[:60]!r})")
            try:
                print(f"[DEBUG] sending to mobile: {repr(item[:200])}", flush=True)
                await ws.send_json({
                    "type": "response_chunk", "content": item,
                    "conversation_id": active_conv_id,
                })
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
            print(f"[OSIA Server]   last 2 history items: {tail_preview}")
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

        def _run_agent_busy_tracked():
            try:
                return agent.run(
                    text,
                    history=history,
                    on_sentence=on_sentence,
                    on_first_token=on_first_token,
                )
            finally:
                agent._in_use = False

        # Off-thread so the event loop stays free to flush response_chunks.
        response_text = await asyncio.to_thread(_run_agent_busy_tracked)
        print(
            f"[DEBUG] agent.run() returned: {repr((response_text or '')[:200])}",
            flush=True,
        )
    except asyncio.CancelledError:
        # Turn cancelled (client disconnected, or the conversation was
        # deleted/superseded mid-stream). The agent worker thread is
        # uncancellable and keeps feeding chunk_queue, so reap the child
        # sender task explicitly here — `except Exception` below does NOT
        # catch CancelledError — then propagate the cancellation.
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass
        raise
    except Exception as e:
        print(f"[OSIA Server] Agent error: {e}")
        traceback.print_exc()
        # Drain any in-flight puts and shut the sender down cleanly.
        for fut in pending_puts:
            try:
                await asyncio.wrap_future(fut)
            except Exception:
                pass
        await chunk_queue.put(_SENTINEL)
        await sender_task
        await ws.send_json({
            "type": "response", "content": f"Erreur: {e}",
            "conversation_id": active_conv_id,
        })
        await ws.send_json({
            "type": "status", "content": "error",
            "conversation_id": active_conv_id,
        })
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

    # Strip any raw <tool_call>…</tool_call> blocks (tags AND JSON payload)
    # that survived the agent's own cleaning. This runs BEFORE the tail
    # flush, the final `response` message, and TTS synthesis — tool markup
    # must never reach the mobile client or be spoken.
    raw_response_text = response_text or ""
    if response_text:
        from osia.agent import strip_raw_tool_tags
        response_text = strip_raw_tool_tags(response_text).strip()
    print(
        f"[DEBUG] cleaned response: {repr((response_text or '')[:200])}",
        flush=True,
    )
    if not response_text and raw_response_text.strip():
        # The agent returned text but strip_raw_tool_tags() emptied it —
        # the reply was nothing but tool markup. Surface this loudly: it
        # means the agent-side cleaning/guards let markup through.
        print(
            "[WARN] agent reply stripped to empty by strip_raw_tool_tags() "
            f"(raw was {len(raw_response_text)} chars: "
            f"{raw_response_text[:200]!r})",
            flush=True,
        )

    # Explicit tail flush: if the agent's final response_text has content
    # that wasn't streamed as a chunk (race in the streaming flush path,
    # short tail under the early-flush threshold, …), emit the missing
    # tail as one last response_chunk so the client never loses the end
    # of a reply.
    if response_text:
        from osia.agent import clean_response
        sent_concat = "".join(sent_chunks)
        cleaned_full = clean_response(response_text).strip()
        if cleaned_full:
            tail = _tail_not_sent(cleaned_full, sent_concat)
            if tail:
                try:
                    print(f"[DEBUG] sending to mobile: {repr(tail[:200])}", flush=True)
                    await ws.send_json({
                        "type": "response_chunk", "content": tail,
                        "conversation_id": active_conv_id,
                    })
                    sent_chunks.append(tail)
                    _ts(t0, "tail response_chunk flushed",
                        f"({len(tail)} chars: {tail[:60]!r})")
                except Exception as e:
                    print(f"[OSIA Server] tail response_chunk send failed: {e}")

    # Pull the updated history back out of the agent so the next request
    # keeps context. Use exported_history() — it strips the system prompt
    # at index 0 AND defensively filters any system-prompt-looking frame,
    # so the system prompt cannot leak into the conversation history.
    new_history = agent.exported_history()

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

    # Persist everything added during this turn (user msg, any
    # assistant/tool rounds, final assistant reply).
    _persist_new_messages(active_conv_id, new_history, prev_len)
    print(
        f"[OSIA Server] ◀ post-persist history check: "
        f"len(session_history)={len(new_history)} after _persist_new_messages()"
    )
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
        print(f"[OSIA Server]   last 2 history items: {tail_preview}")

    if not response_text:
        # Prefer what was actually streamed to the client over a canned
        # apology — if sentences went out as response_chunks, the final
        # text and the TTS must match them.
        streamed = " ".join(sent_chunks).strip()
        if streamed:
            print(
                "[WARN] empty final response — falling back to the "
                f"{len(sent_chunks)} streamed chunk(s) ({len(streamed)} chars)",
                flush=True,
            )
            response_text = streamed
        else:
            print(
                "[WARN] empty final response and nothing was streamed — "
                "using the generic fallback message",
                flush=True,
            )
            response_text = "Désolé, je n'ai pas pu traiter votre demande."
            # Push the fallback as a chunk too so chunk-based clients
            # render it instead of an empty reply.
            try:
                print(
                    f"[DEBUG] sending to mobile: {repr(response_text[:200])}",
                    flush=True,
                )
                await ws.send_json({
                    "type": "response_chunk", "content": response_text,
                    "conversation_id": active_conv_id,
                })
                sent_chunks.append(response_text)
            except Exception as e:
                print(f"[OSIA Server] fallback response_chunk send failed: {e}")

    # Send the canonical full text once (clients that don't track chunks
    # can rely on this; clients that do can ignore it).
    print(f"[DEBUG] sending to mobile: {repr(response_text[:200])}", flush=True)
    await ws.send_json({
        "type": "response", "content": response_text,
        "conversation_id": active_conv_id,
    })

    # Synthesize TTS off-thread so the event loop keeps serving.
    try:
        tts_text = response_text
        print(f"[DEBUG] sending to TTS: {repr(tts_text[:200])}", flush=True)
        print(
            f"[TTS] server: calling synthesize_to_mp3_bytes() "
            f"({len(tts_text)} chars)",
            flush=True,
        )
        mp3_bytes = await asyncio.to_thread(synthesize_to_mp3_bytes, tts_text)
        print(
            f"[TTS] server: synthesize_to_mp3_bytes() returned "
            f"({len(mp3_bytes)} bytes)",
            flush=True,
        )
        mp3_b64 = base64.b64encode(mp3_bytes).decode("ascii")
        # source="response" → the client only auto-plays this when its
        # auto-TTS preference is on.
        await ws.send_json({
            "type": "audio",
            "format": "mp3",
            "data": mp3_b64,
            "source": "response",
            "conversation_id": active_conv_id,
        })
        _ts(t0, "first audio chunk sent to mobile", f"({len(mp3_bytes)} bytes)")
    except Exception as e:
        print(f"[OSIA Server] TTS synthesis failed: {e}")
        traceback.print_exc()

    # Brief settle so any chunks still in flight on the WebSocket land
    # before the client sees `done` and tears down the streaming UI.
    await asyncio.sleep(0.2)
    await ws.send_json({
        "type": "status", "content": "done",
        "conversation_id": active_conv_id,
    })
    _ts(t0, "request complete")
    return new_history, active_conv_id


async def _handle_audio(ws: "_OutboundChannel", b64_data: str, audio_format: str,
                        history: list,
                        active_conv_id: str | None,
                        *, pending_files: list[str] | None = None,
                        get_agent=None) -> tuple[list, str | None]:
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

    # Status: thinking
    await ws.send_json({
        "type": "status", "content": "thinking",
        "conversation_id": active_conv_id,
    })

    try:
        # Decode and resample
        audio, sr = decode_base64_wav(b64_data)
        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            await ws.send_json({
                "type": "transcript", "content": "",
                "conversation_id": active_conv_id,
            })
            await ws.send_json({
                "type": "status", "content": "done",
                "conversation_id": active_conv_id,
            })
            _ts(t0, "silent audio — short-circuit done")
            return history, active_conv_id

        if sr != WHISPER_SAMPLE_RATE:
            audio = resample_audio(audio, sr, WHISPER_SAMPLE_RATE)
            sr = WHISPER_SAMPLE_RATE

        # Whisper inference off-thread (sync, CPU-bound).
        transcript = await asyncio.to_thread(transcribe, audio, sample_rate=sr)
        _ts(t0, "transcription done",
            f"({len(transcript)} chars: {transcript[:60]!r})")

        if not transcript.strip():
            await ws.send_json({
                "type": "transcript", "content": "",
                "conversation_id": active_conv_id,
            })
            await ws.send_json({
                "type": "status", "content": "done",
                "conversation_id": active_conv_id,
            })
            return history, active_conv_id

        # Fire transcript send in parallel with the agent start — don't
        # await it. The send is just an enqueue onto the outbound channel,
        # so it lands long before the agent's first response_chunk, but
        # starting the agent immediately saves the ~1-50 ms a real-network
        # round-trip would otherwise cost.
        transcript_send = asyncio.create_task(
            ws.send_json({
                "type": "transcript", "content": transcript,
                "conversation_id": active_conv_id,
            })
        )

        agent_input = transcript
        if pending_files:
            agent_input = _consume_pending_files(agent_input, pending_files)

        try:
            return await _run_agent_and_respond(
                ws, agent_input, history, active_conv_id, t0=t0,
                get_agent=get_agent,
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
        await ws.send_json({
            "type": "status", "content": "error",
            "conversation_id": active_conv_id,
        })
        return history, active_conv_id


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
