"""Deterministic lifecycle and real-library tests for WebRTC direct."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from osia.relay_crypto import (
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    OutboundSequence,
    RelaySessionContext,
    SequenceReplayGuard,
    b64url_encode,
    decrypt_frame,
    derive_session_keys,
    encrypt_frame,
    generate_nonce_base,
    generate_x25519_private_key,
    handshake_confirmation_tag,
    x25519_public_key,
)
from osia.transport_coordinator import ApplicationTransportCoordinator
from osia.webrtc_signaling import make_signal
from osia.webrtc_transport import (
    DATA_CHANNEL_HIGH_WATER,
    WebRtcDirectSession,
    WebRtcSessionState,
    WebRtcTransportManager,
    _DirectChunkReassembler,
)


FULL_SDP = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=mid:0\r\n"
    "a=ice-ufrag:test\r\n"
    "a=ice-pwd:0123456789012345678901\r\n"
    "a=fingerprint:sha-256 " + ("00:" * 31) + "00\r\n"
    "a=setup:actpass\r\n"
    "a=sctp-port:5000\r\n"
    "a=candidate:1 1 udp 2130706431 127.0.0.1 50000 typ host\r\n"
    "a=end-of-candidates\r\n"
)


class _FakeChannel:
    def __init__(self) -> None:
        self.label = "osia-app-v1"
        self.protocol = "osia.e2e.v1"
        self.ordered = True
        self.maxRetransmits = None
        self.maxPacketLifeTime = None
        self.bufferedAmountLowThreshold = 0
        self.bufferedAmount = 0
        self.readyState = "connecting"
        self.handlers = {}
        self.sent: list[str] = []
        self.close_count = 0

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register

    def send(self, value: str) -> None:
        self.sent.append(value)

    def close(self) -> None:
        self.close_count += 1
        self.readyState = "closed"


class _FakePeer:
    def __init__(self) -> None:
        self.channel = _FakeChannel()
        self.handlers = {}
        self.localDescription = None
        self.remote_descriptions = []
        self.candidates = []
        self.connectionState = "new"
        self.iceConnectionState = "new"
        self.close_count = 0

    def createDataChannel(self, *_args, **_kwargs):
        return self.channel

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register

    async def createOffer(self):
        return RTCSessionDescription(sdp=FULL_SDP, type="offer")

    async def setLocalDescription(self, description):
        self.localDescription = description

    async def setRemoteDescription(self, description):
        self.remote_descriptions.append(description)

    async def addIceCandidate(self, candidate):
        self.candidates.append(candidate)

    async def close(self):
        self.close_count += 1
        self.connectionState = "closed"


class _FakeLocalWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_count = 0

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def close(self) -> None:
        self.close_count += 1

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()


class WebRtcDirectLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.token = "webrtc-test-token"
        self.pair_id = "1" * 64
        self.signals: list[dict] = []
        self.peers: list[_FakePeer] = []
        self.locals: list[_FakeLocalWs] = []
        self.coordinator = ApplicationTransportCoordinator()

        async def send_signal(frame):
            self.signals.append(frame)
            return True

        async def connect_local(_token):
            local = _FakeLocalWs()
            self.locals.append(local)
            return local

        def peer_factory(_configuration):
            peer = _FakePeer()
            self.peers.append(peer)
            return peer

        self.manager = WebRtcTransportManager(
            token=self.token,
            pair_id=self.pair_id,
            enabled=True,
            ice_servers=[],
            send_signal=send_signal,
            peer_connection_factory=peer_factory,
            connect_local=connect_local,
            coordinator=self.coordinator,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close()

    def mobile_signal(self, event_type: str, sequence: int, payload=None, *, generation=1):
        return make_signal(
            event_type,
            attempt_id="a" * 32,
            generation=generation,
            source_role="mobile",
            sequence=sequence,
            payload=payload or {},
        )

    async def start_session(self) -> WebRtcDirectSession:
        await self.manager.handle_signal(
            self.mobile_signal(
                "webrtc_prepare",
                1,
                {"configuration_id": self.manager.configuration_id},
            )
        )
        session = self.manager._session
        self.assertIsNotNone(session)
        return session

    async def authenticate(self, session: WebRtcDirectSession):
        channel = session.channel
        channel.readyState = "open"
        await session._on_channel_open()
        mobile_private = generate_x25519_private_key()
        mobile_public = x25519_public_key(mobile_private)
        mobile_nonce = generate_nonce_base()
        await session._on_channel_message(json.dumps({
            "type": "webrtc_e2e_hello",
            "protocol_version": 1,
            "transport": "webrtc",
            "attempt_id": session.attempt_id,
            "generation": session.generation,
            "source_role": "mobile",
            "destination_role": "pc",
            "data_channel_label": "osia-app-v1",
            "data_channel_protocol": "osia.e2e.v1",
            "e2e_version": 1,
            "alg": "CHACHA20-POLY1305",
            "public_key": b64url_encode(mobile_public),
            "nonce_base": b64url_encode(mobile_nonce),
        }))
        context = RelaySessionContext(
            pair_id=session._direct_pair_id,
            pc_public_key=session._public_key,
            mobile_public_key=mobile_public,
            pc_nonce_base=session._nonce_base,
            mobile_nonce_base=mobile_nonce,
        )
        keys = derive_session_keys(
            token=self.token,
            local_private_key=mobile_private,
            peer_public_key=session._public_key,
            context=context,
        )
        await session._on_channel_message(json.dumps({
            "type": "webrtc_e2e_ready",
            "protocol_version": 1,
            "transport": "webrtc",
            "attempt_id": session.attempt_id,
            "generation": session.generation,
            "source_role": "mobile",
            "destination_role": "pc",
            "e2e_version": 1,
            "alg": "CHACHA20-POLY1305",
            "kid": keys.kid,
            "tag": handshake_confirmation_tag(
                token=self.token, context=context, sender_role="mobile"
            ),
        }))
        ready_envelope = json.loads(channel.sent[-1])
        ready = decrypt_frame(
            ready_envelope,
            keys=keys,
            direction=PC_TO_MOBILE,
            replay_guard=SequenceReplayGuard(),
            replay_key="pc",
        )
        self.assertEqual(ready["event_type"], "webrtc_direct_ready")
        mobile_sequence = OutboundSequence()
        commit = make_signal(
            "webrtc_direct_commit",
            attempt_id=session.attempt_id,
            generation=session.generation,
            source_role="mobile",
            sequence=10,
        )
        envelope = {
            "type": "E2E_FRAME",
            **encrypt_frame(
                commit,
                keys=keys,
                direction=MOBILE_TO_PC,
                pair_id=session._direct_pair_id,
                role="mobile",
                priority="critical",
                final=False,
                outbound_sequence=mobile_sequence,
            ),
        }
        await session._on_channel_message(json.dumps(envelope))
        return keys, mobile_sequence

    async def test_fixed_offerer_buffers_deduplicates_and_applies_one_answer(self) -> None:
        session = await self.start_session()
        self.assertEqual([f["event_type"] for f in self.signals].count("webrtc_offer"), 1)
        self.assertEqual(session.channel.label, "osia-app-v1")
        self.assertEqual(session.channel.protocol, "osia.e2e.v1")
        candidate = {
            "candidate": "candidate:2 1 udp 1 127.0.0.1 50001 typ host",
            "sdp_mid": "0",
            "sdp_mline_index": 0,
        }
        for sequence in range(2, 52):
            await self.manager.handle_signal(
                self.mobile_signal(
                    "webrtc_ice_candidates",
                    sequence,
                    {"candidates": [candidate, candidate]},
                )
            )
        self.assertEqual(len(session._pending_candidates), 1)
        answer_sdp = FULL_SDP.replace("a=candidate:1 1 udp 2130706431 127.0.0.1 50000 typ host\r\n", "")
        await self.manager.handle_signal(
            self.mobile_signal("webrtc_answer", 52, {"type": "answer", "sdp": answer_sdp})
        )
        self.assertEqual(len(self.peers[0].remote_descriptions), 1)
        self.assertEqual(len(self.peers[0].candidates), 1)
        await self.manager.handle_signal(
            self.mobile_signal("webrtc_prepare", 53, {"configuration_id": self.manager.configuration_id})
        )
        self.assertEqual([f["event_type"] for f in self.signals].count("webrtc_offer"), 1)

        self.manager._latest_generation = 3
        self.manager.reset_signaling_epoch()
        self.assertEqual(self.manager._latest_generation, 0)
        restarted = self.mobile_signal(
            "webrtc_prepare",
            1,
            {"configuration_id": self.manager.configuration_id},
        )
        restarted["attempt_id"] = "b" * 32
        await self.manager.handle_signal(restarted)
        self.assertEqual([f["event_type"] for f in self.signals].count("webrtc_offer"), 2)

    async def test_fresh_e2e_precedes_one_lease_and_one_bridge(self) -> None:
        session = await self.start_session()
        self.assertIsNone(session.lease)
        self.assertEqual(self.locals, [])
        keys, mobile_sequence = await self.authenticate(session)
        self.assertEqual(session.state, WebRtcSessionState.READY)
        self.assertEqual(self.coordinator.active_transport, "webrtc")
        self.assertEqual(len(self.locals), 1)
        text = {
            "type": "text",
            "content": "hello",
            "conversation_id": "conv",
            "client_request_id": "req",
            "turn_id": "turn",
        }
        envelope = {
            "type": "E2E_FRAME",
            **encrypt_frame(
                text,
                keys=keys,
                direction=MOBILE_TO_PC,
                pair_id=session._direct_pair_id,
                role="mobile",
                priority="normal",
                final=False,
                outbound_sequence=mobile_sequence,
            ),
        }
        await session._on_channel_message(json.dumps(envelope))
        self.assertEqual(session.state, WebRtcSessionState.ACTIVE_TURN)
        self.assertEqual(json.loads(self.locals[0].sent[-1]), text)
        session._observe_local_frame({
            "type": "audio",
            "client_request_id": "req",
            "turn_id": "turn",
            "media_terminal": True,
        })
        self.assertEqual(session.state, WebRtcSessionState.READY)
        self.assertEqual(self.peers[0].close_count, 0)
        await session.close(notify=False)
        await session.close(notify=False)
        self.assertEqual(self.peers[0].close_count, 1)
        self.assertEqual(self.locals[0].close_count, 1)
        self.assertIsNone(self.coordinator.active_transport)

    async def test_backpressure_waits_and_releases(self) -> None:
        session = await self.start_session()
        session.channel.readyState = "open"
        session.channel.bufferedAmount = DATA_CHANNEL_HIGH_WATER + 1
        task = asyncio.create_task(session._send_messages(["payload"]))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        session.channel.bufferedAmount = 0
        session._buffer_low.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(session.channel.sent[-1], "payload")
        session.channel.sent.clear()
        await session._send_serialized("x" * (2 * 1024 * 1024))
        self.assertGreater(len(session.channel.sent), 128)
        session.channel.sent.clear()
        with mock.patch.object(
            _FakeChannel,
            "bufferedAmount",
            new_callable=mock.PropertyMock,
            create=True,
        ) as buffered_amount:
            buffered_amount.side_effect = [DATA_CHANNEL_HIGH_WATER + 1, 0, 0]
            await asyncio.wait_for(
                session._send_messages(["race-free"]),
                timeout=1,
            )
        self.assertEqual(session.channel.sent, ["race-free"])

    def test_chunk_reassembly_is_bounded_idempotent_and_integrity_checked(self) -> None:
        raw = json.dumps({"type": "response", "content": "x" * 20000}).encode()
        checksum = __import__("hashlib").sha256(raw).hexdigest()
        chunks = []
        for index, part in enumerate((raw[:9000], raw[9000:18000], raw[18000:])):
            chunks.append({
                "type": "WEBRTC_CHUNK",
                "message_id": "a" * 32,
                "chunk_index": index,
                "chunk_count": 3,
                "total_bytes": len(raw),
                "checksum": checksum,
                "data": base64.urlsafe_b64encode(part).decode("ascii"),
            })
        reassembler = _DirectChunkReassembler()
        self.assertIsNone(reassembler.accept(chunks[1]))
        self.assertIsNone(reassembler.accept(chunks[0]))
        self.assertEqual(reassembler.accept(chunks[2]), raw.decode())
        self.assertIsNone(reassembler.accept(chunks[2]))
        broken = {**chunks[0], "message_id": "b" * 32, "checksum": "0" * 64}
        reassembler.accept(broken)
        with self.assertRaises(Exception):
            reassembler.accept({**chunks[1], "message_id": "b" * 32, "checksum": "1" * 64})

        now = [0.0]
        progressive = _DirectChunkReassembler(clock=lambda: now[0])
        self.assertIsNone(progressive.accept(chunks[0]))
        now[0] = 20.0
        self.assertIsNone(progressive.accept(chunks[1]))
        now[0] = 40.0
        self.assertEqual(progressive.accept(chunks[2]), raw.decode())

    async def test_timeout_cleans_preapplication_session_and_allows_fresh_attempt(self) -> None:
        with mock.patch("osia.webrtc_transport.CONNECT_TIMEOUT_SECONDS", 0.01):
            session = await self.start_session()
            await asyncio.sleep(0.03)
        self.assertEqual(session.state, WebRtcSessionState.DISCONNECTED)
        self.assertIsNone(self.coordinator.active_transport)
        self.assertIsNone(self.manager._session)
        second = await self.start_session()
        self.assertIsNot(second, session)
        self.assertEqual(len(self.peers), 2)

    def test_bridge_and_lease_exclusivity_stress(self) -> None:
        for _ in range(50):
            coordinator = ApplicationTransportCoordinator()
            direct = coordinator.try_acquire("webrtc", object())
            self.assertIsNotNone(direct)
            self.assertIsNone(coordinator.try_acquire("lan", object()))
            self.assertIsNone(coordinator.try_acquire("relay", object()))
            direct.release()
            relay = coordinator.try_acquire("relay", object())
            self.assertIsNotNone(relay)
            relay.release()


class AiortcDataChannelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_data_only_ordered_reliable_close_and_reconnect(self) -> None:
        for generation in range(2):
            offerer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            answerer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            opened = asyncio.Event()
            received: list[str] = []
            channel = offerer.createDataChannel(
                "osia-app-v1", ordered=True, protocol="osia.e2e.v1"
            )

            @channel.on("open")
            def on_open():
                opened.set()

            @answerer.on("datachannel")
            def on_datachannel(remote):
                self.assertEqual(remote.label, "osia-app-v1")
                self.assertEqual(remote.protocol, "osia.e2e.v1")

                @remote.on("message")
                def on_message(message):
                    received.append(message)

            offer = await offerer.createOffer()
            await offerer.setLocalDescription(offer)
            await answerer.setRemoteDescription(offerer.localDescription)
            answer = await answerer.createAnswer()
            await answerer.setLocalDescription(answer)
            await offerer.setRemoteDescription(answerer.localDescription)
            await asyncio.wait_for(opened.wait(), timeout=10)
            payloads = [f"generation-{generation}-frame-{index}" for index in range(20)]
            for payload in payloads:
                channel.send(payload)
            for _ in range(100):
                if received == payloads:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(received, payloads)
            self.assertTrue(channel.ordered)
            self.assertIsNone(channel.maxRetransmits)
            self.assertIsNone(channel.maxPacketLifeTime)
            await offerer.close()
            await answerer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
