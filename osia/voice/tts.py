#!/usr/bin/env python3
"""OSIA Text-to-Speech — multi-provider with local fallback.

Provider chain (set via the ``tts_provider`` setting):
  - ``openai`` -> OpenAI ``tts-1-hd`` REST endpoint
  - ``google`` -> Google Cloud TTS (Neural2 / WaveNet voices)
  - ``local``  -> Kokoro (offline, default)

When an API provider fails (missing key, network error), we fall back to
Kokoro so the assistant always has a voice.
"""

import io
import queue
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf
from langdetect import detect, DetectorFactory

from osia.config import get_api_key, get_setting

# Make langdetect deterministic
DetectorFactory.seed = 0

# ── Voice catalogues ───────────────────────────────────────────────────────

OPENAI_VOICES = [
    "alloy", "ash", "coral", "echo", "fable",
    "marin", "nova", "onyx", "sage", "shimmer",
]

GOOGLE_FR_VOICES = [
    "fr-FR-Neural2-A", "fr-FR-Neural2-B", "fr-FR-Neural2-C",
    "fr-FR-Neural2-D", "fr-FR-Neural2-E",
    "fr-FR-Wavenet-A", "fr-FR-Wavenet-B", "fr-FR-Wavenet-C",
    "fr-FR-Wavenet-D", "fr-FR-Wavenet-E",
]

GOOGLE_EN_VOICES = [f"en-US-Neural2-{c}" for c in "ABCDEFGHIJ"]

GOOGLE_VOICES = GOOGLE_FR_VOICES + GOOGLE_EN_VOICES

PROVIDER_DEFAULT_VOICE = {
    "openai": "nova",
    "google": "fr-FR-Neural2-A",
    "local": None,
}

# ── Kokoro (local) ─────────────────────────────────────────────────────────

LANG_CODE = "a"          # American English (Kokoro default)
KOKORO_VOICE_EN = "af_sky"
KOKORO_VOICE_FR = "ff_siwis"
KOKORO_SR = 24000
KOKORO_SPEED = 1.1

_kokoro_pipelines = {}


def _get_kokoro_pipeline(lang_code: str):
    from kokoro import KPipeline
    if lang_code not in _kokoro_pipelines:
        _kokoro_pipelines[lang_code] = KPipeline(lang_code=lang_code)
    return _kokoro_pipelines[lang_code]


def detect_language(text: str) -> str:
    """Return 'fr' or 'en'."""
    try:
        return "fr" if detect(text) == "fr" else "en"
    except Exception:
        return "en"


def speak_kokoro(text: str, block: bool = True) -> bool:
    """Synthesize with Kokoro (local provider). Returns True on success."""
    try:
        lang = detect_language(text)
        lang_code = "f" if lang == "fr" else "a"
        voice = KOKORO_VOICE_FR if lang == "fr" else KOKORO_VOICE_EN
        pipeline = _get_kokoro_pipeline(lang_code)
        segments = []
        for _, _, audio in pipeline(text, voice=voice, speed=KOKORO_SPEED):
            if audio is not None:
                segments.append(audio)
        if not segments:
            return False
        sd.play(np.concatenate(segments), samplerate=KOKORO_SR)
        if block:
            sd.wait()
        return True
    except Exception as e:
        print(f"⚠️  Kokoro synthesis failed: {e}")
        return False


# ── OpenAI TTS ─────────────────────────────────────────────────────────────

def speak_openai(text: str, voice: str | None = None, block: bool = True) -> bool:
    """Synthesize via OpenAI gpt-4o-mini-tts. Returns True on success."""
    import requests
    api_key = get_api_key("openai")
    if not api_key:
        print("⚠️  OpenAI TTS: no API key.")
        return False

    voice = voice or get_setting("tts_voice") or PROVIDER_DEFAULT_VOICE["openai"]
    if voice not in OPENAI_VOICES:
        voice = PROVIDER_DEFAULT_VOICE["openai"]

    try:
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
        audio, sr = sf.read(io.BytesIO(r.content), dtype="float32", always_2d=False)
        sd.play(audio, samplerate=sr)
        if block:
            sd.wait()
        return True
    except Exception as e:
        print(f"⚠️  OpenAI TTS failed: {e}")
        return False


# ── Google Cloud TTS ───────────────────────────────────────────────────────

_google_client = None


def _get_google_client():
    """Build a Google TTS client keyed by the saved API key."""
    global _google_client
    api_key = get_api_key("google")
    if not api_key:
        return None
    # Rebuild if the key changed since last call.
    if _google_client is not None and getattr(_google_client, "_osia_key", None) == api_key:
        return _google_client
    from google.cloud import texttospeech
    from google.api_core.client_options import ClientOptions
    client = texttospeech.TextToSpeechClient(
        client_options=ClientOptions(api_key=api_key)
    )
    client._osia_key = api_key
    _google_client = client
    return client


