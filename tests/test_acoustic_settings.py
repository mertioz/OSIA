"""The microphone opt-in is visible, reversible, and unavailable assets are safe."""
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication
import pytest

from osia.gui import main_popup
from osia.voice import wake


class DormantThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


@pytest.fixture
def panel(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_popup, "threading", SimpleNamespace(Thread=DormantThread))
    monkeypatch.setattr(wake, "verifier_available", lambda: True)
    widget = main_popup._SettingsPanel()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_wake_switch_persists_and_emits_both_on_and_off(panel, monkeypatch):
    writes, signals = [], []
    monkeypatch.setattr(main_popup, "set_setting", lambda *args: writes.append(args))
    panel.wake_word_enabled_changed.connect(signals.append)
    assert panel._wake_word_toggle.isEnabled()
    panel._wake_word_toggle.setChecked(True)
    panel._wake_word_toggle.setChecked(False)
    assert writes == [("wake_word_enabled", True), ("wake_word_enabled", False)]
    assert signals == [True, False]


def test_missing_backend_prevents_microphone_opt_in(panel, monkeypatch):
    monkeypatch.setattr(wake, "verifier_available", lambda: False)
    panel._load_state()
    assert not panel._wake_word_toggle.isEnabled()
    assert not panel._wake_word_toggle.isChecked()
