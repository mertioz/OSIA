"""Offscreen Qt rendering and disclosure checks; no operator input or providers."""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton

from osia import i18n
from osia.execution_trace import ExecutionTraceLedger
from osia.gui.main_popup import _ChatPage, _ChatThinkingTrace
from osia.gui.thinking_orbs import ThinkingOrbs
from test_wor340_thinking import ledger, payload

_APPS = []

@pytest.fixture
def app(monkeypatch):
    value = QApplication.instance() or QApplication([])
    _APPS.append(value)
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    return value


@pytest.mark.parametrize("provider", [
    "chatgpt_account", "deepseek", "openai", "google", "groq",
    "xai", "mistral", "ollama", "anthropic",
])
def test_new_turn_auto_expands_and_same_turn_projection_preserves_manual_choice(app, provider):
    def make_trace(turn):
        return ExecutionTraceLedger(
            conversation_id="conv_v5", request_id=turn, turn_id=turn,
            run_id=turn, provider_id=provider,
            model_id="deepseek-flash" if provider == "deepseek" else "fixture",
            effective_reasoning="auto",
        )

    first = make_trace("turn_v5_a")
    chat = _ChatPage()
    chat.show()
    try:
        assert chat.begin_turn(first.binding_dict())
        widget = chat._thinking_widgets[first.key]
        assert widget._expanded  # Before any provider commentary.
        assert not widget._orbs.isHidden()
        assert widget._toggle.isHidden()
        first.record_public_reasoning_summary("First genuine commentary.")
        assert chat.set_thinking_trace(payload(first))
        app.processEvents()
        assert widget._detail.isVisible()
        assert widget._detail.height() > 0
        widget._orbs.click()
        assert not widget._expanded
        first.record(kind="action", display_key="execution.action.shell")
        first.record_public_reasoning_summary("Second genuine commentary.")
        assert chat.set_thinking_trace(payload(first))
        assert chat.begin_turn(first.binding_dict())  # Accepted/reconnect replay.
        assert chat.set_thinking_trace(payload(first))
        assert not widget._expanded
        widget._orbs.click()
        assert widget._expanded
        assert widget._detail.toPlainText() == "First genuine commentary.\nSecond genuine commentary."
        widget._orbs.click()
        second = make_trace("turn_v5_b")
        assert chat.begin_turn(second.binding_dict())
        next_widget = chat._thinking_widgets[second.key]
        assert next_widget is not widget
        assert next_widget._expanded
        assert next_widget._detail.toPlainText() == ""
        assert not chat.set_thinking_trace(payload(first))
        second.record_public_reasoning_summary("New turn commentary.")
        assert chat.set_thinking_trace(payload(second))
        app.processEvents()
        assert next_widget._detail.isVisible()
        assert next_widget._detail.toPlainText() == "New turn commentary."
    finally:
        chat.close()
        chat.deleteLater()
        app.processEvents()


