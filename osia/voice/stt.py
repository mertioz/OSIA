#!/usr/bin/env python3
"""
OSIA Speech-to-Text — records from microphone and transcribes using faster-whisper.
Phase 1: language=None for auto-detection (supports fr + en).
Phase 3: listen_for_command() with retry loop — keeps listening until
         a quality-accepted command is received.
"""

import io
import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.signal import resample as scipy_resample

# faster-whisper is imported LAZILY inside ``_get_model`` (WOR-47) — never at
# module import or server startup. Its native ctranslate2/torch extensions
# intermittently deadlock or hard-crash (STATUS_ACCESS_VIOLATION 0xC0000005)
# when loaded at cold boot, which used to take down the mobile server before it
# could bind port 8765. Local STT is the only consumer; text + cloud STT never
# touch it.

# sounddevice loads PortAudio at import time; on a machine with no audio backend
# or drivers (common on headless boxes and freshly-provisioned Windows) that
# import itself can fail. Degrade gracefully — set ``sd = None`` and let the
# recording helpers below return empty audio so the STT pipeline yields "" and
# text mode + the mobile server keep working (WOR-11).
try:
    import sounddevice as sd
    _AUDIO_OK = True
except Exception as _audio_exc:  # PortAudio missing / no audio device
    sd = None
    _AUDIO_OK = False
    print(
        f"⚠️  [STT] audio backend unavailable ({_audio_exc}); microphone input "
        "is disabled (text + mobile still work)."
    )

from osia.config import get_setting
from osia.runtime import bundled_model_path, is_frozen
from osia.utils.key_chain import with_key_rotation, has_key
from osia.voice.commands import is_new_chat_command

DEVICE_SAMPLE_RATE = 48000  # Hardware native rate
WHISPER_SAMPLE_RATE = 16000
RECORD_SECONDS = 5
MODEL_SIZE = "small"  # tiny, base, small, medium, large

# Keep the existing public import available to recording callers.
from osia.voice.model_inventory import available_whisper_model_sizes

# Voice Activity Detection parameters
VAD_SAMPLE_RATE = 16000
VAD_CHUNK_SECONDS = 0.5
VAD_SILENCE_RMS = 0.015      # fallback if calibration fails; normally overridden
VAD_SILENCE_SECONDS = 1.2    # silence run length that ends the recording
VAD_MAX_SECONDS = 60.0       # hard cap on recording length
VAD_MIN_SECONDS = 3.0        # don't end before this many seconds

# Ambient calibration
VAD_CALIBRATION_SECONDS = 1.0
VAD_CALIBRATION_MULTIPLIER = 1.5
VAD_THRESHOLD_CEILING = 0.05   # never let calibrated threshold exceed this
_calibrated_silence_rms: float | None = None  # set by calibrate_ambient_threshold()
_calibration_lock = threading.Lock()

# Lazy-loaded model (rebuilt when the configured local size changes)
_model = None
_model_size = None
# Serializes the one-time lazy import + build. ``transcribe`` runs on worker
# threads via ``asyncio.to_thread`` (server.py), so concurrent first requests
# must not import faster-whisper or build the model twice (WOR-47).
_model_lock = threading.Lock()

