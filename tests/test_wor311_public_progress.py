import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from osia.agent import (
    OSIAAgent,
    _extract_tool_calls,
    _public_progress_from_tool_response,
    strip_reasoning_blocks,
)
from osia.execution_trace import (
    ExecutionTraceLedger,
    sanitize_public_model_narration,
)
from osia.gui.main_popup import _ChatThinkingTrace
from osia.gui.theme import get_theme_manager, palette_for_key
from osia.gui.thinking_orbs import ThinkingOrbs


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _ledger(*, conversation="conv_a", request="req_a", turn="turn_a", run="run_a", callback=None):
    return ExecutionTraceLedger(
        conversation_id=conversation,
        request_id=request,
        turn_id=turn,
        run_id=run,
        provider_id="deepseek",
        model_id="deepseek-chat",
        effective_reasoning="auto",
        callback=callback,
    )


def _summary_event(ledger, summary, sequence=1, *, terminal=False):
    return {
        "schema_version": 1,
        "event_id": f"trace_{sequence}",
        **ledger.binding_dict(),
        "sequence": sequence,
        "revision": 1,
        "kind": "public_reasoning_summary",
        "display": {"key": "execution.reasoning.summary", "args": {}},
        "timestamp": "2026-10-03T12:00:00+00:00",
        "terminal": terminal,
        "redaction_class": "public_ui",
        "summary": summary,
    }


def test_native_assistant_text_is_projected_beside_preserved_tool_call():
    content = "The application page is open. I’m checking the four remaining forms."
    message = SimpleNamespace(
        content=content,
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(
                    name="open_application",
                    arguments='{"name":"calculator"}',
                ),
            ),
        ],
    )

    calls = _extract_tool_calls(content, message=message, provider="deepseek")
    narration = _public_progress_from_tool_response(content, calls)

    assert calls == [{"tool": "open_application", "name": "calculator"}]
    assert narration == content


@pytest.mark.parametrize("provider", ["groq", "ollama"])
def test_xml_local_and_groq_paths_keep_surrounding_public_text(provider):
    content = (
        "The upload finished. The site needs one confirmation. "
        '<tool_call>{"tool":"open_application","name":"calculator"}'
        "</tool_call> I’ll verify the result after that."
    )

    calls = _extract_tool_calls(content, provider=provider)
    narration = _public_progress_from_tool_response(
        strip_reasoning_blocks(content), calls,
    )

    assert calls == [{"tool": "open_application", "name": "calculator"}]
    assert narration == (
        "The upload finished. The site needs one confirmation. "
        "I’ll verify the result after that."
    )


def test_hidden_reasoning_and_tool_syntax_never_enter_public_narration():
    content = (
        "<think>private deliberation and hidden credentials</think> "
        "The form is ready for confirmation. "
        '<tool_call>{"tool":"open_application","name":"calculator"}'
        "</tool_call>"
    )
    calls = _extract_tool_calls(content, provider="groq")

    narration = _public_progress_from_tool_response(
        strip_reasoning_blocks(content), calls,
    )

    assert narration == "The form is ready for confirmation."
    assert "private deliberation" not in narration
    assert "tool_call" not in narration


def test_no_narration_keeps_tool_turn_working_and_does_not_add_a_message():
    calls = [{"tool": "open_application", "name": "calculator"}]
    ledger = _ledger()
    agent = object.__new__(OSIAAgent)
    agent._execution_trace_ledger = ledger
    agent._execution_generation_by_turn = {}
    agent.messages = [{"role": "user", "content": "Open Calculator."}]

    assert _public_progress_from_tool_response("", calls) == ""
    agent._emit_public_model_narration("")

    assert len(agent.messages) == 1
    assert ledger.snapshot()["events"] == []


