"""Small, bounded motion primitives shared by the desktop GUI."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from PySide6.QtCore import QEvent, QEasingCurve, QObject, QPoint, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QWidget,
)


@dataclass(frozen=True)
class MotionTokens:
    micro_ms: int = 70
    state_ms: int = 100
    appearance_ms: int = 120
    page_ms: int = 110
    navigation_ms: int = 110
    collapse_ms: int = 90
    theme_ms: int = 150


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
            start = effect.opacity()
            animation.stop()
        animation.setCurrentTime(0)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
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


class ContentTransition(QWidget):
    """Crossfade one visible content area; its live controls stay in place.

    Capture once before a user navigation, then start after the synchronous
    content change. Only a bounded viewport-sized pixmap is painted per frame,
    never the full scrollable history. No input, focus or state is deferred.
    """

    def __init__(self, target: QWidget) -> None:
        super().__init__(target)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frame = QPixmap()
        self._opacity_layer = QPixmap()
        self._alpha = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_alpha)
        self._animation.finished.connect(self.settle)
        target.installEventFilter(self)
        self.hide()

    def capture(self) -> None:
        target = self.parentWidget()
        if (
            not target.isVisible() or target.size().isEmpty()
            or reduced_motion_requested(target)
        ):
            self.settle()
            return
        # Include the current overlay on a rapid second navigation, so the next
        # fade starts from what is actually visible rather than an old endpoint.
        # QWidget.grab only renders OSIA's own client area, not the desktop.
        window = target.window()
        frame = window.grab(QRect(target.mapTo(window, QPoint()), target.size()))
        self._animation.stop()
        self._animation.setCurrentTime(0)
        self._frame = frame
        self._opacity_layer = QPixmap()
        self._alpha = 1.0
        self.setGeometry(target.rect())
        self.raise_()
        self.show()

    def start(self, duration_ms: int = MOTION.navigation_ms) -> None:
        target = self.parentWidget()
        if (
            self._frame.isNull() or not target.isVisible()
            or reduced_motion_requested(target)
        ):
            self.settle()
            return
        self.raise_()
        self._animation.stop()
        self._animation.setCurrentTime(0)
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        self._animation.setDuration(max(0, int(duration_ms)))
        self._animation.start()

    def _set_alpha(self, value: float) -> None:
        self._alpha = float(value)
        self.update()

    def settle(self) -> None:
        self._animation.stop()
        self._alpha = 0.0
        self.hide()
        self._frame = QPixmap()
        self._opacity_layer = QPixmap()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QEvent.Type.Hide, QEvent.Type.Resize):
            self.settle()
        return False

    def paintEvent(self, _event) -> None:
        if self._frame.isNull() or self._alpha <= 0:
            return
        alpha = max(0.0, min(1.0, self._alpha))
        source_alpha = round(255 * alpha)
        if alpha < 1:
            if (
                self._opacity_layer.size() != self._frame.size()
                or self._opacity_layer.devicePixelRatio() != self._frame.devicePixelRatio()
            ):
                self._opacity_layer = QPixmap(self._frame.size())
                self._opacity_layer.setDevicePixelRatio(self._frame.devicePixelRatio())
            self._opacity_layer.fill(Qt.GlobalColor.transparent)
            fade = QPainter(self._opacity_layer)
            fade.drawPixmap(0, 0, self._frame)
            fade.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            fade.fillRect(self.rect(), QColor(0, 0, 0, source_alpha))
            fade.end()

        painter = QPainter(self)
        if alpha >= 1:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.drawPixmap(0, 0, self._frame)
        else:
            # Scale the source on a transparent surface BEFORE adding it. Qt's
            # Plus composition with painter opacity clamps source+destination
            # before applying that opacity: even two identical opaque frames
            # become translucent mid-fade, revealing a bright desktop beneath.
            # Each premultiplied term is now weighted before the sum, keeping
            # the background alpha constant without freezing the live controls.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 255 - source_alpha))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            painter.drawPixmap(0, 0, self._opacity_layer)
        painter.end()


class ThemeTransition(ContentTransition):
    """Use the same alpha-preserving fade for popup and capsule theme changes."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._animation.setDuration(MOTION.theme_ms)

    def capture(self, _key: str) -> None:
        super().capture()

    def start(self, _key: str) -> None:
        super().start(MOTION.theme_ms)

    def _clear(self) -> None:
        self.settle()
