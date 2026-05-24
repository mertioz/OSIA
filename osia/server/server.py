#!/usr/bin/env python3
"""
OSIA Mobile Server — FastAPI application with a WebSocket endpoint (/ws).

Protocol (JSON over WebSocket, port 8765):
  Client → Server:
    {"type": "auth",   "token": "..."}
    {"type": "text",   "content": "..."}
    {"type": "audio",  "format": "wav", "data": "<base64>"}
  Server → Client:
    {"type": "transcript", "content": "..."}
    {"type": "response",   "content": "..."}
    {"type": "audio",      "format": "mp3", "data": "<base64>"}
    {"type": "status",     "content": "thinking|done|error"}
"""

import base64
import io
import json
import os
import secrets
import subprocess
import sys
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path("/home/OSIA/code/osia-project")
CONFIG_DIR = PROJECT_DIR / "config"
TOKEN_FILE = CONFIG_DIR / "mobile_token.txt"

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

    # Try configured provider first, fall back to Kokoro
    if provider == "openai" and get_api_key("openai"):
        try:
            return _openai_to_mp3_bytes(text)
        except Exception as e:
            print(f"⚠️  OpenAI TTS failed, falling back to Kokoro: {e}")
    elif provider == "google" and get_api_key("google"):
        try:
            return _google_to_mp3_bytes(text)
        except Exception as e:
            print(f"⚠️  Google TTS failed, falling back to Kokoro: {e}")

    return _kokoro_to_mp3_bytes(text)


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
                    await ws.send_json({"type": "status", "content": "done"})
                    print("[OSIA Server] Mobile client authenticated.")
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

            # --- Text message ---
            if msg_type == "text":
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue
                await _handle_text(ws, user_text)

            # --- Audio message ---
            elif msg_type == "audio":
                b64_data = msg.get("data", "")
                audio_format = msg.get("format", "wav")
                if not b64_data:
                    continue
                await _handle_audio(ws, b64_data, audio_format)

            else:
                await ws.send_json({"type": "status", "content": "error"})

    except WebSocketDisconnect:
        print("[OSIA Server] Client disconnected.")
    except Exception as e:
        print(f"[OSIA Server] Unexpected error: {e}")
        traceback.print_exc()
        try:
            await ws.send_json({"type": "status", "content": "error"})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
async def _handle_text(ws: WebSocket, text: str):
    """Process a text command: run the agent, send response + TTS audio."""
    from osia.agent import OSIAAgent

    # Status: thinking
    await ws.send_json({"type": "status", "content": "thinking"})

    try:
        # Run the agent
        agent = OSIAAgent()
        response_text = agent.run(text)

        if not response_text:
            response_text = "Désolé, je n'ai pas pu traiter votre demande."

        # Send the text response
        await ws.send_json({"type": "response", "content": response_text})

        # Synthesize to MP3 and send
        try:
            mp3_bytes = synthesize_to_mp3_bytes(response_text)
            mp3_b64 = base64.b64encode(mp3_bytes).decode("ascii")
            await ws.send_json({
                "type": "audio",
                "format": "mp3",
                "data": mp3_b64,
            })
        except Exception as e:
            print(f"[OSIA Server] TTS synthesis failed: {e}")
            traceback.print_exc()

        # Status: done
        await ws.send_json({"type": "status", "content": "done"})

    except Exception as e:
        print(f"[OSIA Server] Agent error: {e}")
        traceback.print_exc()
        await ws.send_json({"type": "response", "content": f"Erreur: {e}"})
        await ws.send_json({"type": "status", "content": "error"})


async def _handle_audio(ws: WebSocket, b64_data: str, audio_format: str):
    """Process an audio command: transcribe, then treat as text."""
    from osia.voice.stt import transcribe, WHISPER_SAMPLE_RATE, resample_audio

    # Status: thinking
    await ws.send_json({"type": "status", "content": "thinking"})

    try:
        # Decode and transcribe
        audio, sr = decode_base64_wav(b64_data)
        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            await ws.send_json({"type": "transcript", "content": ""})
            await ws.send_json({"type": "status", "content": "done"})
            return

        # Resample to 16kHz if needed
        if sr != WHISPER_SAMPLE_RATE:
            audio = resample_audio(audio, sr, WHISPER_SAMPLE_RATE)
            sr = WHISPER_SAMPLE_RATE

        # Transcribe
        transcript = transcribe(audio, sample_rate=sr)

        # Send transcript back
        await ws.send_json({"type": "transcript", "content": transcript})

        if not transcript.strip():
            await ws.send_json({"type": "status", "content": "done"})
            return

        # Now process as text (the transcript becomes the user input)
        await _handle_text(ws, transcript)

    except Exception as e:
        print(f"[OSIA Server] Audio processing error: {e}")
        traceback.print_exc()
        await ws.send_json({"type": "status", "content": "error"})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
