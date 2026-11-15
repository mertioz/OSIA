"""Non-interactive, theme-aware feedback for native desktop input."""
from __future__ import annotations

import base64
import struct
import sys
import threading

from PyQt6.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from osia.control_cursor import CursorClient
from osia.desktop_activity import desktop_activity_visible
from osia.gui.theme import get_theme_manager


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
    HEIGHT = 26

    def __init__(self, screen, theme):
        super().__init__()
        self.screen_ref = screen
        self.theme = theme
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
        accent = self.theme.palette.color("primary")
        width = self.width()
        # Small painted strips only: no full-screen translucent backing store,
        # shadows/effects, focus calls, input hooks or moving cursor window.
        for y, inset, thickness, alpha in ((23, 0, 2.0, 230), (18, 0.025, 1.0, 115), (12, 0.12, 0.8, 65)):
            gradient = QLinearGradient(0, 0, width, 0)
            transparent = QColor(accent)
            transparent.setAlpha(0)
            color = QColor(accent)
            color.setAlpha(alpha)
            for position, stop in ((0, transparent), (0.12, color), (0.5, accent), (0.88, color), (1, transparent)):
                gradient.setColorAt(position, stop)
            painter.setPen(QPen(gradient, thickness))
            painter.drawLine(QPointF(width * inset, y), QPointF(width * (1 - inset), y))
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
        if not self.closed and self.active and screen not in self.edges:
            try:
                edge = ControlEdge(screen, self.theme)
                self.edges[screen] = edge
                edge.show()
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
            for screen in tuple(self.edges):
                self._screen_removed(screen)
        self._update_cursor()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        for screen in tuple(self.edges):
            self._screen_removed(screen)
        self.cursor_client.close()
