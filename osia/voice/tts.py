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
import sys
import threading
import time

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
from osia.runtime import bundled_model_path
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
        _debug(
            f"⚠️ audio output device query FAILED "
            f"({type(e).__name__})"
        )

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
_kokoro_model = None
_stream_playback_context = threading.local()
_playback_start_lock = threading.Lock()
_tts_epoch_lock = threading.RLock()
_playback_condition = threading.Condition(_tts_epoch_lock)
_playback_activity = 0
_stream_pending = 0
_tts_epoch = 0
_tts_accepting = True


def _session_is_current(session_id: int) -> bool:
    with _playback_condition:
        return _tts_accepting and session_id == _tts_epoch


def _resolve_session_id(session_id: int | None = None) -> int | None:
    if session_id is None:
        session_id = getattr(_stream_playback_context, "epoch", None)
    with _playback_condition:
        if not _tts_accepting:
            return None
        return _tts_epoch if session_id is None else session_id


def _begin_playback_activity(session_id: int) -> bool:
    global _playback_activity
    with _playback_condition:
        if not _tts_accepting or session_id != _tts_epoch:
            return False
        _playback_activity += 1
        _playback_condition.notify_all()
        return True


def _end_playback_activity(session_id: int) -> None:
    global _playback_activity
    with _playback_condition:
        if session_id != _tts_epoch:
            return
        _playback_activity = max(0, _playback_activity - 1)
        _playback_condition.notify_all()


def _change_stream_pending(session_id: int, delta: int) -> bool:
    global _stream_pending
    with _playback_condition:
        if session_id != _tts_epoch or (delta > 0 and not _tts_accepting):
            return False
        _stream_pending = max(0, _stream_pending + delta)
        _playback_condition.notify_all()
        return True


def _run_owned_speech(session_id: int, func, *args, **kwargs):
    """Run synthesis/playback only while ``session_id`` owns TTS activity."""

    if not _begin_playback_activity(session_id):
        return False
    previous = getattr(_stream_playback_context, "epoch", None)
    _stream_playback_context.epoch = session_id
    try:
        if not _session_is_current(session_id):
            return False
        return func(*args, **kwargs)
    finally:
        _stream_playback_context.epoch = previous
        _end_playback_activity(session_id)


def playback_active(session_id: int | None = None) -> bool:
    """Return whether synthesis/playback or queued streaming speech is active."""
    with _playback_condition:
        if session_id is not None and session_id != _tts_epoch:
            return False
        return _playback_activity > 0 or _stream_pending > 0


def wait_for_playback_complete(
    timeout: float | None = None,
    *,
    session_id: int | None = None,
) -> bool:
    """Wait for all desktop TTS work using monotonic timeout accounting."""
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with _playback_condition:
        epoch = _tts_epoch if session_id is None else session_id
        while _playback_activity > 0 or _stream_pending > 0:
            if epoch != _tts_epoch:
                return True
            if deadline is None:
                _playback_condition.wait()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _playback_condition.wait(timeout=remaining)
        return True


def _patch_windows_espeak_data_path() -> None:
    """Keep eSpeak's narrow Windows API away from non-ASCII install paths."""
    if sys.platform != "win32":
        return
    import ctypes
    from pathlib import Path

    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    if hasattr(EspeakWrapper, "_osia_data_path_getter"):
        return
    original_getter = EspeakWrapper.data_path.fget
    if original_getter is None:
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path = kernel32.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_short_path.restype = ctypes.c_uint

    def _short_data_path(wrapper):
        value = original_getter(wrapper)
        if value is None:
            return None
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short_path(str(value), buffer, len(buffer))
        if 0 < length < len(buffer):
            return Path(buffer.value)
        return value

    EspeakWrapper._osia_data_path_getter = original_getter
    EspeakWrapper.data_path = property(_short_data_path)


def _play_samples_if_current(
    samples,
    samplerate: int,
    *,
    session_id: int | None = None,
) -> bool:
    """Start playback atomically only while the owning epoch is current."""

    epoch = _resolve_session_id(session_id)
    if epoch is None:
        return False
    with _playback_start_lock:
        if not _session_is_current(epoch):
            _debug(
                f"discarding stale synthesized audio "
                f"(epoch {epoch} is no longer current)"
            )
            return False
        sd.play(samples, samplerate=samplerate)
        return True


