"""Small Qt presentation layer for the Qt-free Windows updater core."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    QObject,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from osia import i18n
from osia.gui.theme import get_theme_manager
from osia.updater import (
    DownloadCancelled,
    UpdateScheduler,
    UpdateStateStore,
    dismiss_update,
    download_installer,
    fetch_channel_manifest,
    fetch_eligible_manifest,
    initialize_preview_opt_in,
    launch_update_installer,
    load_app_metadata,
    parse_manifest,
    prepare_notification_state,
    reconcile_pending_update,
    safe_error_code,
    set_preview_opt_in as persist_preview_opt_in,
    updater_enabled,
)
from osia.release_notes import (
    RELEASE_HISTORY,
    is_major_update,
    normalize_remote_notes,
    release_for_version,
)
from osia.version import PRODUCT_VERSION


def _system_reduced_motion() -> bool:
    """Honor the Windows animation preference, with a deterministic QA override."""

    if os.environ.get("OSIA_REDUCED_MOTION", "").strip() == "1":
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        enabled = ctypes.c_int(1)
        # SPI_GETCLIENTAREAANIMATION is the documented system-wide preference
        # used for client-area transition and breathing effects.
        if ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(enabled), 0
        ):
            return not bool(enabled.value)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return False


def _major_animation(parent: QObject, callback) -> QVariantAnimation:
    animation = QVariantAnimation(parent)
    animation.setDuration(6400)
    animation.setStartValue(0.0)
    animation.setKeyValueAt(0.5, 1.0)
    animation.setEndValue(0.0)
    animation.setEasingCurve(QEasingCurve.Type.InOutSine)
    animation.setLoopCount(-1)
    animation.valueChanged.connect(callback)
    return animation


class UpdateController(QObject):
    """Schedule updater work without ever blocking Qt's event loop."""

    state_changed = pyqtSignal(dict)
    _check_finished = pyqtSignal(object, bool, object, bool)
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
        self.preview_opt_in = False
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
            self.preview_opt_in = initialize_preview_opt_in(candidate, self.store)
            reconcile_pending_update(candidate, self.store)
            self._state = self._new_state("idle")
            self._restore_cached_manifest()
            self._timer.start(max(1, round(self.scheduler.first_delay() * 1000)))
        else:
            stored_preference = self.store.load().get("preview_opt_in")
            self.preview_opt_in = (
                stored_preference if type(stored_preference) is bool else False
            )
            self._state = self._new_state("disabled")

    def _new_state(self, status: str) -> dict:
        return {
            "status": status,
            "installedVersion": self.metadata.version if self.metadata else PRODUCT_VERSION,
            "channel": self.metadata.channel if self.metadata else "disabled",
            "previewOptIn": self.preview_opt_in,
            "availableChannel": None,
            "availableVersion": None,
            "availablePublishedAt": None,
            "releaseNotes": (),
            "progressReceived": 0,
            "progressTotal": 0,
            "error": None,
            "internalUnsigned": False,
            "majorUpdate": False,
            "bannerVisible": False,
        }

    def _restore_cached_manifest(self) -> None:
        """Restore a previously validated newer manifest without network I/O."""

        cached = self.store.load().get("cachedManifest")
        if not isinstance(cached, dict) or self.metadata is None:
            return
        try:
            cached_channel = cached.get("channel")
            if cached_channel not in {"stable", "internal-preview"}:
                return
            if cached_channel == "internal-preview" and not self.preview_opt_in:
                return
            manifest = parse_manifest(
                json.dumps(cached, ensure_ascii=False, allow_nan=False),
                replace(self.metadata, channel=cached_channel),
            )
        except Exception:
            return
        if manifest.rollout_state != "enabled":
            return
        self.manifest = manifest
        self._state.update(
            status="available",
            availableVersion=manifest.version,
            availableChannel=manifest.channel,
            availablePublishedAt=manifest.published_at,
            releaseNotes=manifest.release_notes,
            majorUpdate=is_major_update(manifest.version),
            internalUnsigned=(
                manifest.channel == "internal-preview"
                and not manifest.installer.signed
            ),
        )

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
            checked_preview = self.preview_opt_in
            try:
                result = fetch_eligible_manifest(
                    self.metadata,
                    self.store,
                    preview_opt_in=checked_preview,
                )
                self._check_finished.emit(result, manual, None, checked_preview)
            except Exception as exc:
                self._check_finished.emit(None, manual, exc, checked_preview)

        threading.Thread(target=worker, name="osia-update-check", daemon=True).start()
        return True

    def _on_check_finished(
        self,
        result,
        manual: bool,
        error,
        checked_preview: bool,
    ) -> None:
        code = safe_error_code(error) if error is not None else None
        success = error is None or code in {"equal_version", "downgrade"}
        delay = self.scheduler.complete(success)
        self._timer.start(max(1, round(delay * 1000)))
        if checked_preview != self.preview_opt_in:
            self._timer.stop()
            self.check(manual=manual)
            return
        if error is not None:
            if code in {"equal_version", "downgrade"}:
                self.manifest = None
                self.store.update(lastError=None)
                self._publish(
                    status="no_update", availableVersion=None,
                    availableChannel=None, availablePublishedAt=None,
                    releaseNotes=(), majorUpdate=False, internalUnsigned=False,
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
                availableVersion=None, availableChannel=None,
                availablePublishedAt=None, releaseNotes=(), majorUpdate=False,
                internalUnsigned=False,
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
            availableChannel=manifest.channel,
            availablePublishedAt=manifest.published_at,
            releaseNotes=manifest.release_notes,
            majorUpdate=is_major_update(manifest.version),
            internalUnsigned=(manifest.channel == "internal-preview" and not manifest.installer.signed),
            bannerVisible=visible,
            error=None,
        )

    def set_preview_opt_in(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        if enabled == self.preview_opt_in:
            return False
        persist_preview_opt_in(self.store, enabled)
        self.preview_opt_in = enabled
        if (
            not enabled
            and self.manifest is not None
            and self.manifest.channel == "internal-preview"
        ):
            self.manifest = None
            self.installer = None
            self._publish(
                status="idle",
                previewOptIn=False,
                availableVersion=None,
                availableChannel=None,
                availablePublishedAt=None,
                releaseNotes=(),
                majorUpdate=False,
                internalUnsigned=False,
                bannerVisible=False,
                error=None,
            )
        else:
            self._publish(previewOptIn=enabled, bannerVisible=False, error=None)
        if self.enabled:
            self.check(manual=True)
        return True

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
        major = bool(state.get("majorUpdate") and self._version)
        self.setObjectName("majorUpdateBanner" if major else "updateBanner")
        self.style().unpolish(self)
        self.style().polish(self)
        self._retranslate()
        self.setVisible(bool(state.get("bannerVisible") and self._version))


class _ReleaseCard(QFrame):
    """Compact plain-text release card shared by current/history/remote notes."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("releaseCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 9, 11, 9)
        layout.setSpacing(5)
        heading = QHBoxLayout()
        self.version = QLabel()
        self.version.setObjectName("releaseVersion")
        self.badge = QLabel()
        self.badge.setObjectName("releaseBadge")
        heading.addWidget(self.version)
        heading.addStretch(1)
        heading.addWidget(self.badge)
        self.date = QLabel()
        self.date.setObjectName("sectionSub")
        self.notes_scroll = QScrollArea()
        self.notes_scroll.setObjectName("releaseNotesScroll")
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.notes_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.notes = QLabel()
        self.notes.setObjectName("sectionDesc")
        self.notes.setTextFormat(Qt.TextFormat.PlainText)
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.notes.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.notes_scroll.setWidget(self.notes)
        self._major_update = False
        self._breath = 0.45
        self._reduced_motion = _system_reduced_motion()
        self._animation = _major_animation(self, self._set_breath)
        layout.addLayout(heading)
        layout.addWidget(self.date)
        layout.addWidget(self.notes_scroll)

    def _set_breath(self, value) -> None:
        self._breath = float(value)
        self.update()

    def _set_major_update(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._major_update == enabled:
            return
        self._major_update = enabled
        self.setObjectName("majorUpdateCard" if enabled else "releaseCard")
        self.style().unpolish(self)
        self.style().polish(self)
        if enabled and not self._reduced_motion and self.isVisible():
            self._animation.start()
        else:
            self._animation.stop()
            self._breath = 0.45
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._major_update and not self._reduced_motion:
            self._animation.start()

    def hideEvent(self, event) -> None:
        self._animation.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._major_update:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        theme = get_theme_manager().palette
        dark = QColor(theme.background).lightnessF() < 0.5
        base = QColor(theme.elevated_surface if dark else theme.surface)
        purple = QColor("#8b5cff" if dark else "#6c36e8")
        cyan = QColor("#34d8ff" if dark else "#007f9c")
        glow_alpha = round((34 if dark else 24) + self._breath * 32)
        purple.setAlpha(glow_alpha)
        cyan.setAlpha(glow_alpha)
        gradient = QLinearGradient(
            QPointF(bounds.left(), bounds.top()),
            QPointF(bounds.right(), bounds.bottom()),
        )
        gradient.setColorAt(0.0, purple)
        gradient.setColorAt(0.48 + (self._breath - 0.5) * 0.08, base)
        gradient.setColorAt(1.0, cyan)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(bounds, 11.0, 11.0)
        border = QColor("#a979ff" if dark else "#7442dc")
        border.setAlpha(round(120 + self._breath * 70))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border, 1.2))
        painter.drawRoundedRect(bounds, 11.0, 11.0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._resize_notes)

    def _resize_notes(self) -> None:
        width = max(1, self.notes_scroll.viewport().width() - 4)
        self.notes.setFixedWidth(width)
        self.notes.adjustSize()
        wanted = max(34, self.notes.sizeHint().height() + 6)
        self.notes_scroll.setFixedHeight(min(wanted, 390))

    def apply(
        self,
        *,
        version: str,
        released_on: str | None,
        notes: tuple[str, ...],
        badge: str = "",
        sections: tuple[tuple[str, tuple[str, ...]], ...] = (),
        major_update: bool = False,
    ) -> None:
        self._set_major_update(major_update)
        self.version.setText(i18n.t("updates.version", version=version))
        self.badge.setText(badge)
        self.badge.setVisible(bool(badge))
        self.date.setText(
            i18n.t("updates.release_date", date=released_on)
            if released_on else i18n.t("updates.date_unavailable")
        )
        if sections:
            groups: list[str] = []
            for title, changes in sections:
                block = [title.upper()] if title else []
                block.extend(f"• {note}" for note in changes)
                groups.append("\n".join(block))
            text = "\n\n".join(groups)
        else:
            shown = notes or (i18n.t("updates.notes_unavailable"),)
            text = "\n".join(f"• {note}" for note in shown)
        self.notes.setText(text)
        QTimer.singleShot(0, self._resize_notes)


class _MajorProgressBar(QProgressBar):
    """Updater-only gradient progress with a restrained breathing treatment."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._major_update = False
        self._breath = 0.45
        self._reduced_motion = _system_reduced_motion()
        self._animation = _major_animation(self, self._set_breath)

    def _set_breath(self, value) -> None:
        self._breath = float(value)
        self.update()

    def set_major_update(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._major_update == enabled:
            return
        self._major_update = enabled
        if enabled and not self._reduced_motion and self.isVisible():
            self._animation.start()
        else:
            self._animation.stop()
            self._breath = 0.45
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._major_update and not self._reduced_motion:
            self._animation.start()

    def hideEvent(self, event) -> None:
        self._animation.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        if not self._major_update:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        theme = get_theme_manager().palette
        dark = QColor(theme.background).lightnessF() < 0.5
        track = QColor(theme.surface if dark else theme.elevated_surface)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(bounds, 7.0, 7.0)
        span = self.maximum() - self.minimum()
        fraction = (self.value() - self.minimum()) / span if span > 0 else 0.0
        fill = QRectF(bounds)
        fill.setWidth(max(0.0, bounds.width() * max(0.0, min(1.0, fraction))))
        if fill.width() > 0:
            purple = QColor("#8b5cff" if dark else "#6c36e8")
            cyan = QColor("#34d8ff" if dark else "#007f9c")
            pulse = round(self._breath * 22)
            purple = purple.lighter(100 + pulse)
            cyan = cyan.lighter(100 + pulse)
            gradient = QLinearGradient(fill.topLeft(), fill.topRight())
            gradient.setColorAt(0.0, purple)
            gradient.setColorAt(1.0, cyan)
            path = QPainterPath()
            path.addRoundedRect(bounds, 7.0, 7.0)
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(fill, gradient)
            painter.restore()
        border = QColor("#a979ff" if dark else "#7442dc")
        border.setAlpha(round(125 + self._breath * 65))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(bounds, 7.0, 7.0)
        painter.setPen(QColor(theme.text))
        painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, self.text())


class UpdateSettingsSection(QFrame):
    def __init__(self, controller: UpdateController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("section")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(9)
        self.title = QLabel()
        self.title.setObjectName("sectionTitle")
        self.summary = QLabel()
        self.summary.setObjectName("sectionSub")
        self.summary.setWordWrap(True)
        self.preference_card = QFrame()
        self.preference_card.setObjectName("releaseCard")
        preference_layout = QVBoxLayout(self.preference_card)
        preference_layout.setContentsMargins(11, 9, 11, 9)
        preference_layout.setSpacing(5)
        preference_row = QHBoxLayout()
        preference_row.setSpacing(8)
        self.preview_label = QLabel()
        self.preview_label.setObjectName("releaseVersion")
        self.preview_label.setWordWrap(True)
        self.preview_mode = QLabel()
        self.preview_mode.setObjectName("releaseBadge")
        self.preview_toggle = QCheckBox()
        self.preview_toggle.setObjectName("smartToggle")
        self.preview_label.setBuddy(self.preview_toggle)
        preference_row.addWidget(self.preview_label, 1)
        preference_row.addWidget(self.preview_mode)
        preference_row.addWidget(self.preview_toggle)
        self.preview_help = QLabel()
        self.preview_help.setObjectName("sectionDesc")
        self.preview_help.setWordWrap(True)
        preference_layout.addLayout(preference_row)
        preference_layout.addWidget(self.preview_help)
        self.current_card = _ReleaseCard()
        self.available_card = _ReleaseCard()
        self.available_card.hide()
        self.empty_state = QLabel()
        self.empty_state.setObjectName("sectionDesc")
        self.empty_state.setWordWrap(True)
        self.history_button = QPushButton()
        self.history_button.setObjectName("ghostBtn")
        self.history_button.setCheckable(True)
        self.history_button.setAccessibleName(i18n.t("updates.history"))
        self.history_host = QFrame()
        self.history_host.setObjectName("historyHost")
        self.history_layout = QVBoxLayout(self.history_host)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(7)
        self.history_host.hide()
        self.progress = _MajorProgressBar()
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
        layout.addWidget(self.preference_card)
        layout.addWidget(self.current_card)
        layout.addWidget(self.available_card)
        layout.addWidget(self.empty_state)
        layout.addWidget(self.history_button)
        layout.addWidget(self.history_host)
        layout.addWidget(self.progress)
        layout.addLayout(actions)
        self.history_button.toggled.connect(self._on_history_toggled)
        self.preview_toggle.stateChanged.connect(self._on_preview_toggled)
        self.check_button.clicked.connect(lambda: controller.check(manual=True))
        self.install_button.clicked.connect(lambda: controller.install(self))
        controller.state_changed.connect(self.apply_state)
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))
        self._state = controller.state
        self.apply_state(self._state)

    def _retranslate(self) -> None:
        self.apply_state(self._state)

    def _on_history_toggled(self, visible: bool) -> None:
        self.history_host.setVisible(visible)
        self.history_button.setText(
            i18n.t("updates.hide_history") if visible else i18n.t("updates.history")
        )
        self.history_button.setAccessibleName(self.history_button.text())

    def _on_preview_toggled(self, _state: int) -> None:
        requested = self.preview_toggle.isChecked()
        if requested == self.controller.preview_opt_in:
            return
        if requested:
            answer = QMessageBox.question(
                self,
                i18n.t("updates.preview.confirm_title"),
                i18n.t("updates.preview.confirm_text"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.preview_toggle.blockSignals(True)
                self.preview_toggle.setChecked(False)
                self.preview_toggle.blockSignals(False)
                return
        self.controller.set_preview_opt_in(requested)

    def apply_state(self, state: dict) -> None:
        self._state = dict(state)
        status = state.get("status", "idle")
        installed = state.get("installedVersion", "—")
        language = i18n.get_language()
        self.title.setText(i18n.t("updates.title"))
        self.summary.setText(i18n.t(
            "updates.installed_summary",
            version=installed,
            status=i18n.t(f"updates.status.{status}"),
        ))
        preview_enabled = bool(state.get("previewOptIn", False))
        self.preview_label.setText(i18n.t("updates.preview.label"))
        self.preview_mode.setText(i18n.t(
            "updates.preview.mode.enabled"
            if preview_enabled else "updates.preview.mode.stable"
        ))
        self.preview_help.setText(i18n.t(
            "updates.preview.help.enabled"
            if preview_enabled else "updates.preview.help.stable"
        ))
        self.preview_toggle.setAccessibleName(self.preview_label.text())
        self.preview_toggle.setAccessibleDescription(self.preview_help.text())
        self.preview_toggle.setToolTip(self.preview_help.text())
        self.preview_toggle.blockSignals(True)
        self.preview_toggle.setChecked(preview_enabled)
        self.preview_toggle.blockSignals(False)
        self.preview_toggle.setEnabled(
            status not in {"downloading", "verifying", "pending"}
        )

        current = release_for_version(installed)
        self.current_card.apply(
            version=installed,
            released_on=current.released_on if current else None,
            notes=current.changes(language) if current else (),
            sections=current.note_sections(language) if current else (),
            badge=(
                i18n.t("updates.current_major")
                if is_major_update(installed) else i18n.t("updates.current")
            ),
            major_update=is_major_update(installed),
        )

        available = state.get("availableVersion")
        notes = list(normalize_remote_notes(state.get("releaseNotes") or ()))
        available_entry = release_for_version(str(available)) if available else None
        major_available = bool(
            state.get("majorUpdate") or is_major_update(str(available))
        ) if available else False
        if state.get("internalUnsigned"):
            notes.insert(0, i18n.t("updates.internal_unsigned"))
        self.available_card.setVisible(bool(available))
        if available:
            published = state.get("availablePublishedAt")
            self.available_card.apply(
                version=str(available),
                released_on=str(published)[:10] if published else None,
                notes=(
                    available_entry.changes(language)
                    if available_entry is not None else tuple(notes)
                ),
                sections=(
                    available_entry.note_sections(language)
                    if available_entry is not None else ()
                ),
                badge=(
                    i18n.t("updates.major_update")
                    if major_available else i18n.t("updates.available_badge")
                ),
                major_update=major_available,
            )

        if status == "offline":
            empty = i18n.t("updates.offline_installed_available")
        elif state.get("error"):
            empty = i18n.t("updates.error", code=state["error"])
        elif not available:
            empty = i18n.t("updates.no_newer")
        else:
            empty = ""
        self.empty_state.setText(empty)
        self.empty_state.setVisible(bool(empty))

        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for entry in RELEASE_HISTORY:
            card = _ReleaseCard()
            card.apply(
                version=entry.version,
                released_on=entry.released_on,
                notes=entry.changes(language),
                sections=entry.note_sections(language),
                badge=(
                    i18n.t("updates.current_major")
                    if entry.version == installed and is_major_update(entry.version)
                    else i18n.t("updates.current") if entry.version == installed
                    else i18n.t("updates.internal") if not entry.public else ""
                ),
                major_update=is_major_update(entry.version),
            )
            self.history_layout.addWidget(card)
        self.history_button.setText(
            i18n.t("updates.hide_history")
            if self.history_button.isChecked() else i18n.t("updates.history")
        )
        self.history_button.setAccessibleName(self.history_button.text())
        total = int(state.get("progressTotal") or 0)
        received = int(state.get("progressReceived") or 0)
        self.progress.set_major_update(major_available)
        self.progress.setVisible(status in {"downloading", "verifying"})
        self.progress.setValue(round(received * 1000 / total) if total else 0)
        self.check_button.setText(
            i18n.t("updates.retry") if status in {"error", "offline"}
            else i18n.t("updates.check")
        )
        self.check_button.setEnabled(status not in {"checking", "downloading", "verifying", "pending"})
        self.install_button.setText(i18n.t("updates.install"))
        self.install_button.setVisible(status in {"available", "ready"})