# ── Groq Whisper (cloud) ────────────────────────────────────────────────────
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
GROQ_DEFAULT_MODEL = "whisper-large-v3-turbo"


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Narrow, non-audio result used by the voice-command quality gate."""

    text: str
    language: str | None
    language_probability: float | None
    segment_count: int
    average_log_probability: float | None
    minimum_average_log_probability: float | None
    maximum_no_speech_probability: float | None
    maximum_compression_ratio: float | None
    classification: str
    rejection_reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.classification.startswith("accepted")


def _empty_transcription_result(reason: str) -> TranscriptionResult:
    return TranscriptionResult(
        text="",
        language=None,
        language_probability=None,
        segment_count=0,
        average_log_probability=None,
        minimum_average_log_probability=None,
        maximum_no_speech_probability=None,
        maximum_compression_ratio=None,
        classification="rejected",
        rejection_reasons=(reason,),
    )


def _get_model(model_size: str = None):
    """Return ``(model, size)`` — a cached faster-whisper model and the size
    it was built for.

    The size comes from the ``stt_local_model`` setting (read fresh) unless
    ``model_size`` is given. The cached model is rebuilt only when the
    requested size changes, so switching local model size in settings takes
    effect without a restart.

    faster-whisper is imported **lazily here**, inside the lock, the first
    time a local transcription actually runs — never at module import or
    server startup (WOR-47): its native ctranslate2/torch extensions
    intermittently deadlock or hard-crash at cold boot. The lock serializes
    that one-time import + build because ``transcribe`` runs on worker threads
    via ``asyncio.to_thread`` (server.py) — concurrent first requests must not
    build the model twice. Returning ``size`` lets callers log without reading
    the ``_model_size`` global outside the lock.
    """
    global _model, _model_size
    size = model_size or get_setting("stt_local_model", MODEL_SIZE) or MODEL_SIZE
    bundled = bundled_model_path(f"faster-whisper-{size}")
    if is_frozen() and bundled is None:
        fallback = bundled_model_path("faster-whisper-small")
        if fallback is not None:
            size = "small"
            bundled = fallback
    # Fast path: already built for this size — no lock, no import.
    if _model is not None and _model_size == size:
        return _model, size
    with _model_lock:
        # Re-check under the lock: another thread may have built it meanwhile.
        if _model is None or _model_size != size:
            from faster_whisper import WhisperModel  # lazy: native ext, cold-boot-crash-prone (WOR-47)
            model_ref = str(bundled) if bundled is not None else size
            _model = WhisperModel(
                model_ref,
                device="cpu",
                compute_type="int8",
                local_files_only=is_frozen(),
            )
            _model_size = size
        return _model, size


def record_audio(duration: int = RECORD_SECONDS, sample_rate: int = DEVICE_SAMPLE_RATE) -> np.ndarray:
    """Record audio from the default microphone for `duration` seconds.
    Returns a numpy array of float32 samples at the device sample rate.
    """
    if not _AUDIO_OK:
        print("⚠️  [STT] no audio backend — cannot record.")
        return np.zeros(0, dtype=np.float32)
    print(f"🎤 Recording for {duration} seconds... (speak now)")
    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()  # Wait for recording to finish
    print("✅ Recording complete.")
    return audio.flatten()


def _bounded_vad_threshold(value: float) -> float:
    """Keep the effective RMS threshold finite, positive, and capped."""
    if not math.isfinite(value):
        value = VAD_SILENCE_RMS
    return min(max(value, VAD_SILENCE_RMS), VAD_THRESHOLD_CEILING)


def calibrate_ambient_threshold(
    duration: float = VAD_CALIBRATION_SECONDS,
    multiplier: float = VAD_CALIBRATION_MULTIPLIER,
    sample_rate: int = VAD_SAMPLE_RATE,
) -> float:
    """Record `duration` seconds of ambient noise and derive a VAD silence
    threshold from `ambient_rms * multiplier`, bounded by the existing VAD
    floor and ceiling.

    Result is cached in `_calibrated_silence_rms` so subsequent calls to
    `record_audio_vad()` pick it up automatically.
    """
    global _calibrated_silence_rms
    if not _AUDIO_OK:
        # No mic to sample — keep the conservative default silence threshold.
        _calibrated_silence_rms = VAD_SILENCE_RMS
        return VAD_SILENCE_RMS
    n_samples = int(duration * sample_rate)
    audio = sd.rec(n_samples, samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    samples = audio.flatten()
    ambient_rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    threshold = _bounded_vad_threshold(ambient_rms * multiplier)
    print(f"🎙️ Ambient RMS: {ambient_rms:.4f}, threshold set to {threshold:.4f}")
    _calibrated_silence_rms = threshold
    return threshold


def _get_ambient_threshold(sample_rate: int) -> float:
    """Return the cached ambient threshold, calibrating once if necessary."""
    global _calibrated_silence_rms
    if _calibrated_silence_rms is None:
        with _calibration_lock:
            if _calibrated_silence_rms is None:
                try:
                    _calibrated_silence_rms = calibrate_ambient_threshold(
                        sample_rate=sample_rate,
                    )
                except Exception as exc:
                    try:
                        sd.stop()
                    except Exception:
                        pass
                    _calibrated_silence_rms = VAD_SILENCE_RMS
                    print(
                        "⚠️  [STT] ambient calibration failed "
                        f"({type(exc).__name__}); using fallback threshold."
                    )
    return _calibrated_silence_rms


def record_audio_vad(
    sample_rate: int = VAD_SAMPLE_RATE,
    chunk_seconds: float = VAD_CHUNK_SECONDS,
    silence_rms: float | None = None,
    silence_seconds: float = VAD_SILENCE_SECONDS,
    max_seconds: float = VAD_MAX_SECONDS,
    min_seconds: float = VAD_MIN_SECONDS,
    on_ready: Callable[[], None] | None = None,
) -> np.ndarray:
    """Record from the mic until the user stops talking, using a simple
    RMS-based Voice Activity Detection.

    The stream is consumed in `chunk_seconds`-sized blocks. Before speech
    starts, `min_seconds` is the bounded no-speech window. After speech starts,
    recording ends only after at least `silence_seconds` of consecutive
    sub-threshold samples. A hard cap of `max_seconds` always applies.

    `silence_rms` defaults to the value produced by
    `calibrate_ambient_threshold()`. If calibration hasn't run yet, it is
    triggered automatically on the first call.

    `on_ready`, when provided, runs after calibration and stream opening but
    immediately before the first retained command sample is read.

    Returns mono float32 samples at `sample_rate` — feed straight to
    `transcribe(audio, sample_rate=sample_rate)`.
    """
    if not _AUDIO_OK:
        print("⚠️  [STT] no audio backend — cannot record (VAD).")
        return np.zeros(0, dtype=np.float32)
    if silence_rms is None:
        silence_rms = _get_ambient_threshold(sample_rate)
    silence_rms = _bounded_vad_threshold(silence_rms)

    chunk_size = int(chunk_seconds * sample_rate)
    max_chunks = max(1, int(round(max_seconds / chunk_seconds)))

    print(f"🎤 Listening (VAD)... speak now, silence ends recording.")

    WAITING_FOR_SPEECH = "waiting_for_speech"
    IN_SPEECH = "in_speech"
    state = WAITING_FOR_SPEECH
    chunks: list[np.ndarray] = []
    recorded_seconds = 0.0
    trailing_silence_seconds = 0.0

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=chunk_size,
    ) as stream:
        if on_ready is not None:
            on_ready()
        for i in range(max_chunks):
            data, _ = stream.read(chunk_size)
            chunk = data.flatten()
            chunks.append(chunk)
            chunk_duration = chunk.size / float(sample_rate)
            recorded_seconds += chunk_duration

            rms = float(np.sqrt(np.mean(chunk * chunk)))

            if state == WAITING_FOR_SPEECH:
                if rms >= silence_rms:
                    state = IN_SPEECH
                elif recorded_seconds >= min_seconds:
                    break
                continue

            if rms < silence_rms:
                trailing_silence_seconds += chunk_duration
                if trailing_silence_seconds >= silence_seconds:
                    break
            else:
                trailing_silence_seconds = 0.0

    if state == WAITING_FOR_SPEECH:
        audio = np.zeros(0, dtype=np.float32)
    else:
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    print(f"✅ Recording complete ({len(audio) / sample_rate:.1f}s).")
    return audio


def resample_audio(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample audio from orig_sr to target_sr."""
    if orig_sr == target_sr:
        return audio
    num_samples = int(len(audio) * target_sr / orig_sr)
    return scipy_resample(audio, num_samples).astype(np.float32)


