"""Short local readiness sound; independent of speech synthesis and its mute."""
from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np

try:
    import sounddevice as sd
except Exception:
    sd = None

_playing = threading.Lock()


def wake_chime_samples(sample_rate=24000) -> np.ndarray:
    """Bright, overlapping bell notes, with a clear attack and a short tail."""
    sound = np.zeros(round(sample_rate * 0.18), dtype=np.float32)
    for frequency, offset, weight in ((1174.66, 0.0, 1.0), (1760.0, 0.035, 0.85)):
        first = round(sample_rate * offset)
        times = np.arange(len(sound) - first, dtype=np.float32) / sample_rate
        attack = np.minimum(times / 0.004, 1.0)
        release = np.minimum((times[-1] - times) / 0.012, 1.0)
        envelope = attack * release * np.exp(-times / 0.05)
        phase = 2 * np.pi * frequency * times
        tone = np.sin(phase) + 0.12 * np.sin(2.005 * phase) + 0.025 * np.sin(4.01 * phase)
        sound[first:] += weight * envelope * tone
    # Higher digital level than the old cue, with headroom and no clipping.
    sound *= 0.55 / max(float(np.max(np.abs(sound))), 1e-9)
    return sound


def play_wake_chime(*, cancelled: Callable[[], bool] | None = None) -> bool:
    """Play on one owned output stream, never stopping or delaying input."""
    cancelled = cancelled or (lambda: False)
    if sd is None or cancelled() or not _playing.acquire(blocking=False):
        return False

    def play():
        try:
            for latency in ("low", None):
                opened = False
                try:
                    if cancelled():
                        return
                    samples = wake_chime_samples()
                    with sd.OutputStream(samplerate=24000, channels=1, dtype="float32", latency=latency) as output:
                        opened = True
                        for start in range(0, len(samples), 480):
                            if cancelled():
                                break
                            output.write(samples[start:start + 480])
                    return
                except Exception as exc:
                    # Retry only a rejected low-latency opening, never a cue
                    # that already started playing on an owned output stream.
                    if opened or latency is None:
                        print(f"[voice] wake sound unavailable ({type(exc).__name__})", flush=True)
                        return
        finally:
            _playing.release()

    try:
        threading.Thread(target=play, name="osia-wake-feedback", daemon=True).start()
    except RuntimeError:
        _playing.release()
        return False
    return True
