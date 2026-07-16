"""Focused WOR-129 desktop themes, motion and streaming regression tests."""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest


_TEMP_HOME = tempfile.TemporaryDirectory(prefix="osia-wor129-tests-")
os.environ["USERPROFILE"] = _TEMP_HOME.name
os.environ["HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OSIA_REDUCED_MOTION"] = "0"

from PyQt6 import sip
from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QRect,
    QTimer,
    Qt,
    QVariantAnimation,
)
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QWidget

from osia.gui.main_popup import (
    MainPopup,
    POPUP_H,
    POPUP_W,
    POPUP_W_WIDE,
    _ChatPage,
)
from osia.gui.motion import MOTION, MotionController
from osia.gui.streaming import DesktopTextStreamController
from osia.gui.theme import (
    DEFAULT_THEME_KEY,
    PALETTES,
    SEMANTIC_COLOR_FIELDS,
    THEME_SETTING_KEY,
    ThemeManager,
    themed_stylesheet,
)


class ThemeTests(unittest.TestCase):
    def test_default_palette_is_the_established_pc_palette(self):
        palette = PALETTES[DEFAULT_THEME_KEY]
        self.assertEqual(palette.background, "#0d0b12")
        self.assertEqual(palette.primary, "#c4b5f5")
        self.assertEqual(palette.text, "#e8e6f0")
        self.assertEqual(palette.muted_text, "#9d9aad")
        template = "QWidget { color: #e8e6f0; background: #0d0b12; }"
        self.assertIs(themed_stylesheet(template, palette), template)

    def test_every_theme_resolves_every_semantic_color(self):
        self.assertGreaterEqual(len(PALETTES), 6)
        for palette in PALETTES.values():
            for field in SEMANTIC_COLOR_FIELDS:
                value = getattr(palette, field)
                self.assertTrue(value, f"{palette.key}.{field}")
                self.assertTrue(QColor(value).isValid(), f"{palette.key}.{field}")

    def test_selection_persists_restores_and_emits_once(self):
        store: dict[str, object] = {}

        def load():
            return dict(store)

        def save(key, value):
            store[key] = value

        manager = ThemeManager(load, save)
        changes = QSignalSpy(manager.theme_changed)
        self.assertTrue(manager.set_theme("rose"))
        self.assertFalse(manager.set_theme("rose"))
        self.assertEqual(len(changes), 1)
        self.assertEqual(store[THEME_SETTING_KEY], "rose")
        restored = ThemeManager(load, save)
        self.assertEqual(restored.key, "rose")

    def test_invalid_saved_theme_falls_back_safely(self):
        manager = ThemeManager(
            lambda: {THEME_SETTING_KEY: "not-a-theme"},
            lambda _key, _value: None,
        )
        self.assertEqual(manager.key, DEFAULT_THEME_KEY)


class StreamingControllerTests(unittest.TestCase):
    def test_large_chunk_is_progressive_and_final_is_immediately_exact(self):
        controller = DesktopTextStreamController(
            reduced_motion=lambda: False
        )
        text = "0123456789" * 120
        seen = QSignalSpy(controller.text_changed)
        controller.append_chunk(text)
        self.assertEqual(controller.target_text, text)
        self.assertEqual(len(controller.findChildren(QTimer)), 1)
        controller._on_tick()
        partial = seen[-1][0]
        self.assertGreater(len(partial), 0)
        self.assertLess(len(partial), len(text))
        final = text + " final exact tail"
        controller.finalize(final)
        self.assertEqual(seen[-1][0], final)
        self.assertEqual(controller.displayed_text, final)
        self.assertFalse(controller.timer_active)
        self.assertFalse(controller.active)

    def test_cancel_and_error_stop_and_flush_without_reordering(self):
        controller = DesktopTextStreamController(
            reduced_motion=lambda: False
        )
        seen = QSignalSpy(controller.text_changed)
        controller.append_chunk("alpha")
        controller.append_chunk("beta")
        controller.cancel(flush=True)
        self.assertEqual(seen[-1][0], "alphabeta")
        self.assertFalse(controller.timer_active)
        self.assertFalse(controller.active)

        controller.append_chunk("partial")
        controller.error("Error: stopped safely")
        self.assertEqual(seen[-1][0], "Error: stopped safely")
        self.assertFalse(controller.timer_active)

    def test_reduced_motion_reveals_immediately_without_a_running_timer(self):
        controller = DesktopTextStreamController(
            reduced_motion=lambda: True
        )
        seen = QSignalSpy(controller.text_changed)
        controller.append_chunk("immediate")
        self.assertEqual(seen[-1][0], "immediate")
        self.assertFalse(controller.timer_active)


class QtUiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.popup = MainPopup()

    @classmethod
    def tearDownClass(cls):
        cls.popup.close()
        cls.popup.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        cls.app.processEvents()

    def setUp(self):
        self.popup._window_opacity_anim.stop()
        self.popup.setWindowOpacity(1.0)
        self.popup.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            False,
        )
        self.popup.setGeometry(100, 120, POPUP_W, POPUP_H)
        self.popup._stack.setCurrentIndex(self.popup.PAGE_CHAT)
        self.popup._chat.set_messages([])
        self.popup.show()
        self.app.processEvents()

    def tearDown(self):
        self.popup.hide()
        self.popup._window_opacity_anim.stop()
        self.popup._motion.settle()
        self.app.processEvents()

    def test_window_size_and_page_geometry_contracts_are_unchanged(self):
        self.assertEqual((POPUP_W, POPUP_W_WIDE, POPUP_H), (440, 640, 560))
        self.assertEqual(
            (self.popup.minimumWidth(), self.popup.minimumHeight()),
            (440, 560),
        )
        self.assertEqual(
            (self.popup.maximumWidth(), self.popup.maximumHeight()),
            (640, 560),
        )
        for page in (
            self.popup.PAGE_CHAT,
            self.popup.PAGE_CONVS,
            self.popup.PAGE_MODELS,
            self.popup.PAGE_OSIA,
            self.popup.PAGE_AUTONOMOUS,
        ):
            self.popup._stack.setCurrentIndex(page)
            self.popup._animate_width(POPUP_W)
            self.assertEqual((self.popup.width(), self.popup.height()), (440, 560))
        self.popup._stack.setCurrentIndex(self.popup.PAGE_SETTINGS)
        self.popup._animate_width(POPUP_W_WIDE)
        self.assertEqual((self.popup.width(), self.popup.height()), (640, 560))

    def test_opacity_transitions_preserve_geometry_and_replace_rapid_runs(self):
        widget = QWidget()
        widget.setGeometry(QRect(12, 18, 220, 90))
        widget.show()
        self.app.processEvents()
        geometry = widget.geometry()
        motion = MotionController(reduced_motion=lambda: False)
        for _ in range(30):
            motion.fade_in(widget, MOTION.micro_ms)
            self.assertLessEqual(motion.active_animation_count, 1)
        QTest.qWait(MOTION.micro_ms + 30)
        self.app.processEvents()
        self.assertEqual(widget.geometry(), geometry)
        self.assertEqual(motion.active_animation_count, 0)
        widget.close()
        widget.deleteLater()

    def test_nav_hover_reuses_one_animation_during_rapid_reversal(self):
        button = next(iter(self.popup._nav_buttons.values()))
        geometry = button.geometry()
        for index in range(30):
            button._animate_hover(float(index % 2))
            self.assertEqual(len(button.findChildren(QVariantAnimation)), 1)
        self.assertEqual(button.geometry(), geometry)

    def test_conversation_switch_clears_pending_render(self):
        chat = self.popup._chat
        chat.append_assistant_stream_chunk("old conversation partial")
        self.assertTrue(chat._stream.active)
        chat.set_messages([{"role": "user", "content": "new conversation"}])
        self.assertFalse(chat._stream.active)
        self.assertFalse(chat._stream.timer_active)
        self.assertEqual(chat._stream.target_text, "")
        self.assertIsNone(chat._stream_bubble)

    def test_chat_final_is_exact_and_error_stops_rendering(self):
        chat = self.popup._chat
        chunk = "A substantial streamed sentence for the desktop."
        chat.append_assistant_stream_chunk(chunk)
        chat._stream._on_tick()
        bubble = chat._stream_bubble
        self.assertIsNotNone(bubble)
        final = chunk + " Canonical final tail."
        chat.finalize_assistant_stream(final)
        self.assertEqual(bubble.text(), final)
        self.assertFalse(chat._stream.timer_active)

        chat.append_assistant_stream_chunk("partial replaced by empty final")
        chat._stream._on_tick()
        chat.finalize_assistant_stream("")
        self.assertEqual(chat._stream.displayed_text, "")
        self.assertIsNone(chat._stream_bubble)
        self.assertFalse(chat._stream.timer_active)

        chat.append_assistant_stream_chunk("partial before failure")
        chat._stream._on_tick()
        error_bubble = chat._stream_bubble
        chat.fail_assistant_stream("Error: simulated")
        self.assertEqual(error_bubble.text(), "Error: simulated")
        self.assertFalse(chat._stream.timer_active)

    def test_widget_disposal_deletes_the_only_stream_timer(self):
        host = QWidget()
        controller = DesktopTextStreamController(
            host, reduced_motion=lambda: False
        )
        timer = controller._timer
        self.assertEqual(len(controller.findChildren(QTimer)), 1)
        self.assertNotIn(
            "installEventFilter",
            inspect.getsource(DesktopTextStreamController),
        )
        self.assertNotIn("installEventFilter", inspect.getsource(MotionController))
        controller.append_chunk("still rendering")
        self.assertTrue(timer.isActive())
        host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertTrue(sip.isdeleted(timer))

    def test_user_actions_and_signals_still_fire_exactly_once(self):
        chat = _ChatPage()
        chat.show()
        self.app.processEvents()
        sends = QSignalSpy(chat.send_text)
        chat._input.setText("one action")
        QTest.mouseClick(
            chat._send_btn,
            Qt.MouseButton.LeftButton,
        )
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "one action")

        new_conversations = QSignalSpy(self.popup.new_conversation)
        self.popup._on_new_conv()
        self.assertEqual(len(new_conversations), 1)
        chat.close()
        chat.deleteLater()


def tearDownModule():
    _TEMP_HOME.cleanup()


if __name__ == "__main__":
    unittest.main()