# ── French phonetic post-correction ────────────────────────────────────────
# Whisper-small frequently confuses the /ʒ/ phoneme in French, mishearing
# "j'ai" as "gai/gay/geai/gué", "je" as "ge", and "jeune" as "geune". The
# corrections below target patterns where the misheard form would be very
# unusual French — typically a "g…" word standing where a verb or pronoun
# is grammatically required. They DO NOT touch the same words in contexts
# where the literal form is plausible (e.g. "un homme gai", "les gens").

# Words that almost always follow "j'ai" — past participles, common nouns,
# adjectives describing states. Used as a right-context anchor.
_POST_JAI = (
    r"(?:un|une|des|le|la|les|l'|mon|ma|mes|"
    r"été|fait|vu|dit|pris|trouvé|reçu|compris|oublié|perdu|gagné|"
    r"mangé|bu|appris|écrit|lu|entendu|"
    r"besoin|envie|peur|faim|soif|froid|chaud|mal|hâte|raison|tort|"
    r"l'impression|l'habitude)"
)

# Verbs/auxiliaries that almost always follow "je" — used to disambiguate
# "ge" → "je" without touching real words starting with "ge" (geste, gens,
# général, génial, etc., none of which are verbs in this position).
_POST_JE = (
    r"(?:suis|vais|veux|peux|dois|sais|crois|pense|fais|prends|cherche|"
    r"voudrais|aimerais|préfère|comprends|connais|trouve|vois|sens|"
    r"l'ai|t'ai|m'en|n'ai|n'arrive)"
)

