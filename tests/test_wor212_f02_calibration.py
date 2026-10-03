from __future__ import annotations

import inspect
import threading

import numpy as np

from osia.voice import stt, wake


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
        self._owner.events.append("read")
        amplitude = next(self._amplitudes, 0.0)
        data = np.full((chunk_size, 1), amplitude, dtype=np.float32)
        return data, False


class _FakeSoundDevice:
    def __init__(self, stream_plans, *, calibration_error=None):
        self.stream_plans = list(stream_plans)
        self.calibration_error = calibration_error
        self.events = []
        self.calibration_calls = 0
        self.stop_calls = 0
        self.stream_calls = 0
        self.active_streams = 0
        self.max_active_streams = 0

    def rec(self, n_samples, **_kwargs):
        self.calibration_calls += 1
        self.events.append("calibration_rec")
        if self.calibration_error is not None:
            raise self.calibration_error
        return np.full((n_samples, 1), 0.002, dtype=np.float32)

    def wait(self):
        self.events.append("calibration_wait")

    def stop(self):
        self.stop_calls += 1
        self.events.append("calibration_stop")

    def InputStream(self, **_kwargs):
        plan = self.stream_plans[self.stream_calls]
        self.stream_calls += 1
        return _FakeInputStream(self, plan)


def _command_chunks(amplitude=0.2):
    # Six chunks satisfy the unchanged 3 s VAD minimum; two silent chunks end it.
    return [amplitude] * 6 + [0.0] * 2


def _install_fake_audio(monkeypatch, fake_sd):
    monkeypatch.setattr(stt, "sd", fake_sd)
    monkeypatch.setattr(stt, "_AUDIO_OK", True)
    monkeypatch.setattr(stt, "_calibrated_silence_rms", None)


def test_cold_first_use_calibrates_before_ready_and_retains_first_command_sample(
    monkeypatch,
):
    fake_sd = _FakeSoundDevice([_command_chunks()])
    _install_fake_audio(monkeypatch, fake_sd)
    transcribed_audio = []
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda audio, **_kwargs: transcribed_audio.append(audio.copy())
        or "commande valide maintenant",
    )

    result = stt.listen_for_command(
        max_retries=1,
        on_ready=lambda: fake_sd.events.append("ready"),
    )

    assert result == "commande valide maintenant"
    assert fake_sd.events.index("calibration_wait") < fake_sd.events.index("ready")
    assert fake_sd.events.index("stream_enter") < fake_sd.events.index("ready")
    assert fake_sd.events.index("ready") < fake_sd.events.index("read")
    assert fake_sd.calibration_calls == 1
    assert fake_sd.stop_calls == 0
    assert fake_sd.stream_calls == 1
    assert transcribed_audio[0][0] == np.float32(0.2)
    assert not np.any(np.isclose(transcribed_audio[0], 0.002))


def test_warm_use_skips_calibration_and_emits_ready_once(monkeypatch):
    fake_sd = _FakeSoundDevice([_command_chunks()])
    _install_fake_audio(monkeypatch, fake_sd)
    monkeypatch.setattr(stt, "_calibrated_silence_rms", 0.01)
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda _audio, **_kwargs: "commande valide maintenant",
    )
    ready_calls = []

    result = stt.listen_for_command(
        max_retries=1,
        on_ready=lambda: ready_calls.append("ready"),
    )

    assert result == "commande valide maintenant"
    assert fake_sd.calibration_calls == 0
    assert fake_sd.stream_calls == 1
    assert ready_calls == ["ready"]


def test_rejected_transcription_retries_sequentially_without_recalibration(
    monkeypatch,
):
    fake_sd = _FakeSoundDevice([_command_chunks(), _command_chunks(0.3)])
    _install_fake_audio(monkeypatch, fake_sd)
    transcriptions = iter(["deux mots", "commande valide maintenant"])
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda _audio, **_kwargs: next(transcriptions),
    )
    ready_calls = []

    result = stt.listen_for_command(
        max_retries=3,
        on_ready=lambda: ready_calls.append("ready"),
    )

    assert result == "commande valide maintenant"
    assert fake_sd.calibration_calls == 1
    assert fake_sd.stream_calls == 2
    assert fake_sd.max_active_streams == 1
    assert ready_calls == ["ready"]
    first_exit = fake_sd.events.index("stream_exit")
    second_enter = fake_sd.events.index("stream_enter", first_exit + 1)
    assert first_exit < second_enter


def test_calibration_failure_caches_fallback_and_releases_activation_gate(
    monkeypatch,
):
    fake_sd = _FakeSoundDevice(
        [_command_chunks()],
        calibration_error=OSError("mock device calibration failure"),
    )
    _install_fake_audio(monkeypatch, fake_sd)
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda _audio, **_kwargs: "commande valide maintenant",
    )
    ready_calls = []
    gate = wake.WakeAcceptanceGate(threading.Lock())
    assert gate.reserve(0)
    assert gate.consume(0, is_current=True)

    try:
        result = stt.listen_for_command(
            max_retries=1,
            on_ready=lambda: ready_calls.append("ready"),
        )
    finally:
        gate.finish()

    assert result == "commande valide maintenant"
    assert stt._calibrated_silence_rms == stt.VAD_SILENCE_RMS
    assert fake_sd.calibration_calls == 1
    assert fake_sd.stop_calls == 1
    assert fake_sd.stream_calls == 1
    assert fake_sd.max_active_streams == 1
    assert ready_calls == ["ready"]
    assert gate.reserve(0)
    gate.cancel(0)


def test_manual_ui_listening_state_comes_only_from_stt_ready_callback():
    from osia import main

    source = inspect.getsource(main.wake_mode)
    assert source.count('_emit_voice_state("listening")') == 1
    assert "def _mark_listening_ready()" in source
    assert "on_ready=_mark_listening_ready" in source
    assert "max_retries=3" in source
    assert "finally:\n                _finish_wake_turn()" in source
