"""Small, bounded motion primitives shared by the desktop GUI."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from PyQt6.QtCore import QEasingCurve, QObject, Qt, QVariantAnimation
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QWidget,
)


@dataclass(frozen=True)
class MotionTokens:
    micro_ms: int = 110
    state_ms: int = 140
    appearance_ms: int = 180
    page_ms: int = 190


MOTION = MotionTokens()


@lru_cache(maxsize=1)
def _platform_reduced_motion() -> bool:
    app = QApplication.instance()
    if app is not None:
        try:
            getter = getattr(app.styleHints(), "reduceMotion", None)
            if callable(getter):
                return bool(getter())
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            import ctypes

            enabled = ctypes.c_int(1)
            ok = ctypes.windll.user32.SystemParametersInfoW(
                0x1042, 0, ctypes.byref(enabled), 0
            )
            if ok:
                return not bool(enabled.value)
        except Exception:
            pass
    return False


def reduced_motion_requested(_widget: QWidget | None = None) -> bool:
    """Return the user's reduced-motion preference when the platform exposes it.

    ``OSIA_REDUCED_MOTION`` is also supported for deterministic accessibility,
    test and remote-desktop environments. OS-specific probing is guarded so all
    three desktop platforms keep importing safely.
    """
    value = os.environ.get("OSIA_REDUCED_MOTION", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False

    return _platform_reduced_motion()


class MotionController(QObject):
    """Run replace-in-place opacity transitions without animation queues."""

    def __init__(
        self,
        parent: QObject | None = None,
        reduced_motion: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._reduced_motion = reduced_motion or reduced_motion_requested
        self._states: dict[
            int, tuple[QWidget, QGraphicsOpacityEffect, QVariantAnimation]
        ] = {}

    @property
    def active_animation_count(self) -> int:
        return len(self._states)

    def fade_in(self, widget: QWidget, duration_ms: int = MOTION.page_ms) -> None:
        if self._reduced_motion() or not widget.isVisible():
            self._finish(id(widget), force_opacity=1.0)
            return
        self._animate(widget, 0.0, 1.0, duration_ms)

    def _animate(
        self,
        widget: QWidget,
        start: float,
        end: float,
        duration_ms: int,
    ) -> None:
        key = id(widget)
        state = self._states.get(key)
        if state is None:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(start)
            widget.setGraphicsEffect(effect)
            animation = QVariantAnimation(self)
            animation.valueChanged.connect(effect.setOpacity)
            animation.finished.connect(lambda k=key: self._finish(k))
            widget.destroyed.connect(lambda _=None, k=key: self._discard(k))
            self._states[key] = (widget, effect, animation)
        else:
            _, effect, animation = state
            animation.stop()
            effect.setOpacity(start)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(max(0, int(duration_ms)))
        animation.start()

    def _discard(self, key: int) -> None:
        state = self._states.pop(key, None)
        if state is None:
            return
        _widget, _effect, animation = state
        try:
            animation.stop()
            animation.deleteLater()
        except RuntimeError:
            # A parent can destroy its animation children before the target
            # widget emits destroyed. Its native animation is already stopped.
            pass

    def _finish(self, key: int, force_opacity: float | None = None) -> None:
        state = self._states.pop(key, None)
        if state is None:
            return
        widget, effect, animation = state
        try:
            animation.stop()
        except RuntimeError:
            pass
        try:
            if force_opacity is not None:
                effect.setOpacity(force_opacity)
            widget.setGraphicsEffect(None)
        except RuntimeError:
            pass
        try:
            animation.deleteLater()
        except RuntimeError:
            pass

    def settle(self) -> None:
        for key in tuple(self._states):
            self._finish(key, force_opacity=1.0)


class ThemeTransition(QWidget):
    """Fade the previous frame while the live window adopts its new palette."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frame = QPixmap()
        self._alpha = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        self._animation.valueChanged.connect(self._set_alpha)
        self._animation.finished.connect(self._clear)
        self.hide()

    def capture(self, _key: str) -> None:
        window = self.parentWidget()
        if not window.isVisible() or reduced_motion_requested(window):
            self._animation.stop()
            self._clear()
            return
        # A second switch starts from the frame the user currently sees.
        frame = window.grab()
        self._animation.stop()
        self._frame = frame
        self._alpha = 1.0
        self.setGeometry(window.rect())
        self.raise_()
        self.show()

    def start(self, _key: str) -> None:
        if self.isVisible() and not self._frame.isNull():
            self._animation.start()

    def _set_alpha(self, value: float) -> None:
        self._alpha = value
        self.update()

    def _clear(self) -> None:
        self.hide()
        self._frame = QPixmap()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setOpacity(self._alpha)
        painter.drawPixmap(0, 0, self._frame)
        painter.end()
