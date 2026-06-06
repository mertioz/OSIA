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
  Server → Client:
    {"type": "api_keys",             "keys": {provider: key, ...}}     # actual stored keys, one entry per models.API_PROVIDERS ("" if unset)
    {"type": "heartbeat_config",     "config": {"enabled": bool, "interval_seconds": int}}
    {"type": "heartbeat_data",       "cpu_percent": float, "ram_mb": int,
                                     "ram_percent": float, "disk_percent": float,
                                     "cpu_temp": float|null, "gpu_temp": float|null,
                                     "timestamp": "<ISO>"}              # pushed every heartbeat_interval_seconds while heartbeat_enabled
    {"type": "transcript",           "content": "..."}
    {"type": "response_chunk",       "content": "..."}        # streamed sentence-by-sentence
    {"type": "response",             "content": "..."}        # final assembled reply
    {"type": "audio",                "format": "mp3", "data": "<base64>", "source": "response|speak"}
    {"type": "status",               "content": "thinking|done|error"}
    {"type": "conversations",        "items": [{id,title,created_at,updated_at,message_count}, ...]}
    {"type": "conversations_list",   "conversations": [{id,title}, ...]}  # compact list (≤50 most recent) for the mobile cron "linked conversation" picker
    {"type": "conversation_history", "id": "...", "title": "...",
                                     "messages": [{"role": "user|assistant", "content": "..."}]}
    {"type": "conversation_deleted", "id": "...", "success": true|false}
    {"type": "active_conversation",  "id": "...", "title": "..."}
    {"type": "new_chat"}                                               # broadcast to OTHER connected devices when one device starts a new chat, so every UI clears (WOR-10)
    {"type": "models_list",          "models": [{id,label,provider,tier}, ...], "current": "..."}
    {"type": "file_received",        "filename": "...", "path": "...", "success": true|false}
    {"type": "config",               "config": {...}}                  # full config snapshot (settings incl. api_key_fallbacks, personality, skills)
    {"type": "status",               "status": "done|error"}           # ack for the config-family setters (create_skill adds "name", restart_server adds "message": "restarting")
"""

import asyncio
import base64
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request

from osia import conversations
from osia.config import CONFIG_DIR as _CONFIG_DIR

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

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
def load_or_create_token() -> str:
    """Read the mobile token from disk, or generate + persist a new one."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            # Silently upgrade legacy 16-char tokens to 20 chars.
            if len(token) == 16:
                token = secrets.token_hex(10)  # 20 hex chars
                TOKEN_FILE.write_text(token)
                TOKEN_FILE.chmod(0o600)
            return token
    # Generate a secure random token
    token = secrets.token_hex(32)  # 64 hex chars
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


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
    """Synthesize text with OpenAI TTS and return mp3 bytes."""
    import requests
    from osia.config import get_api_key, get_setting
    from osia.voice.tts import OPENAI_VOICES, PROVIDER_DEFAULT_VOICE

    api_key = get_api_key("openai")
    if not api_key:
        raise RuntimeError("OpenAI TTS: no API key")

    voice = get_setting("tts_voice") or PROVIDER_DEFAULT_VOICE["openai"]
    if voice not in OPENAI_VOICES:
        voice = PROVIDER_DEFAULT_VOICE["openai"]

    r = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini-tts",
            "input": text,
            "voice": voice,
            "response_format": "wav",
        },
        timeout=60,
    )
    r.raise_for_status()
    # OpenAI returns WAV; convert to MP3
    return _wav_to_mp3(r.content)


