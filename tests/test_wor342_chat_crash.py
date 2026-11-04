"""Provider-independent regressions for Windows Qt callback crash causes."""
import io
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from osia.gui.main_popup import _ChatPage, _ChatThinkingTrace
from osia.utils.diagnostics import diagnostic_print

_LIVE_APPS = []


class BrokenOutput:
    def __init__(self, error):
        self.error = error

    def write(self, value):
        raise self.error("unusable diagnostic sink")

    def flush(self):
        raise self.error("unusable diagnostic sink")


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_broken_console_falls_back_and_both_broken_sinks_are_safe(monkeypatch, error):
    fallback = io.StringIO()
    monkeypatch.setattr(sys, "stdout", BrokenOutput(error))
    monkeypatch.setattr(sys, "stderr", fallback)
    diagnostic_print("diagnostic", flush=True)
    assert fallback.getvalue() == "diagnostic\n"
    monkeypatch.setattr(sys, "stderr", BrokenOutput(error))
    diagnostic_print("diagnostic", flush=True)
    monkeypatch.setattr(sys, "stderr", None)
    diagnostic_print("diagnostic", flush=True)


def test_diagnostic_print_keeps_normal_print_behavior():
    target = io.StringIO()
    diagnostic_print("one", "two", sep="/", end="!", file=target, flush=True)
    assert target.getvalue() == "one/two!"


def test_begin_turn_activity_and_terminal_projection_do_not_raise():
    app = QApplication.instance() or QApplication([])
    _LIVE_APPS.append(app)
    page = _ChatPage()
    widget = _ChatThinkingTrace()
    binding = {
        "conversation_id": "crash-conv", "request_id": "crash-request",
        "turn_id": "crash-turn", "run_id": "crash-run",
        "provider_id": "deepseek", "model_id": "deepseek-flash",
        "effective_reasoning": "high",
    }
    try:
        widget.set_active(True)
        assert not widget.can_cancel
        assert widget.apply_trace({**binding, "snapshot": []})
        widget.set_active(True)
        assert widget.can_cancel
        widget._cancel_pending = True
        widget.set_active(True)
        assert not widget.can_cancel
        widget._cancel_pending = False
        widget._binding["terminal"] = True
        widget.set_active(True)
        assert not widget.can_cancel
        widget.mark_inactive()
        page.begin_turn(binding)
        assert page._thinking_widgets
        app.processEvents()
    finally:
        page.close()
        widget.close()
        page.deleteLater()
        widget.deleteLater()
