"""Button cursors survive clicks, page replacement, styles and enable changes."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton, QVBoxLayout, QWidget

from osia.gui.cursors import ButtonCursorPolicy


APP = QApplication.instance() or QApplication([])


def _drain():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


@pytest.fixture
def root():
    widget = QWidget()
    widget.setLayout(QVBoxLayout())
    widget.resize(400, 300)
    yield widget
    widget.close()
    widget.deleteLater()
    _drain()


def test_pointer_stays_on_successive_buttons_after_click_and_replacement(root):
    first, second = QPushButton("First"), QPushButton("Second")
    root.layout().addWidget(first)
    root.layout().addWidget(second)
    policy = ButtonCursorPolicy(root)
    clicked = []
    first.clicked.connect(lambda: clicked.append("first"))
    second.clicked.connect(lambda: clicked.append("second"))
    root.show()
    APP.processEvents()
    for button in (first, second, first):
        QTest.mouseMove(button, button.rect().center())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    replacement = QPushButton("Replacement")
    replacement.ensurePolished()  # Polished before it belongs to OSIA.
    root.layout().replaceWidget(second, replacement)
    second.deleteLater()
    replacement.show()
    _drain()
    assert replacement.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert clicked == ["first", "second", "first"]
    assert APP.overrideCursor() is None
    assert policy.parent() is root


def test_disabled_controls_and_text_inputs_keep_their_own_cursors(root):
    button, combo, text = QPushButton("Button"), QComboBox(), QLineEdit()
    for control in (button, combo, text):
        root.layout().addWidget(control)
    policy = ButtonCursorPolicy(root)
    root.show()
    APP.processEvents()
    assert combo.cursor().shape() == Qt.CursorShape.PointingHandCursor
    combo.setEditable(True)
    assert combo.lineEdit().cursor().shape() == Qt.CursorShape.IBeamCursor
    assert text.cursor().shape() == Qt.CursorShape.IBeamCursor
    button.setEnabled(False)
    assert button.cursor().shape() == Qt.CursorShape.ArrowCursor
    button.setEnabled(True)
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    outsider = QPushButton("Unrelated window")
    try:
        outsider.ensurePolished()
        outsider.show()
        APP.processEvents()
        assert outsider.cursor().shape() == Qt.CursorShape.ArrowCursor
    finally:
        outsider.close()
        outsider.deleteLater()
    assert policy.parent() is root


def test_main_popup_settings_and_rebuilt_projects_keep_button_cursors(monkeypatch):
    from osia.gui import main_popup

    monkeypatch.setattr(main_popup._SettingsPanel, "_probe_chatgpt_account", lambda *_a, **_k: None)
    monkeypatch.setattr(main_popup._SettingsPanel, "_refresh_startup_status", lambda *_a, **_k: None)
    monkeypatch.setattr(main_popup.MainPopup, "_request_local_models_refresh", lambda *_a: None)
    snapshot = {
        "snapshot_revision": 1,
        "folders": [{"id": "project", "name": "Stage", "collapsed": True}],
        "items": [{"id": "chat", "title": "Chat", "folder_id": "project"}],
    }
    monkeypatch.setattr(main_popup.conversations, "conversation_snapshot", lambda: snapshot)
    popup = main_popup.MainPopup()
    popup._convs._poll_timer.stop()

    def acknowledge(request):
        snapshot["folders"][0]["collapsed"] = request["collapsed"]
        snapshot["snapshot_revision"] += 1

    popup.folder_mutation_requested.connect(acknowledge)
    try:
        popup.show()
        QTest.mouseClick(popup._nav_buttons[popup.PAGE_SETTINGS], Qt.MouseButton.LeftButton)
        APP.processEvents()
        assert popup._settings._close_osia_btn.cursor().shape() == Qt.CursorShape.PointingHandCursor
        assert popup._settings._restart_btn.cursor().shape() == Qt.CursorShape.PointingHandCursor
        QTest.mouseClick(popup._nav_buttons[popup.PAGE_CONVS], Qt.MouseButton.LeftButton)
        APP.processEvents()
        old = popup._convs._folder_widgets["project"]
        QTest.mouseClick(old, Qt.MouseButton.LeftButton)
        APP.processEvents()
        new = popup._convs._folder_widgets["project"]
        assert new is not old
        assert not new._collapsed
        assert new.cursor().shape() == Qt.CursorShape.PointingHandCursor
        assert APP.overrideCursor() is None
    finally:
        popup.close()
        popup.deleteLater()
        _drain()


def test_controls_created_later_inside_a_new_page_get_the_pointer(root):
    policy = ButtonCursorPolicy(root)
    root.show()
    page = QWidget()
    page.setLayout(QVBoxLayout())
    button = QPushButton("New page button")
    page.layout().addWidget(button)
    page.ensurePolished()
    root.layout().addWidget(page)
    page.show()
    APP.processEvents()
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    root.setStyleSheet("QPushButton { border: none; }")
    APP.processEvents()
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert policy.parent() is root
