#!/usr/bin/env python3
"""OSIA Text-to-Speech — multi-provider with local fallback.

Provider chain (set via the ``tts_provider`` setting):
  - ``openai`` -> OpenAI ``tts-1-hd`` REST endpoint
  - ``google`` -> Gemini TTS (REST generateContent, 24kHz PCM) — uses the
                  same Gemini API key already configured for vision/chat
  - ``local``  -> Kokoro (offline, default)

When an API provider fails (missing key, network error), we fall back to
Kokoro so the assistant always has a voice.
"""

import io
import json
import os
import queue
import threading

import numpy as np
import soundfile as sf
from langdetect import detect, DetectorFactory

# sounddevice loads PortAudio at import time and can fail on a machine with no
# audio backend / drivers. Degrade gracefully: with ``sd = None`` the synthesis
# helpers below skip playback (the text reply is still shown elsewhere), so OSIA
# never crashes for lack of a speaker (WOR-11).
try:
    import sounddevice as sd
    _AUDIO_OK = True
except Exception as _audio_exc:  # PortAudio missing / no audio device
    sd = None
    _AUDIO_OK = False
    print(
        f"⚠️  [TTS] audio backend unavailable ({_audio_exc}); spoken output is "
        "disabled (text replies still work)."
    )

from osia.config import CONFIG_DIR, get_setting
from osia.utils.key_chain import with_key_rotation, has_key, KeyChainEmpty

# Make langdetect deterministic
DetectorFactory.seed = 0

# Playback rate applied to every TTS provider (1.0 = provider default).
# Verified 2026-06-02: Kokoro and OpenAI (gpt-4o-mini-tts) both synthesize
# correctly at 1.2 — revert to 1.0 if a provider starts rejecting it.
TTS_SPEED = 1.2

# ── Debug logging ──────────────────────────────────────────────────────────
# Every stage of the TTS pipeline prints a [TTS] line so a silent assistant
# can be traced to the exact stage that broke: dispatch, synthesis, or
# audio playback.
TTS_DEBUG = True

_audio_device_logged = False


def _debug(msg: str) -> None:
    if TTS_DEBUG:
        print(f"[TTS] {msg}", flush=True)


def _log_audio_device() -> None:
    """Log the configured audio output device (once per process) so a
    missing/misconfigured sound device shows up in the logs immediately.
    """
    global _audio_device_logged
    if _audio_device_logged:
        return
    _audio_device_logged = True
    try:
        out = sd.query_devices(kind="output")
        _debug(
            f"audio output device: {out['name']!r} @ "
            f"{out['default_samplerate']:.0f}Hz "
            f"(default in/out indices: {sd.default.device})"
        )
    except Exception as e:
        _debug(f"⚠️ audio output device query FAILED: {e}")

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
    "google": "Kore",        # Gemini prebuilt voice (see GEMINI_VOICES below)
    "local": None,
}

# ── Kokoro (local) ─────────────────────────────────────────────────────────

LANG_CODE = "a"          # American English (Kokoro default)
KOKORO_VOICE_EN = "af_sky"
KOKORO_VOICE_FR = "ff_siwis"
KOKORO_SR = 24000
KOKORO_SPEED = TTS_SPEED  # also imported by osia.server.server

_kokoro_pipelines = {}
_stream_playback_context = threading.local()
_playback_start_lock = threading.Lock()


