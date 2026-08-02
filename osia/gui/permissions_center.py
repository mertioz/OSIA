"""Embedded native OSIA Permissions settings page (WOR-196)."""

from __future__ import annotations

import datetime as _datetime
import threading
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QEvent, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QDoubleSpinBox,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from osia import action_policy, i18n
from osia.local_reauth import issue_full_access_activation_proof
from osia.permission_contract import PERMISSION_CATEGORY_CONTRACT


_SWITCH_KEYS = {
    item.identifier: item.description_key
    for item in PERMISSION_CATEGORY_CONTRACT
}

_TEMPORARY_ACTION_KEYS = {
    "local_path": "permissions.allowed.files",
    "local_paths": "permissions.allowed.files",
    "directory_tree": "permissions.allowed.files",
    "local_command": "permissions.allowed.commands",
    "safe_local_setting": "permissions.allowed.settings",
    "tool_operation": "permissions.allowed.action",
    "connector_operation": "permissions.allowed.service",
    "web_origin": "permissions.allowed.website",
    "web_url": "permissions.allowed.website",
    "purchase": "permissions.allowed.payment",
    "unknown": "permissions.allowed.action",
}

_EXECUTION_KEYS = {
    "chat": "permissions.where.chat",
    "autonomous": "permissions.where.autonomous",
    "scheduled": "permissions.where.scheduled",
}

_HISTORY_EVENT_KEYS = {
    "full_access_enabled": "permissions.history.full_enabled",
    "full_access_disabled": "permissions.history.full_disabled",
    "activation_failed": "permissions.history.full_failed",
    "grant_created": "permissions.history.temporary_granted",
    "grant_expired": "permissions.history.temporary_expired",
    "grant_revoked": "permissions.history.revoked",
    "delegated_authority_revoked": "permissions.history.revoked",
    "universal_barrier_blocked": "permissions.history.blocked",
    "authorization_required": "permissions.history.approval_required",
    "optional_constraint_changed": "permissions.history.limits_updated",
    "automatic_permission_changed": "permissions.history.settings_updated",
    "policy_epoch_changed": "permissions.history.settings_updated",
}


@dataclass(frozen=True)
class ActivationOutcome:
    activated: bool
    reason_code: str


