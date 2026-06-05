#!/usr/bin/env python3
"""
OSIA Dynamic Island — small floating capsule inspired by iOS's Dynamic
Island. It starts centered at the top of the screen, can be dragged
anywhere, and restores its last dragged position on the next launch
(persisted as ``island_x`` / ``island_y`` in ``config/settings.json``).

The capsule has four visual states:

* ``idle``      — tiny, semi-transparent, waiting.
* ``listening`` — centred mic glyph + 3 concentric violet pulse rings.
* ``thinking``  — seven violet dots bobbing in a sine wave that travels
                  from left to right across the capsule centre.
* ``speaking``  — bouncing waveform bars.

The window itself is frameless / always-on-top / not in the taskbar.
``WA_TranslucentBackground`` keeps the corners outside the capsule
click-through; only the painted capsule actually receives mouse events.
A click on the capsule (press + release without dragging) emits
``clicked`` (used to toggle the :class:`osia.gui.main_popup.MainPopup`);
press + move drags the capsule instead.
"""

import math

from PyQt6.QtCore import (
    Qt, QTimer, QRect, QRectF, QPointF, pyqtSignal, QPropertyAnimation,
    QEasingCurve, QVariantAnimation,
)
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QFont, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget


# Visual constants
ISLAND_W = 320
ISLAND_H = 52
RADIUS = 26
BG_COLOR = QColor(13, 11, 18)               # #0d0b12
ACCENT = QColor(196, 181, 245)              # #c4b5f5
ACCENT_DIM = QColor(196, 181, 245, 80)
WHITE = QColor(255, 255, 255)

IDLE_OPACITY = 0.55
ACTIVE_OPACITY = 1.0

# Launch animation — the capsule starts tiny at the top-centre and grows
# to its full size, with the "OSIA" wordmark fading in at the tail end.
LAUNCH_W = 60
LAUNCH_H = 20
LAUNCH_MS = 600

# Idle state — the OSIA logo sits to the left of the "OSIA" wordmark.
IDLE_LOGO_H = 44               # px; the capsule is 52px tall
IDLE_LOGO_GAP = 8              # px between the logo and the wordmark

# Thinking animation — traveling wave. Seven dots sit on a horizontal line
# through the capsule centre; each dot oscillates vertically on a sine wave
# with a per-dot phase offset, so the crest glides smoothly left → right.
THINK_DOT_COUNT = 7
THINK_DOT_R = 2.5               # dot radius → 5px diameter
THINK_DOT_SPACING = 12.0        # px between adjacent dot centres
THINK_WAVE_AMP = 8.0            # px of vertical travel
THINK_WAVE_PERIOD_MS = 800      # one full oscillation per dot
THINK_WAVE_PHASE = math.pi / 3  # phase shift between adjacent dots (60°)

VALID_STATES = ("idle", "listening", "thinking", "speaking")