# Each entry is (compiled pattern, replacement). Patterns are
# case-insensitive and use lookaheads for context so the replacement is
# always exactly the misheard word — the substitution preserves the
# original first-character capitalization.
FRENCH_CORRECTIONS: list[tuple[re.Pattern, str]] = [
    # j'ai misheard as gai / gay / geai / gué, when followed by a typical
    # post-"j'ai" word
    (re.compile(rf"\b(?:gai|gay|geai|gué)\b(?=\s+{_POST_JAI}\b)", re.IGNORECASE), "j'ai"),
    # je misheard as "ge" before a typical post-"je" verb
    (re.compile(rf"\bge\b(?=\s+{_POST_JE}\b)", re.IGNORECASE), "je"),
    # jeune / jeunes misheard as geune / geunes (no real French word)
    (re.compile(r"\bgeune\b", re.IGNORECASE), "jeune"),
    (re.compile(r"\bgeunes\b", re.IGNORECASE), "jeunes"),
    # déjà misheard as déga (no real French word)
    (re.compile(r"\bdéga\b", re.IGNORECASE), "déjà"),
    # j'aime / j'aimerais misheard as gaime / gaimerais (no real French word)
    (re.compile(r"\bgaime\b", re.IGNORECASE), "j'aime"),
    (re.compile(r"\bgaimerais\b", re.IGNORECASE), "j'aimerais"),
    # j'y misheard as gît — the verb gésir form is extremely rare in
    # voice commands, so correct only when followed by a verb of motion.
    (re.compile(r"\bgît\b(?=\s+(?:vais|suis|pense|crois|tiens)\b)", re.IGNORECASE), "j'y"),
]


def _apply_french_corrections(text: str) -> str:
    """Run all ``FRENCH_CORRECTIONS`` over ``text`` in order, preserving
    the original first-character capitalization of each replaced span.
    """
    def make_repl(replacement: str):
        def repl(match: re.Match) -> str:
            if match.group(0)[:1].isupper():
                return replacement[:1].upper() + replacement[1:]
            return replacement
        return repl

    for pattern, replacement in FRENCH_CORRECTIONS:
        text = pattern.sub(make_repl(replacement), text)
    return text


def _transcribe_groq(audio_path_or_bytes, sample_rate: int = WHISPER_SAMPLE_RATE,
                     language: str = None, model: str = None) -> str:
    """POST audio to Groq's OpenAI-compatible Whisper endpoint and return
    the transcribed text.

    ``audio_path_or_bytes`` may be:
      * a numpy float32 sample array — resampled to 16kHz and WAV-encoded
        in memory,
      * raw WAV ``bytes`` / ``bytearray`` — sent as-is,
      * a filesystem path to an audio file — read and sent as-is.

    The Groq model comes from the ``stt_groq_model`` setting unless ``model``
    is given (validated against ``GROQ_WHISPER_MODELS``). Raises on a missing
    key or any HTTP/network error so the caller can fall back to local.
    """
    import requests

    if not has_key("groq"):
        raise RuntimeError("Groq STT: no API key configured")

    model = model or get_setting("stt_groq_model", GROQ_DEFAULT_MODEL) or GROQ_DEFAULT_MODEL
    if model not in GROQ_WHISPER_MODELS:
        model = GROQ_DEFAULT_MODEL

    # Normalize the input to WAV bytes.
    if isinstance(audio_path_or_bytes, np.ndarray):
        import soundfile as sf
        audio_16k = resample_audio(audio_path_or_bytes, sample_rate, WHISPER_SAMPLE_RATE)
        buf = io.BytesIO()
        sf.write(buf, audio_16k, WHISPER_SAMPLE_RATE, format="WAV", subtype="PCM_16")
        wav_bytes = buf.getvalue()
    elif isinstance(audio_path_or_bytes, (bytes, bytearray)):
        wav_bytes = bytes(audio_path_or_bytes)
    else:  # treat as a filesystem path
        with open(audio_path_or_bytes, "rb") as f:
            wav_bytes = f.read()

    data = {"model": model, "response_format": "json"}
    if language:
        data["language"] = language

    # Rotate through the user's Groq key chain on a 429/5xx/auth/timeout error
    # before the caller (``transcribe``) falls back to local faster-whisper.
    # ``wav_bytes`` is reusable across attempts, so each retry re-sends the same
    # audio with the next key. Shared rotation via
    # :func:`osia.utils.key_chain.with_key_rotation`.
    def _do(key):
        resp = requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data=data,
            timeout=60,
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()

    text = with_key_rotation("groq", _do, label="stt:groq")
    print(f"[STT] transcribed via groq ({model})")
    return text


