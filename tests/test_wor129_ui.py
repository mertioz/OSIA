"""Focused WOR-129 desktop themes, motion and streaming regression tests."""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from unittest.mock import patch


_TEMP_HOME = tempfile.TemporaryDirectory(prefix="osia-wor129-tests-")
os.environ["USERPROFILE"] = _TEMP_HOME.name
os.environ["HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OSIA_REDUCED_MOTION"] = "0"

from PyQt6 import sip
from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QPoint,
    QRect,
    QTimer,
    Qt,
    QVariantAnimation,
)
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from osia.gui.dynamic_island import (
    CIRCLE_SIZE,
    DOCK_PILL_H,
    DOCK_PILL_W,
    ISLAND_H,
    ISLAND_W,
    MORPH_MS,
    DynamicIsland,
)
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


def _relative_luminance(color: QColor) -> float:
    channels = (color.redF(), color.greenF(), color.blueF())
    linear = tuple(
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(left: QColor, right: QColor) -> float:
    lighter, darker = sorted(
        (_relative_luminance(left), _relative_luminance(right)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _blend(foreground: QColor, background: QColor, alpha: float) -> QColor:
    return QColor.fromRgbF(
        alpha * foreground.redF() + (1.0 - alpha) * background.redF(),
        alpha * foreground.greenF() + (1.0 - alpha) * background.greenF(),
        alpha * foreground.blueF() + (1.0 - alpha) * background.blueF(),
    )


def _chat_binding(suffix: str) -> dict:
    return {
        "conversation_id": "wor129-conversation",
        "request_id": f"wor129-request-{suffix}",
        "turn_id": f"wor129-turn-{suffix}",
        "run_id": f"wor129-turn-{suffix}",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-luna",
        "effective_reasoning": "high",
    }


def _finish_reveal(controller: DesktopTextStreamController) -> None:
    for _ in range(256):
        if not controller.active:
            return
        controller._on_tick()
    raise AssertionError("desktop presentation stream did not settle")


class ThemeTests(unittest.TestCase):
    def test_default_palette_softens_foreground_and_keeps_base_colors(self):
        palette = PALETTES[DEFAULT_THEME_KEY]
        self.assertEqual(palette.background, "#0d0b12")
        self.assertEqual(palette.primary, "#c4b5f5")
        self.assertEqual(palette.text, "#d5d3dd")
        self.assertEqual(palette.muted_text, "#9d9aad")
        template = "QWidget { color: #e8e6f0; background: #0d0b12; }"
        self.assertEqual(themed_stylesheet(template, palette),
                         "QWidget { color: #d5d3dd; background: #0d0b12; }")

    def test_foregrounds_remain_distinct_from_provider_model_counts(self):
        for palette in PALETTES.values():
            with self.subTest(theme=palette.key):
                text = palette.color("text")
                self.assertLess(max(text.red(), text.green(), text.blue()), 235)
                self.assertGreaterEqual(_contrast_ratio(text, palette.color("muted_text")), 1.6)
                style = themed_stylesheet("QWidget { color: #ffffff; }", palette)
                self.assertIn(palette.text, style)

    def test_every_theme_resolves_every_semantic_color(self):
        self.assertGreaterEqual(len(PALETTES), 6)
        for palette in PALETTES.values():
            for field in SEMANTIC_COLOR_FIELDS:
                value = getattr(palette, field)
                self.assertTrue(value, f"{palette.key}.{field}")
                self.assertTrue(QColor(value).isValid(), f"{palette.key}.{field}")

    def test_targeted_text_and_pressed_send_contrast_meet_aa(self):
        for palette in PALETTES.values():
            surfaces = (
                QColor(palette.background),
                QColor(palette.surface),
                QColor(palette.elevated_surface),
            )
            for field in ("text", "muted_text", "disabled", "error", "warning"):
                foreground = QColor(getattr(palette, field))
                for surface in surfaces:
                    self.assertGreaterEqual(
                        _contrast_ratio(foreground, surface),
                        4.5,
                        f"{palette.key}.{field} on {surface.name()}",
                    )

            pressed_background = _blend(
                QColor(palette.primary), QColor(palette.background), 0.66,
            )
            self.assertGreaterEqual(
                _contrast_ratio(QColor(palette.background), pressed_background),
                4.5,
                f"{palette.key}.send.pressed",
            )

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
    def test_large_chunk_and_provider_final_are_progressively_exact(self):
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
        self.assertTrue(controller.active)
        _finish_reveal(controller)
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

        controller.begin()
        controller.append_chunk("partial")
        controller.error("Error: stopped safely")
        _finish_reveal(controller)
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

    def test_project_toggle_keeps_header_and_focus_at_same_scroll_position(self):
        from osia.gui.main_popup import _ConversationsPanel

        panel = _ConversationsPanel()
        panel._poll_timer.stop()
        panel.resize(425, 400)
        folders = [
            {"id": "above", "name": "Above", "collapsed": False},
            {"id": "target", "name": "Target", "collapsed": True},
        ]
        items = [
            {"id": f"chat-{index}", "title": "Fixture discussion", "folder_id": folder_id}
            for index, folder_id in enumerate(["above"] * 8 + ["target"] * 12 + [None] * 20)
        ]
        snapshot = {"snapshot_revision": 1, "folders": folders, "items": items}
        snapshot_patch = patch(
            "osia.gui.main_popup.conversations.conversation_snapshot",
            return_value=snapshot,
        )
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)

        def update(request):
            folders[1]["collapsed"] = request["collapsed"]
            snapshot["snapshot_revision"] += 1

        panel.folder_mutation_requested.connect(update)
        try:
            panel.refresh(snapshot=snapshot)
            panel.show()
            panel.activateWindow()
            QTest.qWait(25)
            scrollbar = panel._scroll.verticalScrollBar()
            header = panel._folder_widgets["target"]
            scrollbar.setValue(header.mapTo(panel._host, QPoint()).y() - 55)
            initial = scrollbar.value()
            for collapsed in (False, True):
                if collapsed:
                    QTest.keyClick(header, Qt.Key.Key_Space)
                else:
                    QTest.mouseClick(header, Qt.MouseButton.LeftButton)
                self.app.processEvents()
                header = panel._folder_widgets["target"]
                self.assertTrue(header.hasFocus())
                # Further snapshots may arrive while the transition is running.
                panel.refresh(snapshot=snapshot)
                panel.refresh(snapshot=snapshot)
                QTest.qWait(25)
                header = panel._folder_widgets["target"]
                self.assertEqual(header._collapsed, collapsed)
                self.assertEqual(scrollbar.value(), initial)
                self.assertEqual(
                    header.mapTo(panel._host, QPoint()).y() - scrollbar.value(), 55,
                )
                self.assertTrue(header.hasFocus())
        finally:
            panel.close()
            panel.deleteLater()
            self.app.processEvents()

    def test_composer_and_conversation_buttons_repaint_on_pointer_hover(self):
        def assert_hover(button):
            window = button.window()
            outside = QPoint(window.width() // 2, 45)
            QTest.mouseMove(window, outside)
            QTest.qWait(12)
            resting = button.grab().toImage()
            QTest.mouseMove(button, button.rect().center())
            QTest.qWait(12)
            self.assertTrue(button.underMouse())
            self.assertNotEqual(button.grab().toImage(), resting, button.objectName())
            QTest.mouseMove(window, outside)
            QTest.qWait(12)
            self.assertEqual(button.grab().toImage(), resting, button.objectName())

        chat = self.popup._chat
        chat._input.clear()
        assert_hover(chat._file_btn)
        self.assertFalse(chat._send_btn.isEnabled())
        assert_hover(chat._send_btn)
        self.assertFalse(chat._send_btn.isEnabled())
        chat._input.setPlainText("Fixture message")
        self.assertTrue(chat._send_btn.isEnabled())
        assert_hover(chat._send_btn)
        chat._input.clear()

        panel = self.popup._convs
        previous_snapshot = panel._last_snapshot
        previous_active = panel._active_id
        was_polling = panel._poll_timer.isActive()
        panel._poll_timer.stop()
        panel._active_id = "active"
        panel.refresh(snapshot={
            "folders": [{"id": "project", "name": "Project", "collapsed": False}],
            "items": [
                {"id": "active", "title": "Current conversation", "folder_id": "project"},
                {"id": "other", "title": "Another conversation"},
            ],
        })
        try:
            self.popup._stack.setCurrentWidget(panel)
            QTest.qWait(30)
            assert_hover(panel._more_btn)
            assert_hover(panel._folder_widgets["project"].findChild(QPushButton, "folderMenu"))
            assert_hover(panel._row_widgets["active"])
            assert_hover(panel._row_widgets["other"])
            assert_hover(panel._row_widgets["other"].findChild(QPushButton, "convRename"))
        finally:
            self.popup._stack.setCurrentWidget(chat)
            panel._active_id = previous_active
            panel._last_snapshot = None
            panel.refresh(snapshot=previous_snapshot or {"items": []})
            if was_polling:
                panel._poll_timer.start()

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
        ):
            self.popup._stack.setCurrentIndex(page)
            self.popup._animate_width(POPUP_W)
            self.assertEqual((self.popup.width(), self.popup.height()), (440, 560))
        self.popup._stack.setCurrentIndex(self.popup.PAGE_SETTINGS)
        self.popup._animate_width(POPUP_W_WIDE)
        self.assertEqual((self.popup.width(), self.popup.height()), (640, 560))

        self.assertEqual(
            (ISLAND_W, ISLAND_H, CIRCLE_SIZE, DOCK_PILL_W, DOCK_PILL_H),
            (320, 52, 60, 28, 80),
        )
        self.assertEqual(MORPH_MS, 300)
        with patch("osia.config.load_settings", return_value={}):
            island = DynamicIsland()
        self.app.processEvents()
        self.assertEqual((island.width(), island.height()), (320, 52))
        self.assertEqual(island._morph_anim.duration(), 300)
        island.close()
        island.deleteLater()

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
        binding = _chat_binding("switch")
        self.assertTrue(chat.begin_turn(binding))
        chat.append_assistant_stream_chunk("old conversation partial", binding)
        self.assertTrue(chat._stream.active)
        chat.set_messages([{"role": "user", "content": "new conversation"}])
        self.assertFalse(chat._stream.active)
        self.assertFalse(chat._stream.timer_active)
        self.assertEqual(chat._stream.target_text, "")
        self.assertIsNone(chat._stream_bubble)

    def test_chat_final_is_exact_and_error_stops_rendering(self):
        chat = self.popup._chat
        chunk = "A substantial streamed sentence for the desktop."
        final_binding = _chat_binding("final")
        self.assertTrue(chat.begin_turn(final_binding))
        chat.append_assistant_stream_chunk(chunk, final_binding)
        chat._stream._on_tick()
        bubble = chat._stream_bubble
        self.assertIsNotNone(bubble)
        final = chunk + " Canonical final tail."
        chat.finalize_assistant_stream(final, final_binding)
        _finish_reveal(chat._stream)
        self.assertEqual(bubble.text(), final)
        self.assertFalse(chat._stream.timer_active)

        empty_binding = _chat_binding("empty")
        self.assertTrue(chat.begin_turn(empty_binding))
        chat.append_assistant_stream_chunk(
            "partial replaced by empty final", empty_binding,
        )
        chat._stream._on_tick()
        chat.finalize_assistant_stream("", empty_binding)
        self.assertEqual(chat._stream.displayed_text, "")
        self.assertIsNone(chat._stream_bubble)
        self.assertFalse(chat._stream.timer_active)

        error_binding = _chat_binding("error")
        self.assertTrue(chat.begin_turn(error_binding))
        chat.append_assistant_stream_chunk("partial before failure", error_binding)
        chat._stream._on_tick()
        error_bubble = chat._stream_bubble
        chat.fail_assistant_stream("Error: simulated", error_binding)
        _finish_reveal(chat._stream)
        self.assertEqual(error_bubble.text(), "Error: simulated")
        self.assertFalse(chat._stream.timer_active)

    def test_final_reply_fades_as_its_first_text_appears(self):
        chat = self.popup._chat
        binding = _chat_binding("fade-final")
        text = "A final answer that appears in one bubble."
        with (
            patch.dict(os.environ, {"OSIA_REDUCED_MOTION": "0"}),
            patch.object(chat._motion, "fade_in", wraps=chat._motion.fade_in) as fade,
        ):
            self.assertTrue(chat.begin_turn(binding))
            self.assertTrue(chat.finalize_assistant_stream(text, binding))
            chat._stream._on_tick()
            wrap, bubble = chat._stream_wrap, chat._stream_bubble
            fade.assert_called_once_with(wrap, chat._FINAL_REPLY_FADE_MS)
            effect = wrap.graphicsEffect()
            self.assertIsNotNone(effect)
            self.assertLess(effect.opacity(), 1.0)
            _finish_reveal(chat._stream)
            self.assertEqual(bubble.text(), text)
            QTest.qWait(chat._FINAL_REPLY_FADE_MS + 50)
            self.assertIsNone(wrap.graphicsEffect())

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
