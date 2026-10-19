#!/usr/bin/env python3
"""WOR-123 receive-loop transaction tests with synthetic application frames."""

from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.server import server  # noqa: E402
from tools.mobile_test_state import isolated_mobile_server_state


class _FakeWebSocket:
    def __init__(self, messages: list[dict], *, pause_after: int | None = None, pause=None):
        self.messages = messages
        self.pause_after = pause_after
        self.pause = pause
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, frame: dict) -> None:
        self.sent.append(dict(frame))

    async def close(self, **_kwargs) -> None:
        self.closed = True

    async def iter_text(self):
        for index, message in enumerate(self.messages):
            yield json.dumps(message)
            if index == self.pause_after and self.pause is not None:
                await asyncio.wait_for(self.pause.wait(), timeout=5)


async def _idle_heartbeat(_channel) -> None:
    await asyncio.Event().wait()


def _attachment_frames(data: bytes) -> list[dict]:
    now = datetime.now(timezone.utc)
    batch_id = "batch_public"
    request_id = "request_public"
    common = {
        "protocol_version": 1,
        "batch_id": batch_id,
        "conversation_id": "",
        "client_request_id": request_id,
        "request_id": request_id,
    }

    def event(event_type: str, sequence: int, **fields) -> dict:
        return {
            "type": event_type,
            **common,
            "event_id": f"event_public_{sequence:02d}",
            "sequence": sequence,
            **fields,
        }

    digest = hashlib.sha256(data).hexdigest()
    return [
        event(
            "attachment_batch_prepare",
            1,
            source_device_role="mobile",
            file_count=1,
            aggregate_size=len(data),
            created_at=now.isoformat(),
            expiry=(now + timedelta(minutes=15)).isoformat(),
            files=[{
                "file_id": "file_public",
                "logical_order": 0,
                "display_name": "public.txt",
                "original_extension": "txt",
                "declared_mime": "text/plain",
                "detected_type_category": "file",
                "declared_byte_size": len(data),
                "sha256": digest,
                "chunk_size": 20 * 1024,
                "expected_chunk_count": 1,
            }],
        ),
        event(
            "attachment_file_begin",
            2,
            file_id="file_public",
            logical_order=0,
        ),
        event(
            "attachment_file_chunk",
            3,
            file_id="file_public",
            chunk_index=0,
            expected_chunk_count=1,
            byte_offset=0,
            payload_length=len(data),
            data=base64.b64encode(data).decode("ascii"),
        ),
        event(
            "attachment_file_complete",
            4,
            file_id="file_public",
            declared_byte_size=len(data),
            sha256=digest,
        ),
        event(
            "attachment_batch_commit",
            5,
            file_count=1,
            aggregate_size=len(data),
        ),
    ]


class AttachmentServerTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="osia-attachment-contract-")))
        self.enterContext(isolated_mobile_server_state(root))
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._ACTIVE_TURN_META_BY_CONV.clear()
        server._ACTIVE_CONV_BY_REQUEST.clear()
        server._WS_CLIENTS.clear()

    async def asyncTearDown(self) -> None:
        for task in list(server._DETACHED_TURNS):
            task.cancel()
        if server._DETACHED_TURNS:
            await asyncio.gather(
                *list(server._DETACHED_TURNS), return_exceptions=True
            )
        server._DETACHED_TURNS.clear()
        server._DETACHED_BY_CONV.clear()
        server._ACTIVE_TURNS_BY_CONV.clear()
        server._ACTIVE_TURN_META_BY_CONV.clear()
        server._ACTIVE_CONV_BY_REQUEST.clear()
        server._WS_CLIENTS.clear()

    def _common_patches(self, temp_root: Path):
        return (
            mock.patch.object(server, "MOBILE_TOKEN", "public-token"),
            mock.patch.object(server, "ATTACHMENT_UPLOAD_DIR", temp_root),
            mock.patch.object(
                server.conversations, "list_conversations", return_value=[]
            ),
            mock.patch.object(
                server, "_pending_cron_notifications", return_value=[]
            ),
            mock.patch.object(
                server, "_active_conversation_busy_ids", return_value=[]
            ),
            mock.patch.object(server, "_heartbeat_push_loop", new=_idle_heartbeat),
        )

    async def test_missing_batch_creates_no_turn_conversation_or_provider_call(self) -> None:
        ws = _FakeWebSocket([
            {"type": "auth", "token": "public-token"},
            {
                "type": "text",
                "content": "public prompt",
                "conversation_id": "",
                "request_id": "request_public",
                "client_request_id": "request_public",
                "attachment_batch_id": "batch_missing",
            },
        ])
        with tempfile.TemporaryDirectory() as temp:
            patches = self._common_patches(Path(temp))
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                create_conversation = stack.enter_context(mock.patch.object(
                    server.conversations, "create_conversation"
                ))
                new_turn_meta = stack.enter_context(mock.patch.object(
                    server, "_new_turn_meta"
                ))
                run_agent = stack.enter_context(mock.patch.object(
                    server, "_run_agent_and_respond"
                ))
                await server.websocket_endpoint(ws)

        self.assertTrue(ws.accepted)
        create_conversation.assert_not_called()
        new_turn_meta.assert_not_called()
        run_agent.assert_not_called()
        failures = [
            frame for frame in ws.sent
            if frame.get("type") == "attachment_batch_failed"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0].get("category"),
            "attachment_batch_not_committed",
        )
        self.assertFalse(any(
            frame.get("type") in {"turn_accepted", "response", "response_chunk"}
            for frame in ws.sent
        ))

    async def test_committed_batch_and_duplicate_text_invoke_provider_once(self) -> None:
        data = b"public attachment marker"
        started = asyncio.Event()
        finish = asyncio.Event()
        provider_inputs = []

        async def run_agent(_channel, agent_input, history, conv_id, **_kwargs):
            provider_inputs.append(agent_input)
            ws.messages[-1]["conversation_id"] = conv_id
            started.set()
            await finish.wait()
            return history, conv_id

        frames = [
            {"type": "auth", "token": "public-token"},
            *_attachment_frames(data),
            {
                "type": "text",
                "content": "inspect the public marker",
                "conversation_id": "",
                "request_id": "request_public",
                "client_request_id": "request_public",
                "attachment_batch_id": "batch_public",
            },
            {
                "type": "text",
                "content": "inspect the public marker",
                "conversation_id": "conv_created",
                "request_id": "request_public",
                "client_request_id": "request_public",
                "attachment_batch_id": "batch_public",
            },
        ]
        # Pause the synthetic client after the first final text until the turn
        # task has claimed its request id. The next identical text is therefore
        # a real concurrent duplicate, not a timing-dependent test race.
        ws = _FakeWebSocket(
            frames,
            pause_after=len(frames) - 2,
            pause=started,
        )

        with tempfile.TemporaryDirectory() as temp:
            upload_root = Path(temp)
            patches = self._common_patches(upload_root)
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                create_conversation = stack.enter_context(mock.patch.object(
                    server.conversations,
                    "resolve_request_conversation",
                    wraps=server.conversations.resolve_request_conversation,
                ))
                stack.enter_context(mock.patch.object(
                    server, "_run_agent_and_respond", new=run_agent
                ))
                await server.websocket_endpoint(ws)
                finish.set()
                detached = list(server._DETACHED_TURNS)
                if detached:
                    await asyncio.gather(*detached)

            self.assertEqual(list(upload_root.glob("batch-*")), [])

        self.assertEqual(create_conversation.call_count, 1)
        self.assertEqual(len(provider_inputs), 1)
        self.assertIsInstance(provider_inputs[0], str)
        self.assertIn("source_ref=src_", provider_inputs[0])
        self.assertNotIn(str(upload_root), provider_inputs[0])
        accepted = [
            frame for frame in ws.sent if frame.get("type") == "turn_accepted"
        ]
        self.assertGreaterEqual(len(accepted), 1)
        self.assertTrue(all(
            frame.get("request_id") == "request_public" for frame in accepted
        ))
        self.assertFalse(any(
            frame.get("type") == "attachment_batch_failed" for frame in ws.sent
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