def _get_kokoro_pipeline(lang_code: str):
    global _kokoro_model
    from kokoro import KModel, KPipeline
    _patch_windows_espeak_data_path()
    if lang_code not in _kokoro_pipelines:
        local = bundled_model_path("kokoro")
        if local is None:
            pipeline = KPipeline(lang_code=lang_code)
        else:
            if _kokoro_model is None:
                _kokoro_model = KModel(
                    repo_id="hexgrad/Kokoro-82M",
                    config=str(local / "config.json"),
                    model=str(local / "kokoro-v1_0.pth"),
                ).to("cpu").eval()
            pipeline = KPipeline(
                lang_code=lang_code,
                repo_id="hexgrad/Kokoro-82M",
                model=_kokoro_model,
                device="cpu",
            )
            import torch
            for voice in (KOKORO_VOICE_EN, KOKORO_VOICE_FR):
                voice_file = local / "voices" / f"{voice}.pt"
                if voice_file.is_file():
                    pipeline.voices[voice] = torch.load(
                        voice_file, map_location="cpu", weights_only=True,
                    )
        _kokoro_pipelines[lang_code] = pipeline
    return _kokoro_pipelines[lang_code]


def detect_language(text: str) -> str:
    """Return 'fr' or 'en'."""
    try:
        return "fr" if detect(text) == "fr" else "en"
    except Exception:
        return "en"


def _authoritative_tts_language(language: str | None, text: str) -> str:
    if language is None:
        return detect_language(text)
    from osia.turn_context import normalize_locale

    return normalize_locale(language) or ""


def local_tts_supports(language: str | None) -> bool:
    """Whether the packaged Kokoro voices have a true language match."""

    from osia.turn_context import normalize_locale

    return normalize_locale(language) in {"en", "fr"}


def speak_kokoro(
    text: str,
    block: bool = True,
    *,
    language: str | None = None,
    session_id: int | None = None,
) -> bool:
    """Synthesize with Kokoro (local provider). Returns True on success."""
    session_id = _resolve_session_id(session_id)
    if session_id is None or not _session_is_current(session_id):
        return False
    try:
        lang = _authoritative_tts_language(language, text)
        if lang not in {"en", "fr"}:
            _debug(
                f"kokoro: no packaged voice matches authoritative language "
                f"{lang or 'unknown'!r}; spoken output unavailable"
            )
            return False
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
        if not _play_samples_if_current(
            samples, KOKORO_SR, session_id=session_id,
        ):
            return True
        if block:
            sd.wait()
            _debug("kokoro: playback finished")
        else:
            _debug("kokoro: playback started (non-blocking)")
        return True
    except Exception as e:
        print(f"⚠️  Kokoro synthesis failed ({type(e).__name__})")
        return False


# ── OpenAI TTS ─────────────────────────────────────────────────────────────

def speak_openai(
    text: str,
    voice: str | None = None,
    block: bool = True,
    *,
    session_id: int | None = None,
) -> bool:
    """Synthesize via OpenAI gpt-4o-mini-tts. Returns True on success.

    The HTTP request rotates through the user's OpenAI key chain on a
    429/5xx/auth/timeout error (shared rotation via
    :func:`osia.utils.key_chain.with_key_rotation`); only when every key is
    exhausted does the caller fall back to Kokoro. Playback stays OUTSIDE the
    rotation so a sounddevice failure never burns API keys.
    """
    session_id = _resolve_session_id(session_id)
    if session_id is None or not _session_is_current(session_id):
        return False
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
        print(f"⚠️  OpenAI TTS failed ({type(e).__name__})")
        return False

    try:
        audio, sr = sf.read(io.BytesIO(content), dtype="float32", always_2d=False)
        _debug(f"openai: synthesis done ({len(audio)} samples @ {sr}Hz)")
        _log_audio_device()
        _debug("openai: playback start")
        if not _play_samples_if_current(
            audio, int(sr), session_id=session_id,
        ):
            return True
        if block:
            sd.wait()
            _debug("openai: playback finished")
        else:
            _debug("openai: playback started (non-blocking)")
        return True
    except Exception as e:
        print(f"⚠️  OpenAI TTS playback failed ({type(e).__name__})")
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


def speak_google(
    text: str,
    voice: str | None = None,
    block: bool = True,
    *,
    session_id: int | None = None,
) -> bool:
    """Synthesize via Gemini TTS (REST generateContent). Returns True on success.

    Tries the newest model first, falling back to the stable preview model if
    the user's tier rejects it. Audio is base64 24kHz 16-bit PCM mono.
    """
    session_id = _resolve_session_id(session_id)
    if session_id is None or not _session_is_current(session_id):
        return False
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
                _debug(
                    f"google(gemini): model {model} FAILED "
                    f"({type(e).__name__})"
                )
                continue
        raise last_err

    try:
        samples = with_key_rotation("google", _synth, label="tts:google")
    except (Exception, KeyChainEmpty) as e:
        print(f"⚠️  Google (Gemini) TTS failed ({type(e).__name__})")
        return False

    try:
        _log_audio_device()
        _debug("google(gemini): playback start")
        if not _play_samples_if_current(
            samples, GEMINI_TTS_SR, session_id=session_id,
        ):
            return True
        if block:
            sd.wait()
            _debug("google(gemini): playback finished")
        else:
            _debug("google(gemini): playback started (non-blocking)")
        return True
    except Exception as e:
        print(
            f"⚠️  Google (Gemini) TTS playback failed "
            f"({type(e).__name__})"
        )
        return False