def _play_samples_if_current(samples, samplerate: int) -> bool:
    """Start playback atomically only while the streaming epoch is current."""

    epoch = getattr(_stream_playback_context, "epoch", None)
    with _playback_start_lock:
        if epoch is not None and epoch != _current_epoch():
            _debug(
                f"discarding stale synthesized audio "
                f"(epoch {epoch} != current {_current_epoch()})"
            )
            return False
        sd.play(samples, samplerate=samplerate)
        return True


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
        _debug(
            f"kokoro: synthesis start (voice={voice}, speed={KOKORO_SPEED}, "
            f"{len(text)} chars)"
        )
        segments = []
        for _, _, audio in pipeline(text, voice=voice, speed=KOKORO_SPEED):
            if audio is not None:
                segments.append(audio)
        if not segments:
            _debug("kokoro: synthesis produced NO audio segments")
            return False
        samples = np.concatenate(segments)
        _debug(
            f"kokoro: synthesis done ({len(samples)} samples, "
            f"{len(samples) / KOKORO_SR:.2f}s)"
        )
        _log_audio_device()
        _debug("kokoro: playback start")
        if not _play_samples_if_current(samples, KOKORO_SR):
            return True
        if block:
            sd.wait()
            _debug("kokoro: playback finished")
        else:
            _debug("kokoro: playback started (non-blocking)")
        return True
    except Exception as e:
        print(f"⚠️  Kokoro synthesis failed: {e}")
        return False


# ── OpenAI TTS ─────────────────────────────────────────────────────────────

def speak_openai(text: str, voice: str | None = None, block: bool = True) -> bool:
    """Synthesize via OpenAI gpt-4o-mini-tts. Returns True on success.

    The HTTP request rotates through the user's OpenAI key chain on a
    429/5xx/auth/timeout error (shared rotation via
    :func:`osia.utils.key_chain.with_key_rotation`); only when every key is
    exhausted does the caller fall back to Kokoro. Playback stays OUTSIDE the
    rotation so a sounddevice failure never burns API keys.
    """
    import requests
    if not has_key("openai"):
        print("⚠️  OpenAI TTS: no API key.")
        return False

    voice = voice or get_setting("tts_voice") or PROVIDER_DEFAULT_VOICE["openai"]
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
                "speed": TTS_SPEED,
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    try:
        _debug(
            f"openai: synthesis start (voice={voice}, speed={TTS_SPEED}, "
            f"{len(text)} chars)"
        )
        content = with_key_rotation("openai", _synth, label="tts:openai")
    except (Exception, KeyChainEmpty) as e:
        print(f"⚠️  OpenAI TTS failed: {e}")
        return False

    try:
        audio, sr = sf.read(io.BytesIO(content), dtype="float32", always_2d=False)
        _debug(f"openai: synthesis done ({len(audio)} samples @ {sr}Hz)")
        _log_audio_device()
        _debug("openai: playback start")
        if not _play_samples_if_current(audio, int(sr)):
            return True
        if block:
            sd.wait()
            _debug("openai: playback finished")
        else:
            _debug("openai: playback started (non-blocking)")
        return True
    except Exception as e:
        print(f"⚠️  OpenAI TTS playback failed: {e}")
        return False


# ── Google (Gemini) TTS ────────────────────────────────────────────────────
# Gemini TTS accepts the same simple Gemini API key already configured for
# vision/chat (stored under the "google" provider). Google *Cloud* TTS rejected
# that key (401, OAuth2 required), so we use the Gemini generateContent REST
# endpoint instead. Audio comes back as base64 24kHz 16-bit signed PCM (mono).

# Newest, most expressive model first; fall back if the tier lacks it.
GEMINI_TTS_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
GEMINI_TTS_SR = 24000          # Gemini TTS always returns 24kHz mono PCM
GEMINI_DEFAULT_VOICE = "Kore"

# Prebuilt Gemini TTS voices (multilingual, 70+ languages). Used to validate
# the configured ``tts_voice`` so a stale Cloud-style voice falls back cleanly.
GEMINI_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]


def gemini_tts_prompt(text: str) -> str:
    """Wrap ``text`` in an explicit read-verbatim instruction for Gemini TTS.

    Gemini TTS sometimes interprets part of a bare input as a *style*
    instruction and speaks an unrelated English phrase before the actual
    (e.g. French) text. Prefixing a clear instruction makes Gemini read ONLY
    the text, in its original language, with no preamble/translation/comment.
    Shared by ``speak_google`` and ``osia.server.server._google_to_mp3_bytes``.
    """
    return (
        "Read aloud exactly the following text, in its original language, "
        "without adding any words, translation, or commentary:\n\n"
        f"{text}"
    )


