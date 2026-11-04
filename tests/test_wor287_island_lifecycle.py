"""Capsule lifecycle regressions, including the real wake_mode callbacks."""

from __future__ import annotations

import ast
import threading
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

from osia.gui.island_lifecycle import IslandLifecycle
from osia.voice.wake import VoiceSessionLifecycle


_LIVE_APPS = []


def _callbacks(namespace, *names):
    path = Path(__file__).resolve().parents[1] / "osia" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def _binding(suffix="a", conversation="chat"):
    return dict(conversation_id=conversation, request_id=f"request-{suffix}",
                turn_id=f"turn-{suffix}", run_id=f"run-{suffix}")


def _runtime():
    notifications = []
    visible = []
    bridge = SimpleNamespace(
        island_state=SimpleNamespace(emit=lambda *args: notifications.append(args)),
        conversation_busy_changed=SimpleNamespace(emit=lambda *_: None),
    )
    lifecycle = IslandLifecycle(bridge.island_state.emit)
    ns = dict(bridge=bridge, island_lifecycle=lifecycle,
              island=SimpleNamespace(set_state=visible.append),
              busy_conversations=set(), busy_turn_keys={}, busy_lock=threading.Lock(),
              voice_ui_lifecycle=VoiceSessionLifecycle(),
              active_chat_turns={}, active_chat_turns_lock=threading.Lock())
    _callbacks(ns, "_chat_turn_key", "_set_conversation_busy", "handle_island_state",
               "_register_chat_turn", "_release_chat_turn", "_set_chat_island_state",
               "_project_chat_island_trace")
    return ns, notifications, visible


def test_delayed_thinking_notification_cannot_outlive_completion():
    ns, notifications, visible = _runtime()
    binding = _binding()
    ns["_set_conversation_busy"]("chat", True, owner=binding)
    ns["_register_chat_turn"](binding, object())
    ns["_set_conversation_busy"]("chat", False, owner=binding)
    # Cross-thread notifications can arrive after the terminal notification.
    for args in reversed(notifications):
        ns["handle_island_state"](*args)
    assert not ns["busy_conversations"]
    assert visible[-1] == "idle"


def _start(ns, binding, agent=None):
    agent = agent or object()
    ns["_set_conversation_busy"](binding["conversation_id"], True, owner=binding)
    ns["_register_chat_turn"](binding, agent)
    return agent


def test_admission_and_persistence_do_not_claim_reasoning():
    ns, _, _ = _runtime()
    binding = _binding()
    ns["_set_conversation_busy"]("chat", True, owner=binding)
    assert ns["island_lifecycle"].current_state() == "idle"
    ns["_register_chat_turn"](binding, object())
    assert ns["island_lifecycle"].current_state() == "thinking"
    ns["_project_chat_island_trace"]({**binding, "terminal": True})
    assert ns["busy_conversations"] == {"chat"}
    assert ns["island_lifecycle"].current_state() == "idle"
    ns["_project_chat_island_trace"]({**binding, "kind": "public_reasoning_summary"})
    assert ns["island_lifecycle"].current_state() == "idle"


@pytest.mark.parametrize("terminal_handler", [
    "handle_assistant_stream_final", "handle_assistant_stream_error",
])
def test_final_and_failure_retire_only_the_correlated_turn(terminal_handler):
    ns, _, _ = _runtime()
    binding = _binding()
    _start(ns, binding)
    ns["active_conv"] = {"id": "another-visible-chat"}
    _callbacks(ns, terminal_handler)
    ns[terminal_handler](binding, "Canonical answer or provider failure")
    assert ns["island_lifecycle"].current_state() == "idle"
    ns["_set_chat_island_state"](binding, "thinking")
    assert ns["island_lifecycle"].current_state() == "idle"


