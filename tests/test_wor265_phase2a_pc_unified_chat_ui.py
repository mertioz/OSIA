"""Focused WOR-265 proofs for the PC Unified Chat surface."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
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
        assert trace._detail.toPlainText() == ""
        assert not ({
            "objective", "reasoning", "diagnostic", "plan",
            "tasks_done", "tasks_total",
        } & set(trace._binding))
        assert trace._expanded
    finally:
        chat.deleteLater()


def test_no_real_event_keeps_thinking_empty_instead_of_inventing_progress(app):
    trace = main_popup._ChatThinkingTrace()
    try:
        assert trace.apply_trace(_payload(_ledger(suffix="empty")))
        app.processEvents()

        assert trace._detail.toPlainText() == ""
        assert trace._toggle.isEnabled()
        assert "Planning" not in trace._toggle.text()
        assert "Working" not in trace._toggle.text()
        assert "Analyzing" not in trace._toggle.text()
    finally:
        trace.deleteLater()


def test_begin_turn_shows_minimal_thinking_before_detailed_activity(app):
    chat = main_popup._ChatPage()
    binding = _ledger(suffix="minimal").binding_dict()
    try:
        assert chat.begin_turn(binding)
        traces = chat.findChildren(main_popup._ChatThinkingTrace)
        assert len(traces) == 1
        trace = traces[0]
        assert trace._detail.toPlainText() == ""
        assert trace._toggle.isEnabled() is True
        assert trace._expanded
        assert chat._send_btn.stop_mode
        assert chat._send_btn.isEnabled()
        assert trace._toggle.isHidden()
        assert not trace._orbs.isHidden()
    finally:
        chat.deleteLater()


def test_only_fully_correlated_trace_events_contribute_thinking_detail(app):
    ledger = _ledger(suffix="event_identity")
    ledger.record_public_reasoning_summary("Current turn summary.")
    payload = _payload(ledger)
    payload["snapshot"][0].pop("request_id")
    trace = main_popup._ChatThinkingTrace()
    try:
        assert trace.apply_trace(payload)
        assert trace._detail.toPlainText() == ""
    finally:
        trace.deleteLater()


def test_removed_permission_state_is_not_presented_as_model_thinking(app):
    ledger = _ledger(suffix="permission_wait")
    ledger.record(
        kind="waiting_permission",
        display_key="execution.permission.waiting",
        permission_request_id="permission_wor265_wait",
    )
    trace = main_popup._ChatThinkingTrace()
    try:
        assert trace.apply_trace(_payload(ledger))
        assert trace._detail.toPlainText() == ""
    finally:
        trace.deleteLater()


def test_legacy_agent_activity_cannot_reach_the_chat_thinking_surface():
    assert main_popup._ChatThinkingTrace._safe_payload({
        "conversation_id": "conv_legacy",
        "request_id": "request_legacy",
        "turn_id": "turn_legacy",
        "run_id": "run_legacy",
        "status": "executing",
        "tasks_done": 1,
        "tasks_total": 3,
    }) == {}
    assert not hasattr(main_popup._ChatPage, "set_execution_activity")
    assert not hasattr(main_popup.MainPopup, "restore_chat_execution_activity")


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
    ledger.record_public_reasoning_summary("I checked the visible result.")
    trace = main_popup._ChatThinkingTrace()
    try:
        with patch.object(main_popup, "reduced_motion_requested", return_value=True):
            assert trace.apply_trace(_payload(ledger))
            assert trace._expanded
            assert trace._detail.maximumHeight() > 0
            trace._toggle.click()
            assert not trace._expanded
            assert trace._detail.isHidden()
            trace._toggle.click()
            assert trace._expanded
            assert not trace._detail.isHidden()
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
    chat = main_popup._ChatPage()
    emitted = []
    commands = []
    chat.execution_cancel_requested.connect(emitted.append)
    chat.send_text.connect(commands.append)
    try:
        assert chat.begin_turn(ledger.binding_dict())
        chat.set_tool_status("waiting_tool_result")
        chat._input.setPlainText("status")
        QTest.keyClick(chat._input, Qt.Key.Key_Return)
        assert commands == ["status"]
        assert chat._send_btn.stop_mode
        chat._send_btn.click()
        chat._send_btn.click()
        app.processEvents()

        assert len(emitted) == 1
        assert {
            key: emitted[0][key]
            for key in (
                "conversation_id", "request_id", "turn_id", "run_id",
                "provider_id", "model_id", "effective_reasoning",
            )
        } == ledger.binding_dict()
        assert chat._send_btn.stop_mode
        assert not chat._send_btn.isEnabled()
    finally:
        chat.deleteLater()


def test_committed_stop_closes_thinking_and_late_trace_cannot_reopen(app, popup):
    ledger = _ledger(suffix="committed_stop")
    ledger.record_public_reasoning_summary("Public reasoning is active.")
    binding = ledger.binding_dict()
    popup.refresh_conversations(binding["conversation_id"])
    popup._active_conversation_id = binding["conversation_id"]
    requested = []
    capture_request = requested.append
    popup.chat_execution_cancel_requested.connect(capture_request)
    try:
        popup.set_conversation_busy(binding["conversation_id"], True)
        assert popup.begin_chat_turn(binding)
        assert popup.update_chat_execution_activity(_payload(ledger))
        key = main_popup._ChatPage._binding_key(binding)
        trace = popup._chat._thinking_widgets[key]
        assert not trace.isHidden()

        popup.refresh_conversations("different-conversation")
        assert not popup._chat._send_btn.stop_mode
        popup._on_chat_execution_cancel_requested(binding)
        assert not requested
        popup.refresh_conversations(binding["conversation_id"])
        popup._chat._send_btn.click()
        assert len(requested) == 1

        assert popup.confirm_chat_execution_cancellation(binding)
        app.processEvents()
        assert not trace._expanded
        assert not popup._chat._send_btn.stop_mode
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
        assert not trace._expanded
        assert not trace.can_cancel
        assert popup._chat._send_btn.stop_mode
    finally:
        popup.chat_execution_cancel_requested.disconnect(capture_request)
        popup._busy_conversation_ids.discard(binding["conversation_id"])


def test_busy_clear_retires_exact_turn_before_delayed_activity_can_reactivate(
    app, popup,
):
    first = _ledger(suffix="retired_first")
    second = _ledger(suffix="retired_second")
    conversation_id = first.conversation_id
    second_binding = {
        **second.binding_dict(),
        "conversation_id": conversation_id,
    }
    first_payload = _payload(first)
    popup.refresh_conversations(conversation_id)
    try:
        popup.set_conversation_busy(conversation_id, True)
        assert popup.begin_chat_turn(first.binding_dict())
        assert popup.update_chat_execution_activity(first_payload)

        popup.set_conversation_busy(conversation_id, False)
        first_key = main_popup._ChatPage._binding_key(first.binding_dict())
        assert first_key in popup._retired_thinking_keys
        assert not popup.begin_chat_turn(first.binding_dict())

        assert popup.begin_chat_turn(second_binding)
        assert not popup.update_chat_execution_activity(first_payload)
        assert popup._active_thinking_key_by_conversation[conversation_id] == (
            main_popup._ChatPage._binding_key(second_binding)
        )
    finally:
        popup._busy_conversation_ids.discard(conversation_id)


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
        assert not chat._send_btn.stop_mode
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
        assert traces[0]._detail.toPlainText() == ""
        assert traces[1]._detail.toPlainText() == "Public summary for turn B."
        assert not traces[0].can_cancel
        assert not traces[1].can_cancel
        assert not chat._send_btn.stop_mode
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


def test_markdown_code_and_unicode_final_drains_incrementally(app, monkeypatch):
    chat = main_popup._ChatPage()
    chat._stream._reduced_motion = lambda: False
    binding = _ledger(suffix="markdown_code").binding_dict()
    markdown_renders = []
    original_set_source_text = main_popup._MessageText.set_source_text

    def record_markdown_render(widget, text):
        markdown_renders.append(text)
        original_set_source_text(widget, text)

    monkeypatch.setattr(
        main_popup._MessageText, "set_source_text", record_markdown_render,
    )
    final = (
        "## Résultat ✓\n\n"
        "Une liste :\n- **premier**\n- `deuxième`\n\n"
        "```python\nprint('café 🐈')\n```"
    )
    try:
        assert chat.begin_turn(binding)
        assert chat.append_assistant_stream_chunk(
            "Provisional preview text.", binding,
        )
        chat._stream._on_tick()
        assert chat.finalize_assistant_stream(final, binding)
        _finish_progressive_stream(chat)
        assistant = [
            bubble for bubble in chat.findChildren(main_popup._Bubble)
            if bubble.property("role") == "assistant"
        ]
        assert len(assistant) == 1
        assert assistant[0].text() == final
        assert "café 🐈" in assistant[0]._label.document().toPlainText()
        assert markdown_renders == [final]
        assert chat._stream.active is False
        assert chat._stream.provider_complete is True
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
        assert not trace_b.can_cancel
        assert not chat._send_btn.stop_mode
        assert trace_b._detail.toPlainText() == ""
        assert _bubble_texts(chat).count("NEW_UNIQUE_TOKEN") == 1
    finally:
        chat.deleteLater()
