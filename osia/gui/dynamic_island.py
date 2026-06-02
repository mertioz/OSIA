#!/usr/bin/env python3
"""
OSIA Dynamic Island — small floating capsule centered at the top of the
screen, inspired by iOS's Dynamic Island.

The capsule has four visual states:

* ``idle``      — tiny, semi-transparent, waiting.
* ``listening`` — centred mic glyph + 3 concentric violet pulse rings.
* ``thinking``  — three violet dots rotating clockwise around the corners
                  of a square (L-shape choreography, 4th corner always empty).
* ``speaking``  — bouncing waveform bars.

The window itself is frameless / always-on-top / not in the taskbar.
``WA_TranslucentBackground`` keeps the corners outside the capsule
click-through; only the painted capsule actually receives mouse events.
A single click on the capsule emits ``clicked`` (used to toggle the
:class:`osia.gui.main_popup.MainPopup`).
"""

import math

from PyQt6.QtCore import (
    Qt, QTimer, QRectF, QPointF, pyqtSignal, QPropertyAnimation, QEasingCurve,
    QVariantAnimation,
)
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QFont, QPen
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

# Thinking animation — L-shape choreography. Three dots occupy three corners
# of a square (top-left, top-right, bottom-left); each cycle every dot hops
# one position clockwise (bottom-left → top-left → top-right → bottom-left)
# with sine easing. The 4th corner (bottom-right) stays empty, creating the
# rotation illusion.
THINK_CYCLE_MS = 600     # duration of one hop
THINK_SQUARE_HALF = 7.0  # half side-length of the corner square (px)
THINK_DOT_R = 3.0        # dot radius → 6px diameter

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

        # Animation state
        self._state = "idle"
        self._phase = 0.0
        # Milliseconds elapsed in the current state — drives time-based
        # animations (thinking dots). Reset on every state change.
        self._anim_ms = 0
        # 0.0 → 1.0 launch-expand progress; starts collapsed.
        self._launch_t = 0.0

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
        self._position_at_top()
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

    def showEvent(self, event):
        super().showEvent(event)
        self._position_at_top()
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

    # ── Click handling ────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Only react if the click is inside the painted capsule —
            # everything outside the rounded rect should feel click-through.
            path = self._capsule_path()
            if path.contains(event.position()):
                self.clicked.emit()
                event.accept()
                return
        super().mousePressEvent(event)

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
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
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
        """Just the OSIA wordmark, dim — fading in at the end of the launch."""
        # Hold the text invisible until the capsule is nearly full size, then
        # fade it in over the last stretch of the launch animation.
        fade = max(0.0, (self._launch_t - 0.7) / 0.3)
        fade = min(1.0, fade)
        if fade <= 0.0:
            return
        p.setPen(QColor(255, 255, 255, int(130 * fade)))
        font = QFont()
        font.setPixelSize(12)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        font.setBold(True)
        p.setFont(font)
        p.drawText(self._capsule_rect(), Qt.AlignmentFlag.AlignCenter, "OSIA")

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
        """Three violet dots in an L-shape clockwise rotation.

        The dots occupy three corners of a square — top-left, top-right and
        bottom-left — and every ~600ms cycle each dot glides one position
        clockwise along the path bottom-left → top-left → top-right →
        bottom-left, with sine (ease-in-out) easing. The fourth corner
        (bottom-right) is never occupied, which reads as a continuous
        rotation illusion.
        """
        cx = self.width() / 2
        cy = self.height() / 2

        d = THINK_SQUARE_HALF
        # Corner positions in clockwise hop order: BL → TL → TR → (BL).
        corners = (
            (cx - d, cy + d),   # bottom-left
            (cx - d, cy - d),   # top-left
            (cx + d, cy - d),   # top-right
        )

        # Which hop we're on, and the eased progress within it.
        step = self._anim_ms // THINK_CYCLE_MS
        progress = (self._anim_ms % THINK_CYCLE_MS) / THINK_CYCLE_MS
        # Sine ease-in-out: zero velocity at both ends of each hop.
        eased = 0.5 - 0.5 * math.cos(math.pi * progress)

        p.setPen(Qt.PenStyle.NoPen)
        color = QColor(ACCENT)              # #c4b5f5
        color.setAlpha(235)
        p.setBrush(color)
        for i in range(3):
            x0, y0 = corners[(i + step) % 3]
            x1, y1 = corners[(i + step + 1) % 3]
            x = x0 + (x1 - x0) * eased
            y = y0 + (y1 - y0) * eased
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
