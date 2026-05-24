#!/usr/bin/env python3
"""
OSIA Speech-to-Text — records from microphone and transcribes using faster-whisper.
Phase 1: language=None for auto-detection (supports fr + en).
Phase 3: listen_for_command() with retry loop — keeps listening until
         a meaningful command (3+ words) is received.
"""

import re

import sounddevice as sd
import numpy as np
from scipy.signal import resample as scipy_resample
from faster_whisper import WhisperModel

DEVICE_SAMPLE_RATE = 48000  # Hardware native rate
WHISPER_SAMPLE_RATE = 16000
RECORD_SECONDS = 5
MODEL_SIZE = "small"  # tiny, base, small, medium, large

# Voice Activity Detection parameters
VAD_SAMPLE_RATE = 16000
VAD_CHUNK_SECONDS = 0.5
VAD_SILENCE_RMS = 0.015      # fallback if calibration fails; normally overridden
VAD_SILENCE_SECONDS = 2.0    # silence run length that ends the recording
VAD_MAX_SECONDS = 60.0       # hard cap on recording length
VAD_MIN_SECONDS = 3.0        # don't end before this many seconds

# Ambient calibration
VAD_CALIBRATION_SECONDS = 1.0
VAD_CALIBRATION_MULTIPLIER = 1.3
VAD_THRESHOLD_CEILING = 0.05   # never let calibrated threshold exceed this
_calibrated_silence_rms: float | None = None  # set by calibrate_ambient_threshold()

# Lazy-loaded model
_model = None

def _get_model():
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def record_audio(duration: int = RECORD_SECONDS, sample_rate: int = DEVICE_SAMPLE_RATE) -> np.ndarray:
    """Record audio from the default microphone for `duration` seconds.
    Returns a numpy array of float32 samples at the device sample rate.
    """
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


def calibrate_ambient_threshold(
    duration: float = VAD_CALIBRATION_SECONDS,
    multiplier: float = VAD_CALIBRATION_MULTIPLIER,
    sample_rate: int = VAD_SAMPLE_RATE,
) -> float:
    """Record `duration` seconds of ambient noise and derive a VAD silence
    threshold from it: `threshold = ambient_rms * multiplier`.

    Result is cached in `_calibrated_silence_rms` so subsequent calls to
    `record_audio_vad()` pick it up automatically.
    """
    global _calibrated_silence_rms
    n_samples = int(duration * sample_rate)
    audio = sd.rec(n_samples, samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    samples = audio.flatten()
    ambient_rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    threshold = min(ambient_rms * multiplier, VAD_THRESHOLD_CEILING)
    print(f"🎙️ Ambient RMS: {ambient_rms:.4f}, threshold set to {threshold:.4f}")
    _calibrated_silence_rms = threshold
    return threshold


def record_audio_vad(
    sample_rate: int = VAD_SAMPLE_RATE,
    chunk_seconds: float = VAD_CHUNK_SECONDS,
    silence_rms: float | None = None,
    silence_seconds: float = VAD_SILENCE_SECONDS,
    max_seconds: float = VAD_MAX_SECONDS,
    min_seconds: float = VAD_MIN_SECONDS,
) -> np.ndarray:
    """Record from the mic until the user stops talking, using a simple
    RMS-based Voice Activity Detection.

    The stream is consumed in `chunk_seconds`-sized blocks. Recording ends as
    soon as we've heard at least `min_seconds` of audio AND we've seen
    `silence_seconds` of consecutive sub-threshold chunks. A hard cap of
    `max_seconds` always applies.

    `silence_rms` defaults to the value produced by
    `calibrate_ambient_threshold()`. If calibration hasn't run yet, it is
    triggered automatically on the first call.

    Returns mono float32 samples at `sample_rate` — feed straight to
    `transcribe(audio, sample_rate=sample_rate)`.
    """
    if silence_rms is None:
        if _calibrated_silence_rms is None:
            calibrate_ambient_threshold(sample_rate=sample_rate)
        silence_rms = _calibrated_silence_rms if _calibrated_silence_rms is not None else VAD_SILENCE_RMS

    chunk_size = int(chunk_seconds * sample_rate)
    silence_chunks_needed = max(1, int(round(silence_seconds / chunk_seconds)))
    max_chunks = max(1, int(round(max_seconds / chunk_seconds)))
    min_chunks = max(1, int(round(min_seconds / chunk_seconds)))

    print(f"🎤 Listening (VAD)... speak now, silence ends recording.")

    chunks: list[np.ndarray] = []
    silent_run = 0

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=chunk_size,
    ) as stream:
        for i in range(max_chunks):
            data, _ = stream.read(chunk_size)
            chunk = data.flatten()
            chunks.append(chunk)

            rms = float(np.sqrt(np.mean(chunk * chunk)))

            if (i + 1) < min_chunks:
                continue

            if rms < silence_rms:
                silent_run += 1
                if silent_run >= silence_chunks_needed:
                    break
            else:
                silent_run = 0

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


