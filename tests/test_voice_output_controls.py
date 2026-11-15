"""Desktop mute preserves Chat work; wake feedback owns only its output stream."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from osia.voice import feedback, tts


@pytest.fixture
def speech(monkeypatch):
    settings = {"desktop_speech_enabled": True}
    device = SimpleNamespace(play=Mock(), stop=Mock())
    monkeypatch.setattr(tts, "get_setting", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(tts, "sd", device)
    monkeypatch.setattr(tts, "_AUDIO_OK", True)
    tts.start_tts_session()
    device.stop.reset_mock()
    yield settings, device
    tts.shutdown(join_timeout=0.5)


@pytest.mark.parametrize("entry", [
    "speak", "speak_streaming", "speak_greeting_cached", "_speak_blocking",
    "speak_kokoro", "speak_openai", "speak_google",
])
def test_muted_speech_never_synthesizes_or_queues(entry, speech, monkeypatch):
    settings, device = speech
    settings["desktop_speech_enabled"] = False
    local = Mock(side_effect=AssertionError("muted output must not load a model"))
    cloud = Mock(side_effect=AssertionError("muted output must not call a provider"))
    monkeypatch.setattr(tts, "_get_kokoro_pipeline", local)
    monkeypatch.setattr(tts, "with_key_rotation", cloud)
    monkeypatch.setattr(tts, "_synthesize_to_buffer", cloud)
    session = tts._tts_epoch
    getattr(tts, entry)("A response that remains available as text")
    assert tts._tts_epoch == session  # No cancellation of a turn/session.
    assert not tts.playback_active()
    device.play.assert_not_called()
    local.assert_not_called()
    cloud.assert_not_called()


@pytest.mark.parametrize("stop_and_reenable", [False, True])
def test_late_audio_is_silent_after_mute_even_after_reenable(speech, monkeypatch, stop_and_reenable):
    settings, device = speech
    synthesizing, release = threading.Event(), threading.Event()

    def delayed(*_args):
        synthesizing.set()
        assert release.wait(2)
        tts._play_samples_if_current(np.ones(4), 24000)

    monkeypatch.setattr(tts, "_speak_blocking", delayed)
    session = tts.start_tts_session()
    worker = threading.Thread(target=tts.speak, args=("Pending speech",),
                              kwargs={"session_id": session})
    worker.start()
    try:
        assert synthesizing.wait(2)
        settings["desktop_speech_enabled"] = False
        if stop_and_reenable:
            tts.stop()
            settings["desktop_speech_enabled"] = True
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        device.play.assert_not_called()
        assert not tts.playback_active()
    finally:
        release.set()
        worker.join(2)


def test_unmuting_allows_new_owned_speech(speech):
    settings, device = speech
    settings["desktop_speech_enabled"] = False
    tts.stop()
    settings["desktop_speech_enabled"] = True
    session = tts.start_tts_session()
    assert tts._play_samples_if_current(np.ones(4), 24000, session_id=session)
    device.play.assert_called_once()


def test_wake_sound_is_short_soft_and_has_smooth_edges():
    sound = feedback.wake_chime_samples()
    assert sound.dtype == np.float32
    assert sound.ndim == 1 and len(sound) == 2760  # 115 ms at 24 kHz.
    assert np.isfinite(sound).all() and 0 < np.max(np.abs(sound)) <= 0.12
    assert np.allclose(sound[[0, 1199, 1560, -1]], 0, atol=1e-7)


def test_wake_feedback_is_async_and_independent_of_muted_speech(speech, monkeypatch):
    settings, speech_device = speech
    settings["desktop_speech_enabled"] = False
    opened, release, closed = threading.Event(), threading.Event(), threading.Event()
    writes = []

    class Output:
        def __enter__(self):
            opened.set()
            assert release.wait(2)
            return self

        def write(self, samples):
            writes.append(samples.copy())

        def __exit__(self, *_args):
            closed.set()

    monkeypatch.setattr(feedback, "sd", SimpleNamespace(OutputStream=lambda **_kw: Output()))
    assert feedback.play_wake_chime()
    try:
        assert opened.wait(2)  # Caller returned while output is still blocked.
        assert not feedback.play_wake_chime()  # No overlapping cue stream.
        speech_device.stop.assert_not_called()
    finally:
        release.set()
    assert closed.wait(2)
    assert feedback._playing.acquire(timeout=2)
    feedback._playing.release()
    np.testing.assert_array_equal(np.concatenate(writes), feedback.wake_chime_samples())


def test_missing_audio_or_obsolete_wake_never_opens_output(monkeypatch):
    monkeypatch.setattr(feedback, "sd", None)
    assert not feedback.play_wake_chime()
    output = Mock()
    monkeypatch.setattr(feedback, "sd", SimpleNamespace(OutputStream=output))
    assert not feedback.play_wake_chime(cancelled=lambda: True)
    output.assert_not_called()


def test_failed_output_is_nonfatal_and_releases_ownership(monkeypatch):
    attempted = threading.Event()

    def unavailable(**_kwargs):
        attempted.set()
        raise RuntimeError("no output device")

    monkeypatch.setattr(feedback, "sd", SimpleNamespace(OutputStream=unavailable))
    assert feedback.play_wake_chime()
    assert attempted.wait(2)
    assert feedback._playing.acquire(timeout=2)
    feedback._playing.release()


def test_cue_stops_on_its_own_stream_when_voice_session_becomes_stale(monkeypatch):
    stale = threading.Event()
    closed = threading.Event()
    writes = []

    class Output:
        def __enter__(self):
            return self

        def write(self, samples):
            writes.append(samples)
            stale.set()

        def __exit__(self, *_args):
            closed.set()

    monkeypatch.setattr(feedback, "sd", SimpleNamespace(OutputStream=lambda **_kw: Output()))
    assert feedback.play_wake_chime(cancelled=stale.is_set)
    assert closed.wait(2)
    assert feedback._playing.acquire(timeout=2)
    feedback._playing.release()
    assert len(writes) == 1


def test_ready_cue_keeps_command_recording_and_handoff_audio_intact(monkeypatch):
    from osia.voice import stt
    from test_wor212_f06_vad_endpointing import _FakeSoundDevice, _install_fake_audio

    microphone = _FakeSoundDevice([[0.2, 0.0, 0.0, 0.0]])
    _install_fake_audio(monkeypatch, microphone, threshold=None)
    opened, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Output:
        def __enter__(self):
            opened.set()
            assert release.wait(2)
            return self

        def write(self, _samples):
            pass

        def __exit__(self, *_args):
            closed.set()

    def ready():
        assert microphone.active_streams == 1
        assert feedback.play_wake_chime()
        assert opened.wait(2)

    monkeypatch.setattr(feedback, "sd", SimpleNamespace(OutputStream=lambda **_kw: Output()))
    prefix = np.full(1600, 0.1, np.float32)
    try:
        # Recording completes while the sound thread is still blocked.
        audio = stt.record_audio_vad(initial_audio=prefix, on_ready=ready)
        np.testing.assert_array_equal(audio[:1600], prefix)
        assert microphone.calibration_calls == 0
        assert microphone.active_streams == 0 and "stop" not in microphone.events
    finally:
        release.set()
    assert closed.wait(2)
    assert feedback._playing.acquire(timeout=2)
    feedback._playing.release()


def test_desktop_mute_keeps_mobile_audio_synthesis_available(speech, monkeypatch):
    from osia import config
    from osia.server import server

    settings, device = speech
    settings["desktop_speech_enabled"] = False
    monkeypatch.setattr(config, "get_setting", lambda key, default=None: settings.get(key, default))
    synthesis = Mock(return_value=b"mobile-audio")
    monkeypatch.setattr(server, "_kokoro_to_mp3_bytes", synthesis)
    assert server.synthesize_to_mp3_bytes("A reply on the phone", language="en") == b"mobile-audio"
    synthesis.assert_called_once()
    device.play.assert_not_called()
