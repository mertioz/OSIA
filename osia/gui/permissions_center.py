"""Embedded native OSIA Permissions settings page (WOR-196)."""

from __future__ import annotations

import datetime as _datetime
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from osia import action_policy, i18n
from osia.local_reauth import (
    LocalReauthProvider,
    ReauthResult,
    ReauthStatus,
    WindowsHelloReauthProvider,
    default_local_reauth_provider,
    issue_full_access_activation_proof,
)


_SWITCH_KEYS = {
    "files_modify": "permissions.switch.files_modify",
    "files_delete": "permissions.switch.files_delete",
    "commands": "permissions.switch.commands",
    "browser_account": "permissions.switch.browser_account",
    "connectors": "permissions.switch.connectors",
    "send_publish_submit": "permissions.switch.send_publish_submit",
    "purchases_payments": "permissions.switch.purchases_payments",
    "autonomous_actions": "permissions.switch.autonomous_actions",
    "scheduled_actions": "permissions.switch.scheduled_actions",
}


@dataclass(frozen=True)
class ActivationOutcome:
    activated: bool
    reason_code: str
    reauth: ReauthResult | None = None


class FullAccessActivationService:
    """Non-Qt activation coordinator with a deterministic reauth test seam."""

    def __init__(
        self,
        *,
        policy: action_policy.PermissionPolicy | None = None,
        reauth_provider: LocalReauthProvider | None = None,
    ) -> None:
        self.policy = policy or action_policy.get_default_permission_policy()
        self.reauth_provider = (
            reauth_provider or default_local_reauth_provider()
        )

    def _failed(self, reason: str, reauth: ReauthResult | None = None):
        action_policy.record_permission_journal_event(
            "activation_failed",
            policy=self.policy,
            authorization_source="full_access",
            result=reason,
        )
        return ActivationOutcome(False, reason, reauth)

    def activate(
        self, *, acknowledged: bool, second_confirmed: bool,
        window_handle: int | None = None,
    ) -> ActivationOutcome:
        snapshot = action_policy.permission_center_snapshot(policy=self.policy)
        if not snapshot["activation_available"]:
            return self._failed(
                "store_corrupt" if snapshot["store_corrupt"]
                else "secure_storage_unavailable"
            )
        if not acknowledged:
            return self._failed("acknowledgement_required")
        if not second_confirmed:
            return self._failed("second_confirmation_required")
        prompt = i18n.t("permissions.reauth_prompt")
        if isinstance(self.reauth_provider, WindowsHelloReauthProvider):
            reauth = self.reauth_provider.reauthenticate(
                prompt, window_handle=window_handle,
            )
        else:
            reauth = self.reauth_provider.reauthenticate(prompt)
        if reauth.status not in {
            ReauthStatus.SUCCESS, ReauthStatus.UNAVAILABLE,
        }:
            return self._failed(f"reauth_{reauth.status.value}", reauth)
        try:
            activation_proof = issue_full_access_activation_proof(reauth)
            action_policy.activate_full_access(
                activation_proof=activation_proof,
                policy=self.policy,
            )
        except Exception:
            return self._failed("protected_store_write_failed", reauth)
        return ActivationOutcome(True, "full_access_enabled", reauth)