def speak_google(text: str, voice: str | None = None, block: bool = True) -> bool:
    """Synthesize via Google Cloud TTS. Returns True on success."""
    client = _get_google_client()
    if client is None:
        print("⚠️  Google TTS: no API key.")
        return False

    voice = voice or get_setting("tts_voice") or PROVIDER_DEFAULT_VOICE["google"]
    if voice not in GOOGLE_VOICES:
        voice = PROVIDER_DEFAULT_VOICE["google"]
    # Language code is the first two segments: "fr-FR-Neural2-A" -> "fr-FR"
    language_code = "-".join(voice.split("-")[:2])

    try:
        from google.cloud import texttospeech
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
        audio, sr = sf.read(io.BytesIO(response.audio_content), dtype="float32", always_2d=False)
        sd.play(audio, samplerate=sr)
        if block:
            sd.wait()
        return True
    except Exception as e:
        print(f"⚠️  Google TTS failed: {e}")
        return False


# ── Dispatcher ─────────────────────────────────────────────────────────────

def get_active_tts_provider() -> str:
    """Read the active provider from settings, falling back to 'local'."""
    return get_setting("tts_provider", "local") or "local"


def _speak_blocking(text: str):
    """One synthesis pass through the dispatcher. Used by both the public
    ``speak`` entrypoint and the streaming worker."""
    provider = get_active_tts_provider()

    if provider == "openai" and get_api_key("openai"):
        if speak_openai(text, block=True):
            return
    elif provider == "google" and get_api_key("google"):
        if speak_google(text, block=True):
            return

    # Local provider — and the fallback when a cloud provider fails.
    speak_kokoro(text, block=True)


def speak(text: str, block: bool = True):
    """Speak ``text`` through the configured TTS provider.

    Honors the ``tts_provider`` / ``tts_voice`` settings written by the
    Voice settings panel. Reads them fresh on every call so changes take
    effect without a restart.
    """
    if not text or not text.strip():
        return
    print(f"🔊 Speaking: {text[:80]}{'...' if len(text) > 80 else ''}")
    if block:
        _speak_blocking(text)
    else:
        threading.Thread(
            target=_speak_blocking, args=(text,), daemon=True,
        ).start()


# ── Streaming playback queue ───────────────────────────────────────────────
# Sentences pushed via `speak_streaming` play back-to-back on a single worker
# thread, so the model can emit them as the response streams in.
#
# Each item is tagged with the epoch in effect when it was enqueued. ``stop()``
# bumps the epoch, drains the queue, and kills sounddevice playback — items
# enqueued after that point by a still-running stream callback carry the old
# epoch and are silently dropped by the worker. This is what makes wake-word
# interrupts clean even when the agent thread is still streaming sentences.

_stream_queue: "queue.Queue[tuple[int, str] | None]" = queue.Queue()
_stream_thread: "threading.Thread | None" = None
_stream_lock = threading.Lock()
_tts_epoch = 0
_tts_epoch_lock = threading.Lock()


def _current_epoch() -> int:
    with _tts_epoch_lock:
        return _tts_epoch


def _stream_worker():
    while True:
        item = _stream_queue.get()
        if item is None:
            return
        epoch, text = item
        if epoch != _current_epoch():
            continue  # stale — a stop() happened after this was enqueued
        try:
            print(f"🔊 Speaking: {text[:80]}{'...' if len(text) > 80 else ''}")
            _speak_blocking(text)
        except Exception as e:
            print(f"⚠️  TTS stream error: {e}")


def speak_streaming(text: str):
    """Queue a sentence for sequential playback. Use this while a chat
    response is streaming in — each call appends to a single FIFO so
    sentences play in order without overlapping. Non-blocking.
    """
    global _stream_thread
    if not text or not text.strip():
        return
    with _stream_lock:
        if _stream_thread is None or not _stream_thread.is_alive():
            _stream_thread = threading.Thread(
                target=_stream_worker, daemon=True, name="osia-tts-stream",
            )
            _stream_thread.start()
    _stream_queue.put((_current_epoch(), text))


def stop() -> None:
    """Interrupt TTS playback. Safe to call from any thread, including
    the wake-word callback thread.

    - Bumps the epoch so any in-flight enqueues from a still-running agent
      stream are dropped by the worker rather than spoken.
    - Drains the streaming queue of anything already pending.
    - Kills the currently-playing audio via ``sd.stop()``, which also
      unblocks any ``sd.wait()`` in a blocking ``speak()`` caller.

    No-op (other than the epoch bump) when nothing is playing.
    """
    global _tts_epoch
    with _tts_epoch_lock:
        _tts_epoch += 1
    while True:
        try:
            _stream_queue.get_nowait()
        except queue.Empty:
            break
    try:
        sd.stop()
    except Exception as e:
        print(f"⚠️  sd.stop() failed: {e}")


if __name__ == "__main__":
    print("Active provider:", get_active_tts_provider())
    speak("Bonjour, je suis OSIA, votre assistant local.")
    speak("Hello, I am OSIA, your local assistant.")