def _google_to_mp3_bytes(text: str) -> bytes:
    """Synthesize text with Gemini TTS (REST generateContent) and return mp3 bytes.

    Mirrors ``osia.voice.tts.speak_google``: Google *Cloud* TTS rejects simple
    Gemini API keys (401, OAuth2 required), so we call the Gemini
    generateContent endpoint, which accepts the ``x-goog-api-key`` header.
    Audio comes back as base64 24kHz 16-bit mono PCM, which we wrap as WAV and
    transcode to MP3 for the phone (the WebSocket protocol still ships mp3).
    """
    import requests
    from osia.config import get_api_key, get_setting
    from osia.voice.tts import (
        GEMINI_TTS_MODELS, GEMINI_TTS_SR, GEMINI_VOICES, GEMINI_DEFAULT_VOICE,
        gemini_tts_prompt,
    )

    api_key = get_api_key("google")
    if not api_key:
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

    last_err = None
    for model in GEMINI_TTS_MODELS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            # base64 PCM lives at candidates[0].content.parts[0].inlineData.data
            b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
            pcm = base64.b64decode(b64)
            # 24kHz 16-bit signed PCM mono -> WAV -> MP3 (matches the kokoro path)
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            wav_buf = io.BytesIO()
            sf.write(wav_buf, samples, GEMINI_TTS_SR, format="WAV")
            wav_buf.seek(0)
            return _wav_to_mp3(wav_buf.read())
        except Exception as e:
            # Model unavailable on this tier / transient error -> try the next.
            last_err = e
            continue

    raise RuntimeError(f"Google (Gemini) TTS failed: {last_err}")


