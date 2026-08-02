#!/usr/bin/env python3
"""
Release-disabled acoustic wake compatibility layer.

WOR-211 intentionally disables acoustic activation because no packaged local
verifier passed the lexical-safety gate. The legacy detector type remains as
an inert compatibility surface; Ctrl+Alt+P is the supported voice activation
path until WOR-214 supplies and validates a replacement.
"""

import threading
import time
from collections import deque
import numpy as np

from osia.config import (
    ACOUSTIC_WAKE_AVAILABLE,
    ACOUSTIC_WAKE_UNAVAILABLE_REASON,
)

# sounddevice loads PortAudio at import time and can fail when no audio backend
# / drivers are present. Degrade gracefully: with ``sd = None`` the listen loop
# bails early and OSIA stays reachable via the capsule / Ctrl+Alt+O (WOR-11).
try:
    import sounddevice as sd
    _AUDIO_OK = True
except Exception as _audio_exc:  # PortAudio missing / no audio device
    sd = None
    _AUDIO_OK = False
    if ACOUSTIC_WAKE_AVAILABLE:
        print(
            f"⚠️  [wake] audio backend unavailable ({_audio_exc}); wake-word "
            "detection is disabled (use the capsule / Ctrl+Alt+O)."
        )

# openWakeWord provides the AudioFeatures mel-spectrogram preprocessor. It is an
# optional dependency: degrade gracefully when it (or its onnxruntime/tflite
# backend) is missing so a fresh install / Windows box without the package still
# starts and stays reachable via the capsule / Ctrl+Alt+O (mirrors _AUDIO_OK).
#
# Importing openwakeword pulls in onnxruntime/tflite and costs ~1.2s on Windows.
# It is only needed inside the wake-detection daemon loop, never on the GUI
# startup path — so here we only CHECK availability with a cheap find_spec (no
# import) and defer the actual ``from openwakeword.utils import AudioFeatures`` to
# _get_feature_extractor(), which runs on the detector's background thread (WOR
# perf fix). ``AudioFeatures`` stays None until first use; ``_WAKE_OK`` still
# drives graceful degradation exactly as before.
import importlib.util as _importlib_util

AudioFeatures = None
try:
    _WAKE_OK = _importlib_util.find_spec("openwakeword") is not None
except Exception:
    _WAKE_OK = False
if not _WAKE_OK and ACOUSTIC_WAKE_AVAILABLE:
    print(
        "⚠️  [wake] openwakeword unavailable; wake-word "
        "detection is disabled (use the capsule / Ctrl+Alt+O)."
    )

# === Audio settings ===
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz — required by openWakeWord

# === Detection settings ===
VERIFIER_THRESHOLD = 0.92  # Score above which we consider "osia" detected
COOLDOWN_SECONDS = 1.5    # Minimum time between detections
PATIENCE_FRAMES = 8       # Number of consecutive positive frames required
# Voice-activity energy gate (RMS over the verifier's ~1.28s context window).
# The universal verifier (WOR-37) has scored silent/ambient rooms around 0.91,
# close to VERIFIER_THRESHOLD, so high scores only count when real speech is
# present in the window. 450 remains far below the local synthetic wake-word
# samples while rejecting more quiet-room and fan/keyboard noise than the
# original gate.
ENERGY_GATE_RMS = 450
DIAGNOSTIC_LOG_INTERVAL_SECONDS = 5.0
DIAGNOSTIC_SCORE_MARGIN = 0.03

# === Lazy-loaded globals ===
_verifier_model = None
_verifier_n_frames = None
_feature_extractor = None
_vad_model = None

# The verifier trails live audio, so use its bounded 0.40-0.72 second
# VAD alignment instead of retaining the maximum from its full 1.28 second
# context. RMS and VAD must agree inside the same six aligned chunks.
VAD_THRESHOLD = 0.5
VAD_ALIGNMENT_OLDER_FRAMES = 10
VAD_ALIGNMENT_NEWER_FRAMES = 4
QUIET_REARM_SECONDS = 0.48
QUIET_REARM_CHUNKS = max(
    1, round(QUIET_REARM_SECONDS / (CHUNK_SIZE / SAMPLE_RATE)),
)


