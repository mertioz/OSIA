from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from osia.voice import acoustic, wake, stt


def test_only_complete_ordered_keyword_words_emit_an_observation():
    observation = {"partial_result": [
        {"word": "hé", "conf": 0.95, "end": 0.3},
        {"word": "oh", "conf": 0.9, "end": 0.5},
        {"word": "sia", "conf": 0.92, "end": 0.9},
    ]}
    assert list(acoustic.KeywordScorer._keyword(observation)) == [(0.9, 14400)]
    assert list(acoustic.KeywordScorer._keyword({"partial_result": observation["partial_result"][:2]})) == []
    assert list(acoustic.KeywordScorer._keyword({"partial": "hé oh sia"})) == []
    observation["partial_result"][-1]["word"] = "océan"
    assert list(acoustic.KeywordScorer._keyword(observation)) == []


@pytest.mark.parametrize("position", [0, 1, 2])
def test_keyword_confidence_rejects_non_finite_values(position):
    words = [{"word": word, "conf": 1.0, "end": 1.0} for word in acoustic.TARGET_WORDS]
    words[position]["conf"] = float("nan")
    assert list(acoustic.KeywordScorer._keyword({"result": words})) == []


def test_command_handoff_excludes_the_observed_wake_phrase():
    scorer = acoustic.KeywordScorer.__new__(acoustic.KeywordScorer)
    scorer._samples = acoustic.WINDOW_SAMPLES
    scorer._keyword_end = 16000
    window = np.arange(acoustic.WINDOW_SAMPLES, dtype=np.int16)
    np.testing.assert_array_equal(scorer.command_audio(window), window[16000:].astype(np.float32)/32768)


def test_post_wake_silence_keeps_manual_no_speech_window(monkeypatch):
    from test_wor212_f06_vad_endpointing import _FakeSoundDevice, _install_fake_audio
    fake = _FakeSoundDevice([[0.0]*120])
    _install_fake_audio(monkeypatch, fake)
    called = []
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: called.append(True))
    assert stt.listen_for_command(max_retries=1, initial_audio=np.zeros(1600,np.float32)) == ""
    assert fake.read_counts == [6]
    assert called == []