def _wav_to_mp3(wav_bytes: bytes, bitrate: str = "128k") -> bytes:
    """Convert WAV audio bytes to MP3 via ffmpeg pipe."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-f", "mp3", "-b:a", bitrate, "pipe:1"],
        input=wav_bytes,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {proc.stderr.decode()}")
    return proc.stdout


def synthesize_to_mp3_bytes(text: str) -> bytes:
    """Synthesize text to MP3 bytes using the configured TTS provider.

    Follows the same provider chain as ``osia.voice.tts._speak_blocking``:
    openai → google → local (Kokoro). Falls back to Kokoro on failure.
    """
    from osia.config import get_api_key, get_setting

    provider = get_setting("tts_provider", "local") or "local"
    print(
        f"[TTS] synthesize_to_mp3_bytes: start "
        f"(provider={provider!r}, {len(text)} chars)",
        flush=True,
    )

    # Try configured provider first, fall back to Kokoro
    if provider == "openai" and get_api_key("openai"):
        try:
            mp3 = _openai_to_mp3_bytes(text)
            print(
                f"[TTS] synthesize_to_mp3_bytes: done via openai ({len(mp3)} bytes)",
                flush=True,
            )
            return mp3
        except Exception as e:
            print(f"⚠️  OpenAI TTS failed, falling back to Kokoro: {e}")
    elif provider == "google" and get_api_key("google"):
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


# Registry of authenticated mobile WebSocket clients (WOR-10). Used to push
# server-initiated frames — currently the "new chat" reset triggered either by
# a mobile device or by the desktop app's voice command. Populated after a
# successful auth, pruned on disconnect.
_WS_CLIENTS: set[WebSocket] = set()


async def _broadcast_new_chat(exclude: WebSocket | None = None) -> None:
    """Best-effort fan-out of {"type": "new_chat"} to all connected clients."""
    for client in list(_WS_CLIENTS):
        if client is exclude:
            continue
        try:
            await client.send_json({"type": "new_chat"})
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


@app.on_event("startup")
async def _warm_models() -> None:
    """Pre-load slow models in the background at server startup so the
    first real audio request doesn't pay their cold-start cost.

    faster-whisper's ``WhisperModel`` is module-level cached in
    ``osia.voice.stt`` — calling ``_get_model()`` once here means every
    subsequent ``transcribe()`` call (including the very first user
    request) reuses the already-loaded model in memory.
    """
    async def _warm() -> None:
        try:
            from osia.voice.stt import _get_model, MODEL_SIZE
            print(f"[OSIA Server] Warming faster-whisper '{MODEL_SIZE}'...")
            t0 = time.perf_counter()
            await asyncio.to_thread(_get_model)
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"[OSIA Server] Whisper '{MODEL_SIZE}' loaded in {elapsed:.0f}ms")
        except Exception as e:
            print(
                f"[OSIA Server] Whisper warm-up failed "
                f"(will lazy-load on first request): {e}"
            )

    # Run in the background so /health responds immediately during warm-up.
    asyncio.create_task(_warm())


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
        "disk_percent": float(psutil.disk_usage("/").percent),
        "cpu_temp": _read_cpu_temp(),
        "gpu_temp": _read_gpu_temp(),
        "timestamp": datetime.now().isoformat(),
    }


async def _heartbeat_push_loop(ws: WebSocket) -> None:
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
                await ws.send_json(_collect_heartbeat_data())
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A transient sampling/send failure must not kill the loop; the
            # main read loop owns disconnect detection and cancellation.
            print(f"[OSIA Server] heartbeat push error: {e}")
            await asyncio.sleep(5)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    authenticated = False
    # Per-session state.
    # ``session_history`` is the OpenAI-format message list (sans system
    # prompt) handed to the agent each turn. ``active_conv_id`` is the
    # conversation file currently being written to. Both reset on
    # disconnect (function returns => GC).
    session_history: list = []
    active_conv_id: str | None = None
    # Files uploaded by the client that haven't been consumed by a turn yet.
    # The next text/audio turn consumes and clears this list.
    pending_files: list[str] = []
    # Live system-metrics push task (heartbeat_data frames). Created once after
    # a successful auth, cancelled in the finally block on disconnect.
    heartbeat_task: asyncio.Task | None = None

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
                    # "new chat" reset) can reach it (WOR-10).
                    _WS_CLIENTS.add(ws)
                    # Send the first data frames the mobile app expects right
                    # after auth. Build the conversation list DEFENSIVELY: a
                    # failure here must not silently tear down a freshly
                    # authenticated socket (that looked like "open -> closed
                    # instantly even with a valid token").
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
                    # stray second auth frame spawning a duplicate loop.
                    if heartbeat_task is None:
                        heartbeat_task = asyncio.create_task(
                            _heartbeat_push_loop(ws)
                        )
                else:
                    print("[WS] closing connection, reason: invalid token")
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
                await ws.send_json({"type": "status", "content": "error"})
                await ws.close(code=4001, reason="Not authenticated")
                return

            # --- Conversation management ---
            if msg_type == "list_conversations":
                await ws.send_json({
                    "type": "conversations",
                    "items": conversations.list_conversations(),
                })
                continue

            if msg_type == "get_conversations":
                # Compact id+title list for the mobile cron editor's "linked
                # conversation" picker (WOR-24). list_conversations() is already
                # sorted by updated_at descending; cap at the 50 most recent.
                recent = conversations.list_conversations()[:50]
                await ws.send_json({
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
                await ws.send_json({
                    "type": "active_conversation",
                    "id": conv["id"],
                    "title": conv["title"],
                })
                await ws.send_json({
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
                await ws.send_json({
                    "type": "active_conversation",
                    "id": conv["id"],
                    "title": conv["title"],
                })
                await ws.send_json({
                    "type": "conversations",
                    "items": conversations.list_conversations(),
                })
                await _broadcast_new_chat(exclude=ws)
                continue

            if msg_type == "restart_server":
                # Self-restart the server process in place (WOR-28). Ack the
                # client first so the phone knows the request landed, then
                # re-exec the same interpreter + argv; os.execv replaces this
                # process image, so nothing after the call runs.
                print("[OSIA Server] restart_server requested → re-exec")
                await ws.send_json({
                    "type": "status",
                    "status": "done",
                    "message": "restarting",
                })
                os.execv(sys.executable, [sys.executable] + sys.argv)
                continue

            if msg_type == "load_conversation":
                conv_id = msg.get("id", "")
                conv = conversations.get_conversation(conv_id)
                if conv is None:
                    await ws.send_json({
                        "type": "status",
                        "content": "error",
                        "message": f"conversation {conv_id!r} not found",
                    })
                    continue
                active_conv_id = conv_id
                session_history = list(conv.get("messages") or [])
                await ws.send_json({
                    "type": "conversation_history",
                    "id": conv_id,
                    "title": conv.get("title"),
                    "messages": _displayable_history(session_history),
                })
                continue

            if msg_type == "delete_conversation":
                conv_id = msg.get("id", "")
                ok = conversations.delete_conversation(conv_id)
                if active_conv_id == conv_id:
                    active_conv_id = None
                    session_history = []
                await ws.send_json({
                    "type": "conversation_deleted",
                    "id": conv_id,
                    "success": ok,
                })
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
                await ws.send_json({
                    "type": "models_list",
                    "models": models,
                    "current": current,
                })
                continue

            if msg_type == "set_model":
                model_id = (msg.get("model") or "").strip()
                if not model_id:
                    await ws.send_json({"type": "status", "content": "error"})
                    continue
                try:
                    from osia.config import set_setting
                    set_setting("selected_model", model_id)
                    print(f"[OSIA Server] selected_model -> {model_id!r}")
                    await ws.send_json({"type": "status", "content": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_model({model_id!r}) failed: {e}")
                    await ws.send_json({"type": "status", "content": "error"})
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
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_api_key({provider!r}) failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
                continue

            # delete_api_key removes a provider's key entirely from the canonical
            # key store (config/api_keys.json) — it does NOT leave behind an empty
            # string, so get_api_key()/load_api_keys() report the provider as unset.
            if msg_type == "delete_api_key":
                provider = (msg.get("provider") or "").strip().lower()
                if not provider:
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] delete_api_key({provider!r}) failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "error"})
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
                        await ws.send_json({"type": "status", "status": "error"})
                        continue
                    fallbacks[provider] = chain
                    set_setting("api_key_fallbacks", fallbacks)
                    print(
                        f"[OSIA Server] set_api_fallback_key provider={provider!r} "
                        f"index={index} ({len(chain)} keys, len={len(key)})"
                    )
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(
                        f"[OSIA Server] set_api_fallback_key({provider!r}) failed: {e}"
                    )
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "error"})
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
                        await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(
                        f"[OSIA Server] delete_api_fallback_key({provider!r}) failed: {e}"
                    )
                    await ws.send_json({"type": "status", "status": "error"})
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
                }
                if setting_key not in allowed_settings:
                    print(
                        f"[OSIA Server] set_setting rejected non-whitelisted "
                        f"key={setting_key!r}"
                    )
                    await ws.send_json({"type": "status", "status": "error"})
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
                    else:
                        set_setting(setting_key, setting_value)
                    print(
                        f"[OSIA Server] set_setting {setting_key!r} -> "
                        f"{setting_value!r}"
                    )
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_setting({setting_key!r}) failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
                continue

            # --- Config snapshot (mobile "OSIA / Personality / Skills" screen) ---
            # get_config returns everything the mobile config screens need in one
            # frame: the settings.json contents (incl. api_key_fallbacks), the two
            # hierarchy chains exposed under their flat mobile names, the
            # personality document, and the full skill list (name/enabled/prose).
            if msg_type == "get_config":
                try:
                    from osia.config import load_settings, load_api_keys, get_setting
                    from osia import personality
                    cfg = load_settings()
                    # api_key_fallbacks holds the raw ordered fallback keys per
                    # provider (WOR-29). The mobile fallback-keys editor needs the
                    # actual keys, so ship them as-is — the connection is already
                    # authenticated + TLS, identical to the get_api_keys path.
                    cfg["api_key_fallbacks"] = get_setting("api_key_fallbacks", {})
                    # Desktop-only capsule position is meaningless to the phone.
                    cfg.pop("island_x", None)
                    cfg.pop("island_y", None)
                    # Mirror the two hierarchy chains under the flat names the
                    # mobile app writes back via set_setting, so the round-trip
                    # is symmetric (read heavy_chain → write heavy_chain).
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
                    await ws.send_json({"type": "config", "config": cfg})
                    print(
                        f"[OSIA Server] get_config sent "
                        f"({len(cfg.get('skills', []))} skills)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] get_config failed: {e}")
                    traceback.print_exc()
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "api_keys", "keys": keys})
                    print(
                        f"[OSIA Server] get_api_keys sent "
                        f"({sum(1 for v in keys.values() if v)}/{len(keys)} set)"
                    )
                except Exception as e:
                    print(f"[OSIA Server] get_api_keys failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({
                        "type": "heartbeat_config",
                        "config": {
                            "enabled": bool(s.get("heartbeat_enabled", False)),
                            "interval_seconds": interval,
                        },
                    })
                    print("[OSIA Server] get_heartbeat_config sent")
                except Exception as e:
                    print(f"[OSIA Server] get_heartbeat_config failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_personality failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
                continue

            # --- Skills: update / create / delete ---
            # set_skill updates an EXISTING skill's enabled flag and/or its prose
            # (both fields optional, so a toggle and a text edit can each arrive
            # alone). create_skill seeds a new <name>.md from the template;
            # delete_skill removes the file and its enabled flag.
            if msg_type == "set_skill":
                skill_name = (msg.get("name") or "").strip()
                if not skill_name:
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] set_skill({skill_name!r}) failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
                continue

            if msg_type == "create_skill":
                skill_name = (msg.get("name") or "").strip()
                if not skill_name:
                    await ws.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia import personality
                    created = personality.create_skill(skill_name)
                    if not created:
                        await ws.send_json({"type": "status", "status": "error"})
                        continue
                    print(f"[OSIA Server] create_skill -> {created!r}")
                    await ws.send_json({
                        "type": "status", "status": "done", "name": created,
                    })
                except Exception as e:
                    print(f"[OSIA Server] create_skill({skill_name!r}) failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
                continue

            if msg_type == "delete_skill":
                skill_name = (msg.get("name") or "").strip()
                if not skill_name:
                    await ws.send_json({"type": "status", "status": "error"})
                    continue
                try:
                    from osia import personality
                    personality.delete_skill(skill_name)
                    print(f"[OSIA Server] delete_skill name={skill_name!r}")
                    await ws.send_json({"type": "status", "status": "done"})
                except Exception as e:
                    print(f"[OSIA Server] delete_skill({skill_name!r}) failed: {e}")
                    await ws.send_json({"type": "status", "status": "error"})
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
                    await ws.send_json({
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
                    await ws.send_json({
                        "type": "file_received",
                        "filename": filename,
                        "path": "",
                        "success": False,
                    })
                    continue
                try:
                    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    # Sanitize: keep basename only, prefix with timestamp to avoid clobbering.
                    safe_name = os.path.basename(filename) or "upload.bin"
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
                    await ws.send_json({
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
                        session_history, active_conv_id = await _run_agent_and_respond(
                            ws, vision_content, session_history, active_conv_id,
                        )
                except Exception as e:
                    print(f"[OSIA Server] file upload failed: {e}")
                    await ws.send_json({
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
                user_text = _consume_pending_files(user_text, pending_files)
                session_history, active_conv_id = await _run_agent_and_respond(
                    ws, user_text, session_history, active_conv_id,
                )

            # --- Audio message ---
            elif msg_type == "audio":
                b64_data = msg.get("data", "")
                audio_format = msg.get("format", "wav")
                if not b64_data:
                    continue
                session_history, active_conv_id = await _handle_audio(
                    ws, b64_data, audio_format,
                    session_history, active_conv_id,
                    pending_files=pending_files,
                )

            else:
                await ws.send_json({"type": "status", "content": "error"})

    except WebSocketDisconnect:
        print("[WS] closing connection, reason: client disconnected (WebSocketDisconnect)")
        print("[OSIA Server] Client disconnected. Resetting session state.")
    except Exception as e:
        print(f"[WS] closing connection, reason: unexpected exception: {e!r}")
        print(f"[OSIA Server] Unexpected error: {e}")
        traceback.print_exc()
        try:
            await ws.send_json({"type": "status", "content": "error"})
        except Exception:
            pass
    finally:
        # Drop this client from the broadcast registry (WOR-10). discard() is
        # a no-op if it was never added (e.g. auth failed).
        _WS_CLIENTS.discard(ws)
        # Stop this client's live heartbeat push (no-op if never started).
        # Cancel then await so the task unwinds cleanly without a pending-task
        # warning; CancelledError on the await is expected.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
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
async def _run_agent_and_respond(ws: WebSocket, text, history: list,
                                 active_conv_id: str | None,
                                 *, t0: float | None = None) -> tuple[list, str]:
    """Run the agent with the given conversation history, persist the
    new turn, stream the spoken response sentence-by-sentence, then send
    the assembled reply + TTS audio.

    Returns ``(updated_history, active_conv_id)``. If no conversation
    was active a new one is created (with a smart keyword title seeded
    from ``text``) so every interaction lives in a persisted file.

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
    await ws.send_json({"type": "status", "content": "thinking"})

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
                await ws.send_json({"type": "response_chunk", "content": item})
                sent_chunks.append(item)
            except Exception as e:
                print(f"[OSIA Server] response_chunk send failed: {e}")
                return

    sender_task = asyncio.create_task(sender())

    try:
        from osia.config import load_settings

        agent = OSIAAgent()
        selected_model = load_settings().get("selected_model")
        if selected_model:
            try:
                agent.set_model(selected_model)
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
        # Off-thread so the event loop stays free to flush response_chunks.
        response_text = await asyncio.to_thread(
            agent.run, text,
            history=history,
            on_sentence=on_sentence,
            on_first_token=on_first_token,
        )
        print(
            f"[DEBUG] agent.run() returned: {repr((response_text or '')[:200])}",
            flush=True,
        )
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
        await ws.send_json({"type": "response", "content": f"Erreur: {e}"})
        await ws.send_json({"type": "status", "content": "error"})
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
                    await ws.send_json({"type": "response_chunk", "content": tail})
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
                })
                sent_chunks.append(response_text)
            except Exception as e:
                print(f"[OSIA Server] fallback response_chunk send failed: {e}")

    # Send the canonical full text once (clients that don't track chunks
    # can rely on this; clients that do can ignore it).
    print(f"[DEBUG] sending to mobile: {repr(response_text[:200])}", flush=True)
    await ws.send_json({"type": "response", "content": response_text})

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
        })
        _ts(t0, "first audio chunk sent to mobile", f"({len(mp3_bytes)} bytes)")
    except Exception as e:
        print(f"[OSIA Server] TTS synthesis failed: {e}")
        traceback.print_exc()

    # Brief settle so any chunks still in flight on the WebSocket land
    # before the client sees `done` and tears down the streaming UI.
    await asyncio.sleep(0.2)
    await ws.send_json({"type": "status", "content": "done"})
    _ts(t0, "request complete")
    return new_history, active_conv_id


