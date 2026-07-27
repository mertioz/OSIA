"""Focused regressions for the embedded WOR-196 Permissions page."""

from __future__ import annotations

import inspect

from PyQt6.QtWidgets import (
    QApplication, QDialog, QLabel, QPushButton, QSizePolicy, QWidget,
)

from osia import action_policy
from osia.action_policy import PermissionPolicy
from osia.gui.main_popup import _SettingsPanel
from osia.gui.permissions_center import (
    FullAccessActivationService,
    PermissionsPage,
)
from osia.local_reauth import (
    ReauthResult,
    ReauthStatus,
    WindowsHelloReauthProvider,
)
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    SecureStorageBackend,
)


_QT_APP = QApplication.instance() or QApplication([])


class _ProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "gui_test", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"GUI" + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"GUI"):
            raise ValueError("corrupt")
        return protected[3:][::-1]


class _SuccessReauth:
    def reauthenticate(self, prompt: str):
        assert prompt
        return ReauthResult(ReauthStatus.SUCCESS, "deterministic_test")


class _RecordingWindowsHello(WindowsHelloReauthProvider):
    def __init__(self):
        self.window_handle = None

    def reauthenticate(self, prompt: str, *, window_handle=None):
        assert prompt
        self.window_handle = window_handle
        return ReauthResult(ReauthStatus.SUCCESS, "windows_hello")


def _policy(tmp_path):
    return PermissionPolicy(PermissionStore(
        tmp_path / "permissions.dat", backend=_ProtectedBackend(),
    ))


def _page(tmp_path):
    return PermissionsPage(
        policy=_policy(tmp_path), reauth_provider=_SuccessReauth(),
    )


def _visible_text(widget: QWidget) -> str:
    labels = [
        child.text()
        for child in widget.findChildren(QLabel)
        if child.isVisibleTo(widget)
    ]
    buttons = [
        child.text()
        for child in widget.findChildren(QPushButton)
        if child.isVisibleTo(widget)
    ]
    return " ".join(labels + buttons)


def test_page_is_embedded_widget_without_dialog_or_private_palette():
    source = inspect.getsource(__import__(
        "osia.gui.permissions_center", fromlist=["PermissionsPage"],
    ))
    assert issubclass(PermissionsPage, QWidget)
    assert not issubclass(PermissionsPage, QDialog)
    assert "QDialog" not in source
    assert "setWindowTitle" not in source
    assert "setStyleSheet" not in source
    assert "#000" not in source and "#ff" not in source.lower()


def test_permissions_content_is_in_existing_settings_stack(monkeypatch, tmp_path):
    policy = _policy(tmp_path)
    monkeypatch.setattr(action_policy, "get_default_permission_policy", lambda: policy)
    panel = _SettingsPanel()
    page = panel._permissions_page
    assert isinstance(page, PermissionsPage)
    assert page.window() is panel.window()
    assert panel._settings_pages[
        panel._settings_nav.currentRow()
    ][0] == "settings.cat.general"
    panel.show_category("settings.cat.permissions")
    assert panel._settings_stack.currentWidget().isAncestorOf(page)
    assert not hasattr(panel, "_permissions_open")
    assert not hasattr(panel, "_permissions_dialog")
    assert panel.findChildren(QDialog) == []
    panel.deleteLater()


def test_default_preset_turns_every_switch_off_and_sets_ask(tmp_path):
    page = _page(tmp_path)
    page._switches["files_delete"].setChecked(True)
    assert page._switches["files_delete"].isChecked()
    page._default_preset.click()
    snapshot = page._snapshot
    assert snapshot["permission_mode"] == "ask"
    assert not any(snapshot["automatic_categories"].values())
    assert page._default_preset.isChecked()
    assert not page._full_preset.isChecked()
    page.deleteLater()


def test_full_preset_changes_nothing_until_complete_inline_ceremony(tmp_path):
    page = _page(tmp_path)
    top_levels = set(QApplication.topLevelWidgets())
    page._full_preset.click()
    assert not page._ceremony.isHidden()
    assert not any(page._snapshot["automatic_categories"].values())
    assert page._confirm_activation.isEnabled() is False
    page._ack.setChecked(True)
    assert page._confirm_activation.isEnabled() is True
    assert not any(page._snapshot["automatic_categories"].values())
    page._confirm_activation.click()
    assert page._snapshot["permission_mode"] == "full_access"
    assert all(page._snapshot["automatic_categories"].values())
    assert page._full_preset.isChecked()
    assert all(
        toggle.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
        for toggle in page._switches.values()
    )
    assert set(QApplication.topLevelWidgets()) == top_levels
    page.deleteLater()


def test_mixed_switches_highlight_neither_preset_and_never_show_custom(tmp_path):
    page = _page(tmp_path)
    page._switches["files_delete"].setChecked(True)
    assert page._snapshot["automatic_categories"]["files_delete"] is True
    assert not page._default_preset.isChecked()
    assert not page._full_preset.isChecked()
    page.show()
    _QT_APP.processEvents()
    assert "custom" not in _visible_text(page).lower()
    page.hide()
    page.deleteLater()


def test_manual_all_on_highlights_full_card_but_keeps_backend_ask(tmp_path):
    page = _page(tmp_path)
    for toggle in page._switches.values():
        toggle.setChecked(True)
    assert all(page._snapshot["automatic_categories"].values())
    assert page._full_preset.isChecked()
    assert page._snapshot["permission_mode"] == "ask"
    page.deleteLater()


def test_disable_is_immediate_and_invalidates_full_access(tmp_path):
    page = _page(tmp_path)
    outcome = page.activation_service.activate(
        acknowledged=True, second_confirmed=True,
    )
    assert outcome.activated is True
    page.refresh()
    epoch = page._snapshot["policy_epoch"]
    page._switches["files_delete"].setChecked(False)
    assert page._snapshot["automatic_categories"]["files_delete"] is False
    assert page._snapshot["permission_mode"] == "ask"
    assert page._snapshot["policy_epoch"] == epoch + 1
    page.deleteLater()


def test_advanced_is_collapsed_and_projection_contains_no_private_values(tmp_path):
    page = _page(tmp_path)
    assert page._advanced.isHidden()
    sentinel = "WOR196_PRIVATE_UI_SENTINEL"
    page.policy.store.update_full_access_constraints(
        payment_enabled=False,
        max_amount_minor=0,
        currency="EUR",
        destinations_enabled=True,
        origins=["https://private.example"],
        connectors=[sentinel],
    )
    page.refresh()
    page._advanced_toggle.setChecked(True)
    page.show()
    _QT_APP.processEvents()
    rendered = _visible_text(page)
    rendered += " " + " ".join(
        page._journal.item(row, column).text()
        for row in range(page._journal.rowCount())
        for column in range(page._journal.columnCount())
        if page._journal.item(row, column) is not None
    )
    assert sentinel not in rendered
    assert "private.example" not in rendered
    for forbidden in (
        "policy epoch", "schema version", "grant_ref", "scope_hash",
        "private_canonical", "normalized scope",
    ):
        assert forbidden not in rendered.lower()
    page.hide()
    page.deleteLater()


def test_activation_service_passes_window_handle_to_windows_hello(tmp_path):
    policy = _policy(tmp_path)
    provider = _RecordingWindowsHello()
    outcome = FullAccessActivationService(
        policy=policy, reauth_provider=provider,
    ).activate(
        acknowledged=True, second_confirmed=True, window_handle=12345,
    )
    assert outcome.activated is True
    assert provider.window_handle == 12345