def speak_google(text: str, voice: str | None = None, block: bool = True) -> bool:
    """Synthesize via Gemini TTS (REST generateContent). Returns True on success.

    Tries the newest model first, falling back to the stable preview model if
    the user's tier rejects it. Audio is base64 24kHz 16-bit PCM mono.
    """
    import base64
    import requests

    if not has_key("google"):
        print("⚠️  Google (Gemini) TTS: no API key.")
        return False

    # Voice is configurable via ``tts_voice``; default to Kore. A stale
    # Cloud-style voice (e.g. "fr-FR-Neural2-A") isn't a Gemini voice, so
    # validate against the prebuilt list and fall back to the default.
    voice = voice or get_setting("tts_voice") or GEMINI_DEFAULT_VOICE
    if voice not in GEMINI_VOICES:
        voice = GEMINI_DEFAULT_VOICE

    # Gemini TTS has no playback-rate parameter (expressiveness is steered via
    # prompt text / audio tags), so TTS_SPEED isn't applied here.
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
        samples on the first success, else raise the last error. A 429/5xx from
        every model propagates out so ``with_key_rotation`` advances to the next
        key; a 404 (model not on tier) is non-technical and falls through to
        Kokoro, exactly as before."""
        last_err = None
        for model in GEMINI_TTS_MODELS:
            try:
                _debug(
                    f"google(gemini): synthesis start (model={model}, "
                    f"voice={voice}, {len(text)} chars)"
                )
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
                # 24kHz 16-bit signed PCM, mono -> float32 in [-1, 1] for sounddevice
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                _debug(
                    f"google(gemini): synthesis done (model={model}, "
                    f"{len(samples)} samples, {len(samples) / GEMINI_TTS_SR:.2f}s)"
                )
                return samples
            except Exception as e:
                # Model unavailable on this tier / transient error -> try the next.
                last_err = e
                _debug(f"google(gemini): model {model} FAILED: {e}")
                continue
        raise last_err

    try:
        samples = with_key_rotation("google", _synth, label="tts:google")
    except (Exception, KeyChainEmpty) as e:
        print(f"⚠️  Google (Gemini) TTS failed: {e}")
        return False

    try:
        _log_audio_device()
        _debug("google(gemini): playback start")
        if not _play_samples_if_current(samples, GEMINI_TTS_SR):
            return True
        if block:
            sd.wait()
            _debug("google(gemini): playback finished")
        else:
            _debug("google(gemini): playback started (non-blocking)")
        return True
    except Exception as e:
        print(f"⚠️  Google (Gemini) TTS playback failed: {e}")
        return False


# ── Dispatcher ─────────────────────────────────────────────────────────────

def get_active_tts_provider() -> str:
    """Read the active provider from settings, falling back to 'local'."""
    return get_setting("tts_provider", "local") or "local"


def _speak_blocking(text: str):
    """One synthesis pass through the dispatcher. Used by both the public
    ``speak`` entrypoint and the streaming worker."""
    if not _AUDIO_OK:
        _debug("_speak_blocking: no audio backend — skipping playback")
        return
    provider = get_active_tts_provider()
    _debug(f"_speak_blocking: dispatch start (provider={provider!r})")

    if provider == "openai" and has_key("openai"):
        if speak_openai(text, block=True):
            _debug("_speak_blocking: dispatch done (openai)")
            return
        _debug("_speak_blocking: openai FAILED — falling back to kokoro")
    elif provider == "google" and has_key("google"):
        if speak_google(text, block=True):
            _debug("_speak_blocking: dispatch done (google)")
            return
        _debug("_speak_blocking: google FAILED — falling back to kokoro")

    # Local provider — and the fallback when a cloud provider fails.
    ok = speak_kokoro(text, block=True)
    _debug(f"_speak_blocking: dispatch done (kokoro, success={ok})")


def speak(text: str, block: bool = True):
    """Speak ``text`` through the configured TTS provider.

    Honors the ``tts_provider`` / ``tts_voice`` settings written by the
    Voice settings panel. Reads them fresh on every call so changes take
    effect without a restart.
    """
    if not text or not text.strip():
        _debug("speak() called with empty text — nothing to do")
        return
    _debug(f"speak() called (block={block}, {len(text)} chars)")
    print(f"🔊 Speaking: {text[:80]}{'...' if len(text) > 80 else ''}")
    if block:
        _speak_blocking(text)
        _debug("speak() returned")
    else:
        threading.Thread(
            target=_speak_blocking, args=(text,), daemon=True,
        ).start()


# ── Wake-greeting audio cache ──────────────────────────────────────────────
# The wake greeting (e.g. "Bonjour, monsieur !") is a short, near-constant
# phrase spoken on EVERY wake-word detection. Re-synthesizing it through a cloud
# TTS API each time adds ~1.5s of latency for nothing. We cache the raw audio on
# disk keyed on (text, provider, voice, speed); when those are unchanged we
# replay the cached wav instantly with no API call. Any change re-synthesizes
# once and refreshes the cache. CONFIG_DIR is imported from osia.config (never
# hardcoded) so the cache lives next to settings.json on every OS.

_GREETING_CACHE_WAV = os.path.join(CONFIG_DIR, "greeting_cache.wav")
_GREETING_CACHE_META = os.path.join(CONFIG_DIR, "greeting_cache.json")


def _synthesize_to_buffer(text: str, provider: str, voice):
    """Synthesize ``text`` to raw audio WITHOUT playing it.

    Returns ``(samples, samplerate)`` — float32 mono ``np.ndarray`` + ``int`` —
    on success, or ``None`` on any failure (so the caller can fall back to the
    normal ``speak`` path). Used only to (re)fill the greeting cache; normal
    speech keeps going through ``speak`` / ``_speak_blocking`` untouched.

    Mirrors the *synthesis* half of ``speak_openai`` / ``speak_google`` /
    ``speak_kokoro`` (minus playback). Returns audio ONLY for the requested
    provider — never a cloud-tagged Kokoro fallback — so the cached bytes always
    match the cache metadata. Keep in sync if a provider's request shape changes.
    """
    try:
        if provider == "openai":
            if not has_key("openai"):
                return None
            import requests
            v = voice or PROVIDER_DEFAULT_VOICE["openai"]
            if v not in OPENAI_VOICES:
                v = PROVIDER_DEFAULT_VOICE["openai"]
            _debug(f"greeting cache: synth openai (voice={v})")

            def _synth(key):
                r = requests.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "gpt-4o-mini-tts",
                        "input": text,
                        "voice": v,
                        "response_format": "wav",
                        "speed": TTS_SPEED,
                    },
                    timeout=60,
                )
                r.raise_for_status()
                return r.content

            content = with_key_rotation("openai", _synth, label="tts-cache:openai")
            audio, sr = sf.read(io.BytesIO(content), dtype="float32", always_2d=False)
            return audio, int(sr)

        if provider == "google":
            if not has_key("google"):
                return None
            import base64
            import requests
            v = voice or GEMINI_DEFAULT_VOICE
            if v not in GEMINI_VOICES:
                v = GEMINI_DEFAULT_VOICE
            body = {
                "contents": [{"parts": [{"text": gemini_tts_prompt(text)}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v}}
                    },
                },
            }
            def _synth(key):
                last_err = None
                for model in GEMINI_TTS_MODELS:
                    try:
                        _debug(f"greeting cache: synth google (model={model}, voice={v})")
                        r = requests.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                            json=body,
                            timeout=60,
                        )
                        r.raise_for_status()
                        data = r.json()
                        b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
                        pcm = base64.b64decode(b64)
                        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                        return samples
                    except Exception as e:
                        last_err = e
                        _debug(f"greeting cache: google model {model} failed: {e}")
                        continue
                raise last_err

            samples = with_key_rotation("google", _synth, label="tts-cache:google")
            return samples, GEMINI_TTS_SR

        # local / unknown provider → Kokoro (offline)
        lang = detect_language(text)
        lang_code = "f" if lang == "fr" else "a"
        kvoice = KOKORO_VOICE_FR if lang == "fr" else KOKORO_VOICE_EN
        _debug(f"greeting cache: synth kokoro (voice={kvoice})")
        pipeline = _get_kokoro_pipeline(lang_code)
        segments = [
            audio
            for _, _, audio in pipeline(text, voice=kvoice, speed=KOKORO_SPEED)
            if audio is not None
        ]
        if not segments:
            return None
        return np.concatenate(segments), KOKORO_SR
    except Exception as e:
        _debug(f"greeting cache: synthesis to buffer failed: {e}")
        return None


def _play_buffer_blocking(samples, sr) -> None:
    """Play raw audio synchronously (used by the greeting-cache path)."""
    _log_audio_device()
    sd.play(samples, samplerate=sr)
    sd.wait()


def speak_greeting_cached(text: str) -> None:
    """Speak the wake greeting, replaying cached audio when possible.

    On a cache hit — the on-disk (text, provider, voice, speed) matches the
    current ``tts_provider`` / ``tts_voice`` settings — the stored wav is played
    instantly with no synthesis/API call. On a miss (first run, or any of those
    changed) the greeting is synthesized once, the cache is refreshed, and the
    fresh audio is played. Always blocks until playback finishes, so the greeting
    never bleeds into the command recording that follows.

    Falls back to the normal blocking ``speak`` path if anything goes wrong, so
    the greeting always plays even when the cache can't be read or written.
    """
    if not text or not text.strip():
        _debug("speak_greeting_cached: empty text — nothing to do")
        return
    if not _AUDIO_OK:
        _debug("speak_greeting_cached: no audio backend — skipping playback")
        return

    text = text.strip()
    provider = get_active_tts_provider()
    voice = get_setting("tts_voice")

    # 1) Cache hit → play the stored wav directly, no synthesis / API call.
    try:
        if os.path.exists(_GREETING_CACHE_WAV) and os.path.exists(_GREETING_CACHE_META):
            with open(_GREETING_CACHE_META, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if (
                meta.get("text") == text
                and meta.get("provider") == provider
                and meta.get("voice") == voice
                and meta.get("speed") == TTS_SPEED
            ):
                audio, sr = sf.read(
                    _GREETING_CACHE_WAV, dtype="float32", always_2d=False
                )
                _debug(
                    f"speak_greeting_cached: cache HIT "
                    f"(provider={provider!r}, voice={voice!r})"
                )
                print(f"🔊 Speaking (cached greeting): {text[:80]}")
                _play_buffer_blocking(audio, int(sr))
                _debug("speak_greeting_cached: cached playback finished")
                return
    except Exception as e:
        _debug(f"speak_greeting_cached: cache read failed ({e}) — re-synthesizing")

    # 2) Cache miss / stale / settings changed → synthesize once, refresh cache.
    _debug(
        f"speak_greeting_cached: cache MISS — synthesizing "
        f"(provider={provider!r}, voice={voice!r})"
    )
    res = _synthesize_to_buffer(text, provider, voice)
    if res is None:
        # Buffer synth failed (cloud API down / no key). Fall back to the proven
        # speak() path so the greeting still plays (uncached this time).
        _debug("speak_greeting_cached: buffer synth failed — falling back to speak()")
        speak(text)
        return
    samples, sr = res

    # Persist the cache atomically BEFORE playing, so a crash mid-playback still
    # leaves a valid cache for next time.
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp_wav = _GREETING_CACHE_WAV + ".tmp"
        # format="WAV" is explicit because the ".tmp" extension hides the format
        # from soundfile's extension sniffing (it raises otherwise).
        sf.write(tmp_wav, samples, sr, format="WAV")
        os.replace(tmp_wav, _GREETING_CACHE_WAV)
        tmp_meta = _GREETING_CACHE_META + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "text": text,
                    "provider": provider,
                    "voice": voice,
                    "speed": TTS_SPEED,
                    "sr": int(sr),
                },
                f,
                indent=2,
            )
        os.replace(tmp_meta, _GREETING_CACHE_META)
        _debug("speak_greeting_cached: cache updated")
    except Exception as e:
        _debug(f"speak_greeting_cached: cache write failed: {e}")

    print(f"🔊 Speaking (greeting): {text[:80]}")
    _play_buffer_blocking(samples, int(sr))
    _debug("speak_greeting_cached: playback finished")


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
            # stale — a stop() happened after this was enqueued
            _debug(
                f"stream worker: DROPPING stale sentence "
                f"(epoch {epoch} != current {_current_epoch()}): {text[:60]!r}"
            )
            continue
        try:
            print(f"[TTS] Speaking: {text[:80]}{'...' if len(text) > 80 else ''}")
            _stream_playback_context.epoch = epoch
            try:
                _speak_blocking(text)
            finally:
                _stream_playback_context.epoch = None
        except Exception as e:
            print(f"[TTS] stream error: {e}")


def start_streaming_session() -> int:
    """Invalidate older speech and return a stable token for one new turn."""

    stop()
    session_id = _current_epoch()
    _debug(f"start_streaming_session(): epoch={session_id}")
    return session_id


def speak_streaming(text: str, session_id: int | None = None):
    """Queue a sentence for sequential playback. Use this while a chat
    response is streaming in — each call appends to a single FIFO so
    sentences play in order without overlapping. ``session_id`` is the stable
    token returned by :func:`start_streaming_session`; late callbacks from an
    invalidated turn are discarded. Non-blocking.
    """
    global _stream_thread
    if not text or not text.strip():
        _debug("speak_streaming() called with empty text — nothing to queue")
        return
    epoch = _current_epoch() if session_id is None else session_id
    if epoch != _current_epoch():
        _debug(
            f"speak_streaming(): dropping stale enqueue "
            f"(epoch {epoch} != current {_current_epoch()})"
        )
        return
    _debug(f"speak_streaming() queued ({len(text)} chars): {text[:60]!r}")
    with _stream_lock:
        if _stream_thread is None or not _stream_thread.is_alive():
            _debug("speak_streaming(): starting stream worker thread")
            _stream_thread = threading.Thread(
                target=_stream_worker, daemon=True, name="osia-tts-stream",
            )
            _stream_thread.start()
    _stream_queue.put((epoch, text))


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
    _debug(f"stop() called — epoch bumped to {_tts_epoch}, draining queue")
    drained = 0
    while True:
        try:
            _stream_queue.get_nowait()
            drained += 1
        except queue.Empty:
            break
    if drained:
        _debug(f"stop(): drained {drained} pending sentence(s)")
    try:
        if _AUDIO_OK:
            with _playback_start_lock:
                sd.stop()
    except Exception as e:
        print(f"⚠️  sd.stop() failed: {e}")


def shutdown(join_timeout: float = 2.0) -> None:
    """Stop playback and reap the streaming worker during application exit."""

    global _stream_thread
    stop()
    with _stream_lock:
        thread = _stream_thread
        if thread is None or not thread.is_alive():
            _stream_thread = None
            return
        _stream_queue.put(None)
    thread.join(timeout=max(0.0, join_timeout))
    with _stream_lock:
        if not thread.is_alive() and _stream_thread is thread:
            _stream_thread = None


if __name__ == "__main__":
    print("Active provider:", get_active_tts_provider())
    speak("Bonjour, je suis OSIA, votre assistant local.")
    speak("Hello, I am OSIA, your local assistant.")