async def _handle_audio(ws: WebSocket, b64_data: str, audio_format: str,
                        history: list,
                        active_conv_id: str | None,
                        *, pending_files: list[str] | None = None) -> tuple[list, str | None]:
    """Process an audio command: transcribe, then run the agent on the
    transcript using the SAME path as text commands (so tools execute
    identically and the transcript ends up in the same conversation).

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
    await ws.send_json({"type": "status", "content": "thinking"})

    try:
        # Decode and resample
        audio, sr = decode_base64_wav(b64_data)
        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            await ws.send_json({"type": "transcript", "content": ""})
            await ws.send_json({"type": "status", "content": "done"})
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
            await ws.send_json({"type": "transcript", "content": ""})
            await ws.send_json({"type": "status", "content": "done"})
            return history, active_conv_id

        # Fire transcript send in parallel with the agent start — don't
        # await it. The send is just an enqueue onto the WebSocket's
        # outgoing buffer, so it lands long before the agent's first
        # response_chunk, but starting the agent immediately saves the
        # ~1-50 ms a real-network round-trip would otherwise cost.
        transcript_send = asyncio.create_task(
            ws.send_json({"type": "transcript", "content": transcript})
        )

        agent_input = transcript
        if pending_files:
            agent_input = _consume_pending_files(agent_input, pending_files)

        try:
            return await _run_agent_and_respond(
                ws, agent_input, history, active_conv_id, t0=t0,
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
        await ws.send_json({"type": "status", "content": "error"})
        return history, active_conv_id


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