def test_late_tool_callback_cannot_reopen_a_completed_or_replaced_turn():
    ns, _, _ = _runtime()
    old = _binding()
    _start(ns, old)
    ns.update(turn_binding=old, turn_meta=old, turn_agent=object())
    ns["bridge"].tool_attempt_status = SimpleNamespace(emit=lambda *_: None)
    _callbacks(ns, "on_tool_attempt_status")
    ns["_set_conversation_busy"]("chat", False, owner=old)
    ns["on_tool_attempt_status"]("resuming_tool_turn", "old-attempt")
    assert ns["island_lifecycle"].current_state() == "idle"
    fresh = _binding("b")
    _start(ns, fresh)
    ns["on_tool_attempt_status"]("waiting_tool_result", "old-attempt")
    assert ns["island_lifecycle"].current_state() == "thinking"
    assert not ns["_set_conversation_busy"]("chat", False, owner=old)
    assert ns["busy_turn_keys"]["chat"] == tuple(fresh.values())


def test_real_reasoning_wait_permission_retry_and_continuation():
    ns, _, _ = _runtime()
    binding = _binding()
    _start(ns, binding)
    ns.update(turn_binding=binding, turn_meta=binding, turn_agent=object())
    ns["bridge"].tool_attempt_status = SimpleNamespace(emit=lambda *_: None)
    _callbacks(ns, "on_tool_attempt_status")
    for kind, expected in [
        ("public_reasoning_summary", "thinking"),
        ("action", "idle"),
        ("waiting_permission", "idle"),
        ("result", "thinking"),
        ("continuation", "thinking"),
    ]:
        ns["_project_chat_island_trace"]({**binding, "kind": kind})
        assert ns["island_lifecycle"].current_state() == expected
    for status, expected in [
        ("waiting_tool_result", "idle"),
        ("verifying_tool_result", "idle"),
        ("resuming_tool_turn", "thinking"),
        ("blocked_unknown_effect", "thinking"),
        ("cancellation_requested", "idle"),
    ]:
        ns["on_tool_attempt_status"](status, "attempt")
        assert ns["island_lifecycle"].current_state() == expected
    assert ns["busy_conversations"] == {"chat"}


@pytest.mark.parametrize("scheduler", [
    "_schedule_desktop_final_persistence_retry",
    "_schedule_desktop_terminal_recovery_retry",
])
def test_durable_retry_keeps_admission_without_fake_thinking(scheduler):
    ns, _, _ = _runtime()
    binding = _binding()
    _start(ns, binding)
    scheduled = []
    ns["_start_or_run_durable_worker"] = lambda worker, **kw: scheduled.append(worker) or True
    _callbacks(ns, scheduler)
    kwargs = dict(conv_id="chat", turn_meta=binding, voice_response=False,
                  language_text=None, user_display_text=None, tts_session_id=None,
                  voice_cancelled=None, emit_voice_state=None)
    if "terminal_recovery" in scheduler:
        kwargs.update(user_input="hello", history=[])
    lifecycle = {"binding": binding}
    assert ns[scheduler](None, lifecycle, **kwargs)
    assert scheduled and lifecycle["durable_wait"]
    assert ns["busy_conversations"] == {"chat"}
    assert ns["island_lifecycle"].current_state() == "idle"


def test_cancel_and_late_worker_cleanup_preserve_other_conversation():
    ns, _, _ = _runtime()
    cancelled = threading.Event()
    started = threading.Event()
    commit = threading.Event()
    completed = Queue()

    def record_intent(_turn_id):
        started.set()
        return commit.wait(2)

    agent = SimpleNamespace(request_turn_cancellation_intent=record_intent,
                            request_turn_cancellation=lambda _: cancelled.set())
    binding = _binding()
    _start(ns, binding, agent)
    other = _binding("b", "other")
    _start(ns, other)
    ns["threading"] = threading
    ns["bridge"].chat_cancel_finished = SimpleNamespace(emit=lambda *args: completed.put(args))
    confirmed = []
    ns["popup"] = SimpleNamespace(
        confirm_chat_execution_cancellation=lambda payload, **_: confirmed.append(payload),
    )
    _callbacks(ns, "handle_chat_execution_cancel", "handle_chat_execution_cancel_finished")
    ns["handle_chat_execution_cancel"](binding)
    try:
        assert started.wait(1)
        assert confirmed == []
        assert ns["busy_conversations"] == {"chat", "other"}
    finally:
        commit.set()
    ns["handle_chat_execution_cancel_finished"](*completed.get(timeout=2))
    assert cancelled.wait(2)
    assert confirmed == [binding]
    ns["_release_chat_turn"](binding, agent)
    ns["_set_chat_island_state"](binding, "thinking")
    assert ns["busy_conversations"] == {"other"}
    assert ns["island_lifecycle"].current_state() == "thinking"
    ns["_set_conversation_busy"]("other", False, owner=other)
    assert ns["island_lifecycle"].current_state() == "idle"


