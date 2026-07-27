"""Focused regressions for the embedded WOR-196 Permissions page."""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QWidget,
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


class _OutcomeReauth:
    def __init__(self, status: ReauthStatus, reason_code: str = ""):
        self.status = status
        self.reason_code = reason_code or status.value

    def reauthenticate(self, prompt: str):
        assert prompt
        method = (
            "unavailable"
            if self.status is ReauthStatus.UNAVAILABLE
            else "deterministic_test"
        )
        return ReauthResult(self.status, method, self.reason_code)


class _RecordingWindowsHello(WindowsHelloReauthProvider):
    def __init__(self):
        self.window_handle = None

    def reauthenticate(
        self, prompt: str, *, window_handle=None, cancellation_event=None,
    ):
        assert prompt
        assert cancellation_event is None or not cancellation_event.is_set()
        self.window_handle = window_handle
        return ReauthResult(ReauthStatus.SUCCESS, "windows_hello")


def _policy(tmp_path):
    return PermissionPolicy(PermissionStore(
        tmp_path / "permissions.dat", backend=_ProtectedBackend(),
    ))


def _page(tmp_path, provider=None):
    return PermissionsPage(
        policy=_policy(tmp_path),
        reauth_provider=provider or _SuccessReauth(),
    )


def _visible_text(widget: QWidget) -> str:
    labels = [
        child.text()
        for child in widget.findChildren(QLabel)
        if child.isVisibleTo(widget)
    ]
    controls = [
        child.text()
        for child in widget.findChildren(QAbstractButton)
        if child.isVisibleTo(widget)
    ]
    cells = [
        table.item(row, column).text()
        for table in widget.findChildren(QTableWidget)
        if table.isVisibleTo(widget)
        for row in range(table.rowCount())
        for column in range(table.columnCount())
        if table.item(row, column) is not None
    ]
    return " ".join(labels + controls + cells)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _QT_APP.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    _QT_APP.processEvents()
    assert predicate()


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
    _wait_until(lambda: not page._activation_in_progress)
    assert page._snapshot["permission_mode"] == "full_access"
    assert all(page._snapshot["automatic_categories"].values())
    assert page._full_preset.isChecked()
    assert all(
        toggle.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
        for toggle in page._switches.values()
    )
    assert set(QApplication.topLevelWidgets()) == top_levels
    page.deleteLater()


@pytest.mark.parametrize(
    ("status", "reason_code"),
    [
        (ReauthStatus.DENIED, "verification_denied"),
        (ReauthStatus.CANCELLED, "user_cancelled"),
        (ReauthStatus.UNAVAILABLE, "not_configured"),
        (ReauthStatus.ERROR, "reauth_timeout"),
        (ReauthStatus.ERROR, "reauth_backend_error"),
    ],
)
def test_failed_auth_outcomes_keep_ui_open_and_restore_controls(
    tmp_path, status, reason_code,
):
    page = _page(tmp_path, _OutcomeReauth(status, reason_code))
    page.show()
    page._full_preset.click()
    page._ack.setChecked(True)
    page._confirm_activation.click()
    assert page._activation_in_progress is True
    assert page._confirm_activation.isEnabled() is False
    _wait_until(lambda: not page._activation_in_progress)
    assert page.isVisible()
    assert page._confirm_activation.isEnabled() is True
    assert page._default_preset.isEnabled() is True
    assert page._snapshot["permission_mode"] == "ask"
    assert not any(page._snapshot["automatic_categories"].values())
    page.hide()
    page.deleteLater()


def test_provider_exception_keeps_ui_usable_and_writes_no_full_access(tmp_path):
    class _RaisingReauth:
        def reauthenticate(self, _prompt: str):
            raise OSError("synthetic_native_failure")

    page = _page(tmp_path, _RaisingReauth())
    page.show()
    page._full_preset.click()
    page._ack.setChecked(True)
    page._confirm_activation.click()
    _wait_until(lambda: not page._activation_in_progress)
    assert page.isVisible()
    assert page._confirm_activation.isEnabled() is True
    assert page._snapshot["permission_mode"] == "ask"
    assert not any(page._snapshot["automatic_categories"].values())
    page.hide()
    page.deleteLater()


def test_duplicate_activation_is_rejected_while_qt_stays_responsive(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class _BlockingReauth:
        calls = 0

        def reauthenticate(self, prompt: str):
            assert prompt
            self.calls += 1
            entered.set()
            assert release.wait(2.0)
            return ReauthResult(ReauthStatus.SUCCESS, "deterministic_test")

    provider = _BlockingReauth()
    page = _page(tmp_path, provider)
    page.show()
    page._full_preset.click()
    page._ack.setChecked(True)
    page._confirm_activation.click()
    assert entered.wait(1.0)
    page._confirm_full_access()
    heartbeat = threading.Event()
    QTimer.singleShot(0, heartbeat.set)
    _QT_APP.processEvents()
    assert heartbeat.is_set()
    assert provider.calls == 1
    release.set()
    _wait_until(lambda: not page._activation_in_progress)
    assert page._snapshot["permission_mode"] == "full_access"
    assert all(page._snapshot["automatic_categories"].values())
    page._default_preset.click()
    assert page._snapshot["permission_mode"] == "ask"
    assert not any(page._snapshot["automatic_categories"].values())
    page.hide()
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
    page._advanced_toggle.setChecked(True)
    page.show()
    _QT_APP.processEvents()
    empty_rendered = _visible_text(page)
    for expected in (
        "Temporary permissions",
        "Limits",
        "Permission history",
        "No temporary permissions are active.",
        "No permission history yet.",
        "No additional limit",
    ):
        assert expected in empty_rendered

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
    _QT_APP.processEvents()
    rendered = _visible_text(page)
    assert sentinel not in rendered
    assert "private.example" not in rendered
    assert "Limits updated" in rendered
    for forbidden in (
        "policy epoch", "protected store", "grant reference", "grant_ref",
        "scope hash", "scope_hash", "call hash", "call_hash",
        "schema version", "authorization source", "normalized scope",
        "execution permit", "private_canonical",
    ):
        assert forbidden not in rendered.lower()
    page.hide()
    page.deleteLater()


def test_temporary_permission_row_is_friendly_private_and_revocable(tmp_path):
    policy = _policy(tmp_path)
    sentinel = "WOR196_PRIVATE_PATH_SENTINEL"
    context = action_policy.make_execution_context(
        execution_type=action_policy.ExecutionType.CHAT,
        interactive=True,
        owner_ref="owner:gui-test",
        machine_ref="machine:gui-test",
        execution_id="execution:gui-test",
        operation_id="operation:gui-test",
        store=policy.store,
    )
    request = action_policy.build_permission_request(
        "delete_file",
        {"path": str(tmp_path / sentinel / "private.txt")},
        execution_context=context,
        deletion_classification=action_policy.trusted_deletion_classification(),
        force_sensitive=True,
        policy=policy,
    )
    policy.store.add_grant(action_policy.create_grant(
        request, persistent=True,
    ))
    page = PermissionsPage(policy=policy, reauth_provider=_SuccessReauth())
    page._advanced_toggle.setChecked(True)
    page.show()
    _QT_APP.processEvents()
    rendered = _visible_text(page)
    assert "Work with approved files or folders" in rendered
    assert "Chat" in rendered
    assert "1 uses left" in rendered
    assert "Revoke" in rendered
    assert sentinel not in rendered
    revoke = next(
        button
        for button in page.findChildren(QAbstractButton)
        if button.text() == "Revoke"
    )
    revoke.click()
    assert page._snapshot["grants"] == []
    assert "No temporary permissions are active." in _visible_text(page)
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