def test_public_progress_is_turn_scoped_deduped_and_not_a_chat_message():
    narration = "The upload finished. I’m checking the confirmation state."
    frames = []
    first = _ledger(callback=frames.append)
    event_a = first.record_public_narration(narration)
    duplicate = first.record_public_narration(narration)
    resumed = _ledger()
    event_b = resumed.record_public_narration(narration)

    assert event_a is not None
    assert duplicate is event_a
    assert event_b is not None and event_b.event_id == event_a.event_id
    assert event_a.conversation_id == "conv_a"
    assert event_a.request_id == "req_a"
    assert event_a.turn_id == "turn_a"
    assert event_a.run_id == "run_a"
    assert "role" not in event_a.public_dict()
    assert "content" not in event_a.public_dict()
    assert "tool_attempt_id" not in event_a.public_dict()
    assert "invocation_id" not in event_a.public_dict()
    assert len(first.snapshot()["events"]) == 1
    assert len(frames) == 1

    turn_b = _ledger(conversation="conv_b", request="req_b", turn="turn_b", run="run_b")
    assert turn_b.snapshot()["events"] == []


def test_model_narration_does_not_append_a_canonical_assistant_message():
    ledger = _ledger()
    agent = object.__new__(OSIAAgent)
    agent._execution_trace_ledger = ledger
    agent._execution_generation_by_turn = {}
    agent.messages = [{"role": "user", "content": "Submit the form."}]
    before = list(agent.messages)

    agent._emit_public_model_narration("The form was submitted; I’m checking it.")

    assert agent.messages == before
    assert [event["summary"] for event in ledger.snapshot()["events"]] == [
        "The form was submitted; I’m checking it."
    ]


def test_public_narration_is_bounded_private_data_filtered_and_cancellation_safe():
    assert sanitize_public_model_narration("Planning...") == ""
    assert sanitize_public_model_narration("password: hunter2") == ""
    assert sanitize_public_model_narration("Reach me at person@example.com") == ""
    assert sanitize_public_model_narration("x" * 501) == ""

    bounded = _ledger()
    for index in range(9):
        bounded.record_public_narration(f"A useful stage changed: {index}.")
    assert len(bounded.snapshot()["events"]) == 8

    ledger = _ledger()
    ledger.record(
        kind="terminal",
        display_key="execution.terminal.cancelled",
        terminal=True,
    )
    assert ledger.record_public_narration("The check is complete.") is None


def test_desktop_six_orbs_respect_active_reduced_motion_and_theme(qapp, monkeypatch):
    import osia.gui.thinking_orbs as orb_module

    monkeypatch.setattr(orb_module, "reduced_motion_requested", lambda *_: False)
    widget = ThinkingOrbs()
    widget.show()
    qapp.processEvents()
    widget.set_active(True)
    qapp.processEvents()

    assert widget.orb_count == 6
    assert widget.is_animating
    manager = get_theme_manager()
    original_key = manager.key
    manager.set_theme("azure", persist=False)
    assert widget.accent_color == palette_for_key("azure").color("primary")
    manager.set_theme(original_key, persist=False)

    monkeypatch.setattr(orb_module, "reduced_motion_requested", lambda *_: True)
    widget.set_active(True)
    assert not widget.is_animating
    widget.set_active(False)
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_desktop_thinking_auto_collapses_once_and_can_reopen(qapp, monkeypatch):
    import osia.gui.main_popup as popup_module
    import osia.gui.thinking_orbs as orb_module

    monkeypatch.setattr(popup_module, "reduced_motion_requested", lambda *_: True)
    monkeypatch.setattr(orb_module, "reduced_motion_requested", lambda *_: True)
    ledger = _ledger()
    payload = {
        **ledger.binding_dict(),
        "snapshot": [_summary_event(ledger, "The upload finished.")],
    }
    widget = _ChatThinkingTrace()

    assert widget.apply_trace(payload)
    assert widget._orbs.orb_count == 6
    widget._orbs.clicked.emit()
    assert widget._expanded
    widget.mark_final_stream_started()
    assert not widget._expanded

    widget._toggle.click()
    assert widget._expanded
    terminal = _summary_event(
        ledger,
        "The upload finished.",
        sequence=2,
        terminal=True,
    )
    terminal["kind"] = "terminal"
    terminal["display"] = {"key": "execution.terminal.success", "args": {}}
    terminal.pop("summary")
    updated = {**payload, "terminal": True, "snapshot": [payload["snapshot"][0], terminal]}
    assert widget.apply_trace(updated)
    assert widget._expanded

    widget.close()
    widget.deleteLater()
    qapp.processEvents()
