import asyncio
import sys
import tempfile
import time
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
        self.frame_received = asyncio.Event()

    async def send_json(self, frame):
        self.frames.append(dict(frame))
        self.frame_received.set()


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

    async def test_turn_progress_loop_sends_correlated_keepalive(self):
        client = _FakeChannel()
        meta = {
            "conversation_id": "conv-long",
            "client_request_id": "req-long",
            "turn_id": "turn-long",
            "persisted": True,
        }

        task = asyncio.create_task(
            server._turn_progress_loop(client, meta, interval=0.01)
        )
        await asyncio.wait_for(client.frame_received.wait(), timeout=1.0)
        await server._cancel_turn_progress_task(task)

        self.assertGreaterEqual(len(client.frames), 1)
        frame = client.frames[0]
        self.assertEqual(frame["type"], "turn_status")
        self.assertEqual(frame["status"], "working")
        self.assertEqual(frame["conversation_id"], "conv-long")
        self.assertEqual(frame["client_request_id"], "req-long")
        self.assertEqual(frame["turn_id"], "turn-long")

    async def test_turn_progress_loop_continues_during_thread_work(self):
        client = _FakeChannel()
        meta = {
            "conversation_id": "conv-thread",
            "client_request_id": "req-thread",
            "turn_id": "turn-thread",
            "persisted": True,
        }

        task = asyncio.create_task(
            server._turn_progress_loop(client, meta, interval=0.01)
        )
        worker = asyncio.create_task(asyncio.to_thread(time.sleep, 0.05))
        await asyncio.sleep(0.035)
        await worker
        await server._cancel_turn_progress_task(task)

        self.assertGreaterEqual(len(client.frames), 2)
        self.assertTrue(
            all(frame["client_request_id"] == "req-thread"
                for frame in client.frames)
        )
        self.assertTrue(
            all(frame["turn_id"] == "turn-thread"
                for frame in client.frames)
        )

    async def test_turn_progress_loop_broadcasts_to_secondary_clients(self):
        origin = _FakeChannel()
        secondary = _FakeChannel()
        server._WS_CLIENTS.add(origin)
        server._WS_CLIENTS.add(secondary)
        meta = {
            "conversation_id": "conv-broadcast",
            "client_request_id": "req-broadcast",
            "turn_id": "turn-broadcast",
            "persisted": True,
        }

        task = asyncio.create_task(
            server._turn_progress_loop(origin, meta, interval=0.01)
        )
        await asyncio.wait_for(
            asyncio.gather(
                origin.frame_received.wait(),
                secondary.frame_received.wait(),
            ),
            timeout=1.0,
        )
        await server._cancel_turn_progress_task(task)

        self.assertGreaterEqual(len(origin.frames), 1)
        self.assertGreaterEqual(len(secondary.frames), 1)
        self.assertEqual(origin.frames[0], secondary.frames[0])
        first = origin.frames[0]
        self.assertEqual(first["type"], "turn_status")
        self.assertEqual(first["event_type"], "turn_status")
        self.assertEqual(first["conversation_id"], "conv-broadcast")
        self.assertEqual(first["request_id"], "req-broadcast")
        self.assertEqual(first["client_request_id"], "req-broadcast")
        self.assertEqual(first["turn_id"], "turn-broadcast")
        self.assertFalse(first["terminal"])

        stopped_counts = (len(origin.frames), len(secondary.frames))
        self.assertTrue(task.done())
        await asyncio.sleep(0)
        self.assertEqual(
            (len(origin.frames), len(secondary.frames)),
            stopped_counts,
        )

    def test_accepted_user_message_persists_with_request_metadata(self):
        old_dir = server.conversations.CONVERSATIONS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            server.conversations.CONVERSATIONS_DIR = Path(tmp)
            try:
                conv = server.conversations.create_conversation()
                meta = server._new_turn_meta(conv["id"], "req-accepted")
                meta["client_content_hash"] = "accepted-content-hash"
                expected_turn_id = meta["turn_id"]
                expected_message_id = meta["user_message_id"]

                self.assertTrue(
                    server._persist_accepted_user_message(
                        conv["id"], "hello", meta,
                    )
                )
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
                self.assertEqual(messages[0]["_conversation_id"], conv["id"])
                self.assertEqual(messages[0]["_request_id"], "req-accepted")
                self.assertEqual(
                    messages[0]["_client_request_id"], "req-accepted",
                )
                self.assertEqual(messages[0]["_turn_id"], expected_turn_id)
                self.assertEqual(messages[0]["_message_id"], expected_message_id)
                self.assertEqual(
                    messages[0]["_client_content_hash"],
                    "accepted-content-hash",
                )

                found = server.conversations.find_correlated_turn(
                    conv["id"], "req-accepted",
                )
                self.assertIsNotNone(found)
                self.assertEqual(found["turn_id"], expected_turn_id)
                self.assertEqual(found["user_message_id"], expected_message_id)

                meta["persisted"] = True
                accepted = server._turn_accepted_frame(meta)
                self.assertEqual(accepted["conversation_id"], conv["id"])
                self.assertEqual(accepted["request_id"], "req-accepted")
                self.assertEqual(accepted["client_request_id"], "req-accepted")
                self.assertEqual(accepted["turn_id"], expected_turn_id)
                self.assertEqual(accepted["message_id"], expected_message_id)
                self.assertEqual(accepted["event_type"], "turn_accepted")
                self.assertEqual(accepted["sequence"], 1)
                self.assertFalse(accepted["terminal"])
                self.assertTrue(accepted["persisted"])
                self.assertEqual(server._turn_accepted_frame(meta), accepted)
            finally:
                server.conversations.CONVERSATIONS_DIR = old_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
