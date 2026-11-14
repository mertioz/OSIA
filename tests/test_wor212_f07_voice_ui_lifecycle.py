"""Deterministic coverage for WOR-212 F07 desktop voice/UI finalization."""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from osia.voice.wake import VoiceSessionLifecycle, WakeAcceptanceGate


class FakeTtsCompletion:
    def __init__(self, trace: list[str]):
        self._condition = threading.Condition()
        self._pending = False
        self.wait_started = threading.Event()
        self.trace = trace

    def schedule(self) -> None:
        with self._condition:
            self._pending = True

    def wait(self) -> bool:
        self.wait_started.set()
        with self._condition:
            while self._pending:
                self._condition.wait()
        self.trace.append("playback_observed_complete")
        return True

    def complete_playback(self) -> None:
        with self._condition:
            self.trace.append("playback_complete")
            self._pending = False
            self._condition.notify_all()


def _active_gate():
    handler_lock = threading.Lock()
    gate = WakeAcceptanceGate(handler_lock)
    assert gate.reserve(0)
    assert gate.consume(0, is_current=True)
    return gate, handler_lock


def _start_finalizer(
    lifecycle: VoiceSessionLifecycle,
    session,
    fake_tts: FakeTtsCompletion,
    release_gate,
    emit_final_state,
):
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            lifecycle.finalize(
                session,
                wait_for_playback=fake_tts.wait,
                release_gate=release_gate,
                emit_final_state=emit_final_state,
            )
        )
    )
    thread.start()
    assert fake_tts.wait_started.wait(timeout=1)
    return thread, result


def test_normal_voice_agent_return_waits_for_real_playback_before_one_idle():
    trace: list[str] = []
    states: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    gate, handler_lock = _active_gate()
    session = object()
    assert lifecycle.begin(session)
    assert lifecycle.emit_if_current(session, lambda: states.append("thinking"))
    assert lifecycle.emit_if_current(session, lambda: states.append("speaking"))

    fake_tts = FakeTtsCompletion(trace)
    fake_tts.schedule()
    thread, result = _start_finalizer(
        lifecycle,
        session,
        fake_tts,
        lambda: (trace.append("gate_released"), gate.finish()),
        lambda: (trace.append("idle"), states.append("idle")),
    )

    assert states == ["thinking", "speaking"]
    assert handler_lock.locked()
    fake_tts.complete_playback()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert result == [True]
    assert states == ["thinking", "speaking", "idle"]
    assert trace == [
        "playback_complete",
        "playback_observed_complete",
        "gate_released",
        "idle",
    ]
    assert not handler_lock.locked()


@pytest.mark.parametrize("path", ["no_speech", "rejected_transcription"])
def test_stt_fallback_does_not_idle_before_fallback_speech(path):
    states = ["listening"]
    trace: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    gate, _handler_lock = _active_gate()
    session = object()
    assert lifecycle.begin(session)
    assert lifecycle.emit_if_current(session, lambda: states.append("speaking"))

    fake_tts = FakeTtsCompletion(trace)
    fake_tts.schedule()
    thread, result = _start_finalizer(
        lifecycle,
        session,
        fake_tts,
        gate.finish,
        lambda: states.append("idle"),
    )
    assert path in {"no_speech", "rejected_transcription"}
    assert states == ["listening", "speaking"]

    fake_tts.complete_playback()
    thread.join(timeout=1)
    assert result == [True]
    assert states == ["listening", "speaking", "idle"]


def test_local_new_chat_confirmation_finishes_before_single_finalization():
    states: list[str] = []
    trace: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    gate, _handler_lock = _active_gate()
    session = object()
    assert lifecycle.begin(session)
    lifecycle.emit_if_current(session, lambda: states.append("speaking"))
    fake_tts = FakeTtsCompletion(trace)
    fake_tts.schedule()

    thread, result = _start_finalizer(
        lifecycle,
        session,
        fake_tts,
        gate.finish,
        lambda: states.append("idle"),
    )
    assert states == ["speaking"]
    fake_tts.complete_playback()
    thread.join(timeout=1)

    assert result == [True]
    assert not lifecycle.finalize(
        session,
        wait_for_playback=lambda: True,
        release_gate=gate.finish,
        emit_final_state=lambda: states.append("idle"),
    )
    assert states.count("idle") == 1