# ── Dispatcher ─────────────────────────────────────────────────────────────

def get_active_tts_provider() -> str:
    """Read the active provider from settings, falling back to 'local'."""
    return get_setting("tts_provider", "local") or "local"


def _speak_blocking(text: str, language: str | None = None):
    """One synthesis pass through the dispatcher. Used by both the public
    ``speak`` entrypoint and the streaming worker."""
    if not _AUDIO_OK:
        _debug("_speak_blocking: no audio backend — skipping playback")
        return
    session_id = _resolve_session_id()
    if session_id is None or not _session_is_current(session_id):
        return
    provider = get_active_tts_provider()
    _debug(f"_speak_blocking: dispatch start (provider={provider!r})")

    if provider == "openai" and has_key("openai"):
        if speak_openai(text, block=True, session_id=session_id):
            _debug("_speak_blocking: dispatch done (openai)")
            return
        if not _session_is_current(session_id):
            return
        _debug("_speak_blocking: openai FAILED — falling back to kokoro")
    elif provider == "google" and has_key("google"):
        if speak_google(text, block=True, session_id=session_id):
            _debug("_speak_blocking: dispatch done (google)")
            return
        if not _session_is_current(session_id):
            return
        _debug("_speak_blocking: google FAILED — falling back to kokoro")

    # Local provider — and the fallback when a cloud provider fails.
    ok = speak_kokoro(
        text, block=True, language=language, session_id=session_id,
    )
    _debug(f"_speak_blocking: dispatch done (kokoro, success={ok})")


