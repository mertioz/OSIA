"""Wake silence has one bounded wait; an actual spoken request stays intact."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import numpy as np
import pytest

from osia import main
from osia.voice import stt
from test_wor212_f06_vad_endpointing import _FakeSoundDevice, _install_fake_audio


def test_wake_silence_closes_after_four_seconds_without_retry_or_transcription(monkeypatch):
    device = _FakeSoundDevice([[0.0] * 120])
    _install_fake_audio(monkeypatch, device, threshold=None)
    transcription = Mock()
    monkeypatch.setattr(stt, "transcribe", transcription)
    assert stt.listen_for_command(initial_audio=np.zeros(1600, np.float32),
                                 no_speech_timeout=4.0) == ""
    assert device.read_counts == [8]  # Eight retained 0.5-second input chunks.
    assert device.calibration_calls == 0
    assert device.active_streams == 0 and device.stream_calls == 1
    transcription.assert_not_called()


def test_speech_started_before_deadline_can_continue_past_four_seconds(monkeypatch):
    device = _FakeSoundDevice([[0.0] * 7 + [0.2] * 10 + [0.0] * 3])
    _install_fake_audio(monkeypatch, device)
    transcription = Mock(return_value="ouvre le navigateur")
    monkeypatch.setattr(stt, "transcribe", transcription)
    assert stt.listen_for_command(initial_audio=np.zeros(1600, np.float32),
                                 no_speech_timeout=4.0) == "ouvre le navigateur"
    assert device.read_counts == [20]
    transcription.assert_called_once()


def test_command_already_in_wake_handoff_is_not_dropped(monkeypatch):
    device = _FakeSoundDevice([[0.0] * 120])
    _install_fake_audio(monkeypatch, device)
    prefix = np.full(3200, 0.1, np.float32)
    audio = stt.record_audio_vad(initial_audio=prefix, min_seconds=4.0)
    np.testing.assert_array_equal(audio[:len(prefix)], prefix)
    assert audio.size > prefix.size and device.read_counts == [3]


def test_rejected_audio_does_not_restart_the_four_second_wait(monkeypatch):
    elapsed, windows = [0.0], []
    monkeypatch.setattr(stt, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))

    def record(*, on_ready, **options):
        on_ready()
        windows.append(options["min_seconds"])
        elapsed[0] += 2
        return np.ones(1600, np.float32)

    def transcribe(*_args, **_kwargs):
        elapsed[0] += 0.5
        return "bruit" if len(windows) == 1 else "ouvre le navigateur"

    monkeypatch.setattr(stt, "record_audio_vad", record)
    monkeypatch.setattr(stt, "transcribe", transcribe)
    assert stt.listen_for_command(initial_audio=np.zeros(0, np.float32),
                                 no_speech_timeout=4.0) == "ouvre le navigateur"
    assert windows == [4.0, 1.5]


def test_manual_capture_keeps_its_existing_retry_behavior(monkeypatch):
    device = _FakeSoundDevice([[0.0] * 120, [0.2, 0.0, 0.0, 0.0]])
    _install_fake_audio(monkeypatch, device)
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_kw: "ouvre le navigateur")
    assert stt.listen_for_command(max_retries=2) == "ouvre le navigateur"
    assert device.read_counts == [6, 4]


@pytest.mark.parametrize("prefix_level", [0.0, 0.1])
def test_early_wake_sound_or_handoff_cannot_cut_off_a_later_command(monkeypatch, prefix_level):
    # Readiness sound/residual wake at 0s; the real command begins at 2s.
    # The old RMS endpoint returned at 1.5–2s, before those words were read.
    device = _FakeSoundDevice([[0.2, 0, 0, 0, 0.3, 0.3, 0.3, 0, 0, 0]])
    _install_fake_audio(monkeypatch, device)
    def transcribe(audio, **_kwargs):
        assert np.max(audio) >= 0.3
        return "ouvre le navigateur"
    monkeypatch.setattr(stt, "transcribe", transcribe)
    assert stt.listen_for_command(initial_audio=np.full(1600, prefix_level, np.float32),
                                 no_speech_timeout=4.0) == "ouvre le navigateur"
    assert device.stream_calls == 1 and device.read_counts == [10]
    assert device.active_streams == 0


def test_empty_acoustic_wake_finalizes_without_spoken_fallback(monkeypatch):
    # Exercise the real desktop worker up to its empty-wake return, including
    # its finally block, without opening a GUI, microphone or provider.
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8-sig"))
    wake_mode = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "wake_mode")
    handle = next(n for n in wake_mode.body if isinstance(n, ast.FunctionDef) and n.name == "handle_wake")
    process = next(n for n in handle.body if isinstance(n, ast.FunctionDef) and n.name == "process")
    listen, finish, speak = Mock(return_value=""), Mock(), Mock()
    monkeypatch.setattr(stt, "listen_for_command", listen)
    namespace = {"generation": 1, "wake_audio": np.zeros(0, np.float32),
                 "voice_shutdown_requested": threading.Event(), "live_settings": {},
                 "voice_tts_session": 7, "_finish_wake_turn": finish, "speak": speak}
    exec(compile(ast.Module(body=[process], type_ignores=[]), "<desktop voice worker>", "exec"), namespace)
    namespace["process"]()
    assert listen.call_args.kwargs["no_speech_timeout"] == 4.0
    speak.assert_not_called()
    finish.assert_called_once_with()


def test_manual_capture_ignores_legacy_spoken_greeting_settings(monkeypatch):
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8-sig"))
    wake_mode = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "wake_mode")
    handle = next(n for n in wake_mode.body if isinstance(n, ast.FunctionDef) and n.name == "handle_wake")
    process = next(n for n in handle.body if isinstance(n, ast.FunctionDef) and n.name == "process")
    shutdown = threading.Event()
    greeting, speak, finish = Mock(), Mock(), Mock()

    def capture(**_options):
        greeting.assert_not_called()
        speak.assert_not_called()
        shutdown.set()
        return ""

    monkeypatch.setattr(stt, "listen_for_command", capture)
    namespace = {"generation": 0, "wake_audio": None,
                 "voice_shutdown_requested": shutdown,
                 "live_settings": {"wake_greeting_enabled": True, "wake_greeting_text": "Old greeting"},
                 "voice_tts_session": 7, "_finish_wake_turn": finish, "speak": speak,
                 "speak_greeting_cached": greeting, "_emit_voice_state": Mock()}
    exec(compile(ast.Module(body=[process], type_ignores=[]), "<manual voice worker>", "exec"), namespace)
    namespace["process"]()
    greeting.assert_not_called()
    finish.assert_called_once_with()
