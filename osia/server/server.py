#!/usr/bin/env python3
"""
OSIA Mobile Server — FastAPI application with a WebSocket endpoint (/ws).

Protocol (JSON over WebSocket, port 8765):
  Client → Server:
    {"type": "auth",                 "token": "..."}
    {"type": "text",                 "content": "..."}
    {"type": "audio",                "format": "wav", "data": "<base64>"}
    {"type": "list_conversations"}
    {"type": "load_conversation",    "id": "..."}
    {"type": "new_conversation"}
    {"type": "delete_conversation",  "id": "..."}
    {"type": "get_models"}
    {"type": "set_model",            "model": "..."}
    {"type": "file",                 "filename": "...", "data": "<base64>"}
  Server → Client:
    {"type": "transcript",           "content": "..."}
    {"type": "response_chunk",       "content": "..."}        # streamed sentence-by-sentence
    {"type": "response",             "content": "..."}        # final assembled reply
    {"type": "audio",                "format": "mp3", "data": "<base64>"}
    {"type": "status",               "content": "thinking|done|error"}
    {"type": "conversations",        "items": [{id,title,created_at,updated_at,message_count}, ...]}
    {"type": "conversation_history", "id": "...", "title": "...",
                                     "messages": [{"role": "user|assistant", "content": "..."}]}
    {"type": "conversation_deleted", "id": "...", "success": true|false}
    {"type": "active_conversation",  "id": "...", "title": "..."}
    {"type": "models_list",          "models": [{id,label,provider,tier}, ...], "current": "..."}
    {"type": "file_received",        "filename": "...", "path": "...", "success": true|false}
"""

import asyncio
import base64
import io
import json
import os
import secrets
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from osia import conversations

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path("/home/OSIA/code/osia-project")
CONFIG_DIR = PROJECT_DIR / "config"
TOKEN_FILE = CONFIG_DIR / "mobile_token.txt"
UPLOAD_DIR = Path("/tmp/osia_uploads")

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
    """Synthesize text with Google Cloud TTS and return mp3 bytes."""
    from osia.config import get_api_key, get_setting
    from osia.voice.tts import GOOGLE_VOICES, PROVIDER_DEFAULT_VOICE

    api_key = get_api_key("google")
    if not api_key:
        raise RuntimeError("Google TTS: no API key")

    from google.cloud import texttospeech
    from google.api_core.client_options import ClientOptions

    client = texttospeech.TextToSpeechClient(
        client_options=ClientOptions(api_key=api_key)
    )

    voice = get_setting("tts_voice") or PROVIDER_DEFAULT_VOICE["google"]
    if voice not in GOOGLE_VOICES:
        voice = PROVIDER_DEFAULT_VOICE["google"]
    language_code = "-".join(voice.split("-")[:2])

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language_code, name=voice,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice_params, audio_config=audio_config,
    )
    # Google returns MP3 directly
    return response.audio_content


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

    try:
        async for raw_msg in ws.iter_text():
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await ws.send_json({"type": "status", "content": "error"})
                continue

            msg_type = msg.get("type", "")

            # --- Auth ---
            if msg_type == "auth":
                token = msg.get("token", "")
                if token == MOBILE_TOKEN:
                    authenticated = True
                    session_history = []
                    active_conv_id = None
                    await ws.send_json({"type": "status", "content": "done"})
                    print("[OSIA Server] Mobile client authenticated.")
                    # Send the full conversation list right away so the
                    # client can render the sidebar without a follow-up call.
                    await ws.send_json({
                        "type": "conversations",
                        "items": conversations.list_conversations(),
                    })
                else:
                    await ws.send_json({"type": "status", "content": "error"})
                    await ws.close(code=4001, reason="Invalid token")
                    return
                continue

            # All other messages require authentication
            if not authenticated:
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
        print("[OSIA Server] Client disconnected. Resetting session state.")
    except Exception as e:
        print(f"[OSIA Server] Unexpected error: {e}")
        traceback.print_exc()
        try:
            await ws.send_json({"type": "status", "content": "error"})
        except Exception:
            pass
    finally:
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
        await ws.send_json({
            "type": "audio",
            "format": "mp3",
            "data": mp3_b64,
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
