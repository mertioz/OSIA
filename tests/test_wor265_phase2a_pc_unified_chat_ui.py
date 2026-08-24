"""Focused WOR-265 proofs for the PC Unified Chat surface."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QFrame

from osia import i18n
from osia.execution_trace import ExecutionTraceLedger
from osia.gui import main_popup


_LIVE_APPS = []
_LIVE_POPUPS = []


@pytest.fixture(scope="module")
def app():
    value = QApplication.instance() or QApplication([])
    _LIVE_APPS.append(value)
    return value


@pytest.fixture(scope="module")
def popup(app):
    with (
        patch.object(
            main_popup._SettingsPanel,
            "_probe_chatgpt_account",
            lambda _self, force=False: None,
        ),
        patch.object(main_popup, "refresh_ollama_models_async"),
        patch.object(main_popup, "autostart_enabled", return_value=False),
    ):
        value = main_popup.MainPopup()
        _LIVE_POPUPS.append(value)
        app.processEvents()
        return value


def _ledger(*, suffix: str = "trace") -> ExecutionTraceLedger:
    return ExecutionTraceLedger(
        conversation_id=f"conv_wor265_{suffix}",
        request_id=f"request_wor265_{suffix}",
        turn_id=f"turn_wor265_{suffix}",
        run_id=f"run_wor265_{suffix}",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-luna",
        effective_reasoning="high",
    )


def _payload(
    ledger: ExecutionTraceLedger,
    *,
    terminal: bool = False,
    **updates,
) -> dict:
    value = {
        **ledger.binding_dict(),
        "terminal": terminal,
        "cancel_requested": False,
        "outcome_unknown": False,
        "snapshot": ledger.snapshot()["events"],
    }
    value.update(updates)
    return value


def _bubble_texts(chat: main_popup._ChatPage) -> list[str]:
    return [bubble.text() for bubble in chat.findChildren(main_popup._Bubble)]


def _finish_progressive_stream(chat: main_popup._ChatPage) -> None:
    for _ in range(256):
        if not chat._stream.active:
            return
        chat._stream._on_tick()
    raise AssertionError("desktop presentation stream did not settle")


def test_pc_navigation_has_no_autonomous_entry_and_chat_is_the_only_agent_entry(
    popup,
):
    assert not hasattr(popup, "PAGE_AUTONOMOUS")
    assert not hasattr(popup, "_autonomous")
    assert popup._stack.widget(popup.PAGE_CHAT) is popup._chat
    assert {button._kind for button in popup._nav_buttons.values()} == {
        "conv", "models", "settings", "osia",
    }


def test_simple_chat_has_no_thinking_or_agent_activity_ui(app):
    chat = main_popup._ChatPage()
    try:
        chat.set_messages([
            {"role": "user", "content": "What is 2 + 2?"},
            {"role": "assistant", "content": "4"},
        ])
        app.processEvents()

        assert chat.findChildren(main_popup._ChatThinkingTrace) == []
        assert not hasattr(main_popup, "_ChatExecutionActivity")
        assert chat.findChildren(QFrame, "agentActivityCard") == []
    finally:
        chat.deleteLater()


def test_real_activity_is_sanitized_and_rendered_as_one_compact_trace(app):
    chat = main_popup._ChatPage()
    ledger = _ledger(suffix="real")
    ledger.record(
        kind="action",
        display_key="execution.action.shell",
        invocation_id="invocation_wor265_real",
    )
    payload = _payload(
        ledger,
        objective="private objective",
        reasoning="private chain of thought",
        diagnostic={"traceback": "private"},
        plan=["must never render"],
        tasks_done=1,
        tasks_total=3,
    )
    try:
        chat.append_message("user", "ouvre calculatrice et calcule 5+23")
        assert chat.set_thinking_trace(payload)
        assert chat.set_thinking_trace(payload)
        app.processEvents()

        traces = chat.findChildren(main_popup._ChatThinkingTrace)
        assert len(traces) == 1
        trace = traces[0]
        assert trace.objectName() == "chatThinkingTrace"
        assert trace._detail.toPlainText() == i18n.t(
            "chat.thinking.action_shell"
        )
        assert not ({
            "objective", "reasoning", "diagnostic", "plan",
            "tasks_done", "tasks_total",
        } & set(trace._binding))
        assert trace._detail.isHidden()
    finally:
        chat.deleteLater()


def test_no_real_event_keeps_thinking_empty_instead_of_inventing_progress(app):
    trace = main_popup._ChatThinkingTrace()
    try:
        assert trace.apply_trace(_payload(_ledger(suffix="empty")))
        app.processEvents()

        assert trace._detail.toPlainText() == ""
        assert not trace._toggle.isEnabled()
        assert "Planning" not in trace._toggle.text()
        assert "Working" not in trace._toggle.text()
        assert "Analyzing" not in trace._toggle.text()
    finally:
        trace.deleteLater()


def test_provider_public_summary_is_displayed_but_raw_reasoning_is_rejected(app):
    safe = _ledger(suffix="summary")
    assert safe.record_public_reasoning_summary(
        "Checked the current Calculator window state."
    ) is not None
    safe_trace = main_popup._ChatThinkingTrace()
    raw = _ledger(suffix="raw")
    assert raw.record_public_reasoning_summary(
        "<think>private chain of thought</think>"
    ) is None
    raw_trace = main_popup._ChatThinkingTrace()
    try:
        assert safe_trace.apply_trace(_payload(safe))
        assert safe_trace._detail.toPlainText() == (
            "Checked the current Calculator window state."
        )
        assert raw_trace.apply_trace(_payload(raw))
        assert raw_trace._detail.toPlainText() == ""
        app.processEvents()
    finally:
        safe_trace.deleteLater()
        raw_trace.deleteLater()


def test_thinking_expands_and_collapses_without_a_large_card(app):
    ledger = _ledger(suffix="expand")
    ledger.record(
        kind="reconciliation",
        display_key="execution.reconciliation.verifying",
    )
    trace = main_popup._ChatThinkingTrace()
    try:
        assert trace.apply_trace(_payload(ledger))
        assert trace._detail.isHidden()

        with patch.object(
            main_popup, "reduced_motion_requested", return_value=True,
        ):
            trace._toggle.click()
            app.processEvents()
            assert trace._expanded is True
            assert not trace._detail.isHidden()
            assert trace._detail.maximumHeight() > 0

            trace._toggle.click()
            app.processEvents()
            assert trace._expanded is False
            assert trace._detail.isHidden()
    finally:
        trace.deleteLater()


def test_final_result_follows_thinking_in_the_same_chat(app):
    chat = main_popup._ChatPage()
    ledger = _ledger(suffix="final")
    ledger.record(
        kind="reconciliation",
        display_key="execution.reconciliation.verifying",
    )
    try:
        chat.append_message("user", "ouvre calculatrice et calcule 5+23")
        assert chat.set_thinking_trace(_payload(ledger))
        trace = chat.findChildren(main_popup._ChatThinkingTrace)[0]
        chat.append_message("assistant", "28")
        app.processEvents()

        bubbles = chat.findChildren(main_popup._Bubble)
        assert [bubble.text() for bubble in bubbles] == [
            "ouvre calculatrice et calcule 5+23", "28",
        ]
        assert chat._messages_layout.indexOf(trace) < chat._messages_layout.indexOf(
            bubbles[-1].parentWidget()
        )
    finally:
        chat.deleteLater()


def test_stop_emits_the_exact_chat_turn_binding_once(app):
    ledger = _ledger(suffix="stop")
    trace = main_popup._ChatThinkingTrace()
    emitted = []
    trace.cancel_requested.connect(emitted.append)
    try:
        assert trace.apply_trace(_payload(ledger))
        trace._request_cancel()
        trace._request_cancel()
        app.processEvents()

        assert len(emitted) == 1
        assert {
            key: emitted[0][key]
            for key in (
                "conversation_id", "request_id", "turn_id", "run_id",
                "provider_id", "model_id", "effective_reasoning",
            )
        } == ledger.binding_dict()
        assert not trace._stop.isVisible()
    finally:
        trace.deleteLater()


def test_committed_stop_closes_thinking_and_late_trace_cannot_reopen(app, popup):
    ledger = _ledger(suffix="committed_stop")
    ledger.record_public_reasoning_summary("Public reasoning is active.")
    binding = ledger.binding_dict()
    popup.refresh_conversations(binding["conversation_id"])
    popup._active_conversation_id = binding["conversation_id"]
    try:
        popup.set_conversation_busy(binding["conversation_id"], True)
        assert popup.begin_chat_turn(binding)
        assert popup.update_chat_execution_activity(_payload(ledger))
        key = main_popup._ChatPage._binding_key(binding)
        trace = popup._chat._thinking_widgets[key]
        assert not trace.isHidden()

        assert popup.confirm_chat_execution_cancellation(binding)
        app.processEvents()
        assert trace.isHidden()
        assert popup._chat._active_thinking_key is None
        assert binding["conversation_id"] not in (
            popup._active_thinking_key_by_conversation
        )
        assert binding["conversation_id"] not in popup._busy_conversation_ids
        assert popup._chat._input.isEnabled()

        next_ledger = _ledger(suffix="committed_stop_next")
        next_binding = {
            **next_ledger.binding_dict(),
            "conversation_id": binding["conversation_id"],
        }
        assert popup.begin_chat_turn(next_binding)
        assert main_popup._ChatPage._binding_key(next_binding) != key

        ledger.record_public_reasoning_summary("Late summary")
        assert not popup.update_chat_execution_activity(_payload(ledger))
        assert trace.isHidden()
    finally:
        popup._busy_conversation_ids.discard(binding["conversation_id"])


def test_historical_completed_trace_is_inspectable_but_never_active(app):
    ledger = _ledger(suffix="history")
    ledger.record(
        kind="terminal",
        display_key="execution.terminal.success",
        terminal=True,
    )
    binding = ledger.binding_dict()
    chat = main_popup._ChatPage()
    try:
        chat.set_messages([{
            "role": "assistant",
            "content": "28",
            "_conversation_id": binding["conversation_id"],
            "_request_id": binding["request_id"],
            "_turn_id": binding["turn_id"],
            "_message_id": "message_wor265_history",
            "_execution_trace_v1": ledger.snapshot(),
        }])
        app.processEvents()

        traces = chat.findChildren(main_popup._ChatThinkingTrace)
        assert len(traces) == 1
        assert traces[0].terminal is True
        assert not traces[0]._stop.isVisible()
        assert chat._active_thinking_key is None
    finally:
        chat.deleteLater()


@pytest.mark.parametrize("route", ["direct", "durable"])
def test_instant_final_new_turn_never_replays_previous_answer(app, route):
    chat = main_popup._ChatPage()
    chat._stream._reduced_motion = lambda: True
    turn_a = _ledger(suffix=f"instant_{route}_a")
    turn_b = _ledger(suffix=f"instant_{route}_b")
    if route == "direct":
        turn_a.run_id = turn_a.turn_id
        turn_b.run_id = turn_b.turn_id
    old = "OLD_UNIQUE_TOKEN"
    new = "NEW_UNIQUE_TOKEN"
    try:
        chat.append_message("user", "question A")
        assert chat.begin_turn(turn_a.binding_dict())
        assert chat.finalize_assistant_stream(old, turn_a.binding_dict())
        assert chat._stream_bubble is None

        chat.append_message("user", "question B")
        assert chat.begin_turn(turn_b.binding_dict())
        assert chat.finalize_assistant_stream(new, turn_b.binding_dict())
        app.processEvents()

        rendered = _bubble_texts(chat)
        assert rendered.count(old) == 1
        assert rendered.count(new) == 1
        assert rendered == ["question A", old, "question B", new]
        assert chat._stream_key == main_popup._ChatPage._binding_key(
            turn_b.binding_dict()
        )
    finally:
        chat.deleteLater()


def test_chunked_turns_keep_thinking_and_stream_buffers_strictly_isolated(app):
    chat = main_popup._ChatPage()
    chat._stream._reduced_motion = lambda: False
    turn_a = _ledger(suffix="chunked_a")
    turn_b = _ledger(suffix="chunked_b")
    turn_a.record(
        kind="action",
        display_key="execution.action.shell",
        invocation_id="invocation_chunked_a",
    )
    turn_b.record_public_reasoning_summary("Public summary for turn B.")
    old = "OLD_UNIQUE_TOKEN"
    new = "NEW_UNIQUE_TOKEN"
    try:
        chat.append_message("user", "question A")
        assert chat.begin_turn(turn_a.binding_dict())
        assert chat.set_thinking_trace(_payload(turn_a))
        assert chat.append_assistant_stream_chunk("OLD_UNIQUE", turn_a.binding_dict())
        assert chat.append_assistant_stream_chunk("TOKEN", turn_a.binding_dict())
        assert chat.finalize_assistant_stream(old, turn_a.binding_dict())
        _finish_progressive_stream(chat)
        assert chat._stream_bubble is None

        chat.append_message("user", "question B")
        assert chat.begin_turn(turn_b.binding_dict())
        assert chat.set_thinking_trace(_payload(turn_b))
        assert chat.append_assistant_stream_chunk("NEW_UNIQUE", turn_b.binding_dict())
        assert chat.append_assistant_stream_chunk("TOKEN", turn_b.binding_dict())
        assert chat.finalize_assistant_stream(new, turn_b.binding_dict())
        _finish_progressive_stream(chat)
        app.processEvents()

        rendered = _bubble_texts(chat)
        assert rendered.count(old) == 1
        assert rendered.count(new) == 1
        assert rendered == ["question A", old, "question B", new]
        traces = chat.findChildren(main_popup._ChatThinkingTrace)
        assert len(traces) == 2
        assert traces[0]._detail.toPlainText() == i18n.t(
            "chat.thinking.action_shell"
        )
        assert traces[1]._detail.toPlainText() == "Public summary for turn B."
        assert traces[0]._stop.isHidden()
        assert not traces[1]._stop.isHidden()
        assert traces[1]._stop.isEnabled()
    finally:
        chat.deleteLater()


def test_reconnect_history_hydration_does_not_reanimate_completed_answer(app):
    chat = main_popup._ChatPage()
    chat._stream._reduced_motion = lambda: True
    turn_a = _ledger(suffix="reconnect_a")
    turn_a.record(
        kind="terminal",
        display_key="execution.terminal.success",
        terminal=True,
    )
    turn_b = _ledger(suffix="reconnect_b")
    old = "OLD_UNIQUE_TOKEN"
    new = "NEW_UNIQUE_TOKEN"
    try:
        chat.set_messages([
            {"role": "user", "content": "question A"},
            {
                "role": "assistant",
                "content": old,
                "_conversation_id": turn_a.conversation_id,
                "_request_id": turn_a.request_id,
                "_turn_id": turn_a.turn_id,
                "_message_id": "message_reconnect_a",
                "_execution_trace_v1": turn_a.snapshot(),
            },
        ])
        chat.append_message("user", "question B")
        assert chat.begin_turn(turn_b.binding_dict())
        assert chat.finalize_assistant_stream(new, turn_b.binding_dict())
        app.processEvents()

        rendered = _bubble_texts(chat)
        assert rendered.count(old) == 1
        assert rendered.count(new) == 1
        assert rendered == ["question A", old, "question B", new]
    finally:
        chat.deleteLater()


def test_cancelled_turn_stop_and_projection_state_do_not_leak_to_next_turn(app):
    chat = main_popup._ChatPage()
    chat._stream._reduced_motion = lambda: True
    turn_a = _ledger(suffix="cancel_a")
    turn_a.record(
        kind="action",
        display_key="execution.action.shell",
        invocation_id="invocation_cancel_a",
    )
    turn_b = _ledger(suffix="cancel_b")
    turn_b.record(
        kind="action",
        display_key="execution.action.browser",
        invocation_id="invocation_cancel_b",
    )
    try:
        assert chat.begin_turn(turn_a.binding_dict())
        assert chat.set_thinking_trace(_payload(turn_a))
        assert chat.tombstone_cancelled_turn(turn_a.binding_dict())
        chat.cancel_assistant_stream(flush=False)

        assert chat.begin_turn(turn_b.binding_dict())
        assert chat.set_thinking_trace(_payload(turn_b))
        assert chat.append_assistant_stream_chunk(
            "NEW_UNIQUE_TOKEN", turn_b.binding_dict()
        )
        assert chat.finalize_assistant_stream(
            "NEW_UNIQUE_TOKEN", turn_b.binding_dict()
        )
        app.processEvents()

        key_b = main_popup._ChatPage._binding_key(turn_b.binding_dict())
        traces = chat.findChildren(main_popup._ChatThinkingTrace)
        trace_b = next(trace for trace in traces if trace.key == key_b)
        assert key_b not in chat._cancelled_stream_keys
        assert not trace_b._stop.isHidden()
        assert trace_b._stop.isEnabled()
        assert trace_b._detail.toPlainText() == i18n.t(
            "chat.thinking.action_browser"
        )
        assert _bubble_texts(chat).count("NEW_UNIQUE_TOKEN") == 1
    finally:
        chat.deleteLater()
