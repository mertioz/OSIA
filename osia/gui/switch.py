"""Canonical animated binary switch for the OSIA desktop interface."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import (
    QEvent,
    QEasingCurve,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QCheckBox, QWidget

from osia.gui.motion import reduced_motion_requested
from osia.gui.theme import ThemeManager, get_theme_manager


def _blend(left: QColor, right: QColor, progress: float) -> QColor:
    progress = max(0.0, min(1.0, float(progress)))
    inverse = 1.0 - progress
    return QColor.fromRgbF(
        left.redF() * inverse + right.redF() * progress,
        left.greenF() * inverse + right.greenF() * progress,
        left.blueF() * inverse + right.blueF() * progress,
        left.alphaF() * inverse + right.alphaF() * progress,
    )


class OsiaSwitch(QCheckBox):
    """OSIA-styled sliding switch with the standard ``QCheckBox`` contract.

    Keeping the native checkbox base preserves Qt's checked state, signals,
    keyboard interaction, focus chain and accessibility exposure. Only the
    presentation and bounded state transition are custom.
    """

    TRACK_WIDTH = 40
    TRACK_HEIGHT = 22
    CONTROL_WIDTH = TRACK_WIDTH + 6
    CONTROL_HEIGHT = TRACK_HEIGHT + 6
    THUMB_DIAMETER = 16
    TEXT_GAP = 8
    ANIMATION_MS = 220

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        reduced_motion: Callable[[], bool] | None = None,
        theme_manager: ThemeManager | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("osiaSwitch")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self._theme = theme_manager or get_theme_manager()
        self._reduced_motion = reduced_motion or (
            lambda: reduced_motion_requested(self)
        )
        self._hovered = False
        self._thumb_position = 1.0 if self.isChecked() else 0.0
        self._target_position = self._thumb_position
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.valueChanged.connect(self._set_thumb_position)
        self._animation.finished.connect(self._settle_animation)
        self._theme.theme_changed.connect(self._on_theme_changed)

    @property
    def thumb_position(self) -> float:
        return self._thumb_position

    @property
    def target_position(self) -> float:
        return self._target_position

    @property
    def active_animation_count(self) -> int:
        return int(
            self._animation.state()
            == QVariantAnimation.State.Running
        )

    def sizeHint(self) -> QSize:
        width = self.CONTROL_WIDTH
        if self.text():
            width += self.TEXT_GAP + self.fontMetrics().horizontalAdvance(self.text())
        height = max(self.CONTROL_HEIGHT, self.fontMetrics().height())
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, position) -> bool:
        return self.rect().contains(position)

    def checkStateSet(self) -> None:
        super().checkStateSet()
        self._sync_visual_state()

    def nextCheckState(self) -> None:
        super().nextCheckState()
        self._sync_visual_state()

    def showEvent(self, event) -> None:
        self._sync_visual_state(animate=False)
        super().showEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() in {
            QEvent.Type.EnabledChange,
            QEvent.Type.FontChange,
            QEvent.Type.LayoutDirectionChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        }:
            self.updateGeometry()
            self.update()
        super().changeEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self.update()

    def _sync_visual_state(self, *, animate: bool = True) -> None:
        target = 1.0 if self.isChecked() else 0.0
        self._target_position = target
        if (
            not animate
            or not self.isVisible()
            or self._reduced_motion()
        ):
            self._animation.stop()
            self._set_thumb_position(target)
            return
        if abs(self._thumb_position - target) < 0.001:
            self._animation.stop()
            self._set_thumb_position(target)
            return
        if self.active_animation_count and self._animation.endValue() == target:
            return
        self._animation.stop()
        self._animation.setStartValue(self._thumb_position)
        self._animation.setEndValue(target)
        self._animation.start()

    def _set_thumb_position(self, value) -> None:
        self._thumb_position = max(0.0, min(1.0, float(value)))
        self.update()

    def _settle_animation(self) -> None:
        self._set_thumb_position(self._target_position)

    def _on_theme_changed(self, _key: str) -> None:
        self.update()

    def _track_rect(self) -> QRectF:
        top = (self.height() - self.TRACK_HEIGHT) / 2.0
        inset = (self.CONTROL_WIDTH - self.TRACK_WIDTH) / 2.0
        if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
            left = self.width() - self.CONTROL_WIDTH + inset
        else:
            left = inset
        return QRectF(left, top, self.TRACK_WIDTH, self.TRACK_HEIGHT)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self._theme.palette
        progress = self._thumb_position

        surface = QColor(palette.surface)
        muted = QColor(palette.muted_text)
        accent = QColor(palette.primary)
        off_track = _blend(surface, muted, 0.42)
        track = _blend(off_track, accent, progress)
        border = _blend(QColor(palette.border), accent, progress)
        if self._hovered and self.isEnabled():
            track = track.lighter(108)
        if self.isDown() and self.isEnabled():
            track = track.darker(108)

        track_rect = self._track_rect()
        if not self.isEnabled():
            painter.setOpacity(0.52)
        if self.hasFocus():
            focus = _blend(QColor(palette.focus), QColor(palette.text), 0.52)
            focus.setAlpha(220 if self.isEnabled() else 100)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(focus, 1.4))
            painter.drawRoundedRect(
                track_rect.adjusted(-2.0, -2.0, 2.0, 2.0),
                self.TRACK_HEIGHT / 2.0 + 2.0,
                self.TRACK_HEIGHT / 2.0 + 2.0,
            )

        painter.setBrush(track)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(
            track_rect,
            self.TRACK_HEIGHT / 2.0,
            self.TRACK_HEIGHT / 2.0,
        )

        radius = self.THUMB_DIAMETER / 2.0
        inset = self.TRACK_HEIGHT / 2.0
        start_x = track_rect.left() + inset
        end_x = track_rect.right() - inset
        center_x = start_x + (end_x - start_x) * progress
        center_y = track_rect.center().y()
        thumb = _blend(QColor(palette.text), QColor(palette.background), progress)
        painter.setPen(QPen(QColor(0, 0, 0, 45), 0.8))
        painter.setBrush(thumb)
        painter.drawEllipse(
            QRectF(
                center_x - radius,
                center_y - radius,
                self.THUMB_DIAMETER,
                self.THUMB_DIAMETER,
            )
        )

        if self.text():
            painter.setOpacity(1.0 if self.isEnabled() else 0.62)
            painter.setPen(QColor(palette.text if self.isEnabled() else palette.disabled))
            if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
                text_rect = QRectF(
                    0.0,
                    0.0,
                    track_rect.left() - self.TEXT_GAP,
                    self.height(),
                )
                alignment = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            else:
                text_rect = QRectF(
                    track_rect.right() + self.TEXT_GAP,
                    0.0,
                    self.width() - track_rect.right() - self.TEXT_GAP,
                    self.height(),
                )
                alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            painter.drawText(text_rect, alignment, self.text())


__all__ = ["OsiaSwitch"]
