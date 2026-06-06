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

# ── Adaptive shape (WOR-22) ──────────────────────────────────────────────────
# Independent of the animation state above: the *shape* is driven by where the
# capsule is dropped on screen, the *state* by what OSIA is doing. The capsule
# morphs between three shapes with a smooth ~300ms geometry animation.
SHAPE_CAPSULE = "capsule"   # default horizontal pill (ISLAND_W x ISLAND_H)
SHAPE_CIRCLE = "circle"     # compact logo+mic disc, snapped into a bottom corner
SHAPE_DOCKED = "docked"     # vertical pill tucked behind a side edge ("ear")

MORPH_MS = 300              # shape-morph animation duration
CIRCLE_SIZE = 60           # circle diameter (px)
# The pill-sized active animations (≈72px wide) are scaled by this factor when
# painted inside the compact circle so they read as a centred glyph rather than
# a clipped fragment.
CIRCLE_ACTIVE_SCALE = 0.7
# Docked "ear": a tall, narrow vertical pill that sits half-tucked behind a side
# edge. Only DOCK_PEEK px (exactly half its width) stays on screen; the outer
# half is clipped by the monitor edge, leaving a rounded edge facing inward. On
# wake word it slides out to the full horizontal capsule and back when idle.
DOCK_PILL_W = 28           # docked pill width (px) — narrow vertical pill
DOCK_PILL_H = 80           # docked pill height (px) — tall vertical pill
DOCK_PEEK = DOCK_PILL_W // 2   # px of the pill kept on screen (exactly half)
SHAPE_MARGIN = 8           # gap kept from the screen edge for visible shapes

# Drop-zone thresholds. A bottom corner is "within 15% of the screen width AND
# 15% of its height" of that corner → circle. Otherwise, within 40px of a side
# edge → docked. Anything else → capsule.
CORNER_FRAC = 0.15
EDGE_DOCK_PX = 40


