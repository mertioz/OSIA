"""WOR-151 deterministic first-install onboarding regressions."""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QLabel

from osia import i18n
from osia import onboarding
from osia.gui import main_popup
from osia.gui.onboarding import OnboardingDialog


ROOT = Path(__file__).resolve().parent.parent
_QT_APP = QApplication.instance() or QApplication([])


def test_clean_install_is_claimed_once_and_restart_does_not_reopen(tmp_path):
    marker = tmp_path / "profile" / "onboarding.json"
    assert onboarding.create_fresh_install_marker(marker)
    assert onboarding.onboarding_state(marker)["status"] == "pending"
    assert onboarding.claim_automatic_onboarding(marker)
    assert onboarding.onboarding_state(marker)["status"] == "presenting"
    assert not onboarding.claim_automatic_onboarding(marker)


def test_existing_users_and_updates_are_not_opted_in(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text("{}", encoding="utf-8")
    marker = profile / "onboarding.json"
    assert not onboarding.create_fresh_install_marker(marker)
    assert not marker.exists()
    assert not onboarding.claim_automatic_onboarding(marker)
    assert onboarding.onboarding_state(marker)["outcome"] == "migrated"


@pytest.mark.parametrize("outcome", ["completed", "skipped", "closed"])
def test_handled_outcomes_persist_across_version_changes(tmp_path, outcome):
    marker = tmp_path / "onboarding.json"
    onboarding.mark_onboarding_handled(outcome, marker)
    state = onboarding.onboarding_state(marker)
    assert state == {
        "schemaVersion": onboarding.ONBOARDING_STATE_VERSION,
        "status": "handled",
        "outcome": outcome,
    }
    assert not onboarding.claim_automatic_onboarding(marker)


def test_installer_marks_only_fresh_non_update_profiles():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    batch = (ROOT / "install.bat").read_text("utf-8")
    assert "FreshInstallOnboardingEligible" in script
    assert "not DirExists(ExpandConstant('{userprofile}\\.osia'))" in script
    assert "not RegKeyExists(HKCU, UninstallKey)" in script
    assert "FreshInstallOnboardingEligible and (not IsUpdateMode)" in script
    assert "onboarding.json" in script
    assert "OSIA_HAD_STATE" in batch
    assert 'if exist "%VENV_PY%" set "OSIA_HAD_STATE=1"' in batch
    assert "create_fresh_install_marker" in batch


def test_frozen_smoke_covers_embedded_onboarding_and_release_notes():
    launcher = (
        ROOT / "packaging" / "windows" / "launcher.py"
    ).read_text(encoding="utf-8")
    assert "create_fresh_install_marker(state_path)" in launcher
    assert 'mark_onboarding_handled("completed", state_path)' in launcher
    assert "release_for_version(metadata.version)" in launcher
    assert "OnboardingDialog(automatic=False)" in launcher


def _all_visible_copy(dialog: OnboardingDialog) -> str:
    return " ".join(label.text() for label in dialog.findChildren(QLabel)).lower()


def test_four_pages_controls_routes_and_no_technical_jargon(tmp_path):
    dialog = OnboardingDialog(automatic=True, state_path=tmp_path / "state.json")
    answers = QSignalSpy(dialog.answer_settings_requested)
    phones = QSignalSpy(dialog.phone_settings_requested)
    handled = QSignalSpy(dialog.handled)
    assert dialog.PAGE_COUNT == 4
    assert dialog._stack.count() == 4
    assert dialog._skip.text() == "Skip"
    assert dialog._back.text() == "Back"
    assert dialog._next.text() == "Next"
    copy = _all_visible_copy(dialog)
    for jargon in ("api", "provider", "localhost", "server", "port", "websocket", "token"):
        assert jargon not in copy

    QTest.mouseClick(dialog._chatgpt, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog._other_answer, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog._open_phone, Qt.MouseButton.LeftButton)
    assert [list(item) for item in answers] == [[True], [False]]
    assert len(phones) == 1

    dialog._stack.setCurrentIndex(3)
    assert dialog._next.text() == "Start using OSIA"
    QTest.mouseClick(dialog._next, Qt.MouseButton.LeftButton)
    assert [list(item) for item in handled] == [["completed"]]
    assert onboarding.onboarding_state(tmp_path / "state.json")["outcome"] == "completed"


def test_skip_persists_and_french_copy_is_available(tmp_path):
    marker = tmp_path / "state.json"
    dialog = OnboardingDialog(automatic=True, state_path=marker)
    try:
        i18n.set_language("fr", persist=False)
        assert dialog._skip.text() == "Passer"
        assert dialog._next.text() == "Suivant"
        assert "bienvenue dans osia" in _all_visible_copy(dialog)
        QTest.mouseClick(dialog._skip, Qt.MouseButton.LeftButton)
        assert onboarding.onboarding_state(marker)["outcome"] == "skipped"
    finally:
        i18n.set_language("en", persist=False)


class _DormantThread:
    def __init__(self, *args, target=None, **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)

    def start(self):
        return


def test_manual_settings_action_reopens_without_resetting_configuration(monkeypatch):
    monkeypatch.setattr(
        main_popup, "threading", types.SimpleNamespace(Thread=_DormantThread),
    )
    writes = []
    panel = main_popup._SettingsPanel()
    spy = QSignalSpy(panel.getting_started_requested)
    monkeypatch.setattr(main_popup, "set_setting", lambda *args: writes.append(args))
    QTest.mouseClick(panel._guide_btn, Qt.MouseButton.LeftButton)
    assert len(spy) == 1
    assert writes == []
    panel.show_answer_settings(chatgpt=True)
    assert panel._settings_pages[panel._settings_nav.currentRow()][0] == "settings.cat.models"
    assert panel._ai_provider_combo.currentData() == "chatgpt_account"
    panel.show_mobile_settings()
    assert panel._settings_pages[panel._settings_nav.currentRow()][0] == "settings.cat.mobile"
    panel.deleteLater()
    _QT_APP.processEvents()


def test_manual_dialog_does_not_create_or_change_marker(tmp_path):
    marker = tmp_path / "missing.json"
    dialog = OnboardingDialog(automatic=False, state_path=marker)
    dialog._stack.setCurrentIndex(3)
    QTest.mouseClick(dialog._next, Qt.MouseButton.LeftButton)
    assert not marker.exists()