def _transcribe_local_result(
    audio: np.ndarray,
    sample_rate: int = DEVICE_SAMPLE_RATE,
    language: str = None,
    model_size: str = None,
) -> TranscriptionResult:
    """Transcribe locally and classify using faster-whisper's own thresholds."""
    model, size = _get_model(model_size)
    print(f"[STT] transcribed via local (faster-whisper {size})")
    # Resample to 16kHz for Whisper
    audio_16k = resample_audio(audio, sample_rate, WHISPER_SAMPLE_RATE)
    # The initial_prompt is conditioning text — it biases decoding without
    # being transcribed. Short command examples across all supported
    # languages help Whisper lock onto brief utterances.
    initial_prompt = (
        "Ouvre Brave. Va sur YouTube. Quel temps fait-il ? Ouvre Spotify. "
        "Nouvelle conversation. Prends une capture d'écran. "
        "Open Brave. Go to YouTube. What's the weather? Open Spotify. "
        "New conversation. Take a screenshot. "
        "打开浏览器。播放音乐。天气怎么样？ "
        "ブラウザを開く。音楽を再生する。新しい会話。 "
        "Öffne Brave. Geh zu YouTube. Wie ist das Wetter? "
        "Abre Brave. Ve a YouTube. ¿Qué tiempo hace?"
    )
    segments, info = model.transcribe(
        audio_16k, beam_size=5, best_of=5, language=language,
        initial_prompt=initial_prompt,
    )
    segment_list = list(segments)
    text = " ".join(segment.text for segment in segment_list).strip()
    if getattr(info, "language", None) == "fr":
        text = _apply_french_corrections(text)

    if not text or not segment_list:
        return TranscriptionResult(
            text="",
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            segment_count=len(segment_list),
            average_log_probability=None,
            minimum_average_log_probability=None,
            maximum_no_speech_probability=None,
            maximum_compression_ratio=None,
            classification="rejected",
            rejection_reasons=("empty_or_no_speech",),
        )

    options = getattr(info, "transcription_options", None)
    try:
        avg_logprobs = [
            float(getattr(segment, "avg_logprob")) for segment in segment_list
        ]
        no_speech_probs = [
            float(getattr(segment, "no_speech_prob")) for segment in segment_list
        ]
        compression_ratios = [
            float(getattr(segment, "compression_ratio")) for segment in segment_list
        ]
        log_prob_threshold = float(getattr(options, "log_prob_threshold"))
        no_speech_threshold = float(getattr(options, "no_speech_threshold"))
        compression_ratio_threshold = float(
            getattr(options, "compression_ratio_threshold")
        )
        thresholds = [
            log_prob_threshold,
            no_speech_threshold,
            compression_ratio_threshold,
        ]
    except (AttributeError, TypeError, ValueError):
        avg_logprobs = []
        no_speech_probs = []
        compression_ratios = []
        thresholds = []

    metrics = avg_logprobs + no_speech_probs + compression_ratios
    if options is None or not metrics or not thresholds or any(
        not math.isfinite(value) for value in metrics + thresholds
    ):
        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            segment_count=len(segment_list),
            average_log_probability=None,
            minimum_average_log_probability=None,
            maximum_no_speech_probability=None,
            maximum_compression_ratio=None,
            classification="rejected",
            rejection_reasons=("quality_metadata_unavailable",),
        )

    reasons: list[str] = []
    if any(value < log_prob_threshold for value in avg_logprobs):
        reasons.append("low_average_log_probability")
    if any(
        no_speech > no_speech_threshold and logprob < log_prob_threshold
        for no_speech, logprob in zip(no_speech_probs, avg_logprobs)
    ):
        reasons.append("probable_no_speech")
    if any(
        value > compression_ratio_threshold for value in compression_ratios
    ):
        reasons.append("degenerate_compression_ratio")

    return TranscriptionResult(
        text=text,
        language=getattr(info, "language", None),
        language_probability=getattr(info, "language_probability", None),
        segment_count=len(segment_list),
        average_log_probability=sum(avg_logprobs) / len(avg_logprobs),
        minimum_average_log_probability=min(avg_logprobs),
        maximum_no_speech_probability=max(no_speech_probs),
        maximum_compression_ratio=max(compression_ratios),
        classification="rejected" if reasons else "accepted",
        rejection_reasons=tuple(reasons),
    )