class DynamicIsland(QWidget):
    """Top-center floating capsule with state-driven animations."""

    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(ISLAND_W, ISLAND_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # OSIA logo shown beside the wordmark in the idle state. Scaled once
        # at load (the idle paint runs at 30 FPS). A null pixmap — asset
        # missing or osia not importable in the standalone self-test — is
        # simply skipped while painting, so the wordmark still shows.
        self._logo = QPixmap()
        try:
            from osia.utils.assets import LOGO_PATH
            logo = QPixmap(LOGO_PATH)
            if not logo.isNull():
                self._logo = logo.scaledToHeight(
                    IDLE_LOGO_H, Qt.TransformationMode.SmoothTransformation
                )
        except Exception:
            pass

        # Animation state
        self._state = "idle"
        self._phase = 0.0
        # Milliseconds elapsed in the current state — drives time-based
        # animations (thinking dots). Reset on every state change.
        self._anim_ms = 0
        # 0.0 → 1.0 launch-expand progress; starts collapsed.
        self._launch_t = 0.0

        # Drag state — a press inside the capsule may become either a click
        # (emit ``clicked`` on release) or a drag (move the window). None /
        # False when no press is in progress.
        self._drag_offset = None
        self._press_global = None
        self._dragging = False

        # Opacity is animated on state changes so the transitions feel smooth.
        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity")
        self._opacity_anim.setDuration(220)
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Launch-expand animation — grows the painted capsule from tiny to full.
        self._launch_anim = QVariantAnimation(self)
        self._launch_anim.setStartValue(0.0)
        self._launch_anim.setEndValue(1.0)
        self._launch_anim.setDuration(LAUNCH_MS)
        self._launch_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._launch_anim.valueChanged.connect(self._on_launch_value)

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 FPS, plenty for the small island
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.setWindowOpacity(IDLE_OPACITY)
        self._restore_position()
        self.show()
        self._launch_anim.start()

    # ── Public API ────────────────────────────────────────────────────────

    def set_state(self, state: str) -> None:
        """Switch visual state. Unknown states are coerced to ``idle``."""
        if state not in VALID_STATES:
            state = "idle"
        if state == self._state:
            return
        self._state = state
        self._phase = 0.0
        self._anim_ms = 0
        target = IDLE_OPACITY if state == "idle" else ACTIVE_OPACITY
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(target)
        self._opacity_anim.start()
        self.update()

    def current_state(self) -> str:
        return self._state

    # ── Internals ─────────────────────────────────────────────────────────

    def _position_at_top(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + 8
        self.move(x, y)

    # ── Position persistence ──────────────────────────────────────────────

    def _restore_position(self) -> None:
        """Move to the last saved position (``island_x``/``island_y`` in
        settings), falling back to top-centre when nothing is saved or the
        saved point is no longer on any connected screen (monitor unplugged,
        resolution change).
        """
        try:
            # Lazy import so the module stays importable standalone
            # (self-test below) even without the project on sys.path.
            from osia.config import load_settings
            settings = load_settings()
            x, y = settings.get("island_x"), settings.get("island_y")
        except Exception:
            x = y = None
        if x is None or y is None:
            self._position_at_top()
            return
        rect = QRect(int(x), int(y), self.width(), self.height())
        for screen in QApplication.screens():
            if screen.availableGeometry().intersects(rect):
                self.move(rect.topLeft())
                return
        self._position_at_top()

    def _save_position(self) -> None:
        """Persist the capsule position so the next launch restores it."""
        try:
            from osia.config import load_settings, save_settings
            settings = load_settings()
            settings["island_x"] = self.x()
            settings["island_y"] = self.y()
            save_settings(settings)
        except Exception as e:
            print(f"[DynamicIsland] failed to save position: {e}")

    def showEvent(self, event):
        super().showEvent(event)
        self._restore_position()
        self.raise_()

    def _tick(self) -> None:
        self._phase += 0.10
        if self._phase > 2 * math.pi:
            self._phase -= 2 * math.pi
        self._anim_ms += self._timer.interval()
        # No need to repaint idle very often, but at 30 FPS the cost is
        # negligible and we avoid having to stop/start the timer.
        self.update()

    def _on_launch_value(self, value) -> None:
        self._launch_t = float(value)
        self.update()

    # ── Click / drag handling ─────────────────────────────────────────────
    # A press inside the capsule starts a potential click OR drag; the two
    # are disambiguated by movement: past the platform drag threshold the
    # press becomes a drag (move the window, save the position on release),
    # otherwise the release emits ``clicked``.

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Only react if the press is inside the painted capsule —
            # everything outside the rounded rect should feel click-through.
            path = self._capsule_path()
            if path.contains(event.position()):
                self._press_global = event.globalPosition().toPoint()
                self._drag_offset = (
                    self._press_global - self.frameGeometry().topLeft()
                )
                self._dragging = False
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            global_pos = event.globalPosition().toPoint()
            # Hand jitter on a click must not count as a drag — only start
            # dragging past the platform drag-distance threshold.
            if (not self._dragging
                    and (global_pos - self._press_global).manhattanLength()
                    < QApplication.startDragDistance()):
                event.accept()
                return
            self._dragging = True
            self.move(global_pos - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._drag_offset is not None):
            was_drag = self._dragging
            self._drag_offset = None
            self._press_global = None
            self._dragging = False
            if was_drag:
                self._save_position()
            else:
                # Plain click (no drag) — toggle the popup.
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── Painting ──────────────────────────────────────────────────────────

    def _capsule_rect(self) -> QRectF:
        """Current painted-capsule rect, interpolated by the launch progress.

        At ``_launch_t == 1`` this fills the whole (full-size) window; while
        the launch animation runs it is a smaller capsule centred in the
        window, so the surrounding translucent area stays click-through.
        """
        t = self._launch_t
        w = LAUNCH_W + (ISLAND_W - LAUNCH_W) * t
        h = LAUNCH_H + (ISLAND_H - LAUNCH_H) * t
        x = (self.width() - w) / 2
        y = (self.height() - h) / 2
        return QRectF(x + 0.5, y + 0.5, w - 1, h - 1)

    def _capsule_path(self) -> QPainterPath:
        rect = self._capsule_rect()
        radius = min(RADIUS, rect.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background capsule
        path = self._capsule_path()
        painter.setPen(QPen(QColor(196, 181, 245, 31), 1))
        painter.fillPath(path, BG_COLOR)
        painter.drawPath(path)

        # State-specific overlay
        if self._state == "idle":
            self._paint_idle(painter)
        elif self._state == "listening":
            self._paint_listening(painter)
        elif self._state == "thinking":
            self._paint_thinking(painter)
        elif self._state == "speaking":
            self._paint_speaking(painter)

        painter.end()

    def _paint_idle(self, p: QPainter) -> None:
        """The OSIA logo + wordmark, dim — fading in at the end of the launch."""
        # Hold the mark invisible until the capsule is nearly full size, then
        # fade it in over the last stretch of the launch animation.
        fade = max(0.0, (self._launch_t - 0.7) / 0.3)
        fade = min(1.0, fade)
        if fade <= 0.0:
            return

        rect = self._capsule_rect()
        cy = rect.center().y()

        font = QFont()
        font.setPixelSize(12)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        font.setBold(True)
        p.setFont(font)
        text_w = p.fontMetrics().horizontalAdvance("OSIA")

        # Logo (left) + wordmark (right) laid out as a single group centred in
        # the capsule. The logo is dropped from the layout when unavailable.
        logo_w = self._logo.width() if not self._logo.isNull() else 0
        gap = IDLE_LOGO_GAP if logo_w else 0
        x = rect.center().x() - (logo_w + gap + text_w) / 2.0

        # One opacity for the whole group so logo and text fade in together.
        p.save()
        p.setOpacity(fade)
        if logo_w:
            p.drawPixmap(
                int(round(x)), int(round(cy - self._logo.height() / 2.0)),
                self._logo,
            )
            x += logo_w + gap
        p.setPen(QColor(232, 230, 240, 200))
        text_rect = QRectF(x, rect.top(), text_w + 2, rect.height())
        p.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            "OSIA",
        )
        p.restore()

    def _paint_listening(self, p: QPainter) -> None:
        """Centred mic glyph wrapped in 3 concentric pulse rings.

        The three rings ripple outward in sequence (each a third of a period
        behind the last), fading as they expand — so the capsule reads as a
        radar-style "listening" pulse around a single centred microphone.
        """
        cx = self.width() / 2
        cy = self.height() / 2

        # Three rings, each offset by a third of the cycle, expanding & fading.
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(3):
            t = (self._phase / (2 * math.pi) + i / 3.0) % 1.0
            ring_r = 13 + 14 * t
            ring_alpha = int(150 * (1.0 - t))
            if ring_alpha <= 0:
                continue
            p.setPen(QPen(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(),
                                 ring_alpha), 2))
            p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

        # Organic, hand-drawn microphone — a calligraphic sketch rather than a
        # crisp UI glyph. Everything is stroke-only with round caps so the
        # bezier curves read like a single flowing pen line.
        pen = QPen(ACCENT, 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Mic head — a slightly asymmetric rounded capsule. The start/end points
        # don't quite meet at the top, leaving a small calligraphic gap.
        head = QPainterPath()
        head.moveTo(cx + 0.6, cy - 10)
        head.cubicTo(cx + 5.2, cy - 9.2, cx + 5.6, cy - 3.5, cx + 4.4, cy - 0.2)
        head.cubicTo(cx + 3.4, cy + 2.6, cx - 3.6, cy + 2.8, cx - 4.6, cy - 0.4)
        head.cubicTo(cx - 5.6, cy - 3.8, cx - 5.0, cy - 9.0, cx - 0.6, cy - 10)
        p.drawPath(head)

        # Stand — a flowing arc that cradles the mic from below.
        stand = QPainterPath()
        stand.moveTo(cx - 7.6, cy - 1.5)
        stand.cubicTo(cx - 7.2, cy + 5.5, cx + 7.2, cy + 5.5, cx + 7.6, cy - 1.5)
        p.drawPath(stand)

        # Neck — a gently wavering stem dropping from the cradle.
        stem = QPainterPath()
        stem.moveTo(cx + 0.2, cy + 5.3)
        stem.cubicTo(cx + 0.5, cy + 7.4, cx - 0.5, cy + 8.6, cx + 0.2, cy + 10.4)
        p.drawPath(stem)

        # Base — a small upturned flourish for the foot.
        base = QPainterPath()
        base.moveTo(cx - 3.4, cy + 9.8)
        base.cubicTo(cx - 1.4, cy + 11.4, cx + 1.4, cy + 11.4, cx + 3.4, cy + 9.8)
        p.drawPath(base)

    def _paint_thinking(self, p: QPainter) -> None:
        """Seven violet dots bobbing in a traveling sine wave.

        The dots are evenly spaced on a horizontal line through the capsule
        centre and each one oscillates vertically:

            y_offset = amplitude * sin(2π * t / period - i * phase_shift)

        The per-dot phase shift makes the crest travel smoothly from left
        to right, which reads as a fluid, organic wave.
        """
        cx = self.width() / 2
        cy = self.height() / 2

        n = THINK_DOT_COUNT
        start_x = cx - (n - 1) * THINK_DOT_SPACING / 2
        omega_t = 2 * math.pi * self._anim_ms / THINK_WAVE_PERIOD_MS

        p.setPen(Qt.PenStyle.NoPen)
        color = QColor(ACCENT)              # #c4b5f5
        color.setAlpha(235)
        p.setBrush(color)
        for i in range(n):
            x = start_x + i * THINK_DOT_SPACING
            y = cy + THINK_WAVE_AMP * math.sin(omega_t - i * THINK_WAVE_PHASE)
            p.drawEllipse(QPointF(x, y), THINK_DOT_R, THINK_DOT_R)

    def _paint_speaking(self, p: QPainter) -> None:
        """Symmetric waveform of bars bouncing at independent frequencies."""
        cy = self.height() / 2
        n = 9
        gap = 5
        bar_w = 3
        total_w = n * bar_w + (n - 1) * gap
        start_x = (self.width() - total_w) / 2

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            # Distance from the centre — central bars swing wider.
            dist = abs(i - (n - 1) / 2)
            amp = max(2, 14 - dist * 2.4)
            phase = self._phase * 2.6 + i * 0.7
            h = amp * (0.4 + 0.6 * abs(math.sin(phase)))
            x = start_x + i * (bar_w + gap)
            rect = QRectF(x, cy - h, bar_w, h * 2)
            color = QColor(ACCENT)
            color.setAlpha(220)
            p.setBrush(color)
            p.drawRoundedRect(rect, 1.5, 1.5)


# ── Self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    app = QApplication(sys.argv)
    island = DynamicIsland()
    island.clicked.connect(lambda: print("clicked"))

    # Cycle through states so the visuals can be eyeballed quickly.
    seq = ["idle", "listening", "thinking", "speaking", "idle"]
    idx = {"i": 0}

    def cycle():
        s = seq[idx["i"] % len(seq)]
        print(f"-> {s}")
        island.set_state(s)
        idx["i"] += 1

    QTimer.singleShot(0, cycle)
    timer = QTimer()
    timer.timeout.connect(cycle)
    timer.start(2500)

    sys.exit(app.exec())
