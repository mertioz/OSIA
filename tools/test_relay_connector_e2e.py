#!/usr/bin/env python3
"""Relay connector E2E integration tests without opening sockets."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.relay_connector import (  # noqa: E402
    E2E_CHUNK_CIPHERTEXT_CHARS,
    E2E_CHUNK_TIMEOUT_SECONDS,
    MAX_E2E_CHUNK_COUNT,
    MAX_E2E_CHUNK_TOTAL_BYTES,
    MAX_LOCAL_SERVER_FRAME_BYTES,
    MAX_RELAY_FRAME_BYTES,
    MAX_SAFE_RELAY_MESSAGE_BYTES,
    RelayBridge,
    RelayConfigurationError,
    RelaySessionSuperseded,
    TYPE_E2E_HELLO,
    TYPE_E2E_READY,
    TYPE_E2E_CHUNK,
    TYPE_E2E_FRAME,
    build_relay_endpoint,
)
from osia.relay_crypto import (  # noqa: E402
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    RelaySessionContext,
    SequenceReplayGuard,
    decrypt_frame,
    derive_session_keys,
    encrypt_frame,
    handshake_confirmation_tag,
    relay_frame_is_final,
    relay_frame_priority,
    verify_handshake_confirmation,
    x25519_public_key,
)


class _FakeRelayWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _BlockingChunkRelayWs(_FakeRelayWs):
    def __init__(self) -> None:
        super().__init__()
        self.first_chunk_sent = asyncio.Event()
        self.release_first_chunk = asyncio.Event()
        self._blocked = False

    async def send(self, data: str) -> None:
        self.sent.append(data)
        decoded = json.loads(data)
        if (
            not self._blocked
            and decoded.get("type") == TYPE_E2E_CHUNK
            and decoded.get("chunk_index") == 0
        ):
            self._blocked = True
            self.first_chunk_sent.set()
            await self.release_first_chunk.wait()


class _FakeLocalWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


_STREAM_END = object()


class _StreamingLocalWs(_FakeLocalWs):
    def __init__(self) -> None:
        super().__init__()
        self.incoming: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is _STREAM_END:
            raise StopAsyncIteration
        return item


class RelayConnectorE2ETests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(
            os.environ,
            {"OSIA_LAN_DIRECT_V1": "0", "OSIA_WEBRTC_DIRECT_V1": "0"},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.token = "osia-e2e-test-token-v2"
        self.bridge = RelayBridge(self.token, "wss://relay.test/ws")
        self.bridge.e2e_private_key = bytes.fromhex(
            "000102030405060708090a0b0c0d0e0f"
            "101112131415161718191a1b1c1d1e1f"
        )
        self.bridge.e2e_public_key = x25519_public_key(self.bridge.e2e_private_key)
        self.bridge.e2e_nonce_base = bytes.fromhex("a0a1a2a3")
        self.mobile_private_key = bytes.fromhex(
            "202122232425262728292a2b2c2d2e2f"
            "303132333435363738393a3b3c3d3e3f"
        )
        self.mobile_public_key = x25519_public_key(self.mobile_private_key)
        self.mobile_nonce_base = bytes.fromhex("b0b1b2b3")
        self._enable_relay_chunking()

        async def connect_local(_relay_ws) -> None:
            if self.bridge._local_ws is None:
                self.bridge._local_ws = _FakeLocalWs()

        self.bridge._connect_local = connect_local

    def test_v2_url_is_exact_pair_only_and_rejects_metadata_bases(self):
        expected = f"wss://relay.test/ws?pair_id={self.bridge.pair_id}"
        self.assertEqual(self.bridge._relay_endpoint(), expected)
        self.assertEqual(
            build_relay_endpoint("wss://relay.test", self.bridge.pair_id),
            expected,
        )
        for base in (
            "https://relay.test",
            "wss://user:pass@relay.test",
            "wss://relay.test/ws?role=pc",
            "wss://relay.test/ws#fragment",
            "wss://relay.test/other",
        ):
            with self.subTest(base=base), self.assertRaises(RelayConfigurationError):
                build_relay_endpoint(base, self.bridge.pair_id)

    def test_pair_start_is_exact_first_v2_envelope(self):
        pair_start = self.bridge._envelope("PAIR_START", {"secret": self.bridge.secret})
        self.assertEqual(
            pair_start,
            {
                "v": 2,
                "type": "PAIR_START",
                "role": "pc",
                "pair_id": self.bridge.pair_id,
                "seq": 1,
                "ack": 0,
                "payload": {"secret": self.bridge.secret},
            },
        )

    async def test_pair_confirm_requires_v2_role_authority_capability(self):
        relay_ws = _FakeRelayWs()
        await self.bridge._handle_relay_frame(
            relay_ws,
            {
                "v": 2,
                "type": "PAIR_CONFIRM",
                "role": "pc",
                "pair_id": self.bridge.pair_id,
                "payload": {},
            },
        )
        self.assertTrue(relay_ws.closed)
        self.assertEqual(self.bridge._terminal_error_code, "update_required")

    async def test_terminal_worker_errors_fail_closed(self):
        for code in (
            "update_required",
            "session_superseded",
            "pair_proof_mismatch",
            "invalid_pair_route",
        ):
            with self.subTest(code=code):
                relay_ws = _FakeRelayWs()
                self.bridge._terminal_error_code = ""
                await self.bridge._handle_relay_frame(
                    relay_ws,
                    {"v": 2, "type": "ERROR", "payload": {"code": code}},
                )
                self.assertTrue(relay_ws.closed)
                self.assertEqual(self.bridge._terminal_error_code, code)

    async def test_resume_not_supported_closes_without_sequence_rollback(self):
        relay_ws = _FakeRelayWs()
        self.bridge.seq = 9
        await self.bridge._handle_relay_frame(
            relay_ws,
            {
                "v": 2,
                "type": "ERROR",
                "payload": {"code": "resume_not_supported"},
            },
        )
        self.assertTrue(relay_ws.closed)
        self.assertEqual(self.bridge.seq, 9)

    async def test_session_superseded_retry_is_hard_bounded(self):
        self.bridge.run_once = mock.AsyncMock(
            side_effect=RelaySessionSuperseded("replaced")
        )
        with mock.patch("osia.relay_connector.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaisesRegex(RelayConfigurationError, "session_superseded"):
                await self.bridge.run_forever()
        self.assertEqual(self.bridge.run_once.await_count, 4)

    async def test_e2e_handshake_encrypts_and_decrypts_frames(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)

        self.assertTrue(self.bridge.e2e_peer_ready)
        self.assertGreaterEqual(len(relay_ws.sent), 2)
        self.assertEqual(
            [json.loads(raw)["type"] for raw in relay_ws.sent[:2]],
            ["E2E_HELLO", "E2E_READY"],
        )

        frame = {
            "type": "response_chunk",
            "client_request_id": "req-test",
            "turn_id": "turn-test",
            "conversation_id": "conv-test",
            "content": "hello encrypted osia",
        }
        await self.bridge._send_e2e_frame(relay_ws, frame)
        encrypted_msg = json.loads(relay_ws.sent[-1])

        self.assertEqual(encrypted_msg["type"], TYPE_E2E_FRAME)
        self.assertNotIn("payload", encrypted_msg)
        for field in ("device_id", "session_id", "from", "to", "to_session_id"):
            self.assertNotIn(field, encrypted_msg)
        self.assertEqual(encrypted_msg["priority"], "normal")
        self.assertFalse(encrypted_msg["final"])
        self.assertNotIn("hello encrypted osia", json.dumps(encrypted_msg))

        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        decrypted = decrypt_frame(
            encrypted_msg,
            keys=mobile_keys,
            direction="pc_to_mobile",
        )
        self.assertEqual(decrypted, frame)

        incoming = encrypt_frame(
            {"type": "text", "content": "from mobile"},
            keys=mobile_keys,
            direction=MOBILE_TO_PC,
            pair_id=self.bridge.pair_id,
            role="mobile",
            priority="normal",
            final=False,
            seq=9,
        )
        self.bridge._local_ws = _FakeLocalWs()
        await self.bridge._handle_e2e_frame(relay_ws, {
            "v": 2,
            "type": TYPE_E2E_FRAME,
            "role": "mobile",
            "pair_id": self.bridge.pair_id,
            "seq": 9,
            "ack": 0,
            **incoming,
        })
        self.assertEqual(
            json.loads(self.bridge._local_ws.sent[-1]),
            {"type": "text", "content": "from mobile"},
        )

    async def test_wrong_key_and_replay_fail_safely(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        incoming = encrypt_frame(
            {"type": "text", "content": "once"},
            keys=mobile_keys,
            direction=MOBILE_TO_PC,
            pair_id=self.bridge.pair_id,
            role="mobile",
            priority="normal",
            final=False,
            seq=10,
        )
        self.bridge._local_ws = _FakeLocalWs()
        msg = {
            "v": 2,
            "type": TYPE_E2E_FRAME,
            "role": "mobile",
            "pair_id": self.bridge.pair_id,
            "seq": 10,
            "ack": 0,
            **incoming,
        }
        await self.bridge._handle_e2e_frame(relay_ws, msg)
        await self.bridge._handle_e2e_frame(relay_ws, msg)
        self.assertEqual(len(self.bridge._local_ws.sent), 1)
        self.assertFalse(relay_ws.closed)

        wrong_bridge = RelayBridge("wrong-token", "wss://relay.test/ws")
        wrong_bridge.pair_id = self.bridge.pair_id
        wrong_bridge.e2e_keys = self.bridge.e2e_keys
        wrong_bridge.e2e_keys = derive_session_keys(
            token="wrong-token",
            local_private_key=self.bridge.e2e_private_key,
            peer_public_key=self.mobile_public_key,
            context=self.bridge.e2e_context,
        )
        wrong_bridge.e2e_replay_guard = SequenceReplayGuard()
        wrong_bridge.e2e_peer_ready = True
        wrong_bridge._local_ws = _FakeLocalWs()
        await wrong_bridge._handle_e2e_frame(relay_ws, msg)
        self.assertEqual(wrong_bridge._local_ws.sent, [])
        self.assertTrue(relay_ws.closed)

    async def test_plaintext_rejected_without_leaking_payload(self):
        relay_ws = _FakeRelayWs()
        self.bridge._local_ws = _FakeLocalWs()

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            await self.bridge._handle_relay_frame(relay_ws, {
                "type": "FRAME",
                "payload": {
                    "frame": {
                        "type": "response_chunk",
                        "content": "plaintext should not log",
                    },
                },
            })
            await self.bridge._handle_relay_frame(relay_ws, {
                "type": "ERROR",
                "payload": {"code": "e2e_required", "message": "private"},
            })

        output = captured.getvalue()
        self.assertEqual(self.bridge._local_ws.sent, [])
        self.assertIn("E2E is required", output)
        self.assertIn("fail-closed relay error: e2e_required", output)
        self.assertNotIn("plaintext should not log", output)
        self.assertNotIn("private", output)

    async def test_plaintext_rejected_after_e2e_even_when_dev_flag_allows(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        self.bridge.allow_plaintext = True
        self.bridge._local_ws = _FakeLocalWs()

        await self.bridge._handle_relay_frame(relay_ws, {
            "type": "FRAME",
            "payload": {"frame": {"type": "text", "content": "downgrade"}},
        })

        self.assertEqual(self.bridge._local_ws.sent, [])

    async def test_public_relay_plaintext_cannot_be_enabled_by_opt_in(self):
        with mock.patch.dict(
            os.environ,
            {
                "OSIA_RELAY_ALLOW_PLAINTEXT": "1",
                "OSIA_RELAY_MODE": "private",
            },
        ):
            bridge = RelayBridge("test-token", "wss://relay.osia.space")

        self.assertFalse(bridge.allow_plaintext)

    async def test_private_relay_plaintext_requires_mode_and_opt_in(self):
        with mock.patch.dict(
            os.environ,
            {
                "OSIA_RELAY_ALLOW_PLAINTEXT": "1",
                "OSIA_RELAY_MODE": "private",
            },
        ):
            bridge = RelayBridge("test-token", "wss://relay.internal.example")

        self.assertTrue(bridge.allow_plaintext)

    async def test_duplicate_mobile_ready_does_not_start_another_handshake(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        before = len(relay_ws.sent)

        for seq in (11, 12):
            await self.bridge._handle_relay_frame(
                relay_ws,
                {
                    **self._hello_message(),
                    "type": "MOBILE_READY",
                    "seq": seq,
                    "payload": {},
                },
            )

        self.assertEqual(relay_ws.sent[before:], [])

    async def test_verified_e2e_ready_does_not_depend_on_mobile_ready_hint(self):
        relay_ws = _FakeRelayWs()
        local_ws = _FakeLocalWs()

        async def connect_local(_relay_ws) -> None:
            self.bridge._local_ws = local_ws

        self.bridge._connect_local = connect_local
        hello = self._hello_message()
        await self.bridge._handle_e2e_hello(
            relay_ws,
            hello,
            self._hello_payload(),
        )
        ready_tag = handshake_confirmation_tag(
            token=self.token,
            context=self.bridge.e2e_context,
            sender_role="mobile",
        )

        ready = self._ready_payload(self.bridge.e2e_keys.kid, ready_tag)
        await self.bridge._handle_e2e_ready(
            relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
        )

        self.assertFalse(self.bridge.mobile_ready)
        self.assertIs(self.bridge._local_ws, local_ws)
        self.assertEqual(json.loads(relay_ws.sent[-1])["type"], "PC_READY")

    async def test_app_frame_before_e2e_ready_is_not_sent(self):
        relay_ws = _FakeRelayWs()

        sent = await self.bridge._send_e2e_frame(
            relay_ws,
            {"type": "text", "content": "must stay local"},
        )

        self.assertFalse(sent)
        self.assertEqual(relay_ws.sent, [])

    async def test_ready_before_hello_and_duplicate_hello_are_idempotent(self):
        relay_ws = _FakeRelayWs()
        hello = self._hello_message()
        expected_context = RelaySessionContext(
            pair_id=self.bridge.pair_id,
            pc_public_key=self.bridge.e2e_public_key,
            mobile_public_key=self.mobile_public_key,
            pc_nonce_base=self.bridge.e2e_nonce_base,
            mobile_nonce_base=self.mobile_nonce_base,
        )
        expected_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=expected_context,
        )
        ready = self._ready_payload(
            expected_keys.kid,
            handshake_confirmation_tag(
                token=self.token,
                context=expected_context,
                sender_role="mobile",
            ),
        )

        await self.bridge._handle_e2e_ready(
            relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
        )
        self.assertIsNotNone(self.bridge.pending_e2e_ready)
        await self.bridge._handle_e2e_hello(
            relay_ws,
            hello,
            self._hello_payload(),
        )
        self.assertTrue(self.bridge.e2e_peer_ready)

        before = len(relay_ws.sent)
        await self.bridge._handle_e2e_hello(
            relay_ws,
            hello,
            self._hello_payload(),
        )
        self.assertEqual(relay_ws.sent[before:], [])

        await self.bridge._handle_e2e_ready(
            relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
        )
        self.assertEqual(relay_ws.sent[before:], [])
        self.assertEqual(
            [json.loads(raw)["type"] for raw in relay_ws.sent].count("PC_READY"),
            1,
        )

    async def test_new_mobile_hello_replaces_stale_crypto_state(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        self.bridge.mobile_ready = True
        old_local = _FakeLocalWs()
        self.bridge._local_ws = old_local
        old_replay_guard = self.bridge.e2e_replay_guard
        self.bridge._chunk_transfers[("old",)] = object()
        self.bridge._chunk_tombstones[("old",)] = object()
        self.bridge._outbound_chunk_ids["old"] = 1.0
        self.bridge.webrtc_capabilities_sent = True
        self.bridge._webrtc_manager._latest_generation = 3

        new_private_key = bytes(range(64, 96))
        new_public_key = x25519_public_key(new_private_key)
        new_nonce_base = bytes.fromhex("c0c1c2c3")
        new_payload = {
            "e2e_version": 1,
            "alg": "CHACHA20-POLY1305",
            "public_key": self._b64(new_public_key),
            "nonce_base": self._b64(new_nonce_base),
            "e2e_chunk_version": 1,
        }
        new_hello = self._control_message(TYPE_E2E_HELLO, new_payload)

        before = len(relay_ws.sent)
        await self.bridge._handle_e2e_hello(
            relay_ws,
            new_hello,
            new_payload,
        )
        sent_types = [json.loads(raw)["type"] for raw in relay_ws.sent[before:]]
        self.assertEqual(sent_types, ["E2E_HELLO", "E2E_READY"])
        self.assertTrue(old_local.closed)
        self.assertEqual(self.bridge.e2e_context.mobile_public_key, new_public_key)
        self.assertIsNot(self.bridge.e2e_replay_guard, old_replay_guard)
        self.assertEqual(self.bridge._chunk_transfers, {})
        self.assertEqual(self.bridge._chunk_tombstones, {})
        self.assertEqual(self.bridge._outbound_chunk_ids, {})
        self.assertFalse(self.bridge.webrtc_capabilities_sent)
        self.assertEqual(self.bridge._webrtc_manager._latest_generation, 0)
        self.assertFalse(self.bridge.e2e_peer_ready)
        self.assertFalse(self.bridge.e2e_pc_ready_sent)

        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=new_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        self.bridge._local_ws = _FakeLocalWs()
        ready = self._ready_payload(
            mobile_keys.kid,
            handshake_confirmation_tag(
                token=self.token,
                context=self.bridge.e2e_context,
                sender_role="mobile",
            ),
        )
        await self.bridge._handle_e2e_ready(
            relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
        )
        self.assertTrue(self.bridge.e2e_peer_ready)
        self.assertEqual(json.loads(relay_ws.sent[-1])["type"], "PC_READY")
        after_ready = len(relay_ws.sent)
        await self.bridge._handle_e2e_ready(
            relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
        )
        self.assertEqual(len(relay_ws.sent), after_ready)

    async def test_settled_handshake_is_application_silent_for_simulated_minute(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        hello_payload = self._hello_payload()
        ready = self._ready_payload(
            self.bridge.e2e_keys.kid,
            handshake_confirmation_tag(
                token=self.token,
                context=self.bridge.e2e_context,
                sender_role="mobile",
            ),
        )
        before = len(relay_ws.sent)

        for second in range(60):
            await self.bridge._handle_e2e_hello(
                relay_ws,
                {**self._hello_message(), "seq": 100 + second},
                hello_payload,
            )
            await self.bridge._handle_e2e_ready(
                relay_ws,
                {
                    **self._control_message(TYPE_E2E_READY, ready),
                    "seq": 200 + second,
                },
                ready,
            )

        self.assertEqual(relay_ws.sent[before:], [])

    async def test_pong_never_generates_ping(self):
        relay_ws = _FakeRelayWs()

        await self.bridge._handle_relay_frame(
            relay_ws,
            self._control_message("PONG", {"t": 1}),
        )

        self.assertEqual(relay_ws.sent, [])

    async def test_authoritative_disconnect_resets_current_peer(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        local_ws = _FakeLocalWs()
        self.bridge._local_ws = local_ws

        await self.bridge._handle_relay_frame(
            relay_ws,
            {
                "v": 2,
                "type": "DISCONNECT",
                "role": "mobile",
                "pair_id": self.bridge.pair_id,
                "payload": {},
            },
        )

        self.assertTrue(local_ws.closed)
        self.assertIsNone(self.bridge.e2e_context)

    async def test_close_local_closes_socket_even_when_reader_clears_state(self):
        local_ws = _FakeLocalWs()
        self.bridge._local_ws = local_ws

        async def reader() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                self.bridge._local_ws = None

        self.bridge._local_reader_task = asyncio.create_task(reader())
        await asyncio.sleep(0)

        await self.bridge._close_local()

        self.assertTrue(local_ws.closed)
        self.assertIsNone(self.bridge._local_ws)
        self.assertIsNone(self.bridge._local_reader_task)

    async def test_intentional_local_close_uses_bounded_ceiling_and_keeps_relay_open(self):
        relay_ws = _FakeRelayWs()
        local_ws = _StreamingLocalWs()
        connect_kwargs = await self._connect_streaming_local(relay_ws, local_ws)

        self.assertEqual(
            connect_kwargs["max_size"],
            MAX_LOCAL_SERVER_FRAME_BYTES,
        )
        self.assertGreater(MAX_LOCAL_SERVER_FRAME_BYTES, MAX_RELAY_FRAME_BYTES)
        await self.bridge._close_local()

        self.assertTrue(local_ws.closed)
        self.assertFalse(relay_ws.closed)
        self.assertIsNone(self.bridge._local_ws)

    async def test_unexpected_local_reader_end_closes_relay(self):
        relay_ws = _FakeRelayWs()
        local_ws = _StreamingLocalWs()
        await self._connect_streaming_local(relay_ws, local_ws)

        await local_ws.incoming.put(_STREAM_END)
        for _ in range(20):
            if relay_ws.closed:
                break
            await asyncio.sleep(0)

        self.assertTrue(relay_ws.closed)
        self.assertIsNone(self.bridge._local_ws)
        await self.bridge._close_local()

    async def test_two_encrypted_turns_survive_oversized_post_final_audio(self):
        relay_ws = _FakeRelayWs()
        local_ws = _StreamingLocalWs()
        connect_kwargs = await self._connect_streaming_local(relay_ws, local_ws)
        self.assertEqual(
            connect_kwargs["max_size"],
            MAX_LOCAL_SERVER_FRAME_BYTES,
        )
        await self._establish_bridge(relay_ws)

        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        conversation_id = "conv-two-turn"
        request_1 = {
            "type": "text",
            "content": "turn one",
            "conversation_id": conversation_id,
            "client_request_id": "req-one",
        }
        await self.bridge._handle_relay_frame(
            relay_ws,
            self._mobile_e2e_frame(request_1, mobile_keys, seq=10),
        )

        turn_1_frames = [
            {
                "type": "response",
                "content": "answer one",
                "conversation_id": conversation_id,
                "client_request_id": "req-one",
                "turn_id": "turn-one",
            },
            {
                "type": "status",
                "content": "done",
                "conversation_id": conversation_id,
                "client_request_id": "req-one",
                "turn_id": "turn-one",
            },
        ]
        for frame in turn_1_frames:
            await local_ws.incoming.put(json.dumps(frame))
        encrypted_frames = await self._wait_for_e2e_frames(relay_ws, 2)

        replay_guard = SequenceReplayGuard()
        decrypted = [
            decrypt_frame(
                frame,
                keys=mobile_keys,
                direction=PC_TO_MOBILE,
                replay_guard=replay_guard,
                replay_key="pc",
            )
            for frame in encrypted_frames
        ]
        self.assertEqual(decrypted, turn_1_frames)
        self.assertEqual([frame["final"] for frame in encrypted_frames], [False, True])

        await local_ws.incoming.put(json.dumps({
            "type": "audio",
            "data": "x" * (MAX_RELAY_FRAME_BYTES + 1),
            "source": "response",
            "conversation_id": conversation_id,
        }))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(self._e2e_frames(relay_ws)), 2)
        audio_chunks = self._e2e_chunks(relay_ws)
        self.assertGreaterEqual(len(audio_chunks), 2)
        decrypted_audio = decrypt_frame(
            self._reassemble_chunk_envelope(audio_chunks),
            keys=mobile_keys,
            direction=PC_TO_MOBILE,
            replay_guard=replay_guard,
            replay_key="pc",
        )
        self.assertEqual(decrypted_audio["type"], "audio")
        self.assertEqual(decrypted_audio["data"], "x" * (MAX_RELAY_FRAME_BYTES + 1))
        self.assertIs(self.bridge._local_ws, local_ws)
        self.assertFalse(relay_ws.closed)

        request_2 = {
            "type": "text",
            "content": "turn two",
            "conversation_id": conversation_id,
            "client_request_id": "req-two",
        }
        await self.bridge._handle_relay_frame(
            relay_ws,
            self._mobile_e2e_frame(request_2, mobile_keys, seq=11),
        )
        local_sends = [json.loads(raw) for raw in local_ws.sent]
        self.assertEqual(
            [frame["type"] for frame in local_sends],
            ["auth", "text", "text"],
        )
        self.assertEqual(local_sends.count(request_1), 1)
        self.assertEqual(local_sends.count(request_2), 1)

        turn_2_frames = [
            {
                "type": "response",
                "content": "answer two",
                "conversation_id": conversation_id,
                "client_request_id": "req-two",
                "turn_id": "turn-two",
            },
            {
                "type": "status",
                "content": "done",
                "conversation_id": conversation_id,
                "client_request_id": "req-two",
                "turn_id": "turn-two",
            },
        ]
        for frame in turn_2_frames:
            await local_ws.incoming.put(json.dumps(frame))
        encrypted_frames = await self._wait_for_e2e_frames(relay_ws, 4)
        decrypted.extend(
            decrypt_frame(
                frame,
                keys=mobile_keys,
                direction=PC_TO_MOBILE,
                replay_guard=replay_guard,
                replay_key="pc",
            )
            for frame in encrypted_frames[2:]
        )

        self.assertEqual(decrypted, turn_1_frames + turn_2_frames)
        self.assertEqual(
            [frame["final"] for frame in encrypted_frames],
            [False, True, False, True],
        )
        await self.bridge._close_local()
        self.assertFalse(relay_ws.closed)

    async def test_small_frame_stays_single_and_large_frame_chunks_exactly(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )

        small_frame = {"type": "response_chunk", "content": "s" * 34000}
        self.assertTrue(await self.bridge._send_e2e_frame(relay_ws, small_frame))
        small_raw = next(
            raw
            for raw in reversed(relay_ws.sent)
            if json.loads(raw).get("type") == TYPE_E2E_FRAME
        )
        small_envelope = json.loads(small_raw)
        self.assertLessEqual(len(small_raw.encode("utf-8")), MAX_SAFE_RELAY_MESSAGE_BYTES)
        self.assertGreater(len(small_raw.encode("utf-8")), 40 * 1024)
        self.assertNotIn("to_session_id", small_envelope)
        self.assertEqual(
            decrypt_frame(small_envelope, keys=mobile_keys, direction=PC_TO_MOBILE),
            small_frame,
        )

        before_single_count = len(self._e2e_frames(relay_ws))
        large_frame = {"type": "response", "content": "L" * 40000}
        self.assertTrue(await self.bridge._send_e2e_frame(relay_ws, large_frame))
        self.assertEqual(len(self._e2e_frames(relay_ws)), before_single_count)
        chunks = self._e2e_chunks(relay_ws)
        self.assertEqual(len(chunks), 2)
        allowed_fields = {
            "v", "type", "role", "pair_id", "seq", "ack",
            "e2e_version", "alg", "kid", "frame_seq", "priority", "final", "nonce",
            "chunk_id", "chunk_index", "chunk_count", "total_bytes",
            "ciphertext_chunk",
        }
        first = chunks[0]
        self.assertEqual(set(first), allowed_fields)
        self.assertEqual(
            [chunk["chunk_index"] for chunk in chunks],
            list(range(len(chunks))),
        )
        self.assertEqual(
            [chunk["seq"] for chunk in chunks],
            [first["frame_seq"] + index for index in range(len(chunks))],
        )
        self.assertTrue(all(
            len(raw.encode("utf-8")) <= MAX_SAFE_RELAY_MESSAGE_BYTES
            for raw in relay_ws.sent
            if json.loads(raw).get("type") == TYPE_E2E_CHUNK
        ))
        self.assertTrue(all(
            len(chunk["ciphertext_chunk"]) == E2E_CHUNK_CIPHERTEXT_CHARS
            for chunk in chunks[:-1]
        ))
        self.assertEqual(
            sum(len(chunk["ciphertext_chunk"]) for chunk in chunks),
            first["total_bytes"],
        )
        self.assertEqual(
            decrypt_frame(
                self._reassemble_chunk_envelope(chunks),
                keys=mobile_keys,
                direction=PC_TO_MOBILE,
            ),
            large_frame,
        )

    async def test_out_of_order_duplicate_large_request_then_messages_two_and_three(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        local_ws = _FakeLocalWs()
        self.bridge._local_ws = local_ws
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        request_1 = {
            "type": "text",
            "content": "large request " + "x" * 50000,
            "conversation_id": "conv-large",
            "client_request_id": "request-one",
        }
        chunks = self._mobile_e2e_chunks(request_1, mobile_keys, seq=10)
        self.assertEqual(len(chunks), 2)

        await self.bridge._handle_relay_frame(relay_ws, chunks[1])
        await self.bridge._handle_relay_frame(relay_ws, chunks[1])
        self.assertEqual(local_ws.sent, [])
        await self.bridge._handle_relay_frame(relay_ws, chunks[0])
        for chunk in chunks:
            await self.bridge._handle_relay_frame(relay_ws, chunk)

        next_seq = 10 + len(chunks)
        request_2 = {
            "type": "text",
            "content": "message two",
            "conversation_id": "conv-large",
            "client_request_id": "request-two",
        }
        request_3 = {
            "type": "text",
            "content": "message three",
            "conversation_id": "conv-large",
            "client_request_id": "request-three",
        }
        await self.bridge._handle_relay_frame(
            relay_ws,
            self._mobile_e2e_frame(request_2, mobile_keys, seq=next_seq),
        )
        await self.bridge._handle_relay_frame(
            relay_ws,
            self._mobile_e2e_frame(request_3, mobile_keys, seq=next_seq + 1),
        )
        delivered = [json.loads(raw) for raw in local_ws.sent]
        self.assertEqual(delivered, [request_1, request_2, request_3])
        self.assertFalse(relay_ws.closed)

    async def test_chunk_series_sequence_and_send_are_atomic(self):
        relay_ws = _BlockingChunkRelayWs()
        await self._establish_bridge(relay_ws)
        baseline = len(relay_ws.sent)
        chunk_task = asyncio.create_task(
            self.bridge._send_e2e_frame(
                relay_ws,
                {"type": "response", "content": "x" * 40000},
            )
        )
        await relay_ws.first_chunk_sent.wait()
        pong_task = asyncio.create_task(
            self.bridge._send_relay(relay_ws, "PONG", {"t": 1})
        )
        await asyncio.sleep(0)
        relay_ws.release_first_chunk.set()
        self.assertTrue(await chunk_task)
        await pong_task

        sent = [json.loads(raw) for raw in relay_ws.sent[baseline:]]
        self.assertEqual(
            [message["type"] for message in sent],
            [TYPE_E2E_CHUNK, TYPE_E2E_CHUNK, "PONG"],
        )
        self.assertEqual(
            [message["seq"] for message in sent],
            list(range(sent[0]["seq"], sent[0]["seq"] + len(sent))),
        )

    async def test_conflicting_duplicate_sends_encrypted_error_and_closes(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        self.bridge._local_ws = _FakeLocalWs()
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        chunks = self._mobile_e2e_chunks(
            {"type": "text", "content": "x" * 50000},
            mobile_keys,
            seq=20,
        )
        await self.bridge._handle_relay_frame(relay_ws, chunks[0])
        conflict = dict(chunks[0])
        first_char = conflict["ciphertext_chunk"][0]
        replacement = "A" if first_char != "A" else "B"
        conflict["ciphertext_chunk"] = (
            replacement + conflict["ciphertext_chunk"][1:]
        )
        await self.bridge._handle_relay_frame(relay_ws, conflict)

        self.assertTrue(relay_ws.closed)
        self.assertEqual(self.bridge._local_ws.sent, [])
        error_envelope = self._e2e_frames(relay_ws)[-1]
        error_frame = decrypt_frame(
            error_envelope,
            keys=mobile_keys,
            direction=PC_TO_MOBILE,
        )
        self.assertEqual(error_frame["type"], "relay_chunk_error")
        self.assertEqual(error_frame["code"], "chunk_conflict")
        self.assertEqual(error_frame["chunk_id"], chunks[0]["chunk_id"])

    async def test_missing_chunk_expires_with_encrypted_error_and_session_survives(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        local_ws = _FakeLocalWs()
        self.bridge._local_ws = local_ws
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        chunks = self._mobile_e2e_chunks(
            {"type": "text", "content": "x" * 50000},
            mobile_keys,
            seq=30,
        )
        await self.bridge._handle_relay_frame(relay_ws, chunks[0])
        transfer = next(iter(self.bridge._chunk_transfers.values()))
        expired = self.bridge._expire_chunk_transfers(
            transfer.created_at + E2E_CHUNK_TIMEOUT_SECONDS,
        )
        self.assertEqual(expired, [chunks[0]["chunk_id"]])
        for chunk_id in expired:
            await self.bridge._send_chunk_error(
                relay_ws,
                chunk_id,
                "chunk_timeout",
            )
        self.assertFalse(relay_ws.closed)
        timeout_frame = decrypt_frame(
            self._e2e_frames(relay_ws)[-1],
            keys=mobile_keys,
            direction=PC_TO_MOBILE,
        )
        self.assertEqual(timeout_frame["code"], "chunk_timeout")

        for chunk in chunks:
            await self.bridge._handle_relay_frame(relay_ws, chunk)
        follow_up = {"type": "text", "content": "after timeout"}
        await self.bridge._handle_relay_frame(
            relay_ws,
            self._mobile_e2e_frame(
                follow_up,
                mobile_keys,
                seq=30 + len(chunks),
            ),
        )
        self.assertEqual([json.loads(raw) for raw in local_ws.sent], [follow_up])
        self.assertFalse(relay_ws.closed)

    async def test_unsupported_chunk_reports_encrypted_error_without_plaintext(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        local_ws = self.bridge._local_ws
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        self.bridge.e2e_peer_chunk_version = 0
        sent = await self.bridge._send_e2e_frame(
            relay_ws,
            {"type": "response", "content": "private" * 7000},
        )
        self.assertFalse(sent)
        self.assertEqual(self._e2e_chunks(relay_ws), [])
        error_envelope = self._e2e_frames(relay_ws)[-1]
        serialized = json.dumps(error_envelope)
        self.assertNotIn("private", serialized)
        error_frame = decrypt_frame(
            error_envelope,
            keys=mobile_keys,
            direction=PC_TO_MOBILE,
        )
        self.assertEqual(error_frame["code"], "unsupported_chunk")
        self.assertIs(self.bridge._local_ws, local_ws)
        self.assertFalse(relay_ws.closed)

    async def test_over_total_cap_reports_error_and_preserves_bridge(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        local_ws = self.bridge._local_ws
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        real_encrypt = encrypt_frame

        def oversized_encrypt(frame, **kwargs):
            encrypted = real_encrypt(frame, **kwargs)
            if frame.get("type") != "relay_chunk_error":
                encrypted["ciphertext"] = "A" * (
                    MAX_E2E_CHUNK_TOTAL_BYTES + 1
                )
            return encrypted

        with mock.patch(
            "osia.relay_connector.encrypt_frame",
            side_effect=oversized_encrypt,
        ):
            sent = await self.bridge._send_e2e_frame(
                relay_ws,
                {"type": "audio", "data": "private audio"},
            )
        self.assertFalse(sent)
        self.assertEqual(self._e2e_chunks(relay_ws), [])
        error_frame = decrypt_frame(
            self._e2e_frames(relay_ws)[-1],
            keys=mobile_keys,
            direction=PC_TO_MOBILE,
        )
        self.assertEqual(error_frame["code"], "frame_too_large")
        self.assertIs(self.bridge._local_ws, local_ws)
        self.assertFalse(relay_ws.closed)

    def test_relay_chunk_capability_rejects_any_lower_advertised_limit(self):
        baseline = {
            "e2e_chunk_version": 1,
            "e2e_chunk_max_frame_bytes": MAX_SAFE_RELAY_MESSAGE_BYTES,
            "e2e_chunk_max_ciphertext_chars": E2E_CHUNK_CIPHERTEXT_CHARS,
            "e2e_chunk_max_total_bytes": MAX_E2E_CHUNK_TOTAL_BYTES,
            "e2e_chunk_max_count": MAX_E2E_CHUNK_COUNT,
        }
        self.bridge.e2e_peer_chunk_version = 1
        self.bridge._set_relay_chunk_capabilities(baseline)
        self.assertTrue(self.bridge._chunking_available())
        for field in (
            "e2e_chunk_max_frame_bytes",
            "e2e_chunk_max_ciphertext_chars",
            "e2e_chunk_max_total_bytes",
            "e2e_chunk_max_count",
        ):
            lowered = dict(baseline)
            lowered[field] -= 1
            self.bridge._set_relay_chunk_capabilities(lowered)
            with self.subTest(field=field):
                self.assertFalse(self.bridge._chunking_available())

    def test_recent_outbound_chunk_tracking_is_hard_bounded(self):
        for value in range(100):
            self.bridge._remember_outbound_chunk(
                self._b64(value.to_bytes(16, "big"))
            )
        self.assertLessEqual(len(self.bridge._outbound_chunk_ids), 64)
        self.assertLessEqual(len(self.bridge._outbound_chunk_order), 64)

    async def test_missing_local_bridge_closes_instead_of_black_holing(self):
        relay_ws = _FakeRelayWs()
        await self._establish_bridge(relay_ws)
        mobile_keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=self.bridge.e2e_public_key,
            context=self.bridge.e2e_context,
        )
        self.bridge._local_ws = None
        await self.bridge._handle_relay_frame(
            relay_ws,
            self._mobile_e2e_frame(
                {"type": "text", "content": "must not disappear"},
                mobile_keys,
                seq=40,
            ),
        )
        self.assertTrue(relay_ws.closed)

    async def test_wrong_confirmation_reports_auth_mismatch(self):
        relay_ws = _FakeRelayWs()
        hello = self._hello_message()
        await self.bridge._handle_e2e_hello(
            relay_ws,
            hello,
            self._hello_payload(),
        )

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            ready = self._ready_payload(self.bridge.e2e_keys.kid, "wrong")
            await self.bridge._handle_e2e_ready(
                relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
            )

        self.assertTrue(relay_ws.closed)
        self.assertIn("E2E auth mismatch", captured.getvalue())

    def test_relay_reconnect_rotates_crypto_and_resets_sequence(self):
        old_private_key = self.bridge.e2e_private_key
        old_nonce_base = self.bridge.e2e_nonce_base
        self.bridge.seq = 42

        self.bridge._begin_relay_connection()

        self.assertNotEqual(self.bridge.e2e_private_key, old_private_key)
        self.assertNotEqual(self.bridge.e2e_nonce_base, old_nonce_base)
        self.assertEqual(self.bridge.seq, 0)
        self.assertIsNone(self.bridge.e2e_context)
        self.assertFalse(self.bridge.e2e_peer_ready)
        self.assertFalse(self.bridge.e2e_initial_hello_sent)
        self.assertIsNone(self.bridge.e2e_peer_hello_fingerprint)
        self.assertIsNone(self.bridge.e2e_hello_sent_for_peer)
        self.assertFalse(self.bridge.e2e_ready_sent)
        self.assertFalse(self.bridge.e2e_pc_ready_sent)

    def test_priority_final_mapping(self):
        cases = [
            ({"type": "response_chunk"}, "normal", False),
            ({"type": "turn_status", "status": "running"}, "critical", False),
            ({"type": "turn_status", "status": "done"}, "critical", True),
            ({"type": "status", "content": "error"}, "critical", True),
            ({"type": "cron_notification"}, "critical", False),
            ({"type": "relay_chunk_error"}, "critical", False),
            ({"type": "mcp_status"}, "droppable", False),
        ]
        for frame, priority, final in cases:
            with self.subTest(frame=frame):
                self.assertEqual(relay_frame_priority(frame), priority)
                self.assertEqual(relay_frame_is_final(frame), final)

    async def _connect_streaming_local(
        self,
        relay_ws: _FakeRelayWs,
        local_ws: _StreamingLocalWs,
    ) -> dict:
        connect_kwargs = {}

        async def connect(_url, **kwargs):
            connect_kwargs.update(kwargs)
            return local_ws

        import websockets

        with mock.patch.object(websockets, "connect", new=connect):
            await RelayBridge._connect_local(self.bridge, relay_ws)
        return connect_kwargs

    def _mobile_e2e_frame(self, frame: dict, mobile_keys, *, seq: int) -> dict:
        encrypted = encrypt_frame(
            frame,
            keys=mobile_keys,
            direction=MOBILE_TO_PC,
            pair_id=self.bridge.pair_id,
            role="mobile",
            priority="normal",
            final=False,
            seq=seq,
        )
        return {
            "v": 2,
            "type": TYPE_E2E_FRAME,
            "role": "mobile",
            "pair_id": self.bridge.pair_id,
            "ack": 0,
            **encrypted,
        }

    def _mobile_e2e_chunks(
        self,
        frame: dict,
        mobile_keys,
        *,
        seq: int,
        chunk_id: str | None = None,
    ) -> list[dict]:
        encrypted = encrypt_frame(
            frame,
            keys=mobile_keys,
            direction=MOBILE_TO_PC,
            pair_id=self.bridge.pair_id,
            role="mobile",
            priority="normal",
            final=False,
            seq=seq,
        )
        ciphertext = encrypted.pop("ciphertext")
        pieces = [
            ciphertext[offset : offset + E2E_CHUNK_CIPHERTEXT_CHARS]
            for offset in range(0, len(ciphertext), E2E_CHUNK_CIPHERTEXT_CHARS)
        ]
        transfer_id = chunk_id or self._b64(bytes(range(16)))
        return [
            {
                "v": 2,
                "type": TYPE_E2E_CHUNK,
                "role": "mobile",
                "pair_id": self.bridge.pair_id,
                "seq": seq + index,
                "ack": 0,
                "e2e_version": encrypted["e2e_version"],
                "alg": encrypted["alg"],
                "kid": encrypted["kid"],
                "frame_seq": seq,
                "priority": encrypted["priority"],
                "final": encrypted["final"],
                "nonce": encrypted["nonce"],
                "chunk_id": transfer_id,
                "chunk_index": index,
                "chunk_count": len(pieces),
                "total_bytes": len(ciphertext),
                "ciphertext_chunk": piece,
            }
            for index, piece in enumerate(pieces)
        ]

    @staticmethod
    def _e2e_frames(relay_ws: _FakeRelayWs) -> list[dict]:
        frames = []
        for raw in relay_ws.sent:
            frame = json.loads(raw)
            if frame.get("type") == TYPE_E2E_FRAME:
                frames.append(frame)
        return frames

    @staticmethod
    def _e2e_chunks(relay_ws: _FakeRelayWs) -> list[dict]:
        return [
            json.loads(raw)
            for raw in relay_ws.sent
            if json.loads(raw).get("type") == TYPE_E2E_CHUNK
        ]

    @staticmethod
    def _reassemble_chunk_envelope(chunks: list[dict]) -> dict:
        ordered = sorted(chunks, key=lambda item: item["chunk_index"])
        first = ordered[0]
        return {
            "e2e_version": first["e2e_version"],
            "alg": first["alg"],
            "kid": first["kid"],
            "pair_id": first["pair_id"],
            "role": first["role"],
            "seq": first["frame_seq"],
            "priority": first["priority"],
            "final": first["final"],
            "nonce": first["nonce"],
            "ciphertext": "".join(
                item["ciphertext_chunk"] for item in ordered
            ),
        }

    async def _wait_for_e2e_frames(
        self,
        relay_ws: _FakeRelayWs,
        count: int,
    ) -> list[dict]:
        for _ in range(100):
            frames = self._e2e_frames(relay_ws)
            if len(frames) >= count:
                return frames
            await asyncio.sleep(0)
        self.fail(f"timed out waiting for {count} E2E frames")

    async def _establish_bridge(self, relay_ws: _FakeRelayWs) -> None:
        hello = self._hello_message()
        await self.bridge._handle_e2e_hello(
            relay_ws,
            hello,
            self._hello_payload(),
        )
        self.assertIsNotNone(self.bridge.e2e_context)
        ready_tag = handshake_confirmation_tag(
            token=self.token,
            context=self.bridge.e2e_context,
            sender_role="mobile",
        )
        self.assertTrue(
            verify_handshake_confirmation(
                token=self.token,
                context=self.bridge.e2e_context,
                sender_role="mobile",
                tag=ready_tag,
            )
        )
        self.bridge.mobile_ready = True
        ready = self._ready_payload(self.bridge.e2e_keys.kid, ready_tag)
        await self.bridge._handle_e2e_ready(
            relay_ws, self._control_message(TYPE_E2E_READY, ready), ready
        )

    def _hello_message(self) -> dict:
        return self._control_message(TYPE_E2E_HELLO, self._hello_payload())

    def _hello_payload(self) -> dict:
        return {
            "e2e_version": 1,
            "alg": "CHACHA20-POLY1305",
            "public_key": self._b64(self.mobile_public_key),
            "nonce_base": self._b64(self.mobile_nonce_base),
            "e2e_chunk_version": 1,
        }

    def _ready_payload(self, kid: str, tag: str) -> dict:
        return {
            "e2e_version": 1,
            "alg": "CHACHA20-POLY1305",
            "kid": kid,
            "tag": tag,
        }

    def _control_message(self, msg_type: str, payload: dict) -> dict:
        return {
            "v": 2,
            "type": msg_type,
            "role": "mobile",
            "pair_id": self.bridge.pair_id,
            "seq": 10,
            "ack": 0,
            "payload": payload,
        }

    def _enable_relay_chunking(self) -> None:
        self.bridge._set_relay_chunk_capabilities({
            "e2e_chunk_version": 1,
            "e2e_chunk_max_frame_bytes": MAX_SAFE_RELAY_MESSAGE_BYTES,
            "e2e_chunk_max_ciphertext_chars": E2E_CHUNK_CIPHERTEXT_CHARS,
            "e2e_chunk_max_total_bytes": MAX_E2E_CHUNK_TOTAL_BYTES,
            "e2e_chunk_max_count": MAX_E2E_CHUNK_COUNT,
        })

    @staticmethod
    def _b64(data: bytes) -> str:
        from osia.relay_crypto import b64url_encode

        return b64url_encode(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