def test_new_trace_auto_expands_before_first_receipt_and_renders_live(app):
    trace = ledger()
    widget = _ChatThinkingTrace()
    widget.show()
    try:
        widget.apply_trace(payload(trace))
        widget.set_active(True)
        assert widget._expanded
        assert widget._toggle.isHidden()
        assert not widget._orbs.isHidden()
        assert widget._detail.isHidden()
        document = widget._detail.document()
        for text in ("I checked", "I checked the first value."):
            trace.record_public_reasoning_summary(text, source_id="one")
            assert widget.apply_trace(payload(trace))
            app.processEvents()
            assert widget._detail.isVisible()
            assert widget._detail.height() > 0
            assert widget._detail.toPlainText() == text
            assert widget._detail.document() is document
        trace.record_public_reasoning_summary("I checked another value.", source_id="two")
        widget.apply_trace(payload(trace))
        assert widget._detail.toPlainText() == "I checked the first value.\nI checked another value."
        assert widget.apply_trace(payload(trace))
        widget._orbs.click()
        assert widget._detail.isHidden()
        widget.apply_trace(payload(trace))
        assert not widget._expanded
        widget._orbs.click()
        assert widget._detail.isVisible()
        widget.mark_final_stream_started()
        assert widget._toggle.isHidden()  # Draft answer is still active.
        widget._request_cancel()
        widget.set_active(False)  # Pending request is not committed completion.
        assert widget._toggle.isHidden()
        assert widget._expanded
        widget.mark_completed()
        assert widget._detail.isHidden()
        assert widget._orbs.isHidden()
        assert not widget._orbs.is_animating
        assert widget._toggle.text() == i18n.t("chat.thinking.title") + " ▸"
        assert widget._toggle.isVisible()
        widget._toggle.click()
        assert widget._detail.isVisible()
        assert widget._expanded
        assert widget.apply_trace(payload(trace))
        assert widget._expanded
        assert not widget.apply_trace({**payload(trace), "turn_id": "other"})
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_previously_received_text_opens_on_first_keyboard_activation(app):
    trace = ledger()
    trace.record_public_reasoning_summary("Already received commentary.")
    widget = _ChatThinkingTrace()
    widget.show()
    try:
        widget.apply_trace(payload(trace))
        assert isinstance(widget._orbs, QPushButton)
        widget._orbs.setFocus()
        app.processEvents()
        assert widget._orbs.hasFocus()
        assert widget._orbs.isCheckable()
        assert widget._orbs.accessibleName() == i18n.t("chat.thinking.title")
        assert widget._detail.isVisible()  # Received text needs no activation.
        QTest.keyClick(widget._orbs, Qt.Key.Key_Space)
        assert not widget._orbs.isChecked()
        assert widget._detail.isHidden()
        QTest.keyClick(widget._orbs, Qt.Key.Key_Space)
        assert widget._orbs.isChecked()
        assert widget._detail.isVisible()
        assert widget._detail.toPlainText() == "Already received commentary."
        assert widget._orbs.accessibleDescription() == i18n.t("chat.thinking.collapse")
        widget.grab()  # Exercise button/focus painting with no native input.
        widget.mark_completed()
    finally:
        widget.close()
        widget.deleteLater()


def test_orbits_match_position_and_velocity_across_the_loop():
    epsilon = 1e-5
    samples = [ThinkingOrbs.orb_positions(p, 27, 14) for p in (0, math.tau, -epsilon, epsilon, math.tau-epsilon, math.tau+epsilon)]
    for index in range(6):
        assert samples[0][index] == pytest.approx(samples[1][index])
        for coordinate in range(4):
            before = (samples[3][index][coordinate] - samples[2][index][coordinate]) / (2*epsilon)
            after = (samples[5][index][coordinate] - samples[4][index][coordinate]) / (2*epsilon)
            assert before == pytest.approx(after, abs=1e-7)


def test_animation_stops_on_hide_terminal_and_reduced_motion(app, monkeypatch):
    widget = ThinkingOrbs()
    try:
        monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
        widget.show()
        widget.set_active(True)
        app.processEvents()
        assert widget.is_animating
        elapsed = widget._elapsed
        widget.set_active(True)
        assert widget._elapsed is elapsed
        widget.hide()
        assert not widget.is_animating
        widget.show()
        app.processEvents()
        assert widget.is_animating
        monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
        widget.set_active(True)
        assert not widget.is_animating
        widget.grab()
        widget.set_active(False)
        assert not widget.is_animating
    finally:
        widget.close()
        widget.deleteLater()


def test_queued_receipt_mutates_widgets_on_gui_thread(app):
    trace = ledger()
    trace.record_public_reasoning_summary("Queued public text.")

    class Receiver(_ChatThinkingTrace):
        @pyqtSlot(dict)
        def receive(self, value):
            self.received_on_gui = QThread.currentThread() == app.thread()
            self.apply_trace(value)

    class Worker(QThread):
        receipt = pyqtSignal(dict)
        def run(self):
            self.receipt.emit(payload(trace))

    widget = Receiver()
    worker = Worker()
    worker.receipt.connect(widget.receive, Qt.ConnectionType.QueuedConnection)
    widget.show()
    try:
        worker.start()
        assert worker.wait(2000)
        app.processEvents()
        assert widget.received_on_gui
        assert widget._detail.isVisible()
        assert widget._detail.toPlainText() == "Queued public text."
        widget.mark_completed()
    finally:
        worker.wait(2000)
        widget.close()
        widget.deleteLater()