class PermissionsPage(QWidget):
    """Permissions controls embedded in the existing Settings stack."""

    state_changed = pyqtSignal(dict)

    def __init__(
        self,
        parent=None,
        *,
        policy: action_policy.PermissionPolicy | None = None,
        reauth_provider: LocalReauthProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("permissionsPage")
        self.policy = policy or action_policy.get_default_permission_policy()
        self.activation_service = FullAccessActivationService(
            policy=self.policy, reauth_provider=reauth_provider,
        )
        self._snapshot: dict[str, Any] = {}
        self._refreshing = False
        self._destinations_dirty = False
        self._grant_row_tokens: list[str] = []
        self._grant_group_tokens: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addLayout(self._build_presets())

        self._ceremony = self._build_ceremony()
        self._ceremony.hide()
        root.addWidget(self._ceremony)

        self._switches: dict[str, QCheckBox] = {}
        for category in action_policy.AUTOMATIC_PERMISSION_CATEGORIES:
            toggle = QCheckBox(i18n.t(_SWITCH_KEYS[category]))
            toggle.setObjectName("smartToggle")
            toggle.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
            )
            toggle.setProperty("permissionCategory", category)
            toggle.toggled.connect(
                lambda enabled, name=category: self._switch_changed(name, enabled)
            )
            self._switches[category] = toggle
            root.addWidget(toggle)

        self._status = QLabel()
        self._status.setObjectName("sectionSub")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._advanced_toggle = QPushButton()
        self._advanced_toggle.setObjectName("ghostBtn")
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.toggled.connect(self._toggle_advanced)
        root.addWidget(self._advanced_toggle)
        self._advanced = self._build_advanced()
        self._advanced.setObjectName("permissionsAdvanced")
        self._advanced.hide()
        root.addWidget(self._advanced)

        i18n.add_listener(self.retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self.retranslate))
        self.retranslate()
        self.refresh()

    def _build_presets(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._default_preset = QPushButton()
        self._default_preset.setObjectName("themeChoice")
        self._default_preset.setCheckable(True)
        self._default_preset.setAutoDefault(False)
        self._default_preset.clicked.connect(self._apply_default)
        self._full_preset = QPushButton()
        self._full_preset.setObjectName("themeChoice")
        self._full_preset.setCheckable(True)
        self._full_preset.setAutoDefault(False)
        self._full_preset.clicked.connect(self._show_full_access_ceremony)
        row.addWidget(self._default_preset, 1)
        row.addWidget(self._full_preset, 1)
        return row

    def _build_ceremony(self) -> QFrame:
        card = QFrame()
        card.setObjectName("providerCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self._warning = QLabel()
        self._warning.setObjectName("sectionSub")
        self._warning.setWordWrap(True)
        self._ack = QCheckBox()
        self._ack.setObjectName("fullAccessAcknowledgement")
        self._ack.toggled.connect(self._update_confirmation_button)
        actions = QHBoxLayout()
        self._cancel_activation = QPushButton()
        self._cancel_activation.setObjectName("ghostBtn")
        self._cancel_activation.clicked.connect(self._cancel_ceremony)
        self._confirm_activation = QPushButton()
        self._confirm_activation.setObjectName("primaryBtn")
        self._confirm_activation.setAutoDefault(False)
        self._confirm_activation.setDefault(False)
        self._confirm_activation.clicked.connect(self._confirm_full_access)
        actions.addWidget(self._cancel_activation)
        actions.addWidget(self._confirm_activation)
        actions.addStretch()
        layout.addWidget(self._warning)
        layout.addWidget(self._ack)
        layout.addLayout(actions)
        return card

    def _build_advanced(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        limits = QFrame()
        limits.setObjectName("providerCard")
        limits_layout = QVBoxLayout(limits)
        limits_layout.setContentsMargins(12, 10, 12, 10)
        self._limits_title = QLabel()
        self._limits_title.setObjectName("sectionTitle")
        self._payment_enabled = QCheckBox()
        payment_row = QHBoxLayout()
        self._payment_max = QSpinBox()
        self._payment_max.setObjectName("permissionsAmount")
        self._payment_max.setRange(0, 2_000_000_000)
        self._currency = QComboBox()
        self._currency.setObjectName("voiceCombo")
        self._currency.setEditable(True)
        self._currency.addItems(["EUR", "USD", "GBP", "JPY", "CAD", "CHF"])
        payment_row.addWidget(self._payment_enabled)
        payment_row.addWidget(self._payment_max)
        payment_row.addWidget(self._currency)
        self._destinations_enabled = QCheckBox()
        self._destinations = QTextEdit()
        self._destinations.setObjectName("mdEditor")
        self._destinations.setMaximumHeight(72)
        self._destinations.textChanged.connect(
            lambda: setattr(self, "_destinations_dirty", True)
        )
        self._destinations_summary = QLabel()
        self._destinations_summary.setObjectName("sectionSub")
        self._destinations_summary.setWordWrap(True)
        self._limits_note = QLabel()
        self._limits_note.setObjectName("sectionSub")
        self._limits_note.setWordWrap(True)
        self._apply_limits = QPushButton()
        self._apply_limits.setObjectName("ghostBtn")
        self._apply_limits.clicked.connect(self._apply_constraints)
        limits_layout.addWidget(self._limits_title)
        limits_layout.addLayout(payment_row)
        limits_layout.addWidget(self._destinations_enabled)
        limits_layout.addWidget(self._destinations_summary)
        limits_layout.addWidget(self._destinations)
        limits_layout.addWidget(self._limits_note)
        limits_layout.addWidget(self._apply_limits)
        layout.addWidget(limits)

        grants = QFrame()
        grants.setObjectName("providerCard")
        grants_layout = QVBoxLayout(grants)
        grants_layout.setContentsMargins(12, 10, 12, 10)
        self._grants_title = QLabel()
        self._grants_title.setObjectName("sectionTitle")
        self._grants = QTableWidget(0, 4)
        self._grants.setObjectName("permissionsTable")
        self._grants.setMaximumHeight(150)
        self._grants.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._grants.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._grants.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._grants.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        grant_actions = QHBoxLayout()
        self._revoke_one = QPushButton()
        self._revoke_group = QPushButton()
        self._revoke_all = QPushButton()
        for button in (self._revoke_one, self._revoke_group, self._revoke_all):
            button.setObjectName("ghostBtn")
            grant_actions.addWidget(button)
        self._revoke_one.clicked.connect(self._revoke_selected)
        self._revoke_group.clicked.connect(self._revoke_selected_group)
        self._revoke_all.clicked.connect(self._revoke_everything)
        grants_layout.addWidget(self._grants_title)
        grants_layout.addWidget(self._grants)
        grants_layout.addLayout(grant_actions)
        layout.addWidget(grants)

        journal = QFrame()
        journal.setObjectName("providerCard")
        journal_layout = QVBoxLayout(journal)
        journal_layout.setContentsMargins(12, 10, 12, 10)
        self._journal_title = QLabel()
        self._journal_title.setObjectName("sectionTitle")
        self._journal = QTableWidget(0, 4)
        self._journal.setObjectName("permissionsTable")
        self._journal.setMaximumHeight(150)
        self._journal.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._journal.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._clear_journal = QPushButton()
        self._clear_journal.setObjectName("ghostBtn")
        self._clear_journal.clicked.connect(self._clear_permission_journal)
        journal_layout.addWidget(self._journal_title)
        journal_layout.addWidget(self._journal)
        journal_layout.addWidget(self._clear_journal)
        layout.addWidget(journal)
        return host

    def retranslate(self) -> None:
        self._default_preset.setText(i18n.t("permissions.default_access"))
        self._full_preset.setText(i18n.t("permissions.full_access"))
        self._warning.setText(i18n.t("permissions.full_warning"))
        self._ack.setText(i18n.t("permissions.acknowledge"))
        self._cancel_activation.setText(i18n.t("permissions.cancel"))
        self._confirm_activation.setText(i18n.t("permissions.enable_confirm"))
        for category, toggle in self._switches.items():
            toggle.setText(i18n.t(_SWITCH_KEYS[category]))
        self._advanced_toggle.setText(i18n.t("permissions.advanced"))
        self._limits_title.setText(i18n.t("permissions.constraints"))
        self._payment_enabled.setText(i18n.t("permissions.payment_limit"))
        self._payment_max.setSuffix(" " + i18n.t("permissions.minor_units"))
        self._destinations_enabled.setText(
            i18n.t("permissions.destination_limit")
        )
        self._destinations.setPlaceholderText(
            i18n.t("permissions.destination_placeholder")
        )
        self._limits_note.setText(i18n.t("permissions.constraints_note"))
        self._apply_limits.setText(i18n.t("permissions.apply_constraints"))
        self._grants_title.setText(i18n.t("permissions.grants"))
        self._grants.setHorizontalHeaderLabels([
            i18n.t("permissions.execution"), i18n.t("permissions.category"),
            i18n.t("permissions.expiration"), i18n.t("permissions.uses"),
        ])
        self._revoke_one.setText(i18n.t("permissions.revoke_one"))
        self._revoke_group.setText(i18n.t("permissions.revoke_group"))
        self._revoke_all.setText(i18n.t("permissions.revoke_all"))
        self._journal_title.setText(i18n.t("permissions.journal"))
        self._journal.setHorizontalHeaderLabels([
            i18n.t("permissions.time"), i18n.t("permissions.event"),
            i18n.t("permissions.category"), i18n.t("permissions.result"),
        ])
        self._clear_journal.setText(i18n.t("permissions.clear_journal"))
        if self._snapshot:
            self._refresh_status_text()

    def _toggle_advanced(self, expanded: bool) -> None:
        self._advanced.setVisible(expanded)

    def _show_full_access_ceremony(self) -> None:
        if (
            self._snapshot.get("permission_mode") == "full_access"
            and all(self._snapshot.get("automatic_categories", {}).values())
        ):
            self.refresh()
            return
        if not self._snapshot.get("activation_available"):
            self.refresh()
            return
        self._full_preset.setChecked(False)
        self._ceremony.show()
        self._ack.setChecked(False)
        self._update_confirmation_button()

    def _cancel_ceremony(self) -> None:
        self._ack.setChecked(False)
        self._ceremony.hide()
        self.refresh()

    def _update_confirmation_button(self) -> None:
        self._confirm_activation.setEnabled(
            bool(self._snapshot.get("activation_available"))
            and self._ack.isChecked()
        )

    def _confirm_full_access(self) -> None:
        if not self._ack.isChecked():
            return
        outcome = self.activation_service.activate(
            acknowledged=True,
            second_confirmed=True,
            window_handle=int(self.window().winId()),
        )
        if outcome.activated:
            self._ceremony.hide()
            self._ack.setChecked(False)
        self.refresh(emit=True, error_reason=(
            "" if outcome.activated else outcome.reason_code
        ))

    def _apply_default(self) -> None:
        try:
            action_policy.apply_default_access(policy=self.policy)
        except Exception:
            self.refresh(error_reason="protected_store_write_failed")
            return
        self._ceremony.hide()
        self._ack.setChecked(False)
        self.refresh(emit=True)

    def _switch_changed(self, category: str, enabled: bool) -> None:
        if self._refreshing:
            return
        try:
            action_policy.update_automatic_permission(
                category, enabled, policy=self.policy,
            )
        except Exception:
            self.refresh(error_reason="protected_store_write_failed")
            return
        self._ceremony.hide()
        self._ack.setChecked(False)
        self.refresh(emit=True)

    def _apply_constraints(self) -> None:
        origins: list[str] | None = None
        connectors: list[str] | None = None
        if self._destinations_dirty:
            origins = []
            connectors = []
            for raw in self._destinations.toPlainText().splitlines():
                value = raw.strip()
                if not value:
                    continue
                if value.lower().startswith(("http://", "https://")):
                    origins.append(value)
                else:
                    connectors.append(value)
        try:
            action_policy.update_full_access_constraints(
                policy=self.policy,
                payment_enabled=self._payment_enabled.isChecked(),
                max_amount_minor=self._payment_max.value(),
                currency=self._currency.currentText().strip().upper(),
                destinations_enabled=self._destinations_enabled.isChecked(),
                origins=origins,
                connectors=connectors,
            )
        except Exception:
            self.refresh(error_reason="invalid_constraints")
            return
        self.refresh(emit=True)

    def _selected_row(self) -> int:
        ranges = self._grants.selectedRanges()
        return ranges[0].topRow() if ranges else -1

    def _revoke_selected(self) -> None:
        row = self._selected_row()
        if 0 <= row < len(self._grant_row_tokens):
            action_policy.revoke_permission_center_grant(
                self._grant_row_tokens[row], policy=self.policy,
            )
            self.refresh(emit=True)

    def _revoke_selected_group(self) -> None:
        row = self._selected_row()
        if 0 <= row < len(self._grant_group_tokens):
            token = self._grant_group_tokens[row]
            if token:
                action_policy.revoke_permission_center_group(
                    token, policy=self.policy,
                )
                self.refresh(emit=True)

    def _revoke_everything(self) -> None:
        action_policy.revoke_all_delegated_authority(policy=self.policy)
        self.refresh(emit=True)

    def _clear_permission_journal(self) -> None:
        action_policy.clear_permission_journal(policy=self.policy)
        self.refresh(emit=True)

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return i18n.t("permissions.never")
        return _datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")

    def _refresh_status_text(self, error_reason: str = "") -> None:
        if error_reason:
            key = (
                "permissions.invalid_constraints"
                if error_reason == "invalid_constraints"
                else "permissions.change_failed"
            )
            self._status.setText(i18n.t(key))
        elif not self._snapshot.get("activation_available"):
            self._status.setText(i18n.t("permissions.unavailable_simple"))
        else:
            self._status.setText(i18n.t("permissions.saved"))

    def refresh(
        self, *, emit: bool = False, error_reason: str = "",
    ) -> None:
        snapshot = action_policy.permission_center_snapshot(policy=self.policy)
        self._snapshot = snapshot
        matrix = snapshot["automatic_categories"]
        available = bool(snapshot["activation_available"])
        self._refreshing = True
        try:
            for category, toggle in self._switches.items():
                toggle.setChecked(bool(matrix.get(category, False)))
                toggle.setEnabled(available)
            all_off = not any(matrix.values())
            all_on = bool(matrix) and all(matrix.values())
            self._default_preset.setChecked(all_off)
            self._full_preset.setChecked(all_on)
            self._full_preset.setEnabled(available)
        finally:
            self._refreshing = False

        constraints = snapshot["constraints"]
        payment = constraints["payment"]
        destinations = constraints["destinations"]
        self._payment_enabled.setChecked(payment["enabled"])
        self._payment_max.setValue(payment["max_amount_minor"])
        index = self._currency.findText(payment["currency"])
        if index >= 0:
            self._currency.setCurrentIndex(index)
        else:
            self._currency.setEditText(payment["currency"])
        self._destinations_enabled.setChecked(destinations["enabled"])
        self._destinations_summary.setText(i18n.t(
            "permissions.destination_summary",
            origins=destinations["origin_count"],
            connectors=destinations["connector_count"],
        ))
        self._destinations.blockSignals(True)
        self._destinations.clear()
        self._destinations.blockSignals(False)
        self._destinations_dirty = False

        grants = snapshot["grants"]
        self._grant_row_tokens = [item["row_token"] for item in grants]
        self._grant_group_tokens = [item["group_token"] for item in grants]
        self._grants.setRowCount(len(grants))
        for row, grant in enumerate(grants):
            expiration = (
                i18n.t("permissions.expired") if grant["expired"]
                else self._format_timestamp(grant["expires_at"])
            )
            uses = (
                i18n.t("permissions.unlimited")
                if grant["remaining_uses"] is None
                else str(grant["remaining_uses"])
            )
            for column, value in enumerate((
                grant["execution_type"], grant["safe_category"],
                expiration, uses,
            )):
                self._grants.setItem(row, column, QTableWidgetItem(str(value)))

        events = snapshot["journal"]
        self._journal.setRowCount(len(events))
        for row, event in enumerate(events):
            for column, value in enumerate((
                self._format_timestamp(event["timestamp"]),
                event["event_code"], event["safe_category"], event["result"],
            )):
                self._journal.setItem(row, column, QTableWidgetItem(str(value)))
        self._update_confirmation_button()
        self._refresh_status_text(error_reason)
        if emit:
            self.state_changed.emit(snapshot)


__all__ = [
    "ActivationOutcome", "FullAccessActivationService", "PermissionsPage",
]