def _get_verifier():
    """The retired custom verifier is never loadable in this release."""

    return None


def _get_n_frames():
    """Get the number of feature frames the verifier expects."""
    global _verifier_n_frames
    if _verifier_n_frames is None:
        _get_verifier()
    return _verifier_n_frames or 16


def _get_feature_extractor():
    """Lazy-load the openWakeWord AudioFeatures preprocessor.

    The heavy ``openwakeword`` import (onnxruntime/tflite, ~1.2s) is performed
    here, on first use, rather than at module import time — this method is only
    ever called from the detector's background ``_listen_loop`` thread, so the
    cost never lands on the GUI startup path.
    """
    global _feature_extractor, AudioFeatures, _WAKE_OK
    if not ACOUSTIC_WAKE_AVAILABLE:
        return None
    if _feature_extractor is None:
        if AudioFeatures is None:
            try:
                from openwakeword.utils import AudioFeatures as _AudioFeatures
            except Exception as exc:
                # find_spec reported openwakeword present but its backend
                # (onnxruntime/tflite) failed to load — degrade exactly as a
                # missing package would, instead of crashing the listen thread.
                _WAKE_OK = False
                print(
                    f"⚠️  [wake] openwakeword import failed ({exc}); wake-word "
                    "detection disabled (use the capsule / Ctrl+Alt+O)."
                )
                return None
            AudioFeatures = _AudioFeatures
        # Force the ONNX backend explicitly (tflite's runtime ships no Windows
        # wheels; onnxruntime does, on CPUExecutionProvider — no CUDA needed),
        # and make sure openWakeWord's feature models are actually present.
        # openWakeWord ships WITHOUT its melspectrogram/embedding models and
        # fetches them on first use — a fresh install (Windows especially) has
        # an empty resources/ dir, so the first AudioFeatures(onnx) raises
        # "melspectrogram.onnx ... File doesn't exist" and wake-word detection
        # silently never works. Self-heal: download the feature + VAD models once
        # (a non-matching model name skips the unused pretrained wakeword models —
        # OSIA classifies with its own verifier) and retry. (WOR-11)
        def _build_audiofeatures():
            return AudioFeatures(
                sr=SAMPLE_RATE, ncpu=1, inference_framework="onnx",
            )

        try:
            _feature_extractor = _build_audiofeatures()
        except Exception as first_exc:
            print(
                f"ℹ️  [wake] openWakeWord feature models not found ({first_exc}); "
                "downloading them now (one-time, requires internet)…"
            )
            try:
                from openwakeword.utils import download_models
                download_models(model_names=["_osia_features_only"])
                _feature_extractor = _build_audiofeatures()
            except Exception as exc:
                _WAKE_OK = False
                print(
                    f"⚠️  [wake] could not download/initialise the openWakeWord "
                    f"ONNX feature extractor ({exc}); wake-word detection "
                    "disabled (use the capsule / Ctrl+Alt+O). Check your internet "
                    "connection and that 'onnxruntime' is installed."
                )
                return None
        provider = getattr(_feature_extractor, "onnx_execution_provider", "?")
        print(f"👂 [wake] openWakeWord feature extractor ready "
              f"(ONNX provider: {provider}).")
    return _feature_extractor


def _get_vad():
    """Lazy-load openWakeWord's bundled Silero VAD model."""

    global _vad_model, _WAKE_OK
    if not ACOUSTIC_WAKE_AVAILABLE:
        return None
    if _vad_model is not None:
        return _vad_model
    try:
        from openwakeword.vad import VAD

        _vad_model = VAD(n_threads=1)
    except Exception as exc:
        _WAKE_OK = False
        print(
            f"âš ï¸  [wake] bundled VAD unavailable ({exc}); wake-word "
            "detection disabled (use the capsule / Ctrl+Alt+O)."
        )
        return None
    return _vad_model


def verifier_available() -> bool:
    """Return the release contract, independent of settings and model files."""

    return ACOUSTIC_WAKE_AVAILABLE


