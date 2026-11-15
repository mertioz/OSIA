"""Opt-in local OSIA wake word and shared manual/acoustic voice ownership."""
from __future__ import annotations

import importlib.util
import threading
import time
from collections import deque

import numpy as np

from osia.config import ACOUSTIC_WAKE_AVAILABLE, ACOUSTIC_WAKE_UNAVAILABLE_REASON
from osia.voice.acoustic import CHUNK_SIZE, SAMPLE_RATE, WINDOW_SAMPLES, KeywordScorer, model_manifest

try:
    import sounddevice as sd
    _AUDIO_OK = True
except Exception:
    sd = None
    _AUDIO_OK = False


def _get_verifier():
    """Never load the retired pickle verifier, even if it is still on disk."""
    return None


def verifier_available() -> bool:
    return bool(acoustic_wake_status()["acoustic_wake_available"])


def acoustic_wake_status() -> dict[str, object]:
    try:
        if not ACOUSTIC_WAKE_AVAILABLE or not _AUDIO_OK or importlib.util.find_spec("vosk") is None:
            raise RuntimeError("Wake backend unavailable")
        model_manifest(verify_hashes=False)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError):
        return {"acoustic_wake_available": False, "reason": ACOUSTIC_WAKE_UNAVAILABLE_REASON}
    return {"acoustic_wake_available": True, "reason": "local_phonetic_keyword_model"}


class WakeAcceptanceGate:
    """Reserve the voice handler before an activation event reaches Qt."""

    def __init__(self, handler_lock, *, is_busy=None, is_tts_active=None):
        self.handler_lock = handler_lock
        self._is_busy = is_busy or (lambda: False)
        self._is_tts_active = is_tts_active or (lambda: False)
        self._reservation_lock = threading.Lock()
        self._pending_generation = None
        self._active = False
        self._closed = False

    def reserve(self, generation: int) -> bool:
        with self._reservation_lock:
            if self._closed:
                return False
        if self._is_busy() or self._is_tts_active():
            return False
        if not self.handler_lock.acquire(blocking=False):
            return False
        keep = False
        try:
            if self._is_busy() or self._is_tts_active():
                return False
            with self._reservation_lock:
                if self._closed or self._pending_generation is not None:
                    return False
                self._pending_generation = generation
                keep = True
            return True
        finally:
            if not keep:
                self.handler_lock.release()

    def consume(self, generation: int, *, is_current: bool) -> bool:
        with self._reservation_lock:
            if self._pending_generation != generation:
                return False
            self._pending_generation = None
            accepted = is_current and not self._closed
            if accepted:
                self._active = True
        if not accepted:
            self.handler_lock.release()
            return False
        return True

    def cancel(self, generation: int) -> None:
        release = False
        with self._reservation_lock:
            if self._pending_generation == generation:
                self._pending_generation = None
                release = True
        if release:
            self.handler_lock.release()

    def finish(self) -> None:
        with self._reservation_lock:
            if not self._active:
                return
            self._active = False
        self.handler_lock.release()

    def close(self) -> None:
        """Permanently close acceptance and release any pending/active owner."""

        release = False
        with self._reservation_lock:
            self._closed = True
            if self._pending_generation is not None:
                self._pending_generation = None
                release = True
            elif self._active:
                self._active = False
                release = True
        if release:
            self.handler_lock.release()


