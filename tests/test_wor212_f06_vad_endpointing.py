from __future__ import annotations

import numpy as np

from osia.voice import stt


class _FakeInputStream:
    def __init__(self, owner, amplitudes):
        self._owner = owner
        self._amplitudes = iter(amplitudes)

    def __enter__(self):
        self._owner.active_streams += 1
        self._owner.max_active_streams = max(
            self._owner.max_active_streams,
            self._owner.active_streams,
        )
        self._owner.events.append("stream_enter")
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._owner.events.append("stream_exit")
        self._owner.active_streams -= 1

    def read(self, chunk_size):
        amplitude = next(self._amplitudes, 0.0)
        self._owner.read_counts[-1] += 1
        self._owner.events.append("read")
        return np.full((chunk_size, 1), amplitude, dtype=np.float32), False


class _FakeSoundDevice:
    def __init__(self, stream_plans=(), *, ambient_amplitude=0.0):
        self.stream_plans = list(stream_plans)
        self.ambient_amplitude = ambient_amplitude
        self.events = []
        self.read_counts = []
        self.calibration_calls = 0
        self.stream_calls = 0
        self.active_streams = 0
        self.max_active_streams = 0

    def rec(self, n_samples, **_kwargs):
        self.calibration_calls += 1
        self.events.append("calibration_rec")
        return np.full(
            (n_samples, 1),
            self.ambient_amplitude,
            dtype=np.float32,
        )

    def wait(self):
        self.events.append("calibration_wait")

    def stop(self):
        self.events.append("stop")

    def InputStream(self, **_kwargs):
        plan = self.stream_plans[self.stream_calls]
        self.stream_calls += 1
        self.read_counts.append(0)
        return _FakeInputStream(self, plan)


def _install_fake_audio(monkeypatch, fake_sd, *, threshold=0.015):
    monkeypatch.setattr(stt, "sd", fake_sd)
    monkeypatch.setattr(stt, "_AUDIO_OK", True)
    monkeypatch.setattr(stt, "_calibrated_silence_rms", threshold)


def _record(**kwargs):
    options = {
        "sample_rate": 10,
        "chunk_seconds": 0.5,
        "silence_seconds": 1.2,
        "max_seconds": 60.0,
        "min_seconds": 3.0,
    }
    options.update(kwargs)
    return stt.record_audio_vad(**options)


def test_zero_ambient_calibration_keeps_effective_threshold_positive(monkeypatch):
    fake_sd = _FakeSoundDevice(ambient_amplitude=0.0)
    _install_fake_audio(monkeypatch, fake_sd, threshold=None)

    threshold = stt.calibrate_ambient_threshold(sample_rate=10, duration=1.0)

    assert threshold == stt.VAD_SILENCE_RMS == 0.015
    assert 0.0 < threshold <= stt.VAD_THRESHOLD_CEILING
    assert stt._calibrated_silence_rms == threshold


def test_complete_silence_exits_at_no_speech_window_without_transcription(
    monkeypatch,
):
    fake_sd = _FakeSoundDevice([[0.0] * 120])
    _install_fake_audio(monkeypatch, fake_sd)
    transcribe_calls = []
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda *_args, **_kwargs: transcribe_calls.append("called") or "accepted",
    )

    result = stt.listen_for_command(max_retries=1)

    assert result == ""
    assert fake_sd.read_counts == [6]
    assert fake_sd.read_counts[0] < 120
    assert transcribe_calls == []


def test_pre_speech_silence_is_retained_but_not_counted_as_trailing(monkeypatch):
    fake_sd = _FakeSoundDevice([[0.0] * 4 + [0.2] + [0.0] * 3])
    _install_fake_audio(monkeypatch, fake_sd)

    audio = _record()

    assert fake_sd.read_counts == [8]
    assert audio.size == 8 * 5
    assert np.all(audio[: 4 * 5] == 0.0)
    assert np.all(audio[4 * 5 : 5 * 5] == np.float32(0.2))


def test_short_clear_speech_does_not_wait_for_three_seconds_of_speech(monkeypatch):
    fake_sd = _FakeSoundDevice([[0.2, 0.0, 0.0, 0.0, 0.9]])
    _install_fake_audio(monkeypatch, fake_sd)

    audio = _record()

    assert fake_sd.read_counts == [4]
    assert audio.size / 10 == 2.0
    assert np.max(audio) == np.float32(0.2)


def test_internal_pause_shorter_than_trailing_silence_keeps_command_open(
    monkeypatch,
):
    fake_sd = _FakeSoundDevice([[0.2, 0.0, 0.2, 0.0, 0.0, 0.0]])
    _install_fake_audio(monkeypatch, fake_sd)

    audio = _record()

    assert fake_sd.read_counts == [6]
    assert np.count_nonzero(np.isclose(audio, 0.2)) == 10


def test_three_half_second_silent_blocks_are_required_for_1_2_seconds(
    monkeypatch,
):
    fake_sd = _FakeSoundDevice([[0.2, 0.0, 0.0, 0.0, 0.9]])
    _install_fake_audio(monkeypatch, fake_sd)

    audio = _record()

    assert fake_sd.read_counts == [4]
    assert audio.size == 4 * 5
    assert np.max(audio) == np.float32(0.2)


def test_speech_resume_resets_accumulated_trailing_silence(monkeypatch):
    fake_sd = _FakeSoundDevice(
        [[0.2, 0.0, 0.0, 0.3, 0.0, 0.0, 0.0, 0.9]],
    )
    _install_fake_audio(monkeypatch, fake_sd)

    audio = _record()

    assert fake_sd.read_counts == [7]
    assert np.any(np.isclose(audio, 0.3))
    assert not np.any(np.isclose(audio, 0.9))


def test_maximum_duration_remains_hard_cap_after_speech_starts(monkeypatch):
    fake_sd = _FakeSoundDevice([[0.2] * 20])
    _install_fake_audio(monkeypatch, fake_sd)

    audio = _record(max_seconds=2.0)

    assert fake_sd.read_counts == [4]
    assert audio.size / 10 == 2.0


def test_retry_reuses_calibration_stream_serially_and_emits_ready_once(monkeypatch):
    fake_sd = _FakeSoundDevice(
        [[0.0] * 120, [0.2, 0.0, 0.0, 0.0]],
        ambient_amplitude=0.0,
    )
    _install_fake_audio(monkeypatch, fake_sd, threshold=None)
    transcribe_calls = []
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda audio, **_kwargs: transcribe_calls.append(audio.copy())
        or "commande valide maintenant",
    )
    ready_calls = []

    result = stt.listen_for_command(
        max_retries=2,
        on_ready=lambda: ready_calls.append("ready"),
    )

    assert result == "commande valide maintenant"
    assert fake_sd.calibration_calls == 1
    assert fake_sd.stream_calls == 2
    assert fake_sd.read_counts == [6, 4]
    assert fake_sd.max_active_streams == 1
    assert fake_sd.active_streams == 0
    assert ready_calls == ["ready"]
    assert len(transcribe_calls) == 1
    first_exit = fake_sd.events.index("stream_exit")
    second_enter = fake_sd.events.index("stream_enter", first_exit + 1)
    assert first_exit < second_enter