def _transcribe_local(audio: np.ndarray, sample_rate: int = DEVICE_SAMPLE_RATE,
                      language: str = None, model_size: str = None) -> str:
    """Compatibility wrapper returning only accepted local text."""
    result = _transcribe_local_result(
        audio,
        sample_rate=sample_rate,
        language=language,
        model_size=model_size,
    )
    return result.text if result.accepted else ""


def _transcribe_with_result(
    provider: str,
    audio: np.ndarray,
    sample_rate: int,
    language: str,
    model_size: str,
) -> TranscriptionResult:
    """Dispatch to a single STT engine. Raises on any failure so the caller
    can decide whether to fall back."""
    if provider == "groq":
        # _transcribe_groq raises if no key is configured.
        text = _transcribe_groq(
            audio,
            sample_rate=sample_rate,
            language=language,
        )
        if not text:
            return _empty_transcription_result("empty_or_no_speech")
        return TranscriptionResult(
            text=text,
            language=language,
            language_probability=None,
            segment_count=1,
            average_log_probability=None,
            minimum_average_log_probability=None,
            maximum_no_speech_probability=None,
            maximum_compression_ratio=None,
            classification="accepted_unverified_provider",
            rejection_reasons=(),
        )
    return _transcribe_local_result(
        audio,
        sample_rate=sample_rate,
        language=language,
        model_size=model_size,
    )


def _transcribe_result(
    audio: np.ndarray,
    sample_rate: int,
    language: str,
    model_size: str,
) -> TranscriptionResult:
    """Run provider fallback while preserving a structured quality result."""
    provider = get_setting("stt_provider", "local") or "local"
    fallback = get_setting("stt_fallback_provider", "local") or "local"

    try:
        return _transcribe_with_result(
            provider,
            audio,
            sample_rate,
            language,
            model_size,
        )
    except Exception as exc:
        if fallback == "none" or fallback == provider:
            print(
                f"⚠️  [STT] primary {provider} failed "
                f"({type(exc).__name__}) — no usable fallback; returning empty."
            )
            return _empty_transcription_result("transcription_error")
        print(
            f"[STT] primary {provider} failed ({type(exc).__name__}), "
            f"falling back to {fallback}."
        )
        try:
            return _transcribe_with_result(
                fallback,
                audio,
                sample_rate,
                language,
                model_size,
            )
        except Exception as fallback_exc:
            print(
                f"⚠️  [STT] fallback {fallback} also failed "
                f"({type(fallback_exc).__name__}); returning empty."
            )
            return _empty_transcription_result("transcription_error")


