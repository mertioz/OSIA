#!/usr/bin/env python3
"""
OSIA Speech-to-Text — records from microphone and transcribes using faster-whisper.
Phase 1: language=None for auto-detection (supports fr + en).
Phase 3: listen_for_command() with retry loop — keeps listening until
         a meaningful command (3+ words) is received.
"""

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


def transcribe(audio: np.ndarray, sample_rate: int = DEVICE_SAMPLE_RATE,
               language: str = None, model_size: str = None) -> str:
    """Transcribe audio samples to text using faster-whisper.
    Audio is expected at the device sample rate and will be resampled to 16kHz.
    language=None enables auto-detection (supports fr + en).
    """
    model = _get_model()
    # Resample to 16kHz for Whisper
    audio_16k = resample_audio(audio, sample_rate, WHISPER_SAMPLE_RATE)
    segments, info = model.transcribe(audio_16k, beam_size=5, language=language,
                                       initial_prompt="Assistant vocal, commandes en français et anglais.")
    text = " ".join(segment.text for segment in segments)
    return text.strip()


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
