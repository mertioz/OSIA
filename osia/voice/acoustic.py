"""Offline French phonetic wake phrase. No native speech engine at import."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from osia.runtime import resource_root

SAMPLE_RATE = 16_000
CHUNK_SIZE = 1_280
WINDOW_SAMPLES = 32_000
MODEL_DIR = resource_root() / "assets" / "wake"
TARGET_WORDS = ("hé", "oh", "sia")


def model_manifest(model_dir: Path = MODEL_DIR, *, verify_hashes=True) -> dict:
    """Check cheap availability on the GUI; verify all bytes on the worker."""
    model_dir = Path(model_dir).resolve()
    manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != "osia-french-phonetic-v1":
        raise ValueError("Unsupported wake model format")
    if manifest.get("sample_rate") != SAMPLE_RATE or manifest.get("window_samples") != WINDOW_SAMPLES:
        raise ValueError("Unsupported wake audio format")
    if not 0.5 <= float(manifest["threshold"]) < 1:
        raise ValueError("Invalid keyword confidence")
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("Missing keyword model files")
    for relative, metadata in files.items():
        path = (model_dir / relative).resolve()
        if not path.is_relative_to(model_dir) or path.stat().st_size != metadata["size"]:
            raise ValueError("Keyword model file mismatch")
        if verify_hashes:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != metadata["sha256"]:
                raise ValueError("Keyword model integrity mismatch")
    return manifest


class KeywordScorer:
    """One bounded streaming recognizer; outputs only a wake confidence.

    The three fixed lexicon entries encode the sounds of “Hé O-ssia”. They are
    never supplied to Chat or command STT. Neighbouring words and [unk] compete
    with this pronunciation; arbitrary speech is not forced into the keyword.
    """

    minimum_samples = CHUNK_SIZE

    def __init__(self, model_dir: Path = MODEL_DIR):
        manifest = model_manifest(model_dir)
        import vosk  # native import and model loading stay on the wake worker

        vosk.SetLogLevel(-1)
        self._vosk = vosk
        self._model = vosk.Model(str(Path(model_dir) / "vosk-fr"))
        self._grammar = json.dumps(manifest["grammar"], ensure_ascii=False)
        self.threshold = float(manifest["threshold"])
        self.reset()

    def reset(self):
        self._recognizer = self._vosk.KaldiRecognizer(self._model, SAMPLE_RATE, self._grammar)
        self._recognizer.SetWords(True)
        self._recognizer.SetPartialWords(True)
        self._previous = None
        self._samples = 0
        self._keyword_end = None

    @staticmethod
    def _keyword(observation):
        words = observation.get("result", observation.get("partial_result", []))
        for start in range(len(words) - len(TARGET_WORDS) + 1):
            selected = words[start:start + len(TARGET_WORDS)]
            if tuple(word.get("word") for word in selected) != TARGET_WORDS:
                continue
            probabilities = [float(word.get("conf", 0)) for word in selected]
            if not all(np.isfinite(value) and 0 <= value <= 1 for value in probabilities):
                continue
            confidence = min(probabilities)
            end = float(selected[-1].get("end", -1))
            if np.isfinite(confidence) and np.isfinite(end) and end >= 0:
                yield confidence, round(end * SAMPLE_RATE)

    def score(self, audio: np.ndarray) -> float:
        window = np.asarray(audio).reshape(-1)
        if not np.isfinite(window).all():
            raise ValueError("Non-finite keyword audio")
        if window.dtype != np.int16:
            window = (np.clip(window, -1, 1) * 32767).astype(np.int16)
        previous = self._previous
        if previous is not None and (
            (window.size == previous.size + CHUNK_SIZE and np.array_equal(window[:-CHUNK_SIZE], previous))
            or (window.size == previous.size == WINDOW_SAMPLES and np.array_equal(window[:-CHUNK_SIZE], previous[CHUNK_SIZE:]))
        ):
            fresh = window[-CHUNK_SIZE:]
        else:
            self.reset()
            fresh = window
        # Bound a continuous ASR utterance without losing a keyword that crosses
        # the boundary: replay the rolling context, not a second microphone.
        if self._samples >= SAMPLE_RATE * 30:
            self.reset()
            fresh = window
        self._previous = window.copy()
        self._samples += fresh.size
        complete = self._recognizer.AcceptWaveform(fresh.tobytes())
        observation = json.loads(self._recognizer.Result() if complete else self._recognizer.PartialResult())
        confidence = 0.0
        for score, end in self._keyword(observation):
            if self._samples - window.size <= end <= self._samples + CHUNK_SIZE:
                confidence = max(confidence, score)
                if score >= self.threshold:
                    self._keyword_end = min(end, self._samples)
        return confidence

    def command_audio(self, window: np.ndarray) -> np.ndarray:
        """Hand over only audio AFTER the observed wake phrase, once in RAM."""
        if self._keyword_end is None:
            return np.empty(0, np.float32)
        first = max(0, self._keyword_end - (self._samples - window.size))
        return np.asarray(window[first:], dtype=np.float32) / 32768
