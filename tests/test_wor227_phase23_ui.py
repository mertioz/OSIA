"""Focused offscreen UI checks for WOR-227 durable lifecycle states."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from osia import i18n
from osia.gui.main_popup import _AutonomousPanel, _ChatPage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def restore_language():
    previous = i18n.get_language()
    try:
        yield
    finally:
        i18n.set_language(previous, persist=False)


def test_chat_waiting_composer_exposes_localized_states_and_bounded_send(app):
    i18n.set_language("fr", persist=False)
    chat = _ChatPage()
    sent = []
    chat.send_text.connect(sent.append)
    try:
        chat.set_busy(True)
        chat.set_tool_status("waiting_tool_result")
        app.processEvents()
        assert chat._input.isEnabled()
        assert not chat._file_btn.isEnabled()
        assert "attente de vérification" in chat._input.placeholderText()

        chat._input.setPlainText("statut")
        app.processEvents()
        assert chat._send_btn.isEnabled()
        chat._send_btn.click()
        assert sent == ["statut"]

        chat._pending_attachments.append(object())
        submitted = []
        chat.submit_draft.connect(lambda *_args: submitted.append(1))
        chat._input.setPlainText("annuler")
        chat._on_send()
        assert sent == ["statut", "annuler"]
        assert submitted == []
        assert len(chat._pending_attachments) == 1

        for status, expected in (
            ("action_starting", "Démarrage"),
            ("verifying", "Vérification"),
            ("resuming", "Reprise"),
            ("cancellation_requested", "Annulation"),
            ("blocked_unknown_effect", "n’a pas pu être établi"),
        ):
            chat.set_tool_status(status)
            app.processEvents()
            assert expected in chat._notice.text()

        chat._input.setPlainText("x" * 161)
        app.processEvents()
        assert not chat._send_btn.isEnabled()
    finally:
        chat.deleteLater()


@pytest.mark.parametrize(
    "event,key_fragment",
    [
        ("action_starting", "Starting"),
        ("action_in_progress", "in progress"),
        ("waiting_tool", "Waiting"),
        ("verifying_tool", "Verifying"),
        ("resuming", "Resuming"),
        ("cancellation_requested", "Cancellation requested"),
        ("blocked_unknown_effect", "could not be established"),
        ("cancelled_proven", "cancelled before"),
    ],
)
def test_autonomous_panel_renders_each_durable_state(app, event, key_fragment):
    i18n.set_language("en", persist=False)
    panel = _AutonomousPanel()
    try:
        panel.apply_progress({"event": event, "done": 0, "total": 1})
        app.processEvents()
        assert key_fragment in panel._prog_status.text()
        assert "attempt_" not in panel._prog_status.text()
    finally:
        panel.deleteLater()
