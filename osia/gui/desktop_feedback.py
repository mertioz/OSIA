"""Non-interactive, theme-aware feedback for native desktop input."""
from __future__ import annotations

import base64
import math
import struct
import sys
import threading
import time

from PyQt6.QtCore import QEasingCurve, QObject, QPointF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from osia.control_cursor import CursorClient
from osia.desktop_activity import desktop_activity_visible
from osia.gui.theme import get_theme_manager
from osia.gui.motion import reduced_motion_requested


def cursor_image(accent: QColor, *, hand: bool = False, scale: float = 1.0) -> QImage:
    width, height = round(32 * scale), round(44 * scale)
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    path = QPainterPath()
    if hand:
        path.moveTo(10, 21)
        path.lineTo(10, 6)
        path.cubicTo(10, 1, 16, 1, 16, 6)
        path.lineTo(16, 17)
        path.cubicTo(17, 12, 22, 13, 22, 18)
        path.cubicTo(23, 14, 28, 16, 28, 21)
        path.lineTo(28, 28)
        path.cubicTo(28, 33, 24, 35, 24, 39)
        path.lineTo(11, 39)
        path.cubicTo(10, 35, 5, 29, 4, 25)
        path.cubicTo(2, 20, 6, 18, 10, 24)
    else:
        path.moveTo(3, 3)
        path.lineTo(3, 30)
        path.lineTo(10, 23)
        path.lineTo(16, 36)
        path.lineTo(21, 34)
        path.lineTo(15, 22)
        path.lineTo(25, 22)
    path.closeSubpath()
    painter.setPen(QPen(accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.GlobalColor.black)
    painter.drawPath(path)
    painter.end()
    return image


def cursor_file_bytes(image: QImage, hot_x: int, hot_y: int) -> bytes:
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width, height = image.width(), image.height()
    rows = bytes(image.constBits().asstring(image.sizeInBytes()))
    bitmap = b"".join(rows[y * image.bytesPerLine():y * image.bytesPerLine() + width * 4]
                      for y in reversed(range(height)))
    mask_stride = ((width + 31) // 32) * 4
    mask = bytearray(mask_stride * height)
    for y in range(height):
        for x in range(width):
            if bitmap[(y * width + x) * 4 + 3] == 0:
                mask[y * mask_stride + x // 8] |= 0x80 >> (x % 8)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, len(bitmap), 0, 0, 0, 0) + bitmap + bytes(mask)
    return struct.pack("<HHH", 0, 2, 1) + struct.pack("<BBBBHHII", width, height, 0, 0, hot_x, hot_y, len(dib), 22) + dib


class ControlEdge(QWidget):
    HEIGHT = 46

    def __init__(self, screen, theme):
        super().__init__()
        self.screen_ref = screen
        self.theme = theme
        self.target_active = False
        self.reveal = 0.0
        self._started_at = time.monotonic()
        self._reduced_motion = reduced_motion_requested(self)
        self._frames = QTimer(self)
        self._frames.setInterval(33)
        self._frames.timeout.connect(self.update)
        self._fade = QVariantAnimation(self)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._fade.valueChanged.connect(self._set_reveal)
        self._fade.finished.connect(self._fade_finished)
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.WindowTransparentForInput)
        if sys.platform.startswith("linux") and QApplication.platformName() == "xcb":
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setWindowTitle("OSIA · Desktop control feedback")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sync_geometry()
        screen.geometryChanged.connect(self.sync_geometry)

    def sync_geometry(self, *_args):
        geometry = self.screen_ref.geometry()
        self.setGeometry(geometry.x(), geometry.bottom() - self.HEIGHT + 1, geometry.width(), self.HEIGHT)

    def _set_reveal(self, value):
        self.reveal = float(value)
        self.update()

    def set_active(self, active):
        active = bool(active)
        if active == self.target_active:
            return
        self.target_active = active
        current_reveal = self.reveal
        self._fade.stop()
        if active:
            self.show()
        if self._reduced_motion:
            self._set_reveal(1.0 if active else 0.0)
            self._fade_finished()
            return
        self._fade.setDuration(220 if active else 280)
        self._fade.setCurrentTime(0)
        self._fade.setStartValue(current_reveal)
        self._fade.setEndValue(1.0 if active else 0.0)
        self._set_reveal(current_reveal)
        self._fade.start()
        self._frames.start()

    def _fade_finished(self):
        if not self.target_active:
            self._frames.stop()
            self.hide()
        elif self._reduced_motion:
            self._frames.stop()

    def closeEvent(self, event):
        self._frames.stop()
        self._fade.stop()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32" and QApplication.platformName() == "windows":
            try:
                import ctypes
                from ctypes import wintypes

                affinity = ctypes.WinDLL("user32", use_last_error=True).SetWindowDisplayAffinity
                affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
                affinity.restype = wintypes.BOOL
                affinity(int(self.winId()), 0x11)  # exclude our own decoration from captures
            except Exception:
                pass

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self.reveal)
        accent = self.theme.palette.color("primary")
        width = self.width()
        phase = 0.0 if self._reduced_motion else (time.monotonic() - self._started_at) * 0.8
        highlight_position = 0.5 + 0.34 * math.sin(phase * 0.75)
        # Paint the neon directly in the small bottom strip. No graphics effect
        # or moving translucent window: those can leave DWM ghosts on Windows.
        path = QPainterPath()
        for index in range(65):
            x = width * index / 64
            y = self.height() - 3.5 + 0.8 * math.sin(index / 64 * math.tau + phase)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        for thickness, alpha in ((22, 8), (14, 16), (8, 32), (3.5, 120), (1.2, 245)):
            gradient = QLinearGradient(0, 0, width, 0)
            transparent = QColor(accent)
            transparent.setAlpha(0)
            color = QColor(accent)
            color.setAlpha(round(alpha * 0.65))
            highlight = accent.lighter(135)
            highlight.setAlpha(alpha)
            for position, stop in ((0, transparent), (0.12, color), (0.5, color), (0.88, color), (1, transparent)):
                gradient.setColorAt(position, stop)
            gradient.setColorAt(max(0.01, highlight_position - 0.12), color)
            gradient.setColorAt(highlight_position, highlight)
            gradient.setColorAt(min(0.99, highlight_position + 0.12), color)
            painter.setPen(QPen(gradient, thickness))
            painter.drawPath(path)
        for y, alpha in ((self.height() - 12, 75), (self.height() - 19, 30)):
            color = QColor(accent)
            color.setAlpha(alpha)
            gradient = QLinearGradient(0, 0, width, 0)
            clear = QColor(accent)
            clear.setAlpha(0)
            gradient.setColorAt(0, clear)
            gradient.setColorAt(0.5 + 0.15 * math.sin(phase * 0.5), color)
            gradient.setColorAt(1, clear)
            painter.setPen(QPen(gradient, 0.8))
            painter.drawLine(QPointF(width * 0.03, y), QPointF(width * 0.97, y))
        painter.end()


class DesktopControlFeedback(QObject):
    activity_changed = pyqtSignal(bool)

    def __init__(self, parent=None, *, theme=None, activity_reader=desktop_activity_visible, cursor_client=None):
        super().__init__(parent)
        self.theme = theme or get_theme_manager()
        self.cursor_client = cursor_client or CursorClient()
        self.edges = {}
        self.active = False
        self.closed = False
        self._stop = threading.Event()
        self._activity_reader = activity_reader
        self.activity_changed.connect(self.set_active, Qt.ConnectionType.QueuedConnection)
        self.theme.theme_changed.connect(self._theme_changed)
        app = QApplication.instance()
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(self._screen_removed)
        app.aboutToQuit.connect(self.close)
        self._thread = threading.Thread(target=self._observe, name="osia-desktop-feedback", daemon=True)
        self._thread.start()

    def _observe(self):
        previous = False
        while not self._stop.is_set():
            try:
                active = bool(self._activity_reader())
            except Exception:
                active = False
            if active != previous:
                previous = active
                try:
                    self.activity_changed.emit(active)
                except RuntimeError:
                    break
            self._stop.wait(0.05)

    def _screen_added(self, screen):
        if not self.closed and self.active:
            try:
                if screen not in self.edges:
                    self.edges[screen] = ControlEdge(screen, self.theme)
                self.edges[screen].set_active(True)
            except (RuntimeError, OSError):
                self._screen_removed(screen)

    def _screen_removed(self, screen):
        edge = self.edges.pop(screen, None)
        if edge is not None:
            try:
                edge.close()
                edge.deleteLater()
            except RuntimeError:
                pass  # a removed QScreen/native window may already be gone

    def _cursor_payload(self):
        color = self.theme.palette.color("primary")
        return {name: base64.b64encode(cursor_file_bytes(cursor_image(color, hand=hand), 12 if hand else 3, 3)).decode("ascii")
                for name, hand in (("arrow", False), ("hand", True))}

    def _theme_changed(self, *_args):
        if self.closed:
            return
        for edge in self.edges.values():
            edge.update()
        if self.active:
            self._update_cursor()

    def _update_cursor(self):
        try:
            self.cursor_client.set_active(self.active, self._cursor_payload() if self.active else None)
        except Exception:
            pass  # optional decoration never terminates the GUI or input work

    def set_active(self, active):
        if self.closed or bool(active) == self.active:
            return
        self.active = bool(active)
        if self.active:
            for screen in QApplication.screens():
                self._screen_added(screen)
        else:
            for edge in self.edges.values():
                edge.set_active(False)
        self._update_cursor()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        for screen in tuple(self.edges):
            self._screen_removed(screen)
        self.cursor_client.close()