class VoiceSessionLifecycle:
    """Keep visible voice state and gate release owned by one session."""

    def __init__(self):
        self._lock = threading.RLock()
        self._current_session = None
        self._closed = False

    def begin(self, session_id) -> bool:
        """Make ``session_id`` authoritative, obsoleting any older finalizer."""

        with self._lock:
            if self._closed:
                return False
            self._current_session = session_id
            return True

    def emit_if_current(self, session_id, emit_state) -> bool:
        """Run a state emission only while ``session_id`` still owns the UI."""

        with self._lock:
            if self._closed or self._current_session is not session_id:
                return False
            emit_state()
            return True

    def emit_when_inactive(self, emit_state) -> bool:
        """Let non-voice state updates through only when voice owns no UI."""

        with self._lock:
            if self._closed or self._current_session is not None:
                return False
            emit_state()
            return True

    def finalize(
        self,
        session_id,
        *,
        wait_for_playback,
        release_gate,
        emit_final_state,
    ) -> bool:
        """Finalize only after playback and only for the authoritative session."""

        with self._lock:
            if self._closed or self._current_session is not session_id:
                return False
        if wait_for_playback() is False:
            return False
        with self._lock:
            if self._closed or self._current_session is not session_id:
                return False
            self._current_session = None
            release_gate()
            emit_final_state()
            return True

    def close(self) -> None:
        """Suppress every later state emission or gate release from a session."""

        with self._lock:
            self._closed = True
            self._current_session = None