def acoustic_wake_status() -> dict[str, object]:
    """Return the truthful source/package acoustic-wake status."""

    return {
        "acoustic_wake_available": ACOUSTIC_WAKE_AVAILABLE,
        "reason": ACOUSTIC_WAKE_UNAVAILABLE_REASON,
    }


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
    """Legacy detector surface; :meth:`start` is inert in this release."""

    def __init__(
        self,
        callback=None,
        *,
        acceptance_gate=None,
        acceptance_cancel=None,
        _clock=None,
        _feature_extractor_factory=None,
        _verifier_factory=None,
        _vad_factory=None,
        _stream_factory=None,
    ):
        self.callback = callback
        self._acceptance_gate = acceptance_gate
        self._acceptance_cancel = acceptance_cancel
        self._clock = _clock or time.monotonic
        self._feature_extractor_factory = (
            _feature_extractor_factory or _get_feature_extractor
        )
        self._verifier_factory = _verifier_factory or _get_verifier
        self._vad_factory = _vad_factory or _get_vad
        self._stream_factory = _stream_factory

        self._condition = threading.Condition(threading.RLock())
        self._running = False
        self._thread = None
        self._generation = 0
        self._armed = False
        self._rearm_requested = False
        self._last_detection = float("-inf")
        self._last_event_generation = None
        self._positive_streak = 0
        self._fresh_chunks = 0
        self._quiet_chunks = 0
        self._last_diag_log = float("-inf")
        self._last_score = None
        self._last_vad_score = None
        self._last_rms = None
        self._preprocessor = None
        self._vad = None
        self._device_logged = False

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    @property
    def last_event_generation(self):
        with self._condition:
            return self._last_event_generation

    def is_generation_current(self, generation: int) -> bool:
        with self._condition:
            return self._running and generation == self._generation

    def _reset_candidate_locked(self, *, reset_detection_time: bool = False) -> None:
        self._positive_streak = 0
        self._fresh_chunks = 0
        self._quiet_chunks = 0
        self._last_diag_log = float("-inf")
        self._last_score = None
        self._last_vad_score = None
        self._last_rms = None
        if reset_detection_time:
            self._last_detection = float("-inf")

    @staticmethod
    def _reset_components(preprocessor, vad) -> None:
        if preprocessor is not None:
            preprocessor.reset()
        if vad is not None:
            vad.reset_states()
            prediction_buffer = getattr(vad, "prediction_buffer", None)
            if prediction_buffer is not None:
                prediction_buffer.clear()

    def _log_diagnostic(self, now: float, message: str) -> None:
        with self._condition:
            if now - self._last_diag_log < DIAGNOSTIC_LOG_INTERVAL_SECONDS:
                return
            self._last_diag_log = now
        print(message, flush=True)

    def _session_current(self, generation: int, *, quiet: bool) -> bool:
        with self._condition:
            if not self._running or generation != self._generation:
                return False
            return self._rearm_requested if quiet else self._armed

    def _new_stream(self):
        factory = self._stream_factory or sd.InputStream
        return factory(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )

    @staticmethod
    def _read_chunk(stream) -> np.ndarray:
        audio_chunk, _ = stream.read(CHUNK_SIZE)
        audio_np = np.asarray(audio_chunk).flatten()
        if audio_np.size != CHUNK_SIZE:
            raise ValueError(
                f"wake audio frame has {audio_np.size} samples; expected {CHUNK_SIZE}"
            )
        if audio_np.dtype != np.int16:
            audio_np = audio_np.astype(np.int16)
        return audio_np

    @staticmethod
    def _speech_metrics(audio_np: np.ndarray, vad) -> tuple[float, float]:
        rms = float(np.sqrt(np.mean(audio_np.astype(np.float64) ** 2)))
        vad_score = float(vad.predict(audio_np, frame_size=640))
        if not np.isfinite(vad_score):
            raise ValueError("wake VAD returned a non-finite score")
        return rms, vad_score

    def _wait_for_quiet(self, generation: int, vad) -> bool:
        self._reset_components(self._preprocessor, vad)
        with self._condition:
            self._reset_candidate_locked()
        with self._new_stream() as stream:
            while self._session_current(generation, quiet=True):
                audio_np = self._read_chunk(stream)
                rms, vad_score = self._speech_metrics(audio_np, vad)
                with self._condition:
                    if not self._session_current(generation, quiet=True):
                        return False
                    self._last_rms = rms
                    self._last_vad_score = vad_score
                    quiet_frame = (
                        vad_score < VAD_THRESHOLD and rms < ENERGY_GATE_RMS
                    )
                    self._quiet_chunks = (
                        self._quiet_chunks + 1 if quiet_frame else 0
                    )
                    if self._quiet_chunks >= QUIET_REARM_CHUNKS:
                        return True
        return False

    def _listen_armed_session(
        self, generation: int, preprocessor, verifier, vad, n_frames: int,
    ):
        self._reset_components(preprocessor, vad)
        with self._condition:
            self._reset_candidate_locked()
        speech_window = deque(maxlen=VAD_ALIGNMENT_OLDER_FRAMES)
        with self._new_stream() as stream:
            while self._session_current(generation, quiet=False):
                audio_np = self._read_chunk(stream)
                preprocessor(audio_np)
                rms, vad_score = self._speech_metrics(audio_np, vad)
                speech_window.append((rms, vad_score))
                aligned = list(speech_window)[
                    -VAD_ALIGNMENT_OLDER_FRAMES:-VAD_ALIGNMENT_NEWER_FRAMES
                ]
                speech_present = any(
                    aligned_vad >= VAD_THRESHOLD
                    and aligned_rms >= ENERGY_GATE_RMS
                    for aligned_rms, aligned_vad in aligned
                )
                aligned_rms = max((item[0] for item in aligned), default=0.0)
                aligned_vad = max((item[1] for item in aligned), default=0.0)

                with self._condition:
                    if not self._session_current(generation, quiet=False):
                        return None
                    self._fresh_chunks += 1
                    fresh_chunks = self._fresh_chunks
                    self._last_rms = rms
                    self._last_vad_score = vad_score

                # AudioFeatures starts with random embeddings. Do not ask the
                # verifier for a window until every one of its 16 positions came
                # from the current microphone generation.
                if fresh_chunks < n_frames:
                    continue

                features = np.asarray(preprocessor.get_features(n_frames))
                expected_shape = (1, n_frames, 96)
                if features.shape != expected_shape:
                    raise ValueError(
                        f"wake feature shape {features.shape}; expected {expected_shape}"
                    )
                features_flat = features.reshape(1, n_frames * 96)
                probabilities = np.asarray(verifier.predict_proba(features_flat))
                if probabilities.ndim != 2 or probabilities.shape[1] < 2:
                    raise ValueError("wake verifier returned an invalid probability shape")
                score = float(probabilities[0, 1])
                if not np.isfinite(score):
                    raise ValueError("wake verifier returned a non-finite score")
                now = self._clock()

                with self._condition:
                    if not self._session_current(generation, quiet=False):
                        return None
                    self._last_score = score
                    if score < VERIFIER_THRESHOLD or not speech_present:
                        self._positive_streak = 0
                        streak = 0
                    else:
                        self._positive_streak += 1
                        streak = self._positive_streak

                if score >= VERIFIER_THRESHOLD and not speech_present:
                    self._log_diagnostic(
                        now,
                        "[wake] rejected high verifier score: "
                        f"score={score:.3f} vad={aligned_vad:.3f} "
                        f"rms={aligned_rms:.0f} reason=speech_gate",
                    )
                    continue
                if score < VERIFIER_THRESHOLD:
                    if speech_present and score >= (
                        VERIFIER_THRESHOLD - DIAGNOSTIC_SCORE_MARGIN
                    ):
                        self._log_diagnostic(
                            now,
                            "[wake] rejected near verifier threshold: "
                            f"score={score:.3f} vad={aligned_vad:.3f} "
                            f"rms={aligned_rms:.0f}",
                        )
                    continue

                self._log_diagnostic(
                    now,
                    "[wake] verifier candidate: "
                    f"score={score:.3f} streak={streak}/{PATIENCE_FRAMES} "
                    f"vad={aligned_vad:.3f} rms={aligned_rms:.0f}",
                )
                if streak < PATIENCE_FRAMES:
                    continue

                with self._condition:
                    if not self._session_current(generation, quiet=False):
                        return None
                    if now - self._last_detection < COOLDOWN_SECONDS:
                        self._positive_streak = 0
                        continue
                    if self._acceptance_gate is not None and not self._acceptance_gate(
                        generation
                    ):
                        self._reset_candidate_locked()
                        return "rejected"
                    self._last_detection = now
                    self._last_event_generation = generation
                    self._armed = False
                    self._positive_streak = 0
                    print(f"âœ… Wake word detected! score={score:.3f}")
                    print(
                        "[wake] accepted verifier score: "
                        f"threshold={VERIFIER_THRESHOLD:.2f} "
                        f"vad={aligned_vad:.3f} rms={aligned_rms:.0f}"
                    )
                    return "accepted"
        return None

    def _deliver(self, generation: int) -> None:
        with self._condition:
            if not self._running or generation != self._generation:
                if self._acceptance_cancel is not None:
                    self._acceptance_cancel(generation)
                return
            try:
                if self.callback is not None:
                    self.callback(generation)
                elif self._acceptance_cancel is not None:
                    self._acceptance_cancel(generation)
            except Exception as exc:
                if self._acceptance_cancel is not None:
                    self._acceptance_cancel(generation)
                print(f"Wake word callback error ({type(exc).__name__})")

    def _listen_loop(self):
        """Run isolated microphone generations until stopped."""
        try:
            if not ACOUSTIC_WAKE_AVAILABLE:
                return
            if not _AUDIO_OK:
                print("âš ï¸  [wake] no audio backend; detection disabled.")
                return
            if not _WAKE_OK:
                print("âš ï¸  [wake] openwakeword unavailable; detection disabled.")
                return
            preprocessor = self._feature_extractor_factory()
            verifier = self._verifier_factory()
            vad = self._vad_factory()
            n_frames = _get_n_frames()
            self._preprocessor = preprocessor
            self._vad = vad
            if preprocessor is None or verifier is None or vad is None:
                print("âš ï¸  Wake verifier/VAD unavailable. Detection disabled.")
                return
            if n_frames != 16:
                print(
                    f"âš ï¸  [wake] verifier expects {n_frames} frames; "
                    "the supported OSIA feature shape is 16 Ã— 96."
                )
                return

            print("ðŸ‘‚ Wake word detection active. Say 'osia' to wake me up...")
            print(f"   Verifier: {n_frames} frames Ã— 96 = {n_frames * 96} features")
            if not self._device_logged:
                self._device_logged = True
                try:
                    info = sd.query_devices(kind="input")
                    name = info.get("name", "?") if isinstance(info, dict) else "?"
                    print(f"ðŸŽ™  [wake] using audio input device: {name}")
                except Exception as exc:
                    print(f"âš ï¸  [wake] input-device query failed ({exc}).")

            while True:
                with self._condition:
                    while self._running and not (
                        self._armed or self._rearm_requested
                    ):
                        self._condition.wait(timeout=0.25)
                    if not self._running:
                        return
                    generation = self._generation
                    quiet = self._rearm_requested
                try:
                    if quiet:
                        if not self._wait_for_quiet(generation, vad):
                            continue
                        self._reset_components(preprocessor, vad)
                        with self._condition:
                            if not self._session_current(generation, quiet=True):
                                continue
                            self._rearm_requested = False
                            self._armed = True
                            self._reset_candidate_locked()
                        continue

                    result = self._listen_armed_session(
                        generation, preprocessor, verifier, vad, n_frames,
                    )
                    if result == "accepted":
                        # The stream closes before Qt can start STT, making the
                        # command recorder the sole microphone owner.
                        self._deliver(generation)
                    elif result == "rejected":
                        self._reset_components(preprocessor, vad)
                except Exception as exc:
                    with self._condition:
                        self._reset_candidate_locked()
                    try:
                        self._reset_components(preprocessor, vad)
                    except Exception:
                        pass
                    print(f"Wake word error ({type(exc).__name__})")
                    time.sleep(0.1)
        finally:
            try:
                self._reset_components(self._preprocessor, self._vad)
            except Exception:
                pass
            with self._condition:
                self._reset_candidate_locked(reset_detection_time=True)
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._running = False
                    self._armed = False
                    self._rearm_requested = False
                self._condition.notify_all()

    def start(self):
        """Start one listener generation; never overlap listener threads."""
        if not ACOUSTIC_WAKE_AVAILABLE:
            print(
                "[wake] acoustic activation unavailable: "
                f"{ACOUSTIC_WAKE_UNAVAILABLE_REASON}"
            )
            return False
        with self._condition:
            if self._running:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._generation += 1
            self._running = True
            self._armed = True
            self._rearm_requested = False
            self._last_event_generation = None
            self._reset_candidate_locked(reset_detection_time=True)
            thread = threading.Thread(
                target=self._listen_loop,
                daemon=True,
                name="osia-wake-listener",
            )
            self._thread = thread
            thread.start()
        return True

    def request_rearm(self, accepted_generation: int | None = None) -> bool:
        """Request VAD-quiet rearm after the accepted turn fully finishes."""
        with self._condition:
            if not self._running or self._armed or self._rearm_requested:
                return False
            if (
                accepted_generation is not None
                and accepted_generation != self._generation
            ):
                return False
            self._generation += 1
            self._rearm_requested = True
            self._last_event_generation = None
            self._reset_candidate_locked()
            self._condition.notify_all()
            return True

    def stop(self):
        """Invalidate the active generation and reap its listener thread."""
        with self._condition:
            thread = self._thread
            was_running = self._running or (
                thread is not None and thread.is_alive()
            )
            self._running = False
            self._generation += 1
            self._armed = False
            self._rearm_requested = False
            self._last_event_generation = None
            self._reset_candidate_locked(reset_detection_time=True)
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._condition:
            if thread is not None and not thread.is_alive() and self._thread is thread:
                self._thread = None
        if thread is None or not thread.is_alive():
            try:
                self._reset_components(self._preprocessor, self._vad)
            except Exception:
                pass
        if was_running:
            print("Wake word detection stopped.")


