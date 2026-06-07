#!/usr/bin/env python3
"""
OSIA Wake Word Detection using openWakeWord + custom trained verifier.

Architecture:
  1. openWakeWord's AudioFeatures preprocessor extracts mel spectrogram features
     from streaming audio (80ms chunks at 16kHz).
  2. A custom logistic regression verifier (trained on "osia" samples)
     classifies each feature window as "osia" or "not osia".
  3. When the verifier score exceeds the threshold, the callback is triggered.

This completely replaces the whisper-based wake word system, eliminating
hallucinations of Chinese/Korean/Spanish.

Fallback: If the verifier model is not found at startup, prints a warning
and the caller (main.py) should use keyboard trigger (Ctrl+Space).
"""

import threading
import time
import os
import pickle
import numpy as np

# sounddevice loads PortAudio at import time and can fail when no audio backend
# / drivers are present. Degrade gracefully: with ``sd = None`` the listen loop
# bails early and OSIA stays reachable via the capsule / Ctrl+Alt+O (WOR-11).
try:
    import sounddevice as sd
    _AUDIO_OK = True
except Exception as _audio_exc:  # PortAudio missing / no audio device
    sd = None
    _AUDIO_OK = False
    print(
        f"⚠️  [wake] audio backend unavailable ({_audio_exc}); wake-word "
        "detection is disabled (use the capsule / Ctrl+Alt+O)."
    )

# openWakeWord provides the AudioFeatures mel-spectrogram preprocessor. It is an
# optional dependency: degrade gracefully when it (or its onnxruntime/tflite
# backend) is missing so a fresh install / Windows box without the package still
# starts and stays reachable via the capsule / Ctrl+Alt+O (mirrors _AUDIO_OK).
try:
    from openwakeword.utils import AudioFeatures
    _WAKE_OK = True
except ImportError as _wake_exc:  # openwakeword not installed
    AudioFeatures = None
    _WAKE_OK = False
    print(
        f"⚠️  [wake] openwakeword unavailable ({_wake_exc}); wake-word "
        "detection is disabled (use the capsule / Ctrl+Alt+O)."
    )

# === Paths ===
_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "wake_training",
)
VERIFIER_PATH = os.path.join(_MODEL_DIR, "osia_verifier.pkl")

# === Audio settings ===
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz — required by openWakeWord

# === Detection settings ===
VERIFIER_THRESHOLD = 0.80  # Score above which we consider "osia" detected
COOLDOWN_SECONDS = 1.5    # Minimum time between detections
PATIENCE_FRAMES = 8       # Number of consecutive positive frames required

# === Lazy-loaded globals ===
_verifier_model = None
_verifier_n_frames = None
_feature_extractor = None


def _get_verifier():
    """Load the custom verifier model (lazy). Returns None if not found."""
    global _verifier_model, _verifier_n_frames
    if _verifier_model is None:
        if os.path.exists(VERIFIER_PATH):
            with open(VERIFIER_PATH, "rb") as f:
                _verifier_model = pickle.load(f)
            # Determine expected feature frame count from the model
            n_features = _verifier_model.named_steps["standardscaler"].n_features_in_
            _verifier_n_frames = n_features // 96
        else:
            return None
    return _verifier_model


def _get_n_frames():
    """Get the number of feature frames the verifier expects."""
    global _verifier_n_frames
    if _verifier_n_frames is None:
        _get_verifier()
    return _verifier_n_frames or 16


def _get_feature_extractor():
    """Lazy-load the openWakeWord AudioFeatures preprocessor."""
    global _feature_extractor
    if _feature_extractor is None:
        _feature_extractor = AudioFeatures(sr=SAMPLE_RATE, ncpu=1)
    return _feature_extractor


def verifier_available() -> bool:
    """Check if the custom verifier model exists."""
    return os.path.exists(VERIFIER_PATH)


class WakeWordDetector:
    """Continuously listens for 'osia' wake word using openWakeWord + custom verifier."""

    def __init__(self, callback=None):
        self.callback = callback
        self._running = False
        self._thread = None
        self._last_detection = 0.0
        self._positive_streak = 0

    def _listen_loop(self):
        """Main loop: stream audio, extract features, run verifier."""
        if not _AUDIO_OK:
            print("⚠️  [wake] no audio backend — wake-word detection disabled; "
                  "use the capsule or Ctrl+Alt+O to reach OSIA.")
            return
        if not _WAKE_OK:
            print("⚠️  [wake] openwakeword not installed — wake-word detection "
                  "disabled; use the capsule or Ctrl+Alt+O to reach OSIA.")
            return
        preprocessor = _get_feature_extractor()
        verifier = _get_verifier()
        n_frames = _get_n_frames()

        if verifier is None:
            print("⚠️  No verifier model found. Wake word detection disabled.")
            print("   Run osia/wake_training/train.py after recording samples.")
            return

        print(f"👂 Wake word detection active. Say 'osia' to wake me up...")
        print(f"   Verifier: {n_frames} frames × 96 = {n_frames * 96} features")

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        ) as stream:
            while self._running:
                try:
                    audio_chunk, _ = stream.read(CHUNK_SIZE)
                    audio_np = audio_chunk.flatten()

                    # Feed audio through the feature extractor
                    preprocessor(audio_np)

                    # Get the latest feature window
                    features = preprocessor.get_features(n_frames)

                    if features is None or features.size == 0:
                        continue

                    # Reshape: (1, n_frames, 96) -> (1, n_frames*96)
                    features_flat = features.squeeze(0).flatten().reshape(1, -1)

                    # Run the verifier
                    score = verifier.predict_proba(features_flat)[0, 1]

                    now = time.time()

                    if score >= VERIFIER_THRESHOLD:
                        self._positive_streak += 1
                        if self._positive_streak >= PATIENCE_FRAMES:
                            if now - self._last_detection > COOLDOWN_SECONDS:
                                print(f"✅ Wake word detected! score={score:.3f}")
                                self._last_detection = now
                                self._positive_streak = 0
                                if self.callback:
                                    self.callback()
                            else:
                                self._positive_streak = 0
                    else:
                        self._positive_streak = max(0, self._positive_streak - 1)

                except Exception as e:
                    print(f"Wake word error: {e}")
                    time.sleep(0.1)

    def start(self):
        """Start the wake word detection thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the wake word detection thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("Wake word detection stopped.")


def default_callback():
    """Default callback when wake word is detected.
    PHASE 3: Uses listen_for_command() with retry loop — only proceeds
    to agent after a valid command (3+ words) is received.
    """
    from osia.voice.stt import listen_for_command
    from osia.agent import run_agent, clean_response
    from osia.voice.tts import speak

    print("\n🎤 Listening for command...")
    user_text = listen_for_command(max_retries=3)

    if not user_text:
        print("❌ No valid command heard.")
        speak("Je n'ai pas compris, répétez.")
        return

    print(f"📝 You said: {user_text}")

    # Only now — after a valid command — think and call agent
    print("🤔 Thinking...")
    response = run_agent(user_text)

    # PHASE 4: Clean response before speaking
    response = clean_response(response)

    print(f"💬 OSIA: {response}")

    # Speak response
    speak(response)


if __name__ == "__main__":
    if not verifier_available():
        print("⚠️  No verifier model found. Please train the model first:")
        print("   1. Run osia/wake_training/record_samples.py")
        print("   2. Run osia/wake_training/record_negative.py")
        print("   3. Run osia/wake_training/train.py")
    else:
        detector = WakeWordDetector(callback=default_callback)
        detector.start()
        print("OSIA Wake Word Detector running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nShutting down...")
            detector.stop()
