"""Focused WOR-310 Desktop navigation, motion, and conversation-row tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


_TEMP_HOME = tempfile.TemporaryDirectory(prefix="osia-wor310-tests-")
os.environ["USERPROFILE"] = _TEMP_HOME.name
os.environ["HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OSIA_REDUCED_MOTION"] = "0"

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QTimer, QVariantAnimation
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from osia.gui.main_popup import (
    MainPopup,
    POPUP_H,
    POPUP_W,
    _Bubble,
    _ConvRow,
    _ConversationsPanel,
)
from osia.gui.motion import MOTION, MotionController


class DesktopUxV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._startup_probe_patches = (
            patch(
                "osia.gui.main_popup._SettingsPanel._probe_chatgpt_account",
                lambda _self, force=False: None,
            ),
            patch(
                "osia.gui.main_popup._SettingsPanel._refresh_startup_status",
                lambda _self, force=False: None,
            ),
        )
        for startup_patch in cls._startup_probe_patches:
            startup_patch.start()
        try:
            cls.popup = MainPopup()
        except Exception:
            for startup_patch in reversed(cls._startup_probe_patches):
                startup_patch.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.popup.close()
        cls.popup.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        cls.app.processEvents()
        for startup_patch in reversed(cls._startup_probe_patches):
            startup_patch.stop()

    def setUp(self):
        self.popup._window_opacity_anim.stop()
        self.popup._motion.settle()
        self.popup._settings._motion.settle()
        self.popup._chat._motion.settle()
        self.popup._convs._motion.settle()
        self.popup.setWindowOpacity(1.0)
        self.popup.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            False,
        )
        self.popup.setGeometry(100, 120, POPUP_W, POPUP_H)
        self.popup._stack.setCurrentIndex(self.popup.PAGE_CHAT)
        for button in self.popup._nav_buttons.values():
            button.setChecked(False)
        self.popup._chat.set_messages([])
        self.popup.show()
        self.app.processEvents()

    def tearDown(self):
        self.popup.hide()
        self.popup._window_opacity_anim.stop()
        self.popup._motion.settle()
        self.popup._settings._motion.settle()
        self.popup._chat._motion.settle()
        self.popup._convs._motion.settle()
        self.app.processEvents()

    def test_clicking_any_active_primary_section_is_a_noop(self):
        for page_idx, button in self.popup._nav_buttons.items():
            with self.subTest(page=page_idx):
                self.popup._stack.setCurrentIndex(page_idx)
                button.setChecked(True)
                page_changes = QSignalSpy(self.popup._stack.currentChanged)
                with patch.object(
                    self.popup, "show_chat", wraps=self.popup.show_chat,
                ) as show_chat:
                    for _ in range(3):
                        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

                self.assertEqual(self.popup._stack.currentIndex(), page_idx)
                self.assertTrue(button.isChecked())
                self.assertEqual(len(page_changes), 0)
                show_chat.assert_not_called()

    def test_repeated_active_settings_click_preserves_nested_category(self):
        settings_button = self.popup._nav_buttons[self.popup.PAGE_SETTINGS]
        QTest.mouseClick(settings_button, Qt.MouseButton.LeftButton)
        self.assertEqual(
            self.popup._stack.currentIndex(), self.popup.PAGE_SETTINGS,
        )

        settings_nav = self.popup._settings._settings_nav
        nested_row = settings_nav.count() - 1
        settings_nav.setCurrentRow(nested_row)
        page_changes = QSignalSpy(self.popup._stack.currentChanged)
        with patch.object(
            self.popup, "show_chat", wraps=self.popup.show_chat,
        ) as show_chat:
            for _ in range(3):
                QTest.mouseClick(settings_button, Qt.MouseButton.LeftButton)

        self.assertEqual(
            self.popup._stack.currentIndex(), self.popup.PAGE_SETTINGS,
        )
        self.assertEqual(settings_nav.currentRow(), nested_row)
        self.assertEqual(len(page_changes), 0)
        show_chat.assert_not_called()

    def test_switching_sections_retains_active_conversation_and_chat(self):
        with patch.object(self.popup._convs, "refresh"):
            self.popup.refresh_conversations("canonical-conversation")
            self.popup.set_messages([{
                "role": "user",
                "content": "Canonical conversation text.",
            }])
            chat = self.popup._chat

            QTest.mouseClick(
                self.popup._nav_buttons[self.popup.PAGE_CONVS],
                Qt.MouseButton.LeftButton,
            )
            self.assertEqual(
                self.popup._stack.currentIndex(), self.popup.PAGE_CONVS,
            )
            QTest.mouseClick(
                self.popup._nav_buttons[self.popup.PAGE_MODELS],
                Qt.MouseButton.LeftButton,
            )
            self.assertEqual(
                self.popup._stack.currentIndex(), self.popup.PAGE_MODELS,
            )
            QTest.mouseClick(
                self.popup._nav_buttons[self.popup.PAGE_CONVS],
                Qt.MouseButton.LeftButton,
            )

        self.assertEqual(
            self.popup._active_conversation_id,
            "canonical-conversation",
        )
        self.assertIs(self.popup._chat, chat)
        self.assertEqual(
            [bubble.text() for bubble in chat.findChildren(_Bubble)],
            ["Canonical conversation text."],
        )

    def test_switching_pages_settles_all_section_fade_animations(self):
        with patch.object(self.popup._convs, "refresh"):
            for page_idx in (
                self.popup.PAGE_CONVS,
                self.popup.PAGE_MODELS,
                self.popup.PAGE_CONVS,
            ):
                QTest.mouseClick(
                    self.popup._nav_buttons[page_idx],
                    Qt.MouseButton.LeftButton,
                )

        QTest.qWait(MOTION.page_ms * 2 + 40)
        self.assertEqual(self.popup._motion.active_animation_count, 0)
        self.assertEqual(
            self.popup._motion.findChildren(QVariantAnimation), [],
        )
        self.assertEqual(self.popup._motion.findChildren(QTimer), [])
        self.assertEqual(
            self.popup._stack.currentIndex(), self.popup.PAGE_CONVS,
        )

    def test_conversation_actions_are_labeled_larger_and_do_not_open_row(self):
        self.popup._stack.setCurrentIndex(self.popup.PAGE_CONVS)
        row = _ConvRow({
            "id": "conversation-actions",
            "title": "Action test",
            "message_count": 1,
            "updated_at": "",
        })
        row.setParent(self.popup._convs)
        row.setGeometry(0, 0, 410, 70)
        row.show()
        self.app.processEvents()

        rename = row.findChild(QPushButton, "convRename")
        move = row.findChild(QPushButton, "convMove")
        delete = row.findChild(QPushButton, "convActionDel")
        self.assertIsNotNone(rename)
        self.assertIsNotNone(move)
        self.assertIsNotNone(delete)

        opened = QSignalSpy(row.open_clicked)
        renamed = QSignalSpy(row.rename_clicked)
        deleted = QSignalSpy(row.delete_clicked)
        with patch(
            "osia.gui.main_popup.QInputDialog.getText",
            return_value=("Renamed conversation", True),
        ):
            QTest.mouseClick(rename, Qt.MouseButton.LeftButton)
        QTest.mouseClick(delete, Qt.MouseButton.LeftButton)

        self.assertEqual(len(opened), 0)
        self.assertEqual(len(renamed), 1)
        self.assertEqual(
            list(renamed[0]),
            ["conversation-actions", "Renamed conversation"],
        )
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0][0], "conversation-actions")
        for button in (rename, move, delete):
            self.assertGreaterEqual(button.width(), 26)
            self.assertGreaterEqual(button.height(), 26)
            self.assertTrue(button.toolTip())
            self.assertEqual(button.accessibleName(), button.toolTip())
        self.assertEqual((rename.width(), rename.height()), (30, 30))
        self.assertEqual((move.width(), move.height()), (30, 30))
        self.assertEqual((delete.width(), delete.height()), (30, 30))
        row.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_active_conversation_row_fades_without_changing_selection(self):
        panel = _ConversationsPanel()
        panel.resize(440, 300)
        panel.show()
        self.app.processEvents()
        snapshot = {
            "snapshot_revision": 1,
            "folders": [],
            "items": [{
                "id": "selected-conversation",
                "title": "Selected conversation",
                "message_count": 1,
                "updated_at": "",
                "folder_id": None,
            }],
        }
        with (
            patch(
                "osia.gui.main_popup.conversations.conversation_snapshot",
                return_value=snapshot,
            ),
            patch(
                "osia.gui.main_popup.reduced_motion_requested",
                return_value=False,
            ),
        ):
            panel.set_active("selected-conversation")

        self.app.processEvents()
        row = panel.findChild(_ConvRow)
        self.assertIsNotNone(row)
        self.assertTrue(row.property("active"))
        self.assertEqual(panel._active_id, "selected-conversation")
        self.assertEqual(
            panel._last_signature,
            panel._snapshot_signature(snapshot),
        )
        self.assertEqual(panel._motion.active_animation_count, 1)
        geometry = row.geometry()
        QTest.qWait(MOTION.state_ms + 30)
        self.assertEqual(panel._motion.active_animation_count, 0)
        self.assertEqual(row.geometry(), geometry)
        self.assertTrue(row.property("active"))
        self.assertEqual(panel._active_id, "selected-conversation")
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_destroying_animated_page_discards_its_animation(self):
        motion = MotionController(reduced_motion=lambda: False)
        page = QWidget()
        page.show()
        motion.fade_in(page, 5000)
        animations = motion.findChildren(QVariantAnimation)
        self.assertEqual(len(animations), 1)
        animation = animations[0]
        self.assertEqual(motion.active_animation_count, 1)

        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

        self.assertTrue(sip.isdeleted(page))
        self.assertEqual(motion.active_animation_count, 0)
        self.assertTrue(sip.isdeleted(animation))
        motion.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_page_fades_leave_active_conversation_text_canonical(self):
        with patch.object(self.popup._convs, "refresh"):
            self.popup.refresh_conversations("canonical-fade-conversation")
            self.popup.set_messages([{
                "role": "assistant",
                "content": "Canonical text survives the visual fade.",
            }])
        before = [
            bubble.text() for bubble in self.popup._chat.findChildren(_Bubble)
        ]
        self.popup._motion.fade_in(self.popup._chat, MOTION.micro_ms)
        self.assertEqual(
            self.popup._active_conversation_id,
            "canonical-fade-conversation",
        )
        self.assertEqual(
            [bubble.text() for bubble in self.popup._chat.findChildren(_Bubble)],
            before,
        )
        self.popup._motion.settle()
        self.assertEqual(
            [bubble.text() for bubble in self.popup._chat.findChildren(_Bubble)],
            before,
        )


def tearDownModule():
    _TEMP_HOME.cleanup()


if __name__ == "__main__":
    unittest.main()
