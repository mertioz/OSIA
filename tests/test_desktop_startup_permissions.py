"""Desktop launch feedback and operating-system network consent."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_entry_can_show_feedback_before_loading_the_chat_engine():
    subprocess.run(
        [sys.executable, "-B", "-c",
         "import sys; import osia.main; assert 'osia.agent' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], check=True,
    )


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_startup_has_a_taskbar_window_with_the_plain_product_name(app):
    from PySide6.QtCore import Qt
    from osia.gui.startup import show_startup
    application, indicator = show_startup()
    try:
        assert application is app and indicator.isVisible()
        assert indicator.windowType() == Qt.WindowType.Window
        assert indicator.windowTitle() == application.applicationDisplayName() == "OSIA"
        assert indicator.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert not indicator.windowIcon().isNull()
    finally:
        indicator.close()
        indicator.deleteLater()


def test_network_review_can_be_requested_again_after_failure(app, monkeypatch):
    from osia.gui import network_permissions as permissions
    calls = []
    def open_settings(program, arguments):
        calls.append((program, arguments))
        return (len(calls) > 1, 42)
    monkeypatch.setattr(permissions, "network_settings_command", lambda: ["settings", "network"])
    monkeypatch.setattr(permissions.QProcess, "startDetached", open_settings)
    panel = permissions.NetworkPermissions()
    try:
        panel.button.click()
        assert not panel.error.isHidden()
        panel.button.click()
        assert panel.error.isHidden()
        assert calls == [("settings", ["network"]), ("settings", ["network"])]
    finally:
        panel.deleteLater()
