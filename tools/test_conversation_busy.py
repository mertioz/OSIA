import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    sys.modules["numpy"] = SimpleNamespace(ndarray=object)

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psutil"] = SimpleNamespace()

try:
    import soundfile  # noqa: F401
except ModuleNotFoundError:
    sys.modules["soundfile"] = SimpleNamespace()

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    class _FakeFastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def _decorator(self, *args, **kwargs):
            def wrap(fn):
                return fn
            return wrap

        get = post = put = delete = websocket = on_event = _decorator

    class _FakeWebSocketDisconnect(Exception):
        pass

    sys.modules["fastapi"] = SimpleNamespace(
        FastAPI=_FakeFastAPI,
        WebSocket=object,
        WebSocketDisconnect=_FakeWebSocketDisconnect,
        Request=object,
    )

from osia.server import server


class _FakeChannel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


class ConversationBusyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._ACTIVE_TURN_META_BY_CONV.clear()
        server._ACTIVE_CONV_BY_REQUEST.clear()
        server._WS_CLIENTS.clear()

    async def asyncTearDown(self):
        for task in list(server._ACTIVE_TURNS_BY_CONV.values()):
            task.cancel()
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._ACTIVE_TURN_META_BY_CONV.clear()
        server._ACTIVE_CONV_BY_REQUEST.clear()
        server._WS_CLIENTS.clear()

    async def test_same_conversation_claim_rejected_different_allowed(self):
        loop = asyncio.get_running_loop()
        turn_a = loop.create_future()
        duplicate_a = loop.create_future()
        turn_b = loop.create_future()

        self.assertTrue(server._claim_conversation_turn("conv-a", turn_a))
        self.assertFalse(server._claim_conversation_turn("conv-a", duplicate_a))
        self.assertTrue(server._claim_conversation_turn("conv-b", turn_b))

        turn_a.cancel()
        duplicate_a.cancel()
        turn_b.cancel()

    async def test_busy_clears_for_done_error_or_cancel_paths(self):
        loop = asyncio.get_running_loop()
        turn = loop.create_future()

        self.assertTrue(server._claim_conversation_turn("conv-a", turn))
        self.assertTrue(server._is_conversation_busy("conv-a"))
        self.assertTrue(server._clear_conversation_turn("conv-a", turn))
        self.assertFalse(server._is_conversation_busy("conv-a"))

    async def test_done_future_is_pruned_from_busy_snapshot(self):
        loop = asyncio.get_running_loop()
        turn = loop.create_future()

        self.assertTrue(server._claim_conversation_turn("conv-a", turn))
        turn.set_result(None)

        self.assertEqual(server._active_conversation_busy_ids(), [])
        self.assertFalse(server._is_conversation_busy("conv-a"))

    async def test_busy_changed_broadcast_frame(self):
        client = _FakeChannel()
        server._WS_CLIENTS.add(client)

        await server._broadcast_conversation_busy_changed("conv-a", True)

        self.assertEqual(
            client.frames,
            [{
                "type": "conversation_busy_changed",
                "conversation_id": "conv-a",
                "busy": True,
            }],
        )

    async def test_busy_changed_broadcast_frame_preserves_turn_ids(self):
        client = _FakeChannel()
        server._WS_CLIENTS.add(client)
        meta = {
            "conversation_id": "conv-a",
            "client_request_id": "req-busy",
            "turn_id": "turn-busy",
        }

        await server._broadcast_conversation_busy_changed("conv-a", False, meta)

        self.assertEqual(client.frames[0]["type"], "conversation_busy_changed")
        self.assertEqual(client.frames[0]["conversation_id"], "conv-a")
        self.assertEqual(client.frames[0]["busy"], False)
        self.assertEqual(client.frames[0]["client_request_id"], "req-busy")
        self.assertEqual(client.frames[0]["turn_id"], "turn-busy")

    async def test_busy_rejection_frame(self):
        client = _FakeChannel()

        await server._send_conversation_busy(client, "conv-a")

        self.assertEqual(client.frames[0]["type"], "conversation_busy")
        self.assertEqual(client.frames[0]["conversation_id"], "conv-a")
        self.assertIn("still working", client.frames[0]["message"])

    async def test_same_client_request_reattaches_to_running_turn(self):
        loop = asyncio.get_running_loop()
        turn = loop.create_future()
        client = _FakeChannel()
        meta = {
            "conversation_id": "conv-a",
            "client_request_id": "req-1",
            "turn_id": "turn-1",
            "persisted": True,
        }

        self.assertTrue(server._claim_conversation_turn("conv-a", turn, meta))
        self.assertTrue(
            await server._send_existing_turn_status(client, "conv-a", "req-1")
        )

        self.assertEqual([f["type"] for f in client.frames],
                         ["turn_accepted", "turn_status"])
        self.assertEqual(client.frames[0]["client_request_id"], "req-1")
        self.assertEqual(client.frames[1]["status"], "running")
        self.assertFalse(
            await server._send_existing_turn_status(client, "conv-a", "req-2")
        )
        turn.cancel()

    def test_accepted_user_message_persists_with_request_metadata(self):
        old_dir = server.conversations.CONVERSATIONS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            server.conversations.CONVERSATIONS_DIR = Path(tmp)
            try:
                conv = server.conversations.create_conversation()
                meta = {
                    "conversation_id": conv["id"],
                    "client_request_id": "req-accepted",
                    "turn_id": "turn-accepted",
                }

                self.assertTrue(
                    server._persist_accepted_user_message(
                        conv["id"], "hello", meta,
                    )
                )

                stored = server.conversations.get_conversation(conv["id"])
                messages = stored["messages"]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["role"], "user")
                self.assertEqual(messages[0]["content"], "hello")
                self.assertEqual(
                    messages[0]["_client_request_id"], "req-accepted",
                )
                self.assertEqual(messages[0]["_turn_id"], "turn-accepted")
            finally:
                server.conversations.CONVERSATIONS_DIR = old_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