def test_stop_error_releases_pending_request_without_releasing_the_turn(monkeypatch):
    from osia import turn_terminality

    monkeypatch.setattr(turn_terminality, "load_persisted_terminal_result", lambda _: None)
    ns, _, _ = _runtime()
    completed = Queue()
    rejected = []

    def fail(_turn_id):
        raise OSError("simulated storage contention")

    agent = SimpleNamespace(
        request_turn_cancellation_intent=fail,
        request_turn_cancellation=lambda _: pytest.fail("Uncommitted Stop propagated"),
    )
    binding = _binding()
    _start(ns, binding, agent)
    ns["threading"] = threading
    ns["bridge"].chat_cancel_finished = SimpleNamespace(emit=lambda *args: completed.put(args))
    ns["popup"] = SimpleNamespace(reject_chat_execution_cancellation=rejected.append)
    _callbacks(ns, "handle_chat_execution_cancel", "handle_chat_execution_cancel_finished")
    ns["handle_chat_execution_cancel"](binding)
    ns["handle_chat_execution_cancel_finished"](*completed.get(timeout=2))
    assert rejected == [binding]
    assert ns["busy_conversations"] == {"chat"}
    assert not ns["active_chat_turns"][tuple(binding.values())].get("cancel_pending")


def test_voice_and_cron_finish_restore_only_current_activity():
    state = IslandLifecycle(lambda: None)
    state.begin("chat", "a", "thinking")
    state.begin("voice", "old", "listening")
    assert state.current_state() == "listening"
    state.update("voice", "old", "speaking")
    state.update("chat", "a", "idle", terminal=True)
    assert state.current_state() == "speaking"
    state.finish("voice", "old")
    assert state.current_state() == "idle"
    state.begin("voice", "new", "listening")
    assert not state.update("voice", "old", "thinking")
    assert not state.finish("voice", "old")
    assert state.current_state() == "listening"
    state.finish("voice", "new")
    state.begin("chat", "b", "thinking")
    state.begin("cron", "occurrence", "thinking")
    state.update("cron", "occurrence", "speaking")
    assert state.current_state() == "speaking"
    state.finish("cron", "occurrence")
    assert state.current_state() == "thinking"
    state.close()
    assert not state.begin("chat", "c", "thinking")
    assert not state.update("chat", "b", "thinking")
    assert state.current_state() == "idle"


def test_actual_voice_callbacks_wait_for_playback_and_fence_old_session():
    ns, _, _ = _runtime()
    session = object()
    playback_done = [False]
    released = []
    ns.update(voice_session=session, owned_tts_session=[42],
              wait_for_playback_complete=lambda **_: playback_done[0],
              wake_gate=SimpleNamespace(finish=lambda: released.append(True)))
    ns["voice_ui_lifecycle"].begin(session)
    ns["island_lifecycle"].begin("voice", session)
    _callbacks(ns, "_emit_voice_state", "_finish_wake_turn")
    ns["_emit_voice_state"]("thinking")
    assert ns["island_lifecycle"].current_state() == "idle"
    ns["_emit_voice_state"]("listening")
    assert ns["island_lifecycle"].current_state() == "listening"
    ns["_emit_voice_state"]("speaking")
    ns["_finish_wake_turn"]()
    assert not released
    assert ns["island_lifecycle"].current_state() == "speaking"
    playback_done[0] = True
    ns["_finish_wake_turn"]()
    assert released == [True]
    assert ns["island_lifecycle"].current_state() == "idle"
    assert not ns["_emit_voice_state"]("thinking")


