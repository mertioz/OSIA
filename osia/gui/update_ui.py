"""Small Qt presentation layer for the Qt-free Windows updater core."""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from osia import i18n
from osia.updater import (
    DownloadCancelled,
    UpdateScheduler,
    UpdateStateStore,
    dismiss_update,
    download_installer,
    fetch_channel_manifest,
    launch_update_installer,
    load_app_metadata,
    prepare_notification_state,
    reconcile_pending_update,
    safe_error_code,
    updater_enabled,
)
from osia.version import PRODUCT_VERSION


class UpdateController(QObject):
    """Schedule updater work without ever blocking Qt's event loop."""

    state_changed = pyqtSignal(dict)
    _check_finished = pyqtSignal(object, bool, object)
    _download_progress = pyqtSignal(int, int)
    _download_finished = pyqtSignal(object, object)
    _launch_finished = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None, *, store=None, scheduler=None):
        super().__init__(parent)
        self.store = store or UpdateStateStore()
        self.scheduler = scheduler or UpdateScheduler()
        self.metadata = None
        self.manifest = None
        self.installer: Path | None = None
        self._confirmation_parent: QWidget | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.check(manual=False))
        self._check_finished.connect(self._on_check_finished)
        self._download_progress.connect(self._on_download_progress)
        self._download_finished.connect(self._on_download_finished)
        self._launch_finished.connect(self._on_launch_finished)
        try:
            candidate = load_app_metadata()
        except Exception:
            candidate = None
        self.enabled = bool(candidate is not None and updater_enabled(candidate))
        if self.enabled:
            self.metadata = candidate
            reconcile_pending_update(candidate, self.store)
            self._state = self._new_state("idle")
            self._timer.start(max(1, round(self.scheduler.first_delay() * 1000)))
        else:
            self._state = self._new_state("disabled")

    def _new_state(self, status: str) -> dict:
        return {
            "status": status,
            "installedVersion": self.metadata.version if self.metadata else PRODUCT_VERSION,
            "channel": self.metadata.channel if self.metadata else "disabled",
            "availableVersion": None,
            "releaseNotes": (),
            "progressReceived": 0,
            "progressTotal": 0,
            "error": None,
            "internalUnsigned": False,
            "bannerVisible": False,
        }

    @property
    def state(self) -> dict:
        return dict(self._state)

    def _publish(self, **changes) -> None:
        self._state.update(changes)
        self.state_changed.emit(dict(self._state))

    def check(self, *, manual: bool = True) -> bool:
        if not self.enabled:
            if manual:
                self._publish(status="disabled", error=None)
            return False
        if self._state["status"] in {"downloading", "verifying", "pending"}:
            return False
        if not self.scheduler.begin():
            return False
        self._timer.stop()
        self._publish(status="checking", error=None, bannerVisible=False)

        def worker() -> None:
            try:
                result = fetch_channel_manifest(self.metadata, self.store)
                self._check_finished.emit(result, manual, None)
            except Exception as exc:
                self._check_finished.emit(None, manual, exc)

        threading.Thread(target=worker, name="osia-update-check", daemon=True).start()
        return True

    def _on_check_finished(self, result, manual: bool, error) -> None:
        code = safe_error_code(error) if error is not None else None
        success = error is None or code in {"equal_version", "downgrade"}
        delay = self.scheduler.complete(success)
        self._timer.start(max(1, round(delay * 1000)))
        if error is not None:
            if code in {"equal_version", "downgrade"}:
                self.manifest = None
                self.store.update(lastError=None)
                self._publish(
                    status="no_update", availableVersion=None, releaseNotes=(),
                    error=None, bannerVisible=False,
                )
                return
            self.store.update(lastError=code)
            self._publish(
                status="offline" if code == "offline" else "error",
                error=code if manual else None,
                bannerVisible=False,
            )
            return
        manifest = result.manifest
        if manifest is None or manifest.rollout_state != "enabled":
            self.manifest = None
            self._publish(
                status="paused" if manifest is not None else "no_update",
                availableVersion=None,
                releaseNotes=(),
                bannerVisible=False,
                error=None,
            )
            return
        self.manifest = manifest
        visible = prepare_notification_state(
            self.store, manifest.version, manual=manual
        )
        self._publish(
            status="available",
            availableVersion=manifest.version,
            releaseNotes=manifest.release_notes,
            internalUnsigned=(manifest.channel == "internal-preview" and not manifest.installer.signed),
            bannerVisible=visible,
            error=None,
        )

    def dismiss(self) -> None:
        if self.manifest is None:
            return
        dismiss_update(self.store, self.manifest.version)
        self._publish(bannerVisible=False)

    def install(self, parent: QWidget | None = None) -> bool:
        if self.manifest is None:
            return False
        if self._state["status"] in {"downloading", "verifying", "pending"}:
            return False
        self._confirmation_parent = parent
        if self.installer is not None and self.installer.is_file():
            self._confirm_and_launch()
            return True
        self._publish(
            status="downloading", progressReceived=0,
            progressTotal=self.manifest.installer.size_bytes, error=None,
        )

        def worker() -> None:
            try:
                path = download_installer(
                    self.manifest,
                    progress=lambda received, total: self._download_progress.emit(received, total),
                )
                self._download_finished.emit(path, None)
            except Exception as exc:
                self._download_finished.emit(None, exc)

        threading.Thread(target=worker, name="osia-update-download", daemon=True).start()
        return True

    def _on_download_progress(self, received: int, total: int) -> None:
        self._publish(
            status="verifying" if total and received >= total else "downloading",
            progressReceived=received, progressTotal=total,
        )

    def _on_download_finished(self, path, error) -> None:
        if error is not None:
            code = safe_error_code(error)
            status = "available" if isinstance(error, DownloadCancelled) else "error"
            self._publish(status=status, error=code)
            return
        self.installer = Path(path)
        self._publish(status="ready", error=None)
        self._confirm_and_launch()

    def _confirm_and_launch(self) -> None:
        if self.manifest is None or self.installer is None:
            return
        answer = QMessageBox.question(
            self._confirmation_parent,
            i18n.t("updates.confirm_title"),
            i18n.t("updates.confirm_text", version=self.manifest.version),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._publish(status="ready", error=None)
            return
        self._publish(status="verifying", error=None)

        def worker() -> None:
            try:
                launch_update_installer(self.manifest, self.installer, self.store)
                self._launch_finished.emit(None)
            except Exception as exc:
                self._launch_finished.emit(exc)

        threading.Thread(target=worker, name="osia-update-launch", daemon=True).start()

    def _on_launch_finished(self, error) -> None:
        if error is not None:
            self._publish(status="error", error=safe_error_code(error))
            return
        self._publish(status="pending", bannerVisible=False, error=None)
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)