def default_callback():
    """Default callback when wake word is detected.
    PHASE 3: Uses listen_for_command() with retry loop — only proceeds
    to agent after a quality-accepted command is received.
    """
    from osia.voice.stt import listen_for_command
    from osia.agent import run_agent, clean_response
    from osia import i18n
    from osia.turn_context import localized_text, resolve_turn_context
    from osia.voice.tts import speak

    print("\n🎤 Listening for command...")
    user_text = listen_for_command(max_retries=3)

    if not user_text:
        print("❌ No valid command heard.")
        fallback_context = resolve_turn_context(
            "", saved_language=i18n.get_language(),
        )
        speak(
            localized_text("stt_not_understood", fallback_context.language),
            language=fallback_context.language,
        )
        return

    print(f"[STT] accepted voice input ({len(user_text)} chars).")

    # Only now — after a valid command — think and call agent
    print("🤔 Thinking...")
    turn_context = resolve_turn_context(user_text)
    response = run_agent(user_text, turn_context=turn_context)

    # PHASE 4: Clean response before speaking
    response = clean_response(response)

    print(f"[voice] assistant response accepted ({len(response)} chars).")

    # Speak response
    speak(response, language=turn_context.language)


if __name__ == "__main__":
    if not verifier_available():
        print(
            "Acoustic wake is unavailable in this release: "
            f"{ACOUSTIC_WAKE_UNAVAILABLE_REASON}. Use Ctrl+Alt+P."
        )
    else:
        detector = WakeWordDetector(
            callback=lambda _generation: default_callback(),
        )
        detector.start()
        print("OSIA Wake Word Detector running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nShutting down...")
            detector.stop()