def test_model_integrity_is_checked_before_native_import(tmp_path):
    model = tmp_path / "weights.bin"
    model.write_bytes(b"example model bytes")
    manifest = {"format": "osia-french-phonetic-v1", "sample_rate": 16000,
                "window_samples": acoustic.WINDOW_SAMPLES, "threshold": 0.8,
                "files": {"weights.bin": {"size": model.stat().st_size,
                           "sha256": hashlib.sha256(model.read_bytes()).hexdigest()}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert acoustic.model_manifest(tmp_path) == manifest
    model.write_bytes(b"changed model bytes")
    assert acoustic.model_manifest(tmp_path, verify_hashes=False) == manifest
    with pytest.raises(ValueError, match="integrity"):
        acoustic.model_manifest(tmp_path)


def test_streaming_recognizer_receives_only_new_samples_and_resets_discontinuity():
    created = []
    class Recognizer:
        def __init__(self, *_args):
            self.chunks = []
            created.append(self)
        def SetWords(self, _value):
            pass
        def SetPartialWords(self, _value):
            pass
        def AcceptWaveform(self, data):
            self.chunks.append(len(data))
            return False
        def PartialResult(self):
            return '{"partial_result": []}'
    scorer = acoustic.KeywordScorer.__new__(acoustic.KeywordScorer)
    scorer._vosk = SimpleNamespace(KaldiRecognizer=Recognizer)
    scorer._model = None
    scorer._grammar = '[]'
    scorer.threshold = 0.8
    scorer.reset()
    first = np.arange(acoustic.CHUNK_SIZE, dtype=np.int16)
    second = np.concatenate((first, first + 1))
    assert scorer.score(first) == 0
    active = scorer._recognizer
    assert scorer.score(second) == 0
    assert scorer._recognizer is active
    assert active.chunks == [acoustic.CHUNK_SIZE*2, acoustic.CHUNK_SIZE*2]
    assert scorer._samples == acoustic.CHUNK_SIZE*2
    scorer.score(np.zeros(acoustic.CHUNK_SIZE, np.int16))
    assert scorer._recognizer is not active
    assert scorer._samples == acoustic.CHUNK_SIZE


class AudioHarness:
    def __init__(self):
        self.events = []
        self.entered = threading.Event()
        self.read_release = threading.Event()
        self.ready_to_read = threading.Event()
        self.scores = 0
        self.active = 0
        self.maximum = 0
        self.reads = 0

    def stream(self, **kwargs):
        assert kwargs["samplerate"] == 16000
        owner = self

        class Stream:
            def __enter__(self):
                owner.active += 1
                owner.maximum = max(owner.maximum, owner.active)
                owner.events.append("open")
                owner.entered.set()
                return self

            def __exit__(self, *_args):
                owner.active -= 1
                owner.events.append("closed")

            def read(self, size):
                owner.reads += 1
                owner.ready_to_read.set()
                owner.read_release.wait(0.005)
                return np.full((size, 1), 800, np.int16), False

        return Stream()

    def scorer(self):
        def score(audio):
            self.scores += 1
            assert audio.shape == (acoustic.WINDOW_SAMPLES,)
            return 1.0
        return SimpleNamespace(threshold=0.98, score=score)


def test_full_fresh_window_single_wake_closes_stream_before_delivery():
    harness = AudioHarness()
    gate = wake.WakeAcceptanceGate(threading.Lock())
    delivered = threading.Event()
    generations = []

    def callback(generation):
        assert harness.active == 0
        assert harness.events[-1] == "closed"
        generations.append(generation)
        delivered.set()

    detector = wake.WakeWordDetector(callback, acceptance_gate=gate.reserve,
        acceptance_cancel=gate.cancel, _scorer_factory=harness.scorer, _stream_factory=harness.stream)
    try:
        assert detector.start()
        assert delivered.wait(2)
        assert harness.reads == 25
        assert harness.scores == 1
        assert len(generations) == 1
        assert harness.maximum == 1
        generation = generations[0]
        audio = detector.take_audio(generation)
        assert audio.shape == (acoustic.WINDOW_SAMPLES,)
        assert detector.take_audio(generation) is None
        assert gate.consume(generation, is_current=True)
        gate.finish()
        assert detector.request_rearm(generation + 123) is False
    finally:
        detector.stop()
        gate.close()


def test_streaming_backend_can_process_the_first_live_chunk_without_startup_delay():
    harness = AudioHarness()
    delivered = threading.Event()
    def scorer():
        def score(audio):
            assert audio.size == acoustic.CHUNK_SIZE
            return 1.0
        return SimpleNamespace(minimum_samples=acoustic.CHUNK_SIZE, threshold=0.8, score=score)
    detector = wake.WakeWordDetector(lambda _: delivered.set(), _scorer_factory=scorer,
                                     _stream_factory=harness.stream)
    try:
        assert detector.start()
        assert delivered.wait(2)
        assert harness.reads == 1
        assert harness.active == 0
    finally:
        detector.stop()


def test_stop_invalidates_pending_wake_and_releases_its_exact_reservation():
    harness = AudioHarness()
    lock = threading.Lock()
    gate = wake.WakeAcceptanceGate(lock)
    delivered = threading.Event()
    detector = wake.WakeWordDetector(lambda _: delivered.set(), acceptance_gate=gate.reserve,
        acceptance_cancel=gate.cancel, _scorer_factory=harness.scorer, _stream_factory=harness.stream)
    assert detector.start()
    assert delivered.wait(2)
    generation = detector.generation
    assert lock.locked()
    detector.stop()
    assert not lock.locked()
    assert detector.take_audio(generation) is None
    assert not detector.is_generation_current(generation)
    assert not gate.consume(generation, is_current=True)


def test_pause_waits_for_microphone_and_does_not_reopen_it():
    harness = AudioHarness()
    detector = wake.WakeWordDetector(_scorer_factory=harness.scorer, _stream_factory=harness.stream,
                                     is_blocked=lambda: True)
    try:
        assert detector.start()
        assert harness.entered.wait(2)
        assert detector.pause()
        assert harness.active == 0
        assert harness.events == ["open", "closed"]
        assert harness.scores == 0
        assert detector.take_audio(detector.generation) is None
    finally:
        detector.stop()


def test_stale_paused_session_never_opens_a_microphone():
    harness = AudioHarness()
    detector = wake.WakeWordDetector(_scorer_factory=harness.scorer, _stream_factory=harness.stream)
    assert detector._listen_session(-1, harness.scorer()) is False
    assert harness.events == []


def test_playback_audio_is_discarded_and_complete_fresh_window_is_required():
    harness = AudioHarness()
    delivered = threading.Event()
    detector = wake.WakeWordDetector(lambda _: delivered.set(),
        _scorer_factory=harness.scorer, _stream_factory=harness.stream,
        is_blocked=lambda: harness.reads <= 20)
    try:
        assert detector.start()
        assert delivered.wait(2)
        assert harness.reads == 45
        assert harness.scores == 1
    finally:
        detector.stop()


def test_repeated_start_stop_never_overlaps_microphone_workers():
    harness = AudioHarness()
    detector = wake.WakeWordDetector(_scorer_factory=harness.scorer,
        _stream_factory=harness.stream, is_blocked=lambda: True)
    try:
        for _ in range(20):
            harness.entered.clear()
            assert detector.start()
            assert detector.start()
            assert harness.entered.wait(2)
            assert detector.stop()
            assert harness.active == 0
            assert not detector.running
        assert harness.maximum == 1
        assert harness.scores == 0
    finally:
        detector.stop()


def test_callback_delivery_does_not_hold_the_microphone_lifecycle_lock():
    harness = AudioHarness()
    entered, release, paused = threading.Event(), threading.Event(), threading.Event()
    def callback(_generation):
        entered.set()
        release.wait(3)
    detector = wake.WakeWordDetector(callback, _scorer_factory=harness.scorer,
                                     _stream_factory=harness.stream)
    def pause():
        detector.pause()
        paused.set()
    worker = threading.Thread(target=pause)
    try:
        assert detector.start()
        assert entered.wait(2)
        worker.start()
        assert paused.wait(1)
        assert harness.active == 0
    finally:
        release.set()
        worker.join(2)
        detector.stop()


def test_native_failure_does_not_start_microphone_or_load_whisper():
    def fail():
        raise RuntimeError("native session unavailable")
    harness = AudioHarness()
    detector = wake.WakeWordDetector(_scorer_factory=fail, _stream_factory=harness.stream)
    assert detector.start()
    detector._thread.join(2)
    assert detector.last_error == "RuntimeError"
    assert harness.events == []
    assert not detector.is_generation_current(detector.generation)


def test_vad_handoff_keeps_audio_without_ambient_recapture(monkeypatch):
    from test_wor212_f06_vad_endpointing import _FakeSoundDevice, _install_fake_audio
    fake = _FakeSoundDevice([[0.2, 0.0, 0.0, 0.0]])
    _install_fake_audio(monkeypatch, fake, threshold=None)
    prefix = np.full(1600, 0.1, np.float32)
    audio = stt.record_audio_vad(initial_audio=prefix)
    np.testing.assert_array_equal(audio[:1600], prefix)
    assert fake.calibration_calls == 0
    assert fake.max_active_streams == 1


def test_handoff_strips_only_transcribed_wake_prefix_and_retries_empty(monkeypatch):
    captures = []
    def record(**kwargs):
        captures.append(kwargs)
        return np.full(16000, 0.2, np.float32)
    texts = iter(["Osia !", "Ossia, ouvre le navigateur"])
    monkeypatch.setattr(stt, "record_audio_vad", record)
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: next(texts))
    assert stt.listen_for_command(initial_audio=np.ones(1600, np.float32)) == "ouvre le navigateur"
    assert "initial_audio" in captures[0]
    assert "initial_audio" not in captures[1]


def test_manual_recording_does_not_strip_any_word(monkeypatch):
    monkeypatch.setattr(stt, "record_audio_vad", lambda **_k: np.full(16000, 0.2, np.float32))
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: "Osia ouvre le navigateur")
    assert stt.listen_for_command() == "Osia ouvre le navigateur"
