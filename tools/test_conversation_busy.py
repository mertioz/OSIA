import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osia.server import server


class _FakeChannel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


class ConversationBusyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._WS_CLIENTS.clear()

    async def asyncTearDown(self):
        for task in list(server._ACTIVE_TURNS_BY_CONV.values()):
            task.cancel()
        server._ACTIVE_TURNS_BY_CONV.clear()
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

    async def test_busy_rejection_frame(self):
        client = _FakeChannel()

        await server._send_conversation_busy(client, "conv-a")

        self.assertEqual(client.frames[0]["type"], "conversation_busy")
        self.assertEqual(client.frames[0]["conversation_id"], "conv-a")
        self.assertIn("still working", client.frames[0]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
