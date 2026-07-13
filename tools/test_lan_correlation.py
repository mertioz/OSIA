import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osia.server import server


class _FakeChannel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


class _ProviderHistoryAgent:
    def __init__(self):
        self.model = "test-model"
        self.provider = "test-provider"
        self._api_key_index = 0
        self._active_base_url = "test-base"
        self._primary_base_url = "test-base"
        self._applied_selected_model = None
        self._history = []

    def set_model(self, model, provider=None):
        self.model = model
        self.provider = provider or self.provider

    def run(self, text, *, history, **_kwargs):
        self._history = list(history) + [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": "provider planning round",
                "_internal": True,
            },
            {"role": "tool", "content": "provider tool result"},
            {"role": "assistant", "content": "final answer"},
        ]
        return "final answer"

    def exported_history(self):
        return list(self._history)


class LanCorrelationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_dir = server.conversations.CONVERSATIONS_DIR
        self._tmp = tempfile.TemporaryDirectory()
        server.conversations.CONVERSATIONS_DIR = Path(self._tmp.name)
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._ACTIVE_TURN_META_BY_CONV.clear()
        server._ACTIVE_CONV_BY_REQUEST.clear()

    def tearDown(self):
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._ACTIVE_TURN_META_BY_CONV.clear()
        server._ACTIVE_CONV_BY_REQUEST.clear()
        server.conversations.CONVERSATIONS_DIR = self._old_dir
        self._tmp.cleanup()

    def _new_meta(self, conv_id, request_id):
        meta = server._new_turn_meta(conv_id, request_id)
        meta["client_content_hash"] = f"hash-{request_id}"
        return meta

    def _persist_completed_turn(self, conv_id, request_id, user, assistant):
        meta = self._new_meta(conv_id, request_id)
        self.assertTrue(server._persist_accepted_user_message(conv_id, user, meta))
        response = server._turn_event_frame(
            meta,
            "response",
            message_id=meta["assistant_message_id"],
            payload={"content": assistant},
        )
        terminal = server._turn_status_frame(meta, "done")
        self.assertTrue(
            server._persist_completed_assistant_message(
                conv_id,
                assistant,
                meta,
                response,
                terminal,
            )
        )
        return meta, response, terminal

    def test_completed_turn_persists_exactly_once_and_survives_reload(self):
        conv = server.conversations.create_conversation()
        meta, response, terminal = self._persist_completed_turn(
            conv["id"], "request-one", "accepted user", "final assistant",
        )

        self.assertTrue(
            server._persist_accepted_user_message(
                conv["id"], "accepted user", meta,
            )
        )
        self.assertTrue(
            server._persist_completed_assistant_message(
                conv["id"],
                "final assistant",
                meta,
                response,
                terminal,
            )
        )

        stored = server.conversations.get_conversation(conv["id"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in stored["messages"]],
            [("user", "accepted user"), ("assistant", "final assistant")],
        )
        found = server.conversations.find_correlated_turn(
            conv["id"], "request-one",
        )
        self.assertEqual(found["turn_id"], meta["turn_id"])
        self.assertEqual(found["user_message_id"], meta["user_message_id"])
        self.assertEqual(
            found["assistant_message_id"], meta["assistant_message_id"],
        )
        self.assertEqual(found["response_event_id"], response["event_id"])
        self.assertEqual(found["response_sequence"], response["sequence"])
        self.assertEqual(found["terminal_event_id"], terminal["event_id"])
        self.assertEqual(found["terminal_sequence"], terminal["sequence"])

    async def test_completed_replay_is_idempotent_and_reuses_event_ids(self):
        conv = server.conversations.create_conversation()
        self._persist_completed_turn(
            conv["id"], "request-replay", "accepted user", "final assistant",
        )
        before = server.conversations.get_conversation(conv["id"])["messages"]
        channel = _FakeChannel()

        self.assertTrue(
            await server._send_persisted_turn_for_request(
                channel, conv["id"], "request-replay",
            )
        )
        first = list(channel.frames)
        self.assertTrue(
            await server._send_persisted_turn_for_request(
                channel, conv["id"], "request-replay",
            )
        )
        second = channel.frames[len(first):]

        self.assertEqual(first, second)
        self.assertEqual([frame["type"] for frame in first], ["response", "turn_status"])
        self.assertTrue(first[-1]["terminal"])
        self.assertEqual(
            server.conversations.get_conversation(conv["id"])["messages"],
            before,
        )

    def test_missing_correlation_and_assistant_without_user_fail_closed(self):
        conv = server.conversations.create_conversation()
        incomplete = {
            "conversation_id": conv["id"],
            "request_id": "request-incomplete",
            "client_request_id": "request-incomplete",
            "turn_id": "turn-incomplete",
        }
        self.assertFalse(
            server._persist_accepted_user_message(
                conv["id"], "accepted user", incomplete,
            )
        )
        self.assertEqual(
            server.conversations.get_conversation(conv["id"])["messages"], [],
        )

        with self.assertRaisesRegex(ValueError, "no accepted user turn"):
            server.conversations.add_message(
                conv["id"],
                "assistant",
                "orphan assistant",
                metadata={
                    "_conversation_id": conv["id"],
                    "_request_id": "request-orphan",
                    "_client_request_id": "request-orphan",
                    "_turn_id": "turn-orphan",
                    "_message_id": "message-orphan",
                },
            )

    def test_stale_request_cannot_mutate_newer_turn(self):
        conv = server.conversations.create_conversation()
        older = self._new_meta(conv["id"], "request-older")
        newer = self._new_meta(conv["id"], "request-newer")
        self.assertTrue(
            server._persist_accepted_user_message(conv["id"], "older", older)
        )
        self.assertTrue(
            server._persist_accepted_user_message(conv["id"], "newer", newer)
        )

        old_response = server._turn_event_frame(
            older,
            "response",
            message_id=older["assistant_message_id"],
            payload={"content": "stale assistant"},
        )
        old_terminal = server._turn_status_frame(older, "done")
        self.assertFalse(
            server._persist_completed_assistant_message(
                conv["id"],
                "stale assistant",
                older,
                old_response,
                old_terminal,
            )
        )

        new_response = server._turn_event_frame(
            newer,
            "response",
            message_id=newer["assistant_message_id"],
            payload={"content": "new assistant"},
        )
        new_terminal = server._turn_status_frame(newer, "done")
        self.assertTrue(
            server._persist_completed_assistant_message(
                conv["id"],
                "new assistant",
                newer,
                new_response,
                new_terminal,
            )
        )
        self.assertEqual(
            [item["content"] for item in server.conversations.get_conversation(conv["id"])["messages"]],
            ["older", "newer", "new assistant"],
        )

    async def test_cross_conversation_replay_remains_isolated(self):
        conv_a = server.conversations.create_conversation()
        conv_b = server.conversations.create_conversation()
        self._persist_completed_turn(
            conv_a["id"], "request-a", "user a", "assistant a",
        )
        self._persist_completed_turn(
            conv_b["id"], "request-b", "user b", "assistant b",
        )
        channel = _FakeChannel()

        self.assertTrue(
            await server._send_persisted_turn_for_request(
                channel, conv_a["id"], "request-a",
            )
        )
        self.assertTrue(channel.frames)
        self.assertTrue(
            all(frame["conversation_id"] == conv_a["id"] for frame in channel.frames)
        )
        self.assertFalse(
            await server._send_persisted_turn_for_request(
                channel, conv_a["id"], "request-b",
            )
        )

    async def test_provider_internal_rounds_are_not_persisted_as_messages(self):
        conv = server.conversations.create_conversation()
        meta = self._new_meta(conv["id"], "request-provider-rounds")
        channel = _FakeChannel()
        agent = _ProviderHistoryAgent()

        with patch.object(server, "_schedule_response_audio", return_value=None):
            new_history, returned_conv = await server._run_agent_and_respond(
                channel,
                "accepted user",
                [],
                conv["id"],
                get_agent=lambda _conv_id: agent,
                turn_meta=meta,
            )

        self.assertEqual(returned_conv, conv["id"])
        self.assertGreater(len(new_history), 2)
        stored = server.conversations.get_conversation(conv["id"])["messages"]
        self.assertEqual(
            [(item["role"], item["content"]) for item in stored],
            [("user", "accepted user"), ("assistant", "final answer")],
        )

    def test_media_events_do_not_create_or_finalize_text_messages(self):
        conv = server.conversations.create_conversation()
        meta, _response, _terminal = self._persist_completed_turn(
            conv["id"], "request-text", "accepted user", "final assistant",
        )
        before = server.conversations.get_conversation(conv["id"])["messages"]
        audio = server._turn_event_frame(
            meta,
            "audio",
            message_id=meta["assistant_message_id"],
            payload={"phase": "media", "media_terminal": True},
        )
        speak_meta = self._new_meta(conv["id"], "request-speak")
        speak = server._turn_event_frame(
            speak_meta,
            "media_status",
            message_id=meta["assistant_message_id"],
            payload={
                "phase": "media",
                "status": "error",
                "media_terminal": True,
            },
        )

        self.assertEqual(audio["event_type"], "audio")
        self.assertEqual(speak["event_type"], "media_status")
        self.assertNotEqual(audio["request_id"], speak["request_id"])
        self.assertNotEqual(audio["turn_id"], speak["turn_id"])
        self.assertNotEqual(audio["event_id"], speak["event_id"])
        self.assertEqual(
            server.conversations.get_conversation(conv["id"])["messages"],
            before,
        )

    def test_cron_notification_cannot_infer_foreground_correlation(self):
        foreground = server._new_turn_meta("foreground-conv", "foreground-request")
        foreground["_event_sequence"] = 7
        server._ACTIVE_TURN_META_BY_CONV["foreground-conv"] = foreground
        payload = {
            "id": "cron-note-fixed",
            "task_id": "cron-task-fixed",
            "title": "Background task",
            "status": "completed",
            "conversation_id": "cron-conv",
            "created_at": "2026-07-13T18:00:00+02:00",
        }

        first = server._cron_notification_frame(payload)
        second = server._cron_notification_frame(payload)

        self.assertEqual(first, second)
        self.assertEqual(first["conversation_id"], "cron-conv")
        self.assertNotEqual(first["request_id"], foreground["request_id"])
        self.assertNotEqual(first["turn_id"], foreground["turn_id"])
        self.assertTrue(first["terminal"])
        self.assertEqual(foreground["_event_sequence"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
