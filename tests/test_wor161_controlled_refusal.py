"""Focused WOR-161 controlled-refusal rendering regressions."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

from osia.chatgpt_account.app_server_client import AppServerError
from osia.chatgpt_account.provider import AccountProviderError
from osia.gui.main_popup import _Bubble, _ChatPage
from osia.main import _finalize_controlled_refusal


_REFUSAL = (
    "OSIA couldn’t safely translate the action requested by ChatGPT. "
    "No action was performed. An alternative workflow supported by OSIA "
    "may be available."
)


def _provider_error(*, category: str = "native_item:commandExecution"):
    return AccountProviderError(AppServerError(
        _REFUSAL,
        native_action=True,
        controlled_refusal=True,
        allow_fallback=False,
        category=category,
    ))


class ControlledRefusalFinalizationTests(unittest.TestCase):
    def test_controlled_refusal_persists_and_emits_one_normal_final(self):
        agent = SimpleNamespace(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Run an action"},
        ])
        emitted = []

        with patch("osia.main._persist_agent_delta") as persist:
            display = _finalize_controlled_refusal(
                _provider_error(),
                agent,
                "conversation-1",
                0,
                lambda conv_id, text: emitted.append((conv_id, text)),
            )

        self.assertEqual(display, _REFUSAL)
        self.assertFalse(display.startswith("Error:"))
        self.assertEqual(emitted, [("conversation-1", _REFUSAL)])
        self.assertEqual(
            [message for message in agent.messages if message["role"] == "assistant"],
            [{"role": "assistant", "content": _REFUSAL}],
        )
        persist.assert_called_once_with(
            "conversation-1", agent, 0, user_display_text=None,
        )

    def test_real_technical_auth_and_quota_failures_stay_on_error_path(self):
        for error in (
            RuntimeError("transport failed"),
            AppServerError("ERROR: login required", category="login_missing"),
            AppServerError("ERROR: quota exceeded", category="quota_exceeded"),
            AppServerError(
                "ERROR: non-native refusal",
                controlled_refusal=True,
                native_action=False,
            ),
        ):
            with self.subTest(error=error):
                agent = SimpleNamespace(messages=[])
                emitted = []
                with patch("osia.main._persist_agent_delta") as persist:
                    display = _finalize_controlled_refusal(
                        error,
                        agent,
                        "conversation-1",
                        0,
                        lambda *_args: emitted.append(_args),
                    )
                self.assertIsNone(display)
                self.assertEqual(agent.messages, [])
                self.assertEqual(emitted, [])
                persist.assert_not_called()


class ControlledRefusalPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_partial_stream_is_replaced_by_one_normal_unlocked_response(self):
        chat = _ChatPage()
        chat.show()
        chat.set_busy(True)
        chat.append_assistant_stream_chunk("Partial account text.")
        chat._stream._on_tick()
        bubble = chat._stream_bubble

        chat.finalize_assistant_stream(_REFUSAL)
        chat.set_busy(False)
        self.app.processEvents()

        self.assertIsNotNone(bubble)
        self.assertEqual(bubble.text(), _REFUSAL)
        self.assertFalse(bubble.property("error"))
        self.assertFalse(chat._stream.active)
        self.assertFalse(chat._stream.timer_active)
        self.assertTrue(chat._input.isEnabled())
        self.assertEqual(len(chat.findChildren(_Bubble)), 1)
        self.assertFalse(any(
            "retry" in button.objectName().lower()
            for button in chat.findChildren(QPushButton)
        ))
        chat.close()
        chat.deleteLater()

    def test_real_technical_failure_keeps_error_prefix_and_visual_treatment(self):
        chat = _ChatPage()
        chat.show()
        chat.set_busy(True)
        chat.append_assistant_stream_chunk("Partial technical response.")
        chat._stream._on_tick()
        bubble = chat._stream_bubble

        chat.fail_assistant_stream("Error: transport failed")
        chat.set_busy(False)
        self.app.processEvents()

        self.assertIsNotNone(bubble)
        self.assertEqual(bubble.text(), "Error: transport failed")
        self.assertTrue(bubble.property("error"))
        self.assertTrue(chat._input.isEnabled())
        chat.close()
        chat.deleteLater()


if __name__ == "__main__":
    unittest.main()