def transcribe(
    audio: np.ndarray,
    sample_rate: int = DEVICE_SAMPLE_RATE,
    language: str = None,
    model_size: str = None,
    *,
    return_result: bool = False,
) -> str | TranscriptionResult:
    """Transcribe audio samples to text.

    The active engine is chosen fresh on every call from the ``stt_provider``
    setting (like TTS reads ``tts_provider``), so switching it in settings
    takes effect without a restart:

      * ``groq``  -> Groq's hosted Whisper (``stt_groq_model``) via REST.
      * ``local`` -> faster-whisper on-device (``stt_local_model``), default.

    If the primary engine fails (error, missing key, timeout), OSIA falls back
    to the provider named by the ``stt_fallback_provider`` setting (``local``,
    ``groq``, or ``none``). When the fallback is ``none`` — or is the same
    engine that just failed — no fallback is attempted and an empty string is
    returned. The transition is logged as
    ``[STT] primary <X> failed, falling back to <Y>``.

    Audio is expected at the device sample rate and will be resampled to 16kHz.
    language=None enables auto-detection (supports fr + en). When the local
    engine detects French, a post-correction pass fixes common Whisper /ʒ/
    confusions (j'ai vs gai/gay, je vs ge, …). The French corrections are
    specific to faster-whisper-small and are not applied to Groq output.

    By default this compatibility wrapper returns accepted text or ``""``.
    ``return_result=True`` exposes the narrow internal quality result used by
    voice-command callers; it never exposes audio, tokens, or word timestamps.
    """
    result = _transcribe_result(
        audio,
        sample_rate,
        language,
        model_size,
    )
    if return_result:
        return result
    return result.text if result.accepted else ""


def listen(duration: int = RECORD_SECONDS) -> str:
    """Record (via VAD) and transcribe — one-step convenience function.
    The `duration` argument is kept for backward compatibility but is
    interpreted as the maximum recording length in seconds.
    """
    audio = record_audio_vad(max_seconds=float(duration) if duration else VAD_MAX_SECONDS)
    if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
        return ""  # Basically silent
    text = transcribe(audio, sample_rate=VAD_SAMPLE_RATE)
    return text


# ── PHASE 3: Listening loop with retries ────────────────────────────────────

def listen_for_command(
    max_retries: int = 3,
    on_ready: Callable[[], None] | None = None,
) -> str:
    """Keep recording (via VAD) until a valid command is received.

    Each attempt records until the user stops talking, then transcribes. Only a
    quality-accepted result is returned; rejected results stay inside this
    sequential retry loop. `on_ready` is emitted at most once, immediately
    before the first retained capture begins.
    """
    ready_emitted = False

    def emit_ready_once() -> None:
        nonlocal ready_emitted
        if ready_emitted:
            return
        ready_emitted = True
        if on_ready is not None:
            on_ready()

    for attempt in range(1, max_retries + 1):
        print(f"👂 Listening... (attempt {attempt}/{max_retries})")
        audio = record_audio_vad(on_ready=emit_ready_once)

        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            print("   ⚠️  Silent chunk, retrying...")
            continue

        raw_result = transcribe(
            audio,
            sample_rate=VAD_SAMPLE_RATE,
            return_result=True,
        )
        if isinstance(raw_result, TranscriptionResult):
            result = raw_result
        else:
            # Keep deterministic callers that inject the historical string
            # result compatible; production engines return structured results.
            text = (raw_result or "").strip()
            legacy_valid = bool(text) and (
                is_new_chat_command(text) or len(text.split()) >= 3
            )
            result = TranscriptionResult(
                text=text,
                language=None,
                language_probability=None,
                segment_count=1 if text else 0,
                average_log_probability=None,
                minimum_average_log_probability=None,
                maximum_no_speech_probability=None,
                maximum_compression_ratio=None,
                classification="accepted_legacy" if legacy_valid else "rejected",
                rejection_reasons=() if legacy_valid else ("legacy_text_invalid",),
            )

        if not result.accepted:
            reasons = ",".join(result.rejection_reasons) or "quality_rejected"
            print(f"   ⚠️  Transcript rejected ({reasons}); retrying...")
            continue

        print(
            "   ✅ Transcript accepted "
            f"(language={result.language or 'unknown'}, "
            f"segments={result.segment_count})."
        )
        return result.text

    print("   ❌ Max retries reached, no valid command.")
    return ""


if __name__ == "__main__":
    print("Testing listen_for_command with VAD (max 3 retries)...")
    result = listen_for_command(max_retries=3)
    if result:
        print(f"Command received ({len(result)} chars).")
    else:
        print("No command received.")
