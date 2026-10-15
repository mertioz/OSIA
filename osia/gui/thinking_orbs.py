"""Compact local-only six-orb indicator for an active Thinking trace."""

from __future__ import annotations

import math

from PyQt6.QtCore import QElapsedTimer, QPointF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QPushButton, QStyle, QStyleOptionFocusRect, QWidget

from osia.gui.motion import reduced_motion_requested
from osia.gui.theme import get_theme_manager


class ThinkingOrbs(QPushButton):
    """Draw six deterministic accent orbs and animate only while visible."""

    ORB_COUNT = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("thinkingOrbs")
        self.setFixedSize(54, 28)
        self.setFlat(True)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Thinking progress")
        self._active = False
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._on_frame)
        self._theme = get_theme_manager()
        self._theme.theme_changed.connect(self._on_theme_changed)

    @property
    def orb_count(self) -> int:
        return self.ORB_COUNT

    @property
    def is_animating(self) -> bool:
        return self._timer.isActive()

    @property
    def accent_color(self) -> QColor:
        return self._theme.palette.color("primary")

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            self._sync_animation()
            return
        self._active = active
        self._sync_animation()
        self.update()

    def _is_exposed(self) -> bool:
        return self.isVisible() and not self.visibleRegion().isEmpty()

    def _sync_animation(self) -> None:
        if (
            not self._active
            or reduced_motion_requested(self)
            or not self.isVisible()
        ):
            self._timer.stop()
            return
        if not self._elapsed.isValid():
            self._elapsed.start()
        self._timer.setInterval(33 if self._is_exposed() else 250)
        if not self._timer.isActive():
            self._timer.start()

    def _on_frame(self) -> None:
        if not self._active or reduced_motion_requested(self):
            self._timer.stop()
            self.update()
            return
        if not self._is_exposed():
            # Keep only a low-frequency visibility check while clipped by the
            # chat scroll area; no repaints or animation work happen offscreen.
            self._timer.setInterval(250)
            return
        if self._timer.interval() != 33:
            self._timer.setInterval(33)
        self.update()

    def _on_theme_changed(self, _key: str) -> None:
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_animation)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().hideEvent(event)

    @staticmethod
    def orb_positions(
        phase: float, center_x: float, center_y: float,
    ) -> list[tuple[float, float, float, float, int]]:
        """One true periodic orbit: position and velocity match at 0/2π."""
        positions = []
        for index in range(ThinkingOrbs.ORB_COUNT):
            angle = phase + index * math.tau / ThinkingOrbs.ORB_COUNT
            x = center_x + math.cos(angle) * 15.4
            y = center_y + math.sin(angle) * 3.7
            radius = 3.1 + 0.4 * math.sin(angle)
            positions.append((y, x, y, radius, index))
        return positions

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = self.accent_color
        center_x = self.width() / 2
        center_y = self.height() / 2
        if self._active and not reduced_motion_requested(self):
            cycle = (
                (self._elapsed.elapsed() % 1_800) / 1_800.0
            ) * (math.pi * 2)
            positions = self.orb_positions(cycle, center_x, center_y)
        else:
            positions = [
                (
                    center_y + (index % 2) * 1.0,
                    5.0 + index * 8.8,
                    center_y + (index % 2) * 1.0,
                    3.2,
                    index,
                )
                for index in range(self.ORB_COUNT)
            ]

        for depth, x, y, radius, index in sorted(positions):
            orb = QColor(accent)
            orb.setAlpha(max(115, min(255, 174 + int((depth - center_y) * 12))))
            if index % 3 == 1:
                orb = orb.lighter(108)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(orb)
            painter.drawEllipse(QPointF(x, y), radius, radius)
        if self.hasFocus():
            focus = QStyleOptionFocusRect()
            focus.initFrom(self)
            focus.rect = self.rect().adjusted(1, 1, -1, -1)
            self.style().drawPrimitive(
                QStyle.PrimitiveElement.PE_FrameFocusRect, focus, painter, self,
            )