def _clamp(value, low, high):
    """Clamp ``value`` into ``[low, high]`` (tolerant of inverted bounds)."""
    if high < low:
        return low
    return max(low, min(high, value))


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
            # The WM otherwise clamps the docked "ear" back on screen (x=0),
            # cancelling the negative-x left dock (e.g. x=-14). Bypassing the WM
            # makes this an override-redirect window so it keeps the exact
            # off-screen geometry we commit. Override-redirect windows also stay
            # above managed windows, so WindowStaysOnTopHint above still holds.
            | Qt.WindowType.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Not setFixedSize: the shape morph animates the window geometry between
        # the capsule (ISLAND_W x ISLAND_H) and the circle (CIRCLE_SIZE²), so the
        # window must be resizable. It starts as the full capsule.
        self.resize(ISLAND_W, ISLAND_H)
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
        # Adaptive shape state (WOR-22). ``_shape`` is one of SHAPE_*; for the
        # circle/docked shapes ``_dock_side`` records which side ("left"/"right")
        # it snapped to. ``_docked_slid_out`` is True while a docked capsule is
        # temporarily slid out on screen (active state) before tucking back in.
        self._shape = SHAPE_CAPSULE
        self._dock_side = None
        self._docked_slid_out = False
        # Vertical centre the docked "ear" anchors to, so the tucked pill and the
        # slid-out capsule stay aligned across the slide animation. None unless
        # docked.
        self._dock_center_y = None
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

        # Shape-morph animation — smoothly animates the window geometry (size +
        # position) between shapes. The painted corner radius follows the live
        # geometry (see _capsule_path), so the pill rounds into a full circle as
        # the window approaches CIRCLE_SIZE² with no separate radius animation.
        # Driven as a QRect QVariantAnimation (not a "geometry" QPropertyAnimation)
        # so each frame is committed via _commit_geometry, which re-asserts the
        # rect on the native window and lets a docked capsule rest partially
        # off-screen past WM clamping (negative x / x beyond the right edge).
        self._morph_anim = QVariantAnimation(self)
        self._morph_anim.setDuration(MORPH_MS)
        self._morph_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._morph_anim.valueChanged.connect(self._on_morph_value)

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
        # Re-assert frameless on the native window so the WM honours the
        # off-screen docked geometry (negative x on the left, x beyond the right
        # edge) instead of pulling the window back fully on screen.
        handle = self.windowHandle()
        if handle is not None:
            handle.setFlag(Qt.WindowType.FramelessWindowHint, True)
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

        # A docked capsule slides out from behind the edge while OSIA is active
        # (so the mic + thinking animation are fully visible) and tucks back in
        # when it returns to idle. The shape itself stays SHAPE_DOCKED.
        if self._shape == SHAPE_DOCKED:
            if state == "idle":
                if self._docked_slid_out:
                    self._docked_slid_out = False
                    self._apply_shape_geometry(animate=True, slid_out=False)
            elif not self._docked_slid_out:
                self._docked_slid_out = True
                self._apply_shape_geometry(animate=True, slid_out=True)

        self.update()

    def current_state(self) -> str:
        return self._state

    def set_capsule_visible(self, visible: bool) -> None:
        """Show or hide the whole capsule window (WOR-22 disable toggle)."""
        if visible:
            self.show()
            self.raise_()
        else:
            self.hide()

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
        settings) and restore the last shape (``island_shape`` /
        ``island_dock_side``), falling back to top-centre/capsule when nothing
        is saved or the saved point is no longer on any connected screen
        (monitor unplugged, resolution change).
        """
        try:
            # Lazy import so the module stays importable standalone
            # (self-test below) even without the project on sys.path.
            from osia.config import load_settings
            settings = load_settings()
            x, y = settings.get("island_x"), settings.get("island_y")
            shape = settings.get("island_shape", SHAPE_CAPSULE)
            side = settings.get("island_dock_side")
        except Exception:
            x = y = None
            shape, side = SHAPE_CAPSULE, None
        if shape not in (SHAPE_CAPSULE, SHAPE_CIRCLE, SHAPE_DOCKED):
            shape = SHAPE_CAPSULE
        self._shape = shape
        self._dock_side = side
        self._docked_slid_out = False

        moved = False
        if x is not None and y is not None:
            rect = QRect(int(x), int(y), self.width(), self.height())
            for screen in QApplication.screens():
                if screen.availableGeometry().intersects(rect):
                    self.move(rect.topLeft())
                    moved = True
                    break
        if not moved:
            self._position_at_top()
        # Anchor the docked ear on the restored drop position's vertical centre.
        if shape == SHAPE_DOCKED:
            self._dock_center_y = self.geometry().center().y()
        # Snap straight to the restored shape's geometry (no launch-time morph).
        if shape != SHAPE_CAPSULE:
            self._apply_shape_geometry(animate=False)

    def _save_position(self) -> None:
        """Persist the capsule position + shape so the next launch restores it.

        ``island_x``/``island_y`` record where the capsule was dropped (before
        any morph), and ``island_shape``/``island_dock_side`` the resolved shape
        so a docked/circle capsule comes back the same way on next launch.
        """
        try:
            from osia.config import load_settings, save_settings
            settings = load_settings()
            settings["island_x"] = self.x()
            settings["island_y"] = self.y()
            settings["island_shape"] = self._shape
            settings["island_dock_side"] = self._dock_side
            save_settings(settings)
        except Exception as e:
            print(f"[DynamicIsland] failed to save position: {e}")

    def showEvent(self, event):
        super().showEvent(event)
        # Re-assert the CURRENT in-memory shape's geometry (honouring an active
        # docked slide-out) rather than re-reading settings: the disk read +
        # shape restore happens once in __init__. This keeps a re-shown capsule
        # (e.g. after the visibility toggle) from snapping back behind the edge
        # mid-conversation or clobbering the live slide-out state.
        self._apply_shape_geometry(animate=False, slid_out=self._docked_slid_out)
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
                # The grab takes over geometry control — stop any in-flight
                # morph (drop-driven shape change or docked slide) so the two
                # geometry writers don't fight and jitter under the cursor.
                self._morph_anim.stop()
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
                # Re-evaluate the shape from where the capsule was dropped
                # (capsule / circle / docked), morphing to it, then persist the
                # drop position + resolved shape.
                self._evaluate_and_apply_shape()
                self._save_position()
            else:
                # Plain click (no drag) — toggle the popup.
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── Adaptive shape (WOR-22) ───────────────────────────────────────────
    # The shape is resolved from the dropped position (not on every move()):
    # morphing mid-drag would fight the drag, and "dropped in a corner" /
    # "dragged close to an edge" are inherently drop-time decisions.

    def _evaluate_and_apply_shape(self) -> None:
        shape, side = self._evaluate_shape()
        self._set_shape(shape, side)

    def _evaluate_shape(self) -> tuple[str, str | None]:
        """Resolve (shape, side) from the capsule's current screen position."""
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        geo = self.geometry()
        corner_w = avail.width() * CORNER_FRAC
        corner_h = avail.height() * CORNER_FRAC
        left_gap = geo.left() - avail.left()
        right_gap = avail.right() - geo.right()
        bottom_gap = avail.bottom() - geo.bottom()

        # Bottom corners take priority → compact circle.
        if bottom_gap <= corner_h and left_gap <= corner_w:
            return SHAPE_CIRCLE, "left"
        if bottom_gap <= corner_h and right_gap <= corner_w:
            return SHAPE_CIRCLE, "right"
        # Otherwise, hugging a side edge → docked.
        if left_gap <= EDGE_DOCK_PX:
            return SHAPE_DOCKED, "left"
        if right_gap <= EDGE_DOCK_PX:
            return SHAPE_DOCKED, "right"
        return SHAPE_CAPSULE, None

    def _set_shape(self, shape: str, side: str | None) -> None:
        """Switch to ``shape`` (snapping to ``side``) with an animated morph."""
        self._shape = shape
        self._dock_side = side
        if shape == SHAPE_DOCKED:
            # Anchor the ear on the drop point's vertical centre; start slid out
            # if OSIA is mid-interaction so the active animation stays visible.
            self._dock_center_y = self.geometry().center().y()
            self._docked_slid_out = self._state != "idle"
        else:
            self._dock_center_y = None
            self._docked_slid_out = False
        self._apply_shape_geometry(animate=True, slid_out=self._docked_slid_out)

    def _apply_shape_geometry(self, animate: bool = True,
                              slid_out: bool = False) -> None:
        """Move/resize the window to the current shape's target geometry."""
        target = self._target_geometry(self._shape, self._dock_side, slid_out)
        self._morph_anim.stop()
        if animate and self.isVisible():
            self._morph_anim.setStartValue(self.geometry())
            self._morph_anim.setEndValue(target)
            self._morph_anim.start()
        else:
            self._commit_geometry(target)
        self.update()

    def _on_morph_value(self, rect: QRect) -> None:
        """Per-frame morph step — commit the interpolated geometry."""
        self._commit_geometry(rect)
        self.update()

    def _commit_geometry(self, rect: QRect) -> None:
        """Apply ``rect`` to the window, forcing it past WM clamping so a docked
        capsule can rest partially off-screen (negative x on the left, or x
        beyond the right edge). Setting the geometry on the native ``QWindow``
        handle in addition to the widget bypasses managers that would otherwise
        pull a top-level window back fully on screen.
        """
        self.setGeometry(rect)
        handle = self.windowHandle()
        if handle is not None:
            handle.setGeometry(rect)

    def _target_geometry(self, shape: str, side: str | None,
                         slid_out: bool = False) -> QRect:
        """Compute the window rect for ``shape`` on the current screen."""
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        cur = self.geometry()
        m = SHAPE_MARGIN
        if side is None and shape in (SHAPE_CIRCLE, SHAPE_DOCKED):
            side = "left" if cur.center().x() < avail.center().x() else "right"

        if shape == SHAPE_CIRCLE:
            w = h = CIRCLE_SIZE
            x = avail.left() + m if side == "left" else avail.right() - w - m
            y = avail.bottom() - h - m
        elif shape == SHAPE_DOCKED:
            anchor_y = (self._dock_center_y if self._dock_center_y is not None
                        else cur.center().y())
            if slid_out:
                # Wake word: the full horizontal capsule slides on screen so the
                # mic + animation read normally, flush against the docked edge
                # (x = 0 on the left, x = screen_width - full_width on the right).
                w, h = ISLAND_W, ISLAND_H
                x = avail.left() if side == "left" else avail.right() + 1 - w
            else:
                # Idle: a tall vertical pill, exactly half tucked behind the edge
                # (DOCK_PEEK px showing). The outer half is clipped by the monitor
                # edge, leaving a rounded "ear" facing inward (left dock x = -14,
                # right dock x = screen_width - 14 for a 28px pill).
                w, h = DOCK_PILL_W, DOCK_PILL_H
                x = (avail.left() - DOCK_PEEK if side == "left"
                     else avail.right() + 1 - DOCK_PEEK)
            y = _clamp(anchor_y - h // 2,
                       avail.top() + m, avail.bottom() - h - m)
        else:  # SHAPE_CAPSULE — centre on the drop point, kept fully on screen.
            w, h = ISLAND_W, ISLAND_H
            x = _clamp(cur.center().x() - w // 2,
                       avail.left() + m, avail.right() - w - m)
            y = _clamp(cur.center().y() - h // 2,
                       avail.top() + m, avail.bottom() - h - m)
        return QRect(int(x), int(y), int(w), int(h))

    # ── Painting ──────────────────────────────────────────────────────────

    def _capsule_rect(self) -> QRectF:
        """Current painted-capsule rect, interpolated by the launch progress.

        At ``_launch_t == 1`` this fills the whole (current-size) window; while
        the launch animation runs it is a smaller capsule centred in the
        window, so the surrounding translucent area stays click-through. The
        full size tracks the live window size (not ISLAND_W/H) so the painted
        capsule follows the shape-morph animation between pill and circle.
        """
        t = self._launch_t
        full_w = self.width()
        full_h = self.height()
        # min() guards the narrow docked pill: its full width (DOCK_PILL_W) is
        # smaller than LAUNCH_W, so the raw interpolation would paint a capsule
        # wider than the window. Clamp so the painted shape never exceeds it.
        w = min(full_w, LAUNCH_W + (full_w - LAUNCH_W) * t)
        h = min(full_h, LAUNCH_H + (full_h - LAUNCH_H) * t)
        x = (self.width() - w) / 2
        y = (self.height() - h) / 2
        return QRectF(x + 0.5, y + 0.5, w - 1, h - 1)

    def _capsule_path(self) -> QPainterPath:
        rect = self._capsule_rect()
        # Radius follows the live geometry: a wide pill keeps the 26px rounding,
        # and as the window squares off toward CIRCLE_SIZE² it becomes a full
        # circle — the morph between shapes needs no separate radius animation.
        radius = min(rect.width(), rect.height()) / 2
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

        # Content overlay.
        if self._shape == SHAPE_DOCKED and not self._docked_slid_out:
            # Tucked "ear": only a grip hint on the visible inner half. The mic +
            # active animation appear once it slides out on the wake word.
            self._paint_docked_ear(painter)
        # The circle is a compact logo+mic disc when idle, and clips its
        # active-state animation to the disc so nothing spills out.
        elif self._shape == SHAPE_CIRCLE:
            painter.save()
            painter.setClipPath(path)
            if self._state == "idle":
                self._paint_circle_idle(painter)
            else:
                # Scale the pill-sized active animation down around the disc
                # centre so it fits the circle instead of being clipped.
                cx, cy = self.width() / 2, self.height() / 2
                painter.translate(cx, cy)
                painter.scale(CIRCLE_ACTIVE_SCALE, CIRCLE_ACTIVE_SCALE)
                painter.translate(-cx, -cy)
                self._paint_state(painter)
            painter.restore()
        else:
            self._paint_state(painter)

        painter.end()

    def _paint_state(self, p: QPainter) -> None:
        """Dispatch to the active animation-state painter (idle/…/speaking)."""
        if self._state == "idle":
            self._paint_idle(p)
        elif self._state == "listening":
            self._paint_listening(p)
        elif self._state == "thinking":
            self._paint_thinking(p)
        elif self._state == "speaking":
            self._paint_speaking(p)

    def _paint_docked_ear(self, p: QPainter) -> None:
        """A subtle three-dot grip on the visible (inner) half of the docked
        pill, hinting it can be pulled out. Docked left → the right half is on
        screen; docked right → the left half is, so the grip hugs that side.
        """
        rect = self._capsule_rect()
        cy = rect.center().y()
        if self._dock_side == "right":
            gx = rect.left() + rect.width() * 0.30
        else:
            gx = rect.right() - rect.width() * 0.30
        p.setPen(Qt.PenStyle.NoPen)
        color = QColor(ACCENT)
        color.setAlpha(200)
        p.setBrush(color)
        for dy in (-7.0, 0.0, 7.0):
            p.drawEllipse(QPointF(gx, cy + dy), 1.6, 1.6)

    def _paint_circle_idle(self, p: QPainter) -> None:
        """Compact circle: the OSIA logo with a small mic glyph beneath it."""
        fade = min(1.0, max(0.0, (self._launch_t - 0.7) / 0.3))
        if fade <= 0.0:
            return
        rect = self._capsule_rect()
        cx = rect.center().x()
        cy = rect.center().y()

        p.save()
        p.setOpacity(fade)
        if not self._logo.isNull():
            size = int(rect.height() * 0.42)
            logo = self._logo.scaledToHeight(
                max(1, size), Qt.TransformationMode.SmoothTransformation
            )
            p.drawPixmap(
                int(cx - logo.width() / 2.0),
                int(cy - logo.height() - 1),
                logo,
            )
        self._draw_mini_mic(p, cx, cy + rect.height() * 0.20)
        p.restore()

    def _draw_mini_mic(self, p: QPainter, cx: float, cy: float) -> None:
        """A small static microphone glyph centred at (cx, cy), ~16px tall."""
        pen = QPen(ACCENT, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Mic body — a rounded vertical capsule.
        p.drawRoundedRect(QRectF(cx - 3.4, cy - 7.0, 6.8, 10.5), 3.4, 3.4)
        # Cradle arc beneath the body.
        stand = QPainterPath()
        stand.moveTo(cx - 6.0, cy - 0.5)
        stand.cubicTo(cx - 6.0, cy + 5.5, cx + 6.0, cy + 5.5, cx + 6.0, cy - 0.5)
        p.drawPath(stand)
        # Stem + base foot.
        p.drawLine(QPointF(cx, cy + 5.5), QPointF(cx, cy + 8.5))
        p.drawLine(QPointF(cx - 3.0, cy + 8.5), QPointF(cx + 3.0, cy + 8.5))

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
