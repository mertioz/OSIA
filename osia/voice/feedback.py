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
    """Two soft, rising notes with smooth edges, generated without a file/API."""
    count = int(sample_rate * 0.05)
    times = np.arange(count, dtype=np.float32) / sample_rate
    envelope = np.sin(np.linspace(0, np.pi, count, dtype=np.float32)) ** 2
    notes = [
        0.12 * np.sin(2 * np.pi * frequency * times) * envelope
        for frequency in (1046.5, 1568.0)
    ]
    silence = np.zeros(int(sample_rate * 0.015), dtype=np.float32)
    return np.concatenate((notes[0], silence, notes[1])).astype(np.float32)


def play_wake_chime(*, cancelled: Callable[[], bool] | None = None) -> bool:
    """Play on one owned output stream, never stopping or delaying input."""
    cancelled = cancelled or (lambda: False)
    if sd is None or cancelled() or not _playing.acquire(blocking=False):
        return False

    def play():
        try:
            if cancelled():
                return
            samples = wake_chime_samples()
            with sd.OutputStream(samplerate=24000, channels=1, dtype="float32") as output:
                for start in range(0, len(samples), 480):
                    if cancelled():
                        break
                    output.write(samples[start:start + 480])
        except Exception as exc:
            print(f"[voice] wake sound unavailable ({type(exc).__name__})", flush=True)
        finally:
            _playing.release()

    try:
        threading.Thread(target=play, name="osia-wake-feedback", daemon=True).start()
    except RuntimeError:
        _playing.release()
        return False
    return True