@pytest.mark.parametrize("path", ["provider_error", "controlled_refusal"])
def test_agent_direct_speech_paths_follow_the_same_completion_rule(path):
    states = ["thinking"]
    trace: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    gate, _handler_lock = _active_gate()
    session = object()
    assert lifecycle.begin(session)
    lifecycle.emit_if_current(session, lambda: states.append("speaking"))
    fake_tts = FakeTtsCompletion(trace)
    fake_tts.schedule()

    thread, result = _start_finalizer(
        lifecycle,
        session,
        fake_tts,
        gate.finish,
        lambda: states.append("idle"),
    )
    assert path in {"provider_error", "controlled_refusal"}
    assert "idle" not in states
    fake_tts.complete_playback()
    thread.join(timeout=1)
    assert result == [True]
    assert states == ["thinking", "speaking", "idle"]


@pytest.mark.parametrize("path", ["tts_disabled", "no_audio_produced"])
def test_no_audio_finalizes_immediately_without_artificial_wait(path):
    states: list[str] = []
    releases: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    session = object()
    assert lifecycle.begin(session)

    assert lifecycle.finalize(
        session,
        wait_for_playback=lambda: True,
        release_gate=lambda: releases.append("released"),
        emit_final_state=lambda: states.append("idle"),
    )
    assert path in {"tts_disabled", "no_audio_produced"}
    assert releases == ["released"]
    assert states == ["idle"]


def test_old_session_finalizer_cannot_reset_newer_listening_or_processing():
    states: list[str] = []
    trace: list[str] = []
    old_releases: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    old_session = object()
    new_session = object()
    assert lifecycle.begin(old_session)
    fake_tts = FakeTtsCompletion(trace)
    fake_tts.schedule()
    thread, result = _start_finalizer(
        lifecycle,
        old_session,
        fake_tts,
        lambda: old_releases.append("released"),
        lambda: states.append("idle"),
    )

    assert lifecycle.begin(new_session)
    assert lifecycle.emit_if_current(new_session, lambda: states.append("listening"))
    assert lifecycle.emit_if_current(new_session, lambda: states.append("thinking"))
    fake_tts.complete_playback()
    thread.join(timeout=1)

    assert result == [False]
    assert old_releases == []
    assert states == ["listening", "thinking"]


def test_shutdown_closes_acceptance_and_suppresses_late_ready_state():
    states = ["speaking"]
    trace: list[str] = []
    lifecycle = VoiceSessionLifecycle()
    gate, handler_lock = _active_gate()
    session = object()
    assert lifecycle.begin(session)
    fake_tts = FakeTtsCompletion(trace)
    fake_tts.schedule()
    thread, result = _start_finalizer(
        lifecycle,
        session,
        fake_tts,
        gate.finish,
        lambda: states.append("idle"),
    )

    lifecycle.close()
    gate.close()
    fake_tts.complete_playback()
    thread.join(timeout=1)

    assert result == [False]
    assert states == ["speaking"]
    assert not handler_lock.locked()
    assert not gate.reserve(0)


def test_text_chat_busy_contract_remains_immediate_and_not_playback_coupled():
    states: list[str] = []
    busy_events: list[tuple[str, bool]] = []
    lifecycle = VoiceSessionLifecycle()

    busy_events.append(("text", True))
    assert lifecycle.emit_when_inactive(lambda: states.append("thinking"))
    busy_events.append(("text", False))
    assert lifecycle.emit_when_inactive(lambda: states.append("idle"))
    assert busy_events == [("text", True), ("text", False)]
    assert states == ["thinking", "idle"]

    voice_session = object()
    assert lifecycle.begin(voice_session)
    busy_events.append(("other-text", True))
    assert not lifecycle.emit_when_inactive(lambda: states.append("thinking"))
    assert lifecycle.finalize(
        voice_session,
        wait_for_playback=lambda: True,
        release_gate=lambda: None,
        emit_final_state=lambda: states.append("thinking"),
    )
    busy_events.append(("other-text", False))
    assert lifecycle.emit_when_inactive(lambda: states.append("idle"))

    assert busy_events[-2:] == [("other-text", True), ("other-text", False)]
    assert states[-2:] == ["thinking", "idle"]


def test_main_wires_one_ready_boundary_and_no_scattered_voice_idle_writes():
    source = (Path(__file__).parents[1] / "osia" / "main.py").read_text(
        encoding="utf-8"
    )
    functions = {
        node.name: node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
    }
    voice_path = ast.get_source_segment(source, functions["handle_wake"])
    new_chat_handler = ast.get_source_segment(source, functions["handle_voice_new_chat"])

    assert source.count('_emit_voice_state("listening")') == 1
    assert "on_ready=_mark_listening_ready" in voice_path
    assert 'bridge.island_state.emit("idle")' not in voice_path
    assert "voice_ui_lifecycle.finalize(" in voice_path
    assert "wait_for_playback_complete(" in voice_path
    assert "tts_stop()" not in new_chat_handler
    assert 'island.set_state("idle")' not in new_chat_handler
