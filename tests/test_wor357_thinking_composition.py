"""Real Qt message layout across turns, replay and cancellation; no inference."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from osia.execution_trace import ExecutionTraceLedger
from osia.gui.main_popup import _ChatPage, _ChatThinkingTrace, _Bubble
from test_wor340_thinking import payload

_APPS = []

def test_thinking_layout_stream_completion_cancel_and_history(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    app = QApplication.instance() or QApplication([])
    _APPS.append(app)
    chat = _ChatPage()
    chat.show()
    traces = []
    history = []
    def composition():
        result = []
        for index in range(chat._messages_layout.count()):
            widget = chat._messages_layout.itemAt(index).widget()
            if isinstance(widget, _ChatThinkingTrace):
                if not widget.isHidden():
                    result.append("thinking")
            elif widget is not None:
                bubbles = widget.findChildren(_Bubble)
                if bubbles:
                    result.append(bubbles[0].text())
        return result
    try:
        for number in (1, 2):
            trace = ExecutionTraceLedger(
                conversation_id="composition", request_id=f"r{number}",
                turn_id=f"t{number}", run_id=f"run{number}",
                provider_id="deepseek", model_id="deepseek-flash", effective_reasoning="high",
            )
            traces.append(trace)
            question, answer = f"Question {number}", f"Answer {number}"
            chat.append_message("user", question)
            assert chat.begin_turn(trace.binding_dict())
            trace.record_public_reasoning_summary(f"Public commentary {number}.")
            chat.set_thinking_trace(payload(trace))
            original = chat._thinking_widgets[trace.key]
            assert chat.append_assistant_stream_chunk(answer, trace.binding_dict())
            app.processEvents()
            assert composition()[-3:] == [question, "thinking", answer]
            if number == 2:
                chat.cancel_assistant_stream(flush=True)
                chat.tombstone_cancelled_turn(trace.binding_dict())
            else:
                chat.finalize_assistant_stream(answer, trace.binding_dict())
            app.processEvents()
            assert chat._thinking_widgets[trace.key] is original
            assert composition()[-3:] == [question, "thinking", answer]
            # Replayed snapshots update the same widget, never append a second.
            chat.set_thinking_trace(payload(trace), historical=True)
            assert chat._thinking_widgets[trace.key] is original
            history.extend([
                {"role": "user", "content": question, "_execution_trace_v1": payload(trace)},
                {"role": "assistant", "content": answer, "_execution_trace_v1": payload(trace)},
            ])
        chat.set_messages(history)
        app.processEvents()
        assert composition() == ["Question 1", "thinking", "Answer 1", "Question 2", "thinking", "Answer 2"]
        assert len(chat._thinking_widgets) == 2
        for number, widget in enumerate(chat._thinking_widgets.values(), 1):
            assert widget._detail.toPlainText() == f"Public commentary {number}."
        chat.set_messages([
            {"role": "user", "content": "Legacy question"},
            {"role": "assistant", "content": "Legacy answer"},
        ])
        app.processEvents()
        assert composition() == ["Legacy question", "Legacy answer"]

        empty_trace = ExecutionTraceLedger(
            conversation_id="composition", request_id="r3", turn_id="t3",
            run_id="run3", provider_id="deepseek", model_id="deepseek-flash",
            effective_reasoning="high",
        )
        binding = empty_trace.binding_dict()
        chat.append_message("user", "Question 3")
        assert chat.begin_turn(binding)
        widget = chat._thinking_widgets[empty_trace.key]
        assert not widget._orbs.isHidden()
        assert chat.finalize_assistant_stream("Answer 3", binding)
        app.processEvents()
        assert widget.isHidden()
        assert widget._toggle.isHidden()
        assert composition()[-2:] == ["Question 3", "Answer 3"]
    finally:
        chat.close()
        chat.deleteLater()
        app.processEvents()