def speak(
    text: str,
    block: bool = True,
    *,
    language: str | None = None,
    session_id: int | None = None,
):
    """Speak ``text`` through the configured TTS provider.

    Honors the ``tts_provider`` / ``tts_voice`` settings written by the
    Voice settings panel. Reads them fresh on every call so changes take
    effect without a restart.
    """
    if not text or not text.strip():
        _debug("speak() called with empty text — nothing to do")
        return
    session_id = _resolve_session_id(session_id)
    if session_id is None or not _session_is_current(session_id):
        _debug("speak() discarded for an obsolete TTS session")
        return
    _debug(f"speak() called (block={block}, {len(text)} chars)")
    print(f"🔊 Speaking ({len(text)} chars)")
    if block:
        _run_owned_speech(session_id, _speak_blocking, text, language)
        _debug("speak() returned")
    else:
        threading.Thread(
            target=_run_owned_speech,
            args=(session_id, _speak_blocking, text, language),
            daemon=True,
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
                        _debug(
                            f"greeting cache: google model {model} failed "
                            f"({type(e).__name__})"
                        )
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
        _debug(
            "greeting cache: synthesis to buffer failed "
            f"({type(e).__name__})"
        )
        return None


def _play_buffer_blocking(samples, sr, session_id: int) -> bool:
    """Play raw audio synchronously (used by the greeting-cache path)."""
    _log_audio_device()
    if not _play_samples_if_current(
        samples, samplerate=sr, session_id=session_id,
    ):
        return False
    sd.wait()
    return True


def _speak_greeting_cached(text: str, session_id: int) -> None:
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
                print(f"🔊 Speaking cached greeting ({len(text)} chars)")
                if not _play_buffer_blocking(audio, int(sr), session_id):
                    return
                _debug("speak_greeting_cached: cached playback finished")
                return
    except Exception as e:
        _debug(
            "speak_greeting_cached: cache read failed "
            f"({type(e).__name__}) — re-synthesizing"
        )

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
        _speak_blocking(text)
        return
    samples, sr = res
    if not _session_is_current(session_id):
        _debug("speak_greeting_cached: discarding stale synthesized audio")
        return

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
        _debug(
            "speak_greeting_cached: cache write failed "
            f"({type(e).__name__})"
        )

    print(f"🔊 Speaking greeting ({len(text)} chars)")
    if not _play_buffer_blocking(samples, int(sr), session_id):
        return
    _debug("speak_greeting_cached: playback finished")


def speak_greeting_cached(
    text: str,
    *,
    session_id: int | None = None,
) -> None:
    """Play the cached greeting only for the owning TTS session."""

    session_id = _resolve_session_id(session_id)
    if session_id is None or not _session_is_current(session_id):
        _debug("speak_greeting_cached: obsolete TTS session")
        return
    _run_owned_speech(session_id, _speak_greeting_cached, text, session_id)


# ── Streaming playback queue ───────────────────────────────────────────────
# Sentences pushed via `speak_streaming` play back-to-back on a single worker
# thread, so the model can emit them as the response streams in.
#
# Each item is tagged with the epoch in effect when it was enqueued. ``stop()``
# bumps the epoch, drains the queue, and kills sounddevice playback — items
# enqueued after that point by a still-running stream callback carry the old
# epoch and are silently dropped by the worker. This is what makes wake-word
# interrupts clean even when the agent thread is still streaming sentences.

_stream_queue: "queue.Queue[tuple[int, str, str | None] | None]" = queue.Queue()
_stream_thread: "threading.Thread | None" = None
_stream_lock = threading.Lock()


def _current_epoch() -> int:
    with _playback_condition:
        return _tts_epoch


def _stream_worker():
    while True:
        item = _stream_queue.get()
        if item is None:
            return
        epoch, text, language = item
        try:
            if not _session_is_current(epoch):
                _debug(
                    f"stream worker: DROPPING stale sentence "
                    f"(epoch {epoch} is no longer current)"
                )
                continue
            print(f"[TTS] Speaking queued segment ({len(text)} chars)")
            if language is None:
                _run_owned_speech(epoch, _speak_blocking, text)
            else:
                _run_owned_speech(epoch, _speak_blocking, text, language)
        except Exception as e:
            print(f"[TTS] stream error ({type(e).__name__})")
        finally:
            _change_stream_pending(epoch, -1)


def start_tts_session(*, reopen_after_shutdown: bool = True) -> int:
    """Invalidate older speech and return one token for direct or queued TTS."""

    global _tts_accepting
    stop()
    with _playback_condition:
        if reopen_after_shutdown:
            _tts_accepting = True
        session_id = _tts_epoch
        _playback_condition.notify_all()
    _debug(f"start_tts_session(): epoch={session_id}")
    return session_id


def start_streaming_session() -> int:
    """Backward-compatible name for a new owned TTS session."""

    return start_tts_session()


def speak_streaming(
    text: str,
    session_id: int | None = None,
    *,
    language: str | None = None,
):
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
    epoch = _resolve_session_id(session_id)
    if epoch is None or not _session_is_current(epoch):
        _debug(
            f"speak_streaming(): dropping stale enqueue "
            f"(session is no longer current)"
        )
        return
    _debug(f"speak_streaming() queued ({len(text)} chars)")
    with _stream_lock:
        if _stream_thread is None or not _stream_thread.is_alive():
            _debug("speak_streaming(): starting stream worker thread")
            _stream_thread = threading.Thread(
                target=_stream_worker, daemon=True, name="osia-tts-stream",
            )
            _stream_thread.start()
    if not _change_stream_pending(epoch, 1):
        _debug("speak_streaming(): ownership changed before enqueue")
        return
    _stream_queue.put((epoch, text, language))


def _invalidate_playback(*, disable: bool) -> None:
    global _tts_epoch, _tts_accepting, _playback_activity, _stream_pending
    with _playback_start_lock:
        with _playback_condition:
            _tts_epoch += 1
            if disable:
                _tts_accepting = False
            _playback_activity = 0
            _stream_pending = 0
            epoch = _tts_epoch
            _playback_condition.notify_all()
        _debug(f"stop() called — epoch bumped to {epoch}, draining queue")
        try:
            if _AUDIO_OK:
                sd.stop()
        except Exception as e:
            print(f"⚠️  sd.stop() failed ({type(e).__name__})")
    drained = 0
    while True:
        try:
            item = _stream_queue.get_nowait()
            drained += 1
        except queue.Empty:
            break
    if drained:
        _debug(f"stop(): drained {drained} pending sentence(s)")


def stop() -> None:
    """Interrupt TTS playback and invalidate every older owning session.

    Epoch invalidation and ``sd.stop()`` share the playback-boundary lock, so a
    stale synthesis result cannot begin output after this function takes effect.
    Completion counters are reconciled and all waiters are notified immediately.
    """

    _invalidate_playback(disable=False)


def shutdown(join_timeout: float = 2.0) -> None:
    """Stop playback and reap the streaming worker during application exit."""

    global _stream_thread
    _invalidate_playback(disable=True)
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
