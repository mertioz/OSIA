"""Focused WOR-168 conversation-bubble wrapping and geometry tests."""

from __future__ import annotations

import os
import tempfile
import unittest


_TEMP_HOME = tempfile.TemporaryDirectory(prefix="osia-wor168-tests-")
os.environ["USERPROFILE"] = _TEMP_HOME.name
os.environ["HOME"] = _TEMP_HOME.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OSIA_REDUCED_MOTION"] = "1"

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
from PyQt6.QtGui import QTextCursor, QTextOption
from PyQt6.QtWidgets import QApplication

from osia.gui.main_popup import (
    _BUBBLE_MAX_WIDTH,
    _Bubble,
    _ChatPage,
    _MessageText,
)


_LONG_URL = "https://example.invalid/synthetic/" + "pathpart" * 45
_OPAQUE = "SYNTH" + "A7" * 170
_LONG_WORD = "w" * 300
_QUERY_URL = (
    "https://example.invalid/resource?synthetic="
    + "q" * 310
    + "&mode=local#local-fragment"
)
_MARKDOWN_TARGET = "https://example.invalid/docs/" + "target" * 55
_MARKDOWN_LINK = f"[synthetic documentation]({_MARKDOWN_TARGET})"
_INLINE_CODE = "`" + "inline" * 55 + "`"
_FENCED_CODE = "```text\n" + "code" * 85 + "\n```"
_UNICODE = "Unicode café — 東京 🚀🙂 " + "界" * 310
_HTML_LIKE = "<b>literal markup</b> & <opaque-tag>value</opaque-tag>"


class BubbleWrappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._widgets = []

    def tearDown(self):
        for widget in self._widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _chat(self, width: int, height: int = 500) -> _ChatPage:
        chat = _ChatPage()
        self._widgets.append(chat)
        chat.resize(width, height)
        chat.show()
        self.app.processEvents()
        return chat

    def _bubble(
        self, chat: _ChatPage, role: str, text: str,
    ) -> _Bubble:
        chat.append_message(role, text)
        self.app.processEvents()
        bubbles = chat.findChildren(_Bubble)
        self.assertTrue(bubbles)
        return bubbles[-1]

    def _assert_source_and_document(
        self, bubble: _Bubble, source: str,
    ) -> None:
        label = bubble.findChild(_MessageText, "msgText")
        self.assertIsNotNone(label)
        self.assertEqual(bubble.text(), source)
        self.assertEqual(label.toPlainText(), source)
        self.assertEqual(
            label.document().defaultTextOption().wrapMode(),
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere,
        )
        self.assertEqual(label.horizontalScrollBar().maximum(), 0)
        self.assertEqual(label.verticalScrollBar().maximum(), 0)
        self.assertLessEqual(
            label.document().textWidth(), label.viewport().width()
        )
        self.assertLessEqual(
            label.document().size().height(), label.viewport().height()
        )

    def _assert_viewport_geometry(
        self, chat: _ChatPage, bubble: _Bubble,
    ) -> None:
        viewport = chat._scroll.viewport()
        margins = chat._messages_layout.contentsMargins()
        maximum = min(
            _BUBBLE_MAX_WIDTH,
            viewport.width() - margins.left() - margins.right(),
        )
        top_left = bubble.mapTo(viewport, QPoint(0, 0))
        bottom_right = bubble.mapTo(
            viewport, QPoint(bubble.width() - 1, bubble.height() - 1)
        )
        self.assertEqual(bubble.maximumWidth(), maximum)
        self.assertLessEqual(bubble.width(), maximum)
        self.assertGreaterEqual(top_left.x(), viewport.rect().left())
        self.assertLessEqual(bottom_right.x(), viewport.rect().right())
        self.assertEqual(chat._messages_host.width(), viewport.width())
        self.assertEqual(chat._scroll.horizontalScrollBar().maximum(), 0)

    def test_plain_markdown_code_url_and_unicode_sources_wrap_exactly(self):
        cases = {
            "short prose": "A short synthetic reply.",
            "multiline prose": (
                "First synthetic paragraph.\n\n"
                "Second paragraph stays separate and selectable."
            ),
            "heading and list": (
                "# Synthetic heading\n\n- first item\n- second item"
            ),
            "long URL": _LONG_URL,
            "opaque identifier": _OPAQUE,
            "300-character word": _LONG_WORD,
            "query and fragment": _QUERY_URL,
            "Markdown link": _MARKDOWN_LINK,
            "inline code": _INLINE_CODE,
            "fenced code": _FENCED_CODE,
            "Unicode and emoji": _UNICODE,
            "HTML-like text": _HTML_LIKE,
        }
        heights = {}
        for name, source in cases.items():
            with self.subTest(name=name):
                bubble = _Bubble("assistant", source)
                self._widgets.append(bubble)
                bubble.set_available_width(_BUBBLE_MAX_WIDTH)
                bubble.show()
                self.app.processEvents()
                self._assert_source_and_document(bubble, source)
                heights[name] = bubble.height()

        for name in (
            "long URL",
            "opaque identifier",
            "300-character word",
            "query and fragment",
            "Markdown link",
            "inline code",
            "fenced code",
            "Unicode and emoji",
        ):
            self.assertGreater(heights[name], heights["short prose"], name)

        self.assertGreater(len(_LONG_URL), 300)
        self.assertGreater(len(_OPAQUE), 300)
        self.assertEqual(len(_LONG_WORD), 300)
        self.assertIn(_MARKDOWN_TARGET, _MARKDOWN_LINK)

    def test_selected_and_copied_url_and_markdown_target_stay_exact(self):
        copied = {}
        for source in (
            _QUERY_URL, _MARKDOWN_LINK, _INLINE_CODE, _FENCED_CODE, _HTML_LIKE,
        ):
            with self.subTest(source_kind=source[:12]):
                bubble = _Bubble("assistant", source)
                self._widgets.append(bubble)
                label = bubble._label
                cursor = label.textCursor()
                cursor.select(QTextCursor.SelectionType.Document)
                label.setTextCursor(cursor)
                label.copy()
                self.app.processEvents()
                self.assertEqual(QApplication.clipboard().text(), source)
                self.assertEqual(bubble.text(), source)
                copied[source] = QApplication.clipboard().text()
        self.assertIn(_MARKDOWN_TARGET, copied[_MARKDOWN_LINK])

    def test_assistant_and_user_bubbles_fit_narrow_medium_and_wide_views(self):
        for width in (260, 440, 640):
            for role in ("assistant", "user"):
                with self.subTest(width=width, role=role):
                    chat = self._chat(width)
                    short = self._bubble(chat, role, "Short synthetic reply.")
                    bubble = self._bubble(chat, role, _LONG_URL)
                    self.assertGreater(bubble.height(), short.height())
                    self._assert_source_and_document(bubble, _LONG_URL)
                    self._assert_viewport_geometry(chat, bubble)
                    if role == "assistant":
                        self.assertEqual(
                            bubble.mapTo(
                                chat._scroll.viewport(), QPoint(0, 0)
                            ).x(),
                            chat._messages_layout.contentsMargins().left(),
                        )
                    else:
                        right_gap = (
                            chat._scroll.viewport().rect().right()
                            - bubble.mapTo(
                                chat._scroll.viewport(),
                                QPoint(bubble.width() - 1, 0),
                            ).x()
                        )
                        self.assertEqual(
                            right_gap,
                            chat._messages_layout.contentsMargins().right(),
                        )

    def test_resize_narrow_wide_narrow_reflows_width_and_height(self):
        chat = self._chat(260)
        bubble = self._bubble(chat, "assistant", _OPAQUE)
        narrow_height = bubble.height()
        self._assert_viewport_geometry(chat, bubble)

        chat.resize(640, 500)
        self.app.processEvents()
        wide_height = bubble.height()
        self._assert_viewport_geometry(chat, bubble)
        self.assertLess(wide_height, narrow_height)

        chat.resize(260, 500)
        self.app.processEvents()
        self._assert_viewport_geometry(chat, bubble)
        self.assertEqual(bubble.height(), narrow_height)
        self._assert_source_and_document(bubble, _OPAQUE)

    def test_streaming_growth_reuses_bubble_and_recomputes_height(self):
        chat = self._chat(440)
        chat.append_assistant_stream_chunk("Short synthetic stream.")
        self.app.processEvents()
        bubble = chat._stream_bubble
        self.assertIsNotNone(bubble)
        short_height = bubble.height()

        final = "Streaming result: " + _OPAQUE
        chat.finalize_assistant_stream(final)
        self.app.processEvents()
        self.assertEqual(bubble.text(), final)
        self.assertGreater(bubble.height(), short_height)
        self._assert_source_and_document(bubble, final)
        self._assert_viewport_geometry(chat, bubble)

    def test_vertical_overflow_does_not_create_horizontal_overflow(self):
        chat = self._chat(440, 260)
        for index in range(24):
            self._bubble(chat, "assistant", f"Synthetic message {index}.")
        bubble = self._bubble(chat, "user", _QUERY_URL)
        self.assertGreater(chat._scroll.verticalScrollBar().maximum(), 0)
        self._assert_viewport_geometry(chat, bubble)
        self._assert_source_and_document(bubble, _QUERY_URL)

    def test_font_metric_change_recomputes_wrapped_height(self):
        chat = self._chat(440)
        bubble = self._bubble(chat, "assistant", _LONG_WORD)
        before = bubble.height()
        font = bubble._label.font()
        font.setPointSizeF(max(18.0, font.pointSizeF() * 1.7))
        bubble._label.setFont(font)
        self.app.processEvents()
        self.assertGreater(bubble.height(), before)
        self._assert_source_and_document(bubble, _LONG_WORD)
        self._assert_viewport_geometry(chat, bubble)


if __name__ == "__main__":
    unittest.main()