@pytest.mark.parametrize("failure", [False, True])
def test_actual_cron_finalizer_cannot_clobber_parallel_chat(failure):
    import uuid

    ns, _, _ = _runtime()
    phases = []
    agent = SimpleNamespace(reset=lambda: None)

    def run(*_args, **_kwargs):
        phases.append(ns["island_lifecycle"].current_state())
        # A text turn can start while cron owns only the audio gate.
        _start(ns, _binding())
        if failure:
            raise RuntimeError("synthetic provider failure")
        return "Canonical cron answer"

    ns.update(uuid=uuid, handler_lock=threading.Lock(), cron_agent=agent,
              tts_stop=lambda: None, load_settings=lambda: {}, clean_response=lambda text: text,
              _run_turn_to_terminality=run,
              _terminality_result=lambda *_: {"terminal_state": "success_verified"},
              speak=lambda *_a, **_kw: phases.append(ns["island_lifecycle"].current_state()))
    _callbacks(ns, "run_cron_task")
    result = ns["run_cron_task"]("test cron lifecycle")
    assert result["status"] == ("failed" if failure else "success_verified")
    assert phases == (["thinking"] if failure else ["thinking", "speaking"])
    assert not ns["handler_lock"].locked()
    assert ns["island_lifecycle"].current_state() == "thinking"
    ns["_set_conversation_busy"]("chat", False, owner=_binding())
    assert ns["island_lifecycle"].current_state() == "idle"


def test_canonical_trace_projection_uses_current_turn_ownership():
    ns, _, _ = _runtime()
    binding = _binding()
    _start(ns, binding)
    project = ns["_project_chat_island_trace"]

    project({**binding, "kind": "public_reasoning_summary"})
    assert ns["island_lifecycle"].current_state() == "thinking"
    project({**binding, "kind": "action"})
    assert ns["island_lifecycle"].current_state() == "idle"
    project({**binding, "kind": "continuation"})
    assert ns["island_lifecycle"].current_state() == "thinking"

    fresh = _binding("b")
    _start(ns, fresh)
    project({**binding, "kind": "terminal", "terminal": True})
    assert ns["island_lifecycle"].current_state() == "thinking"
    project({**fresh, "kind": "terminal", "terminal": True})
    assert ns["island_lifecycle"].current_state() == "idle"

@pytest.mark.parametrize("shape", ["capsule", "circle", "docked"])
@pytest.mark.parametrize("reduced_motion", [False, True])
def test_real_qt_queued_delivery_and_animation_settle(shape, reduced_motion, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtWidgets import QApplication
    from osia.gui import dynamic_island

    app = QApplication.instance() or QApplication([])
    _LIVE_APPS.append(app)
    monkeypatch.setattr("osia.config.load_settings", lambda: {})
    monkeypatch.setattr(dynamic_island, "reduced_motion_requested", lambda *_: reduced_motion)
    island = dynamic_island.DynamicIsland()
    island._shape = shape
    island._dock_side = "left"
    island._apply_shape_geometry(animate=False)

    class Bridge(QObject):
        changed = pyqtSignal()

    bridge = Bridge()
    lifecycle = IslandLifecycle(bridge.changed.emit)

    class Receiver(QObject):
        @pyqtSlot()
        def project(self):
            island.set_state(lifecycle.current_state())

    receiver = Receiver()
    bridge.changed.connect(receiver.project, Qt.ConnectionType.QueuedConnection)
    try:
        lifecycle.begin("chat", "a", "thinking")
        app.processEvents()
        assert island.current_state() == "thinking"
        for _ in range(100):
            island._tick()
        assert island.current_state() == "thinking"  # no timeout cuts real work
        if not reduced_motion:
            assert island._anim_ms >= 3300

        def finish_then_late_callback():
            lifecycle.update("chat", "a", "idle", terminal=True)
            lifecycle.update("chat", "a", "thinking")
            bridge.changed.emit()  # a notification already queued by old work

        worker = threading.Thread(target=finish_then_late_callback)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        app.processEvents()
        island._tick()
        assert island.current_state() == "idle"
        if shape == "docked":
            assert island._docked_slid_out is False
    finally:
        island._timer.stop()
        island.close()
        island.deleteLater()
        app.processEvents()