class WakeWordDetector:
    """One microphone worker, one-shot activations, exact generation ownership.

    A successful wake closes its stream before delivering to Qt. Manual capture
    pauses and awaits that same stream. Raw audio lives only in bounded RAM and
    is erased on pause, cancellation, shutdown and handoff consumption.
    """

    def __init__(self, callback=None, *, acceptance_gate=None, acceptance_cancel=None,
                 is_blocked=None, _scorer_factory=None, _stream_factory=None, _clock=None):
        self.callback = callback
        self._acceptance_gate = acceptance_gate
        self._acceptance_cancel = acceptance_cancel
        self._is_blocked = is_blocked or (lambda: False)
        self._scorer_factory = _scorer_factory or KeywordScorer
        self._stream_factory = _stream_factory
        self._clock = _clock or time.monotonic
        self._condition = threading.Condition(threading.RLock())
        self._mic_idle = threading.Event()
        self._mic_idle.set()
        self._thread = None
        self._running = False
        self._armed = False
        self._generation = 0
        self._last_event_generation = None
        self._handoff = None
        self._not_before = 0.0
        self._capture_latency = "low"
        self.last_error = None

    @property
    def generation(self):
        with self._condition:
            return self._generation

    @property
    def running(self):
        with self._condition:
            return self._running

    @property
    def last_event_generation(self):
        with self._condition:
            return self._last_event_generation

    def is_generation_current(self, generation):
        with self._condition:
            return self._running and generation == self._generation

    def take_audio(self, generation):
        with self._condition:
            if not self.is_generation_current(generation) or self._last_event_generation != generation:
                return None
            audio, self._handoff = self._handoff, None
            return audio

    def _listen_session(self, generation, scorer):
        chunks = deque(maxlen=1 + WINDOW_SAMPLES // CHUNK_SIZE)
        factory = self._stream_factory or sd.InputStream
        with self._condition:
            if not self.is_generation_current(generation) or not self._armed:
                return False
            self._mic_idle.clear()
        capture_opened = False
        try:
            with factory(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK_SIZE, latency=self._capture_latency) as stream:
                capture_opened = True
                print(f"[wake] microphone capture active generation={generation}", flush=True)
                ready_logged = False
                while self.is_generation_current(generation):
                    with self._condition:
                        if not self._armed:
                            return False
                    data, overflow = stream.read(CHUNK_SIZE)
                    audio = np.asarray(data, dtype=np.int16).reshape(-1)
                    if audio.size != CHUNK_SIZE:
                        raise ValueError("Incomplete wake microphone frame")
                    if overflow and self._capture_latency == "low":
                        # Some drivers need larger buffers. Release this exact
                        # stream before reopening with its stable default.
                        self._capture_latency = None
                        return False
                    if overflow or self._is_blocked() or self._clock() < self._not_before:
                        chunks.clear()
                        ready_logged = False
                        continue
                    chunks.append(audio.copy())
                    if sum(x.size for x in chunks) < getattr(scorer, "minimum_samples", WINDOW_SAMPLES):
                        continue
                    if not ready_logged:
                        print(f"[wake] fresh keyword window armed generation={generation}", flush=True)
                        ready_logged = True
                    window = np.concatenate(chunks)[-WINDOW_SAMPLES:]
                    score = scorer.score(window)
                    if score < scorer.threshold:
                        continue
                    with self._condition:
                        if not self.is_generation_current(generation) or not self._armed:
                            return False
                        if self._acceptance_gate is not None and not self._acceptance_gate(generation):
                            chunks.clear()
                            continue
                        self._armed = False
                        self._last_event_generation = generation
                        handoff = getattr(scorer, "command_audio", None)
                        self._handoff = (handoff(window) if callable(handoff)
                                         else window.astype(np.float32) / 32768)
                    print(f"[wake] keyword accepted score={score:.4f} generation={generation}", flush=True)
                    return True
        except Exception as exc:
            if not capture_opened and self._capture_latency == "low":
                self._capture_latency = None
                print(f"[wake] low-latency input unavailable ({type(exc).__name__}); using device default", flush=True)
                return False
            raise
        finally:
            chunks.clear()
            self._mic_idle.set()

    def _listen_loop(self):
        try:
            scorer = self._scorer_factory()
            print("[wake] local OSIA detector ready (CPU, offline)", flush=True)
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: not self._running or self._armed)
                    if not self._running:
                        return
                    generation = self._generation
                if self._listen_session(generation, scorer):
                    with self._condition:
                        if not self.is_generation_current(generation):
                            if self._acceptance_cancel:
                                self._acceptance_cancel(generation)
                            continue
                    # Delivery never holds the detector lock. Qt consumes the
                    # exact reserved generation and rejects any stop/pause race.
                    try:
                        if self.callback:
                            self.callback(generation)
                        elif self._acceptance_cancel:
                            self._acceptance_cancel(generation)
                    except Exception:
                        with self._condition:
                            self._handoff = None
                        if self._acceptance_cancel:
                            self._acceptance_cancel(generation)
                        self.request_rearm(generation)
        except Exception as exc:
            self.last_error = type(exc).__name__
            print(f"[wake] local detector unavailable ({self.last_error}); Ctrl+Alt+P remains available", flush=True)
        finally:
            with self._condition:
                pending = self._last_event_generation
                self._handoff = None
                self._running = False
                self._armed = False
                self._condition.notify_all()
            if pending is not None and self._acceptance_cancel:
                self._acceptance_cancel(pending)
            self._mic_idle.set()

    def start(self):
        if not verifier_available() and self._scorer_factory is KeywordScorer:
            self.last_error = "ModelOrAudioUnavailable"
            return False
        with self._condition:
            if self._running:
                return True
            if self._thread is not None and self._thread.is_alive():
                return False
            self._generation += 1
            self._running = self._armed = True
            self.last_error = None
            self._capture_latency = "low"
            self._handoff = None
            self._thread = threading.Thread(target=self._listen_loop, name="osia-wake-listener", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._thread = None
                self._running = self._armed = False
                self.last_error = "ListenerStartFailed"
                return False
        return True

    def pause(self):
        with self._condition:
            self._armed = False
            self._generation += 1
            self._handoff = None
            self._condition.notify_all()
        return self._mic_idle.wait(timeout=2.0)

    def request_rearm(self, accepted_generation=None):
        with self._condition:
            if not self._running or self._armed:
                return False
            if accepted_generation is not None and accepted_generation != self._generation:
                return False
            self._generation += 1
            self._last_event_generation = None
            self._handoff = None
            self._not_before = self._clock() + 0.48
            self._armed = True
            self._condition.notify_all()
            return True

    def stop(self):
        with self._condition:
            generation = self._last_event_generation
            self._running = self._armed = False
            self._generation += 1
            self._handoff = None
            self._last_event_generation = None
            self._condition.notify_all()
            thread = self._thread
        if generation is not None and self._acceptance_cancel:
            self._acceptance_cancel(generation)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return self._mic_idle.is_set()


if __name__ == "__main__":
    # Even this developer entry point uses the same canonical desktop pipeline.
    from osia.main import main
    main()