def transcribe(audio: np.ndarray, sample_rate: int = DEVICE_SAMPLE_RATE,
               language: str = None, model_size: str = None) -> str:
    """Transcribe audio samples to text using faster-whisper.
    Audio is expected at the device sample rate and will be resampled to 16kHz.
    language=None enables auto-detection (supports fr + en). When the
    detected language is French, a post-correction pass fixes common
    Whisper /ʒ/ confusions (j'ai vs gai/gay, je vs ge, …).
    """
    model = _get_model()
    # Resample to 16kHz for Whisper
    audio_16k = resample_audio(audio, sample_rate, WHISPER_SAMPLE_RATE)
    # The initial_prompt is conditioning text — it biases decoding without
    # being transcribed. Listing common French words that exercise the J/G
    # /ʒ/ distinction nudges Whisper toward the right spellings.
    initial_prompt = (
        "Assistant vocal, commandes en français et en anglais. "
        "L'utilisateur dit souvent : j'ai besoin de, j'ai vu, j'ai oublié, "
        "j'aimerais, je veux, je vais, je pense que, je cherche. "
        "Mots fréquents avec j ou g doux : jamais, jardin, jeune, journée, "
        "déjà, déjeuner, juste, joli, jour, jouer, "
        "général, genre, gens, geste, girafe, gérer, génial. "
        "Distinguer « j'ai » (passé composé) de « gai » (joyeux) et "
        "« geai » (l'oiseau)."
    )
    segments, info = model.transcribe(
        audio_16k, beam_size=5, language=language,
        initial_prompt=initial_prompt,
    )
    text = " ".join(segment.text for segment in segments).strip()
    if getattr(info, "language", None) == "fr":
        text = _apply_french_corrections(text)
    return text


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

def listen_for_command(max_retries: int = 3) -> str:
    """Keep recording (via VAD) until a valid command is received.

    Each attempt records until the user stops talking, then transcribes.
    A valid command is at least 3 words. We retry up to `max_retries` times,
    then give up and return an empty string.
    """
    for attempt in range(1, max_retries + 1):
        print(f"👂 Listening... (attempt {attempt}/{max_retries})")
        audio = record_audio_vad()

        if audio.size == 0 or float(np.max(np.abs(audio))) < 0.01:
            print("   ⚠️  Silent chunk, retrying...")
            continue

        text = transcribe(audio, sample_rate=VAD_SAMPLE_RATE)
        if not text:
            print("   ⚠️  Nothing transcribed, retrying...")
            continue

        words = text.strip().split()
        if len(words) < 3:
            print(f"   ⚠️  Too few words: '{text}' ({len(words)} word(s)), retrying...")
            continue

        print(f"   ✅ Valid command: '{text}'")
        return text

    print("   ❌ Max retries reached, no valid command.")
    return ""


if __name__ == "__main__":
    print("Testing listen_for_command with VAD (max 3 retries)...")
    result = listen_for_command(max_retries=3)
    if result:
        print(f"Command received: {result}")
    else:
        print("No command received.")
