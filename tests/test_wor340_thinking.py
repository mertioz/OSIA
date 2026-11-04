"""Honest public Thinking projection and exact-turn disclosure regressions."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from osia import i18n
from osia.agent import _public_progress_from_tool_response, strip_reasoning_blocks
from osia.agent import OSIAAgent
from osia.execution_trace import ExecutionTraceLedger, MAX_LIVE_FRAMES, validate_persisted_trace
from osia.gui.main_popup import _ChatPage, _ChatThinkingTrace

_LIVE_APPS = []

@pytest.fixture(scope="module")
def app():
    value = QApplication.instance() or QApplication([])
    _LIVE_APPS.append(value)
    return value


def ledger(callback=None):
    return ExecutionTraceLedger(
        conversation_id="conv340", request_id="req340", turn_id="turn340",
        run_id="run340", provider_id="deepseek", model_id="deepseek-flash",
        effective_reasoning="high", callback=callback,
    )


def payload(trace):
    return {**trace.binding_dict(), "snapshot": trace.snapshot()["events"]}


def test_public_summary_stream_survives_activity_budget_and_keeps_terminal():
    frames = []
    trace = ledger(frames.append)
    for index in range(MAX_LIVE_FRAMES + 2):
        trace.record(kind="action", display_key="execution.action.shell")
    for index in range(40):
        trace.record_public_reasoning_summary("I checked " + "a" * index)
    assert frames[-1]["summary"] == "I checked " + "a" * 39
    assert frames[-1]["revision"] == 40
    trace.record(kind="terminal", display_key="execution.terminal.success", terminal=True)
    assert frames[-1]["terminal"]
    assert trace.record_public_reasoning_summary("Late commentary") is None


def test_no_displayable_content_does_not_invent_thoughts(app):
    trace = ledger()
    trace.record(kind="goal", display_key="execution.goal.complete_task")
    trace.record(kind="action", display_key="execution.action.preparing")
    trace.record(kind="reconciliation", display_key="execution.reconciliation.verifying")
    widget = _ChatThinkingTrace()
    try:
        assert widget.apply_trace(payload(trace))
        assert widget._expanded
        assert not widget._detail.toPlainText()
        trace.record(kind="terminal", display_key="execution.terminal.success", terminal=True)
        assert widget.apply_trace(payload(trace))
        widget.set_active(False)
        assert not widget.isHidden()  # One disclosure remains, without invented prose.
        assert not widget._detail.toPlainText()
    finally:
        widget.deleteLater()


def test_history_stays_collapsed_and_activity_is_not_model_text(app):
    trace = ledger()
    wording = "I checked  both visible values."
    trace.record_public_reasoning_summary(wording)
    trace.record(kind="action", display_key="execution.action.shell")
    widget = _ChatThinkingTrace(auto_expand=False)
    try:
        assert widget.apply_trace(payload(trace))
        assert not widget._expanded
        assert widget._detail.toPlainText() == wording
        widget._toggle.click()
        assert widget._expanded
        malformed = payload(trace)
        malformed["snapshot"][1]["display"]["args"] = {"command": "private"}
        safe = widget._safe_payload(malformed)
        assert safe["lines"] == [wording]
    finally:
        widget.deleteLater()


@pytest.mark.parametrize("failure", [False, True])
def test_canonical_completion_collapses_even_without_terminal_trace(app, failure):
    trace = ledger()
    trace.record_public_reasoning_summary("I checked the visible values.")
    chat = _ChatPage()
    chat._stream._reduced_motion = lambda: True
    try:
        binding = trace.binding_dict()
        assert chat.begin_turn(binding)
        assert chat.set_thinking_trace(payload(trace))
        widget = chat._thinking_widgets[trace.key]
        assert widget._expanded
        assert chat.append_assistant_stream_chunk("A draft", binding)
        assert widget._expanded
        finish = chat.fail_assistant_stream if failure else chat.finalize_assistant_stream
        assert finish("The unchanged final answer.", binding)
        assert not widget._expanded
        assert not chat._send_btn.stop_mode
        widget._toggle.click()
        assert widget._expanded
        assert widget.apply_trace(payload(trace))
        widget.set_active(True)
        assert widget._expanded
        assert not widget.can_cancel
    finally:
        chat.deleteLater()


@pytest.mark.parametrize("text", [
    "Working...", "<think>private</think>", "Bearer fake",
    "<｜DSML｜invoke>private</｜DSML｜invoke>",
])
def test_hidden_or_unsafe_text_is_not_manufactured_into_commentary(text):
    calls = [{"tool": "read_file"}]
    assert _public_progress_from_tool_response(strip_reasoning_blocks(text), calls) == ""


def test_final_answer_is_not_reclassified_as_progress():
    assert _public_progress_from_tool_response("The final answer.", []) == ""


def test_provider_designated_summary_still_rejects_private_protocol_tokens():
    trace = ledger()
    assert trace.record_public_reasoning_summary("<|im_start|>analysis") is None
    assert trace.record_public_reasoning_summary("<｜DSML｜invoke>private") is None


def test_empty_new_turn_is_expanded_and_manual_close_survives_content(app):
    trace = ledger()
    chat = _ChatPage()
    try:
        assert chat.begin_turn(trace.binding_dict())
        widget = chat._thinking_widgets[trace.key]
        assert widget._expanded
        assert widget._toggle.isEnabled()
        widget._toggle.click()
        trace.record_public_narration("Checking the current status before opening it.")
        assert chat.set_thinking_trace(payload(trace))
        assert not widget._expanded
        assert widget._detail.toPlainText() == "Checking the current status before opening it."
    finally:
        chat.deleteLater()


def test_native_provider_rounds_append_and_older_aggregate_cannot_erase_text():
    trace = ledger()
    agent = object.__new__(OSIAAgent)
    agent._execution_trace_ledger = trace
    base = {name: value for name, value in trace.binding_dict().items()
            if name != "effective_reasoning"}
    first = {**base, "native_thread_id": "thread", "native_turn_id": "round1",
             "summary": "Checking the current status."}
    agent._emit_provider_public_reasoning_summary(first)
    agent._emit_provider_public_reasoning_summary({**first, "summary": first["summary"] + " Comparing both values."})
    agent._emit_provider_public_reasoning_summary(first)  # stale aggregate
    agent._emit_provider_public_reasoning_summary({**first, "native_turn_id": "round2", "summary": "Opening the requested app."})
    events = trace.snapshot()["events"]
    assert [event["summary"] for event in events] == [
        "Checking the current status. Comparing both values.", "Opening the requested app.",
    ]
    assert events[0]["event_id"] != events[1]["event_id"]
    assert events[0]["revision"] == 2


def test_tool_only_and_compact_final_snapshots_never_replace_commentary(app):
    trace = ledger()
    wording = "Checking  the visible status before opening the requested app."
    trace.record_public_narration(wording)
    widget = _ChatThinkingTrace()
    try:
        assert widget.apply_trace(payload(trace))
        trace.record(kind="action", display_key="execution.action.shell")
        partial = payload(trace)
        partial["snapshot"] = partial["snapshot"][-1:]
        assert widget.apply_trace(partial)
        assert widget._detail.toPlainText() == wording
        trace.record(kind="terminal", display_key="execution.terminal.success", terminal=True)
        partial = payload(trace)
        partial["snapshot"] = partial["snapshot"][-1:]
        assert widget.apply_trace(partial)
        assert not widget._expanded
        widget._toggle.click()
        assert widget._expanded
        assert widget._detail.toPlainText() == wording
        widget.retranslate()
        assert widget._detail.toPlainText() == wording
    finally:
        widget.deleteLater()


def test_commentary_survives_compaction_persistence_and_qt_history_rerender(app, tmp_path):
    import json

    trace = ledger()
    wording = "A public model statement with original spacing. " * 100
    trace.record_public_narration(wording.rstrip())
    for index in range(35):
        trace.record_public_narration(f"Checked the public value for item {index}.")
        trace.record(kind="action", display_key="execution.action.capability")
    trace.record(kind="terminal", display_key="execution.terminal.success", terminal=True)
    canonical = trace.snapshot()
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(canonical), encoding="utf-8")
    restored = validate_persisted_trace(json.loads(path.read_text(encoding="utf-8")))
    assert restored == canonical
    summaries = [event["summary"] for event in restored["events"] if "summary" in event]
    assert len(summaries) == 36
    assert summaries[0] == wording.rstrip()
    assert restored["events"][-1]["terminal"]
    chat = _ChatPage()
    try:
        for _ in range(2):
            chat.set_messages([{"role": "assistant", "content": "The final answer.", "_execution_trace_v1": restored}])
            widget = chat._thinking_widgets[trace.key]
            assert not widget._expanded
            widget._toggle.click()
            assert widget._detail.toPlainText() == "\n".join(summaries)
    finally:
        chat.deleteLater()


@pytest.mark.parametrize("display_key", [
    "execution.action.browser", "execution.action.document", "execution.action.filesystem",
    "execution.action.shell", "execution.action.connector", "execution.action.memory",
    "execution.action.scheduled_task", "execution.action.capability",
    "execution.result.completed", "execution.result.observed", "execution.result.needs_attention",
])
def test_structured_facts_never_generate_thinking_sentences(display_key):
    trace = ledger()
    trace.record(kind="action" if ".action." in display_key else "result", display_key=display_key)
    assert trace.snapshot()["events"]  # diagnostics remain available
    assert _ChatThinkingTrace._safe_payload(payload(trace))["lines"] == []


def test_thinking_detail_style_is_flat_and_keeps_font_size():
    import inspect
    import re
    from osia.gui import main_popup

    source = inspect.getsource(main_popup)
    detail_style = re.search(r"QTextEdit#chatThinkingDetail \{([^}]+)\}", source).group(1)
    assert "background: transparent;" in detail_style
    assert "border: none;" in detail_style
    assert "font-size: 10px;" in detail_style
    assert "border-left" not in detail_style


def test_provider_public_summary_is_not_cut_at_old_display_or_input_limits():
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt

    client = AppServerClient()
    attempt = _TurnAttempt(thread_id="thread", turn_id="native_turn")
    wording = "Checking the current public status. " * 400
    client._capture_public_reasoning_summary(attempt, "item/reasoning/summaryTextDelta", {
        "itemId": "public_summary", "summaryIndex": 0, "delta": wording,
    })
    assert client._public_reasoning_summary(attempt) == wording.rstrip()


def test_stop_tap_does_not_collapse_until_cancellation_is_committed(app):
    trace = ledger()
    trace.record_public_narration("Checking the public status.")
    chat = _ChatPage()
    try:
        assert chat.begin_turn(trace.binding_dict())
        assert chat.set_thinking_trace(payload(trace))
        widget = chat._thinking_widgets[trace.key]
        chat._send_btn.click()
        assert widget._expanded
        assert chat.tombstone_cancelled_turn(trace.binding_dict())
        assert not widget._expanded
        assert not widget.isHidden()
        widget._toggle.click()
        assert widget._expanded
        assert widget._detail.toPlainText() == "Checking the public status."
    finally:
        chat.deleteLater()


def test_desktop_mobile_bridge_is_public_correlated_and_ordered(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from osia.server import server
    from osia.durable_transport import DurableTransportStore

    trace = ledger()
    frames = []
    trace._callback = frames.append
    trace.record_public_narration("Checking the current public status.")
    trace.record(kind="terminal", display_key="execution.terminal.success", terminal=True)
    monkeypatch.setattr("osia.turn_terminality.load_persisted_execution_binding", lambda _: trace.binding_dict())
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    received = []

    class Client:
        async def send_json(self, frame):
            received.append(frame)

    monkeypatch.setattr(server, "_WS_CLIENTS", {Client()})

    async def request(frame, *, host="127.0.0.1", announce=False):
        async def json_body():
            return {"frame": frame, "announce": announce}
        return await server.internal_execution_trace(SimpleNamespace(
            client=SimpleNamespace(host=host), json=json_body,
        ))

    async def exercise():
        assert (await request(frames[0], host="203.0.113.1"))["error"] == "forbidden"
        unsafe = {**frames[0], "raw_reasoning": "hidden"}
        assert (await request(unsafe))["error"] == "invalid_trace"
        stale = {**frames[0], "model_id": "stale"}
        assert not (await request(stale))["ok"]
        assert (await request(frames[0], announce=True))["ok"]
        assert (await request(frames[1]))["ok"]

    asyncio.run(exercise())
    assert [frame["type"] for frame in received] == [
        "turn_accepted", "execution_trace", "execution_trace", "turn_status",
    ]
    assert received[1]["summary"] == "Checking the current public status."
    assert received[-1]["status"] == "done"
    assert received[-1]["terminal"]
    assert all(frame["run_id"] == trace.run_id for frame in received)


def test_desktop_forwarder_sends_once_announces_once_and_preserves_fifo(monkeypatch):
    import json
    from osia import main

    frames = []
    trace = ledger(frames.append)
    trace.record_public_narration("Checking the public status.")
    trace.record(kind="terminal", display_key="execution.terminal.cancelled", terminal=True)
    delivered = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self):
            return b'{"ok": true}'

    def open_request(request, **_):
        delivered.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    monkeypatch.setattr(main, "_MOBILE_TRACE_FORWARDER", None)
    monkeypatch.setattr(main, "_MOBILE_TRACE_ANNOUNCED", {})
    try:
        futures = [main._notify_mobile_execution_trace(frame) for frame in frames]
        for future in futures:
            future.result(timeout=2)
        assert [body["frame"]["event_id"] for body in delivered] == [frame["event_id"] for frame in frames]
        assert [body["announce"] for body in delivered] == [True, False]
        assert main._notify_mobile_execution_trace({"type": "execution_trace", "summary": "password: unsafe"}) is None
    finally:
        if main._MOBILE_TRACE_FORWARDER is not None:
            main._MOBILE_TRACE_FORWARDER.shutdown(wait=True)