class FullAccessActivationService:
    """Non-Qt coordinator for the explicit Full Access ceremony."""

    def __init__(
        self,
        *,
        policy: action_policy.PermissionPolicy | None = None,
        reauth_provider: Any | None = None,
    ) -> None:
        self.policy = policy or action_policy.get_default_permission_policy()
        # Compatibility-only constructor seam. Full Access is deliberately
        # disconnected from every local reauthentication provider.
        del reauth_provider
        self._activation_lock = threading.Lock()

    def _failed(self, reason: str):
        action_policy.record_permission_journal_event(
            "activation_failed",
            policy=self.policy,
            authorization_source="full_access",
            result=reason,
        )
        return ActivationOutcome(False, reason)

    def activate(
        self, *, acknowledged: bool, second_confirmed: bool,
        window_handle: int | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> ActivationOutcome:
        del window_handle, cancellation_event
        try:
            acquired = self._activation_lock.acquire(blocking=False)
        except Exception:
            acquired = False
        if not acquired:
            return ActivationOutcome(False, "activation_in_progress")
        try:
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
            try:
                activation_proof = issue_full_access_activation_proof()
                action_policy.activate_full_access(
                    activation_proof=activation_proof,
                    policy=self.policy,
                )
            except Exception:
                return self._failed("protected_store_write_failed")
            return ActivationOutcome(True, "full_access_enabled")
        finally:
            self._activation_lock.release()


class PermissionsPage(QWidget):
    """Permissions controls embedded in the existing Settings stack."""

    state_changed = pyqtSignal(dict)
    _activation_completed = pyqtSignal(object)

    def __init__(
        self,
        parent=None,
        *,
        policy: action_policy.PermissionPolicy | None = None,
        reauth_provider: Any | None = None,
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
        self._activation_in_progress = False
        self._activation_cancel_event = threading.Event()
        self._activation_thread: threading.Thread | None = None
        self._activation_completed.connect(self._finish_full_access_activation)

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
        self.destroyed.connect(lambda *_: self._activation_cancel_event.set())
        self.destroyed.connect(lambda: i18n.remove_listener(self.retranslate))
        self.retranslate()
        self.refresh()
        self._canonical_refresh_timer = QTimer(self)
        self._canonical_refresh_timer.setInterval(500)
        self._canonical_refresh_timer.timeout.connect(
            self._poll_external_epoch
        )
        self._canonical_refresh_timer.start()

    def _poll_external_epoch(self) -> None:
        if self.isVisible():
            self._refresh_external_epoch()

    def _refresh_external_epoch(self) -> None:
        try:
            epoch = self.policy.store.load_state().policy_epoch
        except Exception:
            return
        if epoch != self._snapshot.get("policy_epoch"):
            self.refresh(emit=True)

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
        self._confirm_activation.installEventFilter(self)
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

        temporary = QFrame()
        temporary.setObjectName("providerCard")
        temporary_layout = QVBoxLayout(temporary)
        temporary_layout.setContentsMargins(12, 10, 12, 10)
        temporary_layout.setSpacing(8)
        self._temporary_title = QLabel()
        self._temporary_title.setObjectName("sectionTitle")
        self._temporary_empty = QLabel()
        self._temporary_empty.setObjectName("sectionSub")
        self._temporary_empty.setWordWrap(True)
        self._grants = QTableWidget(0, 4)
        self._grants.setObjectName("permissionsTable")
        self._grants.setMaximumHeight(170)
        self._grants.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._grants.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._grants.verticalHeader().setVisible(False)
        grant_header = self._grants.horizontalHeader()
        grant_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            grant_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents,
            )
        temporary_layout.addWidget(self._temporary_title)
        temporary_layout.addWidget(self._temporary_empty)
        temporary_layout.addWidget(self._grants)
        layout.addWidget(temporary)

        limits = QFrame()
        limits.setObjectName("providerCard")
        limits_layout = QVBoxLayout(limits)
        limits_layout.setContentsMargins(12, 10, 12, 10)
        limits_layout.setSpacing(8)
        self._limits_title = QLabel()
        self._limits_title.setObjectName("sectionTitle")
        self._payment_enabled = QCheckBox()
        payment_row = QHBoxLayout()
        self._payment_max = QDoubleSpinBox()
        self._payment_max.setObjectName("permissionsAmount")
        self._payment_max.setDecimals(2)
        self._payment_max.setRange(0, 20_000_000.00)
        self._currency = QComboBox()
        self._currency.setObjectName("voiceCombo")
        self._currency.setEditable(True)
        self._currency.addItems(["EUR", "USD", "GBP", "JPY", "CAD", "CHF"])
        payment_row.addWidget(self._payment_enabled)
        payment_row.addWidget(self._payment_max)
        payment_row.addWidget(self._currency)
        self._payment_state = QLabel()
        self._payment_state.setObjectName("sectionSub")
        self._payment_example = QLabel()
        self._payment_example.setObjectName("sectionSub")
        self._payment_example.setWordWrap(True)
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
        self._destinations_example = QLabel()
        self._destinations_example.setObjectName("sectionSub")
        self._destinations_example.setWordWrap(True)
        self._apply_limits = QPushButton()
        self._apply_limits.setObjectName("ghostBtn")
        self._apply_limits.clicked.connect(self._apply_constraints)
        self._payment_enabled.toggled.connect(self._sync_limit_controls)
        self._destinations_enabled.toggled.connect(self._sync_limit_controls)
        limits_layout.addWidget(self._limits_title)
        limits_layout.addLayout(payment_row)
        limits_layout.addWidget(self._payment_state)
        limits_layout.addWidget(self._payment_example)
        limits_layout.addWidget(self._destinations_enabled)
        limits_layout.addWidget(self._destinations_summary)
        limits_layout.addWidget(self._destinations)
        limits_layout.addWidget(self._destinations_example)
        limits_layout.addWidget(self._apply_limits)
        layout.addWidget(limits)

        journal = QFrame()
        journal.setObjectName("providerCard")
        journal_layout = QVBoxLayout(journal)
        journal_layout.setContentsMargins(12, 10, 12, 10)
        journal_layout.setSpacing(8)
        self._journal_title = QLabel()
        self._journal_title.setObjectName("sectionTitle")
        self._journal_empty = QLabel()
        self._journal_empty.setObjectName("sectionSub")
        self._journal_empty.setWordWrap(True)
        self._journal = QTableWidget(0, 2)
        self._journal.setObjectName("permissionsTable")
        self._journal.setMaximumHeight(150)
        self._journal.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._journal.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._journal.verticalHeader().setVisible(False)
        journal_header = self._journal.horizontalHeader()
        journal_header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents,
        )
        journal_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._clear_journal = QPushButton()
        self._clear_journal.setObjectName("ghostBtn")
        self._clear_journal.clicked.connect(self._clear_permission_journal)
        journal_layout.addWidget(self._journal_title)
        journal_layout.addWidget(self._journal_empty)
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
        self._advanced_toggle.setText(i18n.t("permissions.more_controls"))
        self._temporary_title.setText(i18n.t("permissions.temporary_title"))
        self._temporary_empty.setText(i18n.t("permissions.temporary_empty"))
        self._grants.setHorizontalHeaderLabels([
            i18n.t("permissions.allowed_action"),
            i18n.t("permissions.applies_to"),
            i18n.t("permissions.expires_or_uses"),
            i18n.t("permissions.action"),
        ])
        self._limits_title.setText(i18n.t("permissions.limits_title"))
        self._payment_enabled.setText(i18n.t("permissions.payment_limit"))
        self._payment_example.setText(i18n.t("permissions.payment_example"))
        self._destinations_enabled.setText(
            i18n.t("permissions.website_limit")
        )
        self._destinations.setPlaceholderText(
            i18n.t("permissions.destination_placeholder")
        )
        self._destinations_example.setText(
            i18n.t("permissions.website_example")
        )
        self._apply_limits.setText(i18n.t("permissions.save_limits"))
        self._journal_title.setText(i18n.t("permissions.history_title"))
        self._journal_empty.setText(i18n.t("permissions.history_empty"))
        self._journal.setHorizontalHeaderLabels([
            i18n.t("permissions.when"), i18n.t("permissions.what_happened"),
        ])
        self._clear_journal.setText(i18n.t("permissions.clear_history"))
        self._sync_limit_controls()
        if self._snapshot:
            self.refresh()

    def _toggle_advanced(self, expanded: bool) -> None:
        self._advanced.setVisible(expanded)

    def _sync_limit_controls(self) -> None:
        payment_enabled = self._payment_enabled.isChecked()
        self._payment_max.setEnabled(payment_enabled)
        self._currency.setEnabled(payment_enabled)
        self._payment_state.setText(i18n.t(
            "permissions.limit_enabled"
            if payment_enabled else "permissions.no_additional_limit"
        ))

        destinations_enabled = self._destinations_enabled.isChecked()
        self._destinations.setEnabled(destinations_enabled)
        if not destinations_enabled:
            self._destinations_summary.setText(
                i18n.t("permissions.no_additional_limit")
            )
        elif self._snapshot:
            destinations = self._snapshot["constraints"]["destinations"]
            self._destinations_summary.setText(i18n.t(
                "permissions.allowed_summary",
                websites=destinations["origin_count"],
                services=destinations["connector_count"],
            ))
        else:
            self._destinations_summary.setText(
                i18n.t("permissions.limit_enabled")
            )

    def _show_full_access_ceremony(self) -> None:
        if self._activation_in_progress:
            return
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
        if self._activation_in_progress:
            return
        self._ack.setChecked(False)
        self._ceremony.hide()
        self.refresh()

    def _update_confirmation_button(self) -> None:
        self._confirm_activation.setEnabled(
            not self._activation_in_progress
            and bool(self._snapshot.get("activation_available"))
            and self._ack.isChecked()
        )

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self._confirm_activation
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            return True
        return super().eventFilter(watched, event)

    def _confirm_full_access(self) -> None:
        if not self._ack.isChecked() or self._activation_in_progress:
            return
        self._activation_cancel_event.clear()
        self._activation_in_progress = True
        self._set_activation_controls()
        self._refresh_status_text("activation_in_progress")
        worker = threading.Thread(
            target=self._run_full_access_activation,
            daemon=True,
        )
        self._activation_thread = worker
        worker.start()

    def _run_full_access_activation(self) -> None:
        try:
            outcome = self.activation_service.activate(
                acknowledged=True,
                second_confirmed=True,
                cancellation_event=self._activation_cancel_event,
            )
        except Exception:
            outcome = ActivationOutcome(False, "protected_store_write_failed")
        try:
            self._activation_completed.emit(outcome)
        except RuntimeError:
            self._activation_cancel_event.set()

    def _finish_full_access_activation(self, outcome: ActivationOutcome) -> None:
        self._activation_in_progress = False
        self._activation_thread = None
        if outcome.activated:
            self._ceremony.hide()
            self._ack.setChecked(False)
        self.refresh(emit=True, error_reason=(
            "" if outcome.activated else outcome.reason_code
        ))

    def _set_activation_controls(self) -> None:
        available = bool(self._snapshot.get("activation_available"))
        enabled = available and not self._activation_in_progress
        self._default_preset.setEnabled(enabled)
        self._full_preset.setEnabled(enabled)
        self._cancel_activation.setEnabled(not self._activation_in_progress)
        self._advanced.setEnabled(not self._activation_in_progress)
        for toggle in self._switches.values():
            toggle.setEnabled(enabled)
        self._update_confirmation_button()

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
                elif "." in value and not any(
                    character.isspace() for character in value
                ):
                    origins.append("https://" + value)
                else:
                    connectors.append(value)
        try:
            action_policy.update_full_access_constraints(
                policy=self.policy,
                payment_enabled=self._payment_enabled.isChecked(),
                max_amount_minor=int(round(self._payment_max.value() * 100)),
                currency=self._currency.currentText().strip().upper(),
                destinations_enabled=self._destinations_enabled.isChecked(),
                origins=origins,
                connectors=connectors,
            )
        except Exception:
            self.refresh(error_reason="invalid_constraints")
            return
        self.refresh(emit=True)

    def _revoke_grant(self, row_token: str) -> None:
        action_policy.revoke_permission_center_grant(
            row_token, policy=self.policy,
        )
        self.refresh(emit=True)

    def _clear_permission_journal(self) -> None:
        action_policy.clear_permission_journal(policy=self.policy)
        self.refresh(emit=True)

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return ""
        return _datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")

    @classmethod
    def _temporary_duration_text(cls, grant: dict[str, Any]) -> str:
        if grant["expired"]:
            return i18n.t("permissions.expired")
        pieces: list[str] = []
        expiration = cls._format_timestamp(grant["expires_at"])
        if expiration:
            pieces.append(i18n.t("permissions.expires_at", time=expiration))
        remaining = grant["remaining_uses"]
        if remaining is not None:
            pieces.append(i18n.t("permissions.uses_left", count=remaining))
        return " · ".join(pieces) or i18n.t("permissions.until_revoked")

    @staticmethod
    def _history_text(event_code: str) -> str:
        key = _HISTORY_EVENT_KEYS.get(
            event_code, "permissions.history.settings_updated",
        )
        return i18n.t(key)

    def _refresh_status_text(self, error_reason: str = "") -> None:
        if error_reason:
            key = {
                "invalid_constraints": "permissions.invalid_constraints",
                "activation_in_progress": "permissions.activation_in_progress",
            }.get(error_reason, "permissions.change_failed")
            self._status.setText(i18n.t(key))
        elif not self._snapshot.get("activation_available"):
            self._status.setText(i18n.t("permissions.unavailable_simple"))
        elif self._activation_in_progress:
            self._status.setText(i18n.t("permissions.activation_in_progress"))
        else:
            self._status.setText(i18n.t("permissions.saved"))

    def refresh(
        self, *, emit: bool = False, error_reason: str = "",
    ) -> None:
        snapshot = action_policy.permission_center_snapshot(policy=self.policy)
        self._snapshot = snapshot
        matrix = snapshot["automatic_categories"]
        self._refreshing = True
        try:
            for category, toggle in self._switches.items():
                toggle.setChecked(bool(matrix.get(category, False)))
            all_off = not any(matrix.values())
            all_on = bool(matrix) and all(matrix.values())
            self._default_preset.setChecked(all_off)
            self._full_preset.setChecked(all_on)
        finally:
            self._refreshing = False

        constraints = snapshot["constraints"]
        payment = constraints["payment"]
        destinations = constraints["destinations"]
        self._payment_enabled.setChecked(payment["enabled"])
        self._payment_max.setValue(payment["max_amount_minor"] / 100)
        index = self._currency.findText(payment["currency"])
        if index >= 0:
            self._currency.setCurrentIndex(index)
        else:
            self._currency.setEditText(payment["currency"])
        self._destinations_enabled.setChecked(destinations["enabled"])
        self._destinations.blockSignals(True)
        self._destinations.clear()
        self._destinations.blockSignals(False)
        self._destinations_dirty = False

        grants = snapshot["grants"]
        self._grants.clearContents()
        self._grants.setRowCount(len(grants))
        for row, grant in enumerate(grants):
            action_key = _TEMPORARY_ACTION_KEYS.get(
                grant["safe_category"], "permissions.allowed.action",
            )
            where_key = _EXECUTION_KEYS.get(
                grant["execution_type"], "permissions.where.chat",
            )
            for column, value in enumerate((
                i18n.t(action_key),
                i18n.t(where_key),
                self._temporary_duration_text(grant),
            )):
                self._grants.setItem(row, column, QTableWidgetItem(str(value)))
            revoke = QPushButton(i18n.t("permissions.revoke"))
            revoke.setObjectName("ghostBtn")
            revoke.clicked.connect(
                lambda _checked=False, token=grant["row_token"]: (
                    self._revoke_grant(token)
                )
            )
            self._grants.setCellWidget(row, 3, revoke)
        has_grants = bool(grants)
        self._temporary_empty.setVisible(not has_grants)
        self._grants.setVisible(has_grants)

        events = snapshot["journal"]
        self._journal.clearContents()
        self._journal.setRowCount(len(events))
        for row, event in enumerate(events):
            for column, value in enumerate((
                self._format_timestamp(event["timestamp"]),
                self._history_text(event["event_code"]),
            )):
                self._journal.setItem(row, column, QTableWidgetItem(str(value)))
        has_events = bool(events)
        self._journal_empty.setVisible(not has_events)
        self._journal.setVisible(has_events)
        self._clear_journal.setEnabled(has_events)
        self._sync_limit_controls()
        self._set_activation_controls()
        self._refresh_status_text(error_reason)
        if emit:
            self.state_changed.emit(snapshot)


__all__ = [
    "ActivationOutcome", "FullAccessActivationService", "PermissionsPage",
]
