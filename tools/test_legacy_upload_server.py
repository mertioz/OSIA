#!/usr/bin/env python3
"""Authenticated server-boundary tests for WOR-136 legacy uploads."""

from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
import json
import io
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.server import server  # noqa: E402
from tools.mobile_test_state import isolated_mobile_server_state
from osia.document_artifacts import artifact_access_scope
from osia.document_sources import get_source_store


class _FakeWebSocket:
    def __init__(self, messages: list[dict], *, pause_after=None, pause=None):
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


def _file_frame(data: bytes, filename: str, **fields) -> dict:
    return {
        "type": "file",
        "filename": filename,
        "data": base64.b64encode(data).decode("ascii"),
        **fields,
    }


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    return output.getvalue()


class LegacyUploadServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="osia-legacy-contract-")))
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

    def _patches(self, root: Path):
        return (
            mock.patch.object(server, "MOBILE_TOKEN", "synthetic-token"),
            mock.patch.object(server, "UPLOAD_DIR", root / "legacy"),
            mock.patch.object(server, "ATTACHMENT_UPLOAD_DIR", root / "modern"),
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

    async def _run(self, ws: _FakeWebSocket, root: Path, run_agent) -> None:
        with ExitStack() as stack:
            for patcher in self._patches(root):
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(server, "_run_agent_and_respond", new=run_agent)
            )
            await server.websocket_endpoint(ws)
            detached = list(server._DETACHED_TURNS)
            if detached:
                await asyncio.gather(*detached, return_exceptions=True)

    async def test_authenticated_regular_file_turn_reads_then_cleans(self) -> None:
        consumed = []

        async def run_agent(_channel, agent_input, history, conv_id, **_kwargs):
            reference = re.search(r"source_ref=(src_[0-9a-f]{32})", agent_input).group(1)
            with artifact_access_scope(conv_id):
                consumed.append(get_source_store().resolve(reference)[0])
            return history, conv_id

        ws = _FakeWebSocket([
            {"type": "auth", "token": "synthetic-token"},
            _file_frame(b"regular synthetic", "../../regular.txt"),
            {
                "type": "text",
                "content": "inspect",
                "conversation_id": "",
                "request_id": "request_regular",
                "client_request_id": "request_regular",
            },
        ])
        with tempfile.TemporaryDirectory(prefix="osia-legacy-server-") as temp:
            root = Path(temp)
            await self._run(ws, root, run_agent)
            self.assertEqual(list((root / "legacy").glob("legacy-*")), [])
        self.assertTrue(ws.accepted)
        self.assertEqual(consumed, [b"regular synthetic"])
        received = [frame for frame in ws.sent if frame.get("type") == "file_received"]
        self.assertEqual(received[-1]["filename"], "regular.txt")
        self.assertTrue(received[-1]["success"])

    async def test_authenticated_image_turn_consumes_then_cleans(self) -> None:
        consumed = []
        image_bytes = _image_bytes()

        async def run_agent(_channel, agent_input, history, conv_id, **_kwargs):
            reference = re.search(r"source_ref=(src_[0-9a-f]{32})", agent_input).group(1)
            with artifact_access_scope(conv_id):
                consumed.append(get_source_store().resolve(reference)[0])
            return history, conv_id

        ws = _FakeWebSocket([
            {"type": "auth", "token": "synthetic-token"},
            _file_frame(
                image_bytes,
                "image.png",
                conversation_id="",
                request_id="request_image",
                client_request_id="request_image",
            ),
        ])
        with tempfile.TemporaryDirectory(prefix="osia-legacy-server-") as temp:
            root = Path(temp)
            await self._run(ws, root, run_agent)
            self.assertEqual(list((root / "legacy").glob("legacy-*")), [])
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0], image_bytes)

    async def test_provider_exception_still_cleans_owned_file(self) -> None:
        async def run_agent(*_args, **_kwargs):
            raise RuntimeError("synthetic provider failure")

        ws = _FakeWebSocket([
            {"type": "auth", "token": "synthetic-token"},
            _file_frame(
                _image_bytes(),
                "image.png",
                request_id="request_failure",
                client_request_id="request_failure",
            ),
        ])
        with tempfile.TemporaryDirectory(prefix="osia-legacy-server-") as temp:
            root = Path(temp)
            await self._run(ws, root, run_agent)
            self.assertEqual(list((root / "legacy").glob("legacy-*")), [])

    async def test_disconnect_cleans_unconsumed_pending_file(self) -> None:
        async def run_agent(*_args, **_kwargs):
            self.fail("provider must not run without a turn")

        ws = _FakeWebSocket([
            {"type": "auth", "token": "synthetic-token"},
            _file_frame(b"pending synthetic", "pending.txt"),
        ])
        with tempfile.TemporaryDirectory(prefix="osia-legacy-server-") as temp:
            root = Path(temp)
            await self._run(ws, root, run_agent)
            self.assertEqual(list((root / "legacy").glob("legacy-*")), [])

    async def test_invalid_base64_error_is_structured_and_path_free(self) -> None:
        async def run_agent(*_args, **_kwargs):
            self.fail("provider must not run for a rejected upload")

        ws = _FakeWebSocket([
            {"type": "auth", "token": "synthetic-token"},
            {
                "type": "file",
                "filename": "../../bad.txt",
                "data": "not base64!",
            },
        ])
        with tempfile.TemporaryDirectory(prefix="osia-legacy-server-") as temp:
            root = Path(temp)
            await self._run(ws, root, run_agent)
            self.assertFalse((root / "legacy").exists())
        received = [frame for frame in ws.sent if frame.get("type") == "file_received"]
        self.assertEqual(received[-1]["error"], {"code": "invalid_base64"})
        self.assertNotIn("path", received[-1])
        self.assertEqual(received[-1]["filename"], "bad.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
