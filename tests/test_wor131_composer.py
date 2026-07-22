"""Focused WOR-131 desktop chat composer regression tests."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEMP_HOME = tempfile.TemporaryDirectory(prefix="osia-wor131-tests-")
os.environ["USERPROFILE"] = _TEMP_HOME.name
os.environ["HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OSIA_REDUCED_MOTION"] = "1"

from PyQt6.QtCore import QCoreApplication, QEvent, QRectF, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication

from osia.gui.main_popup import (
    COMPOSER_BUTTON_RADIUS,
    COMPOSER_BUTTON_SIZE,
    MainPopup,
    POPUP_H,
    POPUP_W,
    POPUP_W_WIDE,
)
from osia.gui.theme import PALETTES, get_theme_manager


class ComposerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.popup = MainPopup()
        cls.popup.setGeometry(100, 120, POPUP_W, POPUP_H)
        cls.popup._stack.setCurrentIndex(cls.popup.PAGE_CHAT)
        cls.popup.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        get_theme_manager().set_theme("osia", persist=False)
        cls.popup.close()
        cls.popup.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        cls.app.processEvents()

    def setUp(self):
        self.chat = self.popup._chat
        get_theme_manager().set_theme("osia", persist=False)
        self.chat._file_btn.setEnabled(True)
        self.chat.set_busy(False)
        self.chat._drafts.clear()
        self.chat._draft_context = None
        self.chat._pending_attachments = []
        self.chat._rebuild_attachment_chips()
        self.chat._input.clear()
        self.popup.setGeometry(100, 120, POPUP_W, POPUP_H)
        self.app.processEvents()

    def test_buttons_are_identical_squares_and_icons_are_centered(self):
        file_btn = self.chat._file_btn
        send_btn = self.chat._send_btn
        self.assertEqual(COMPOSER_BUTTON_SIZE, 34)
        self.assertEqual(COMPOSER_BUTTON_RADIUS, 8)
        self.assertEqual(file_btn.size(), send_btn.size())
        self.assertEqual(
            (file_btn.width(), file_btn.height()),
            (COMPOSER_BUTTON_SIZE, COMPOSER_BUTTON_SIZE),
        )
        self.assertEqual(
            file_btn.geometry().center().y(),
            send_btn.geometry().center().y(),
        )

        common_style = re.search(
            r"QPushButton#iconBtn,\s*"
            r"QPushButton#sendBtn\s*\{([^}]+)\}",
            self.popup.styleSheet(),
        )
        self.assertIsNotNone(common_style)
        self.assertIn(
            f"border-radius: {COMPOSER_BUTTON_RADIUS}px",
            common_style.group(1),
        )

        for button in (file_btn, send_btn):
            widget_center = QRectF(button.rect()).center()
            icon_center = button._icon_path().boundingRect().center()
            self.assertLessEqual(abs(widget_center.x() - icon_center.x()), 0.5)
            self.assertLessEqual(abs(widget_center.y() - icon_center.y()), 0.5)

    def test_text_state_tracks_trimmed_input_immediately(self):
        self.assertFalse(self.chat._send_btn.isEnabled())
        self.chat._input.setText(" \t  ")
        self.assertFalse(self.chat._send_btn.isEnabled())
        self.chat._input.setText("x")
        self.assertTrue(self.chat._send_btn.isEnabled())
        self.chat._input.selectAll()
        QTest.keyClick(self.chat._input, Qt.Key.Key_Backspace)
        self.assertEqual(self.chat._input.text(), "")
        self.assertFalse(self.chat._send_btn.isEnabled())

    def test_successful_send_clears_disables_and_emits_once(self):
        sends = QSignalSpy(self.popup.send_text)
        self.chat._input.setText("  hello once  ")
        QTest.mouseClick(self.chat._send_btn, Qt.MouseButton.LeftButton)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "hello once")
        self.assertEqual(self.chat._input.text(), "")
        self.assertFalse(self.chat._send_btn.isEnabled())

    def test_disabled_click_and_empty_or_whitespace_enter_send_nothing(self):
        sends = QSignalSpy(self.popup.send_text)
        QTest.mouseClick(self.chat._send_btn, Qt.MouseButton.LeftButton)
        self.chat._input.setFocus()
        QTest.keyClick(self.chat._input, Qt.Key.Key_Return)
        self.chat._input.setText("   \t")
        QTest.keyClick(self.chat._input, Qt.Key.Key_Return)
        self.assertFalse(self.chat._send_btn.isEnabled())
        self.assertEqual(len(sends), 0)

    def test_click_and_enter_remain_exactly_once_after_state_changes(self):
        sends = QSignalSpy(self.popup.send_text)
        self.chat._input.setText("click")
        QTest.mouseClick(self.chat._send_btn, Qt.MouseButton.LeftButton)
        self.chat._input.setText("   ")
        self.chat._input.clear()
        self.chat._input.setText("enter")
        QTest.keyClick(self.chat._input, Qt.Key.Key_Return)
        self.assertEqual(len(sends), 2)
        self.assertEqual([entry[0] for entry in sends], ["click", "enter"])

    def test_file_pick_stages_once_without_emitting_legacy_send_signal(self):
        picks = QSignalSpy(self.popup.pick_file)
        source = Path(_TEMP_HOME.name) / "one.txt"
        source.write_text("synthetic", encoding="utf-8")
        with patch(
            "osia.gui.main_popup.QFileDialog.getOpenFileNames",
            return_value=([str(source)], ""),
        ):
            QTest.mouseClick(self.chat._file_btn, Qt.MouseButton.LeftButton)
        self.assertEqual(len(picks), 0)
        self.assertEqual(len(self.chat.pending_attachments()), 1)
        self.assertTrue(self.chat._send_btn.isEnabled())
        self.assertTrue(self.chat._attachment_scroll.isVisible())

    def test_all_themes_resolve_active_and_disabled_composer_styles(self):
        manager = get_theme_manager()
        for key, palette in PALETTES.items():
            manager.set_theme(key, persist=False)
            self.app.processEvents()
            stylesheet = self.popup.styleSheet()
            primary = palette.color("primary")
            disabled = palette.color("disabled")
            primary_rgb = f"{primary.red()}, {primary.green()}, {primary.blue()}"
            disabled_rgb = f"{disabled.red()}, {disabled.green()}, {disabled.blue()}"

            for selector in ("iconBtn", "sendBtn"):
                active = re.search(
                    rf"QPushButton#{selector}\s*\{{([^}}]+)\}}",
                    stylesheet,
                )
                self.assertIsNotNone(active, f"{key}.{selector}.active")
                self.assertIn(primary_rgb, active.group(1))
                for state in ("hover", "pressed", "focus"):
                    self.assertIn(f"QPushButton#{selector}:{state}", stylesheet)

            inactive = re.search(
                r"QPushButton#iconBtn:disabled,\s*"
                r"QPushButton#sendBtn:disabled\s*\{([^}]+)\}",
                stylesheet,
            )
            self.assertIsNotNone(inactive, f"{key}.composer.disabled")
            self.assertIn(disabled_rgb, inactive.group(1))

            for button in (self.chat._file_btn, self.chat._send_btn):
                button.setEnabled(True)
                self.assertTrue(button._icon_color().isValid())
                button.setEnabled(False)
                icon_color = button._icon_color()
                self.assertTrue(icon_color.isValid())
                self.assertEqual(icon_color, QColor(palette.disabled))
        self.chat._file_btn.setEnabled(True)
        self.chat._input.clear()

    def test_popup_and_composer_geometry_keep_the_compact_contract(self):
        self.assertEqual((POPUP_W, POPUP_W_WIDE, POPUP_H), (440, 640, 560))
        self.assertEqual(self.popup.geometry().getRect(), (100, 120, 440, 560))
        self.assertEqual(
            (
                self.popup.minimumWidth(),
                self.popup.minimumHeight(),
                self.popup.maximumWidth(),
                self.popup.maximumHeight(),
            ),
            (440, 560, 640, 560),
        )
        input_wrap = self.chat._input.parentWidget()
        self.assertEqual(input_wrap.width(), 364)
        self.assertEqual(
            self.chat._file_btn.size(), self.chat._send_btn.size(),
        )
        self.assertEqual(self.chat._file_btn.width(), COMPOSER_BUTTON_SIZE)
        self.assertLess(self.chat._input.width(), input_wrap.width())


def tearDownModule():
    _TEMP_HOME.cleanup()


if __name__ == "__main__":
    unittest.main()
