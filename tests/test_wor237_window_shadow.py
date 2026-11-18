"""Focused WOR-237 external window-shadow regression coverage."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest


_TEMP_HOME = tempfile.TemporaryDirectory(prefix="osia-wor237-tests-")
os.environ["USERPROFILE"] = _TEMP_HOME.name
os.environ["HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QImage
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication

from osia.gui import main_popup


class ExternalWindowShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.popup = main_popup.MainPopup()
        self.popup.show()
        self.app.processEvents()

    def tearDown(self):
        self.popup.close()
        self.popup.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _render_popup(self) -> QImage:
        image = QImage(
            self.popup.size(),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        self.popup.render(image)
        return image

    def _assert_shadow_policy(self) -> None:
        image = self._render_popup()
        shadow_y = (
            self.popup.height() - main_popup.SHADOW_MARGIN
            + main_popup.SHADOW_DY
        )
        shadow_alpha = image.pixelColor(self.popup.width() // 2, shadow_y).alpha()
        if sys.platform == "win32":
            self.assertEqual(shadow_alpha, 0)
        else:
            self.assertGreater(shadow_alpha, 0)

        card_alpha = image.pixelColor(
            self.popup.width() // 2,
            main_popup.SHADOW_MARGIN + 8,
        ).alpha()
        self.assertGreater(card_alpha, 0)

    def test_platform_policy_changes_only_windows_shadow_paint(self):
        self.assertEqual(
            main_popup._PAINT_EXTERNAL_WINDOW_SHADOW,
            sys.platform != "win32",
        )
        flags = self.popup.windowFlags()
        self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.Tool)
        self.assertTrue(
            self.popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        )
        self.assertIsNone(self.popup.graphicsEffect())
        self._assert_shadow_policy()

    def test_shadow_policy_survives_restore_and_width_state_changes(self):
        self.assertEqual(
            (self.popup.width(), self.popup.height()),
            (main_popup.POPUP_W, main_popup.POPUP_H),
        )
        self._assert_shadow_policy()

        self.popup.hide()
        self.popup.show()
        self.app.processEvents()
        self._assert_shadow_policy()

        self.popup._animate_width(main_popup.POPUP_W_WIDE)
        self.app.processEvents()
        self.assertEqual(
            (self.popup.width(), self.popup.height()),
            (main_popup.POPUP_W_WIDE, main_popup.POPUP_H),
        )
        self._assert_shadow_policy()

        self.popup._animate_width(main_popup.POPUP_W)
        self.app.processEvents()
        self.assertEqual(
            (self.popup.width(), self.popup.height()),
            (main_popup.POPUP_W, main_popup.POPUP_H),
        )
        self._assert_shadow_policy()


    def test_context_meter_shares_the_composer_backing_store(self):
        # A Quick child has its own compositing layer and punches through the
        # translucent composer on the native Windows renderer.
        self.assertNotIsInstance(self.popup._chat._context_meter, QQuickWidget)

    def test_dwm_uses_the_painted_card_corners_on_windows(self):
        if sys.platform != "win32" or self.app.platformName() != "windows":
            self.skipTest("Requires the native Windows compositor")

        import ctypes
        from ctypes import wintypes

        for width in (main_popup.POPUP_W, main_popup.POPUP_W_WIDE, main_popup.POPUP_W):
            self.popup.hide()
            self.popup._animate_width(width)
            self.popup.show()
            self.app.processEvents()
            preference = ctypes.c_int()
            result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(int(self.popup.winId())), 33,
                ctypes.byref(preference), ctypes.sizeof(preference),
            )
            if result != 0:
                self.skipTest("DWM corner preferences unavailable")
            self.assertEqual(preference.value, 1)

if __name__ == "__main__":
    unittest.main()