class UpdateBanner(QFrame):
    view_requested = pyqtSignal()
    install_requested = pyqtSignal()
    dismiss_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("updateBanner")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(7)
        self.label = QLabel()
        self.label.setObjectName("updateBannerText")
        self.view_button = QPushButton()
        self.view_button.setObjectName("secondaryBtn")
        self.install_button = QPushButton()
        self.install_button.setObjectName("primaryBtn")
        self.dismiss_button = QPushButton()
        self.dismiss_button.setObjectName("secondaryBtn")
        row.addWidget(self.label, 1)
        row.addWidget(self.view_button)
        row.addWidget(self.install_button)
        row.addWidget(self.dismiss_button)
        self.view_button.clicked.connect(self.view_requested)
        self.install_button.clicked.connect(self.install_requested)
        self.dismiss_button.clicked.connect(self.dismiss_requested)
        self._version = ""
        self._retranslate()
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))
        self.hide()

    def _retranslate(self) -> None:
        self.label.setText(i18n.t("updates.banner", version=self._version))
        self.view_button.setText(i18n.t("updates.view"))
        self.install_button.setText(i18n.t("updates.install"))
        self.dismiss_button.setText(i18n.t("updates.later"))

    def apply_state(self, state: dict) -> None:
        self._version = state.get("availableVersion") or ""
        self._retranslate()
        self.setVisible(bool(state.get("bannerVisible") and self._version))


class UpdateSettingsSection(QFrame):
    def __init__(self, controller: UpdateController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("sectionCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(7)
        self.title = QLabel()
        self.title.setObjectName("sectionTitle")
        self.summary = QLabel()
        self.summary.setObjectName("sectionSub")
        self.summary.setWordWrap(True)
        self.notes = QLabel()
        self.notes.setObjectName("sectionDesc")
        self.notes.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        actions = QHBoxLayout()
        self.check_button = QPushButton()
        self.check_button.setObjectName("secondaryBtn")
        self.install_button = QPushButton()
        self.install_button.setObjectName("primaryBtn")
        self.install_button.hide()
        actions.addWidget(self.check_button)
        actions.addWidget(self.install_button)
        actions.addStretch(1)
        layout.addWidget(self.title)
        layout.addWidget(self.summary)
        layout.addWidget(self.notes)
        layout.addWidget(self.progress)
        layout.addLayout(actions)
        self.check_button.clicked.connect(lambda: controller.check(manual=True))
        self.install_button.clicked.connect(lambda: controller.install(self))
        controller.state_changed.connect(self.apply_state)
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))
        self._state = controller.state
        self.apply_state(self._state)

    def _retranslate(self) -> None:
        self.apply_state(self._state)

    def apply_state(self, state: dict) -> None:
        self._state = dict(state)
        status = state.get("status", "idle")
        installed = state.get("installedVersion", "—")
        channel = state.get("channel", "—")
        available = state.get("availableVersion") or i18n.t("updates.none")
        self.title.setText(i18n.t("updates.title"))
        self.summary.setText(i18n.t(
            "updates.summary", installed=installed, channel=channel,
            available=available, status=i18n.t(f"updates.status.{status}"),
        ))
        notes = list(state.get("releaseNotes") or ())
        if state.get("internalUnsigned"):
            notes.insert(0, i18n.t("updates.internal_unsigned"))
        error = state.get("error")
        if error:
            notes.append(i18n.t("updates.error", code=error))
        self.notes.setText("\n".join(f"• {note}" for note in notes))
        self.notes.setVisible(bool(notes))
        total = int(state.get("progressTotal") or 0)
        received = int(state.get("progressReceived") or 0)
        self.progress.setVisible(status in {"downloading", "verifying"})
        self.progress.setValue(round(received * 1000 / total) if total else 0)
        self.check_button.setText(
            i18n.t("updates.retry") if status in {"error", "offline"}
            else i18n.t("updates.check")
        )
        self.check_button.setEnabled(status not in {"checking", "downloading", "verifying", "pending"})
        self.install_button.setText(i18n.t("updates.install"))
        self.install_button.setVisible(status in {"available", "ready"})
