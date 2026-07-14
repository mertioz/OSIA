"""Data-only WebRTC application transport for the PC relay connector.

The relay carries only already-encrypted signaling frames. Once the ordered,
reliable DataChannel is open, OSIA performs a fresh pairing-authenticated E2E
handshake and bridges exactly one loopback application socket.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Mapping

from aiortc import (
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from osia.relay_crypto import (
    ALG_CHACHA20_POLY1305,
    E2E_VERSION,
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    OutboundSequence,
    RelayCryptoError,
    RelaySessionContext,
    SequenceReplayGuard,
    b64url_decode,
    b64url_encode,
    decrypt_frame,
    derive_session_keys,
    encrypt_frame,
    generate_nonce_base,
    generate_x25519_private_key,
    handshake_confirmation_tag,
    relay_frame_is_final,
    relay_frame_priority,
    verify_handshake_confirmation,
    x25519_public_key,
)
from osia.transport_coordinator import (
    APPLICATION_TRANSPORT_COORDINATOR,
    ApplicationTransportCoordinator,
)
from osia.webrtc_signaling import (
    MAX_CANDIDATES,
    MAX_CANDIDATES_PER_EVENT,
    SIGNAL_EVENT_TYPES,
    WEBRTC_DATA_CHANNEL_LABEL,
    WEBRTC_DATA_CHANNEL_PROTOCOL,
    WEBRTC_PROTOCOL_VERSION,
    WebRtcSignalingError,
    candidate_dedup_key,
    ice_configuration_id,
    make_signal,
    strip_sdp_candidates,
    validate_candidate,
    validate_ice_servers,
    validate_signal,
)


LOCAL_WS_URL = "ws://127.0.0.1:8765/ws"
MAX_APP_FRAME_BYTES = 16 * 1024 * 1024
MAX_DIRECT_ENVELOPE_BYTES = 24 * 1024 * 1024
DIRECT_CHUNK_BYTES = 9 * 1024
MAX_DIRECT_CHUNKS = 3072
MAX_DIRECT_INFLIGHT = 4
DIRECT_REASSEMBLY_TIMEOUT_SECONDS = 30.0
DATA_CHANNEL_HIGH_WATER = 512 * 1024
DATA_CHANNEL_LOW_WATER = 256 * 1024
MAX_SEND_QUEUE_ITEMS = MAX_DIRECT_CHUNKS
MAX_SEND_QUEUE_BYTES = 24 * 1024 * 1024
SEND_TIMEOUT_SECONDS = 15.0
SEND_TIMEOUT_PER_CHUNK_SECONDS = 0.5
MAX_SEND_TIMEOUT_SECONDS = 300.0
CONNECT_TIMEOUT_SECONDS = 20.0
DISCONNECTED_GRACE_SECONDS = 5.0

_DIRECT_HELLO_FIELDS = frozenset({
    "type",
    "protocol_version",
    "transport",
    "attempt_id",
    "generation",
    "source_role",
    "destination_role",
    "data_channel_label",
    "data_channel_protocol",
    "e2e_version",
    "alg",
    "public_key",
    "nonce_base",
})
_DIRECT_READY_FIELDS = frozenset({
    "type",
    "protocol_version",
    "transport",
    "attempt_id",
    "generation",
    "source_role",
    "destination_role",
    "e2e_version",
    "alg",
    "kid",
    "tag",
})
_DIRECT_E2E_FIELDS = frozenset({
    "type",
    "e2e_version",
    "alg",
    "kid",
    "pair_id",
    "role",
    "seq",
    "priority",
    "final",
    "nonce",
    "ciphertext",
})
_DIRECT_CHUNK_FIELDS = frozenset({
    "type",
    "message_id",
    "chunk_index",
    "chunk_count",
    "total_bytes",
    "checksum",
    "data",
})
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class WebRtcTransportError(Exception):
    """Fail-closed direct-transport failure with no sensitive detail."""


class WebRtcSessionState(Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    OFFERING = "offering"
    GATHERING = "gathering"
    CONNECTING = "connecting"
    DATA_CHANNEL_OPEN = "data_channel_open"
    AUTHENTICATING = "authenticating"
    READY = "ready"
    ACTIVE_TURN = "active_turn"
    CLOSING = "closing"
    DISCONNECTED = "disconnected"


def _direct_pair_id(pair_id: str, attempt_id: str, generation: int) -> str:
    transcript = (
        f"osia-webrtc-direct-v{WEBRTC_PROTOCOL_VERSION}:"
        f"{pair_id}:{attempt_id}:{generation}:pc:mobile:"
        f"{WEBRTC_DATA_CHANNEL_LABEL}:{WEBRTC_DATA_CHANNEL_PROTOCOL}"
    )
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()


def _candidate_batches(candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [
        candidates[index : index + MAX_CANDIDATES_PER_EVENT]
        for index in range(0, len(candidates), MAX_CANDIDATES_PER_EVENT)
    ]


def _rtc_configuration(records: list[dict[str, Any]]) -> RTCConfiguration:
    servers = []
    for record in records:
        servers.append(
            RTCIceServer(
                urls=record["urls"],
                username=record.get("username"),
                credential=record.get("credential"),
            )
        )
    return RTCConfiguration(iceServers=servers)


async def _default_connect_local(token: str):
    import websockets

    local_ws = await websockets.connect(
        LOCAL_WS_URL,
        max_size=MAX_APP_FRAME_BYTES,
        ping_interval=20,
        ping_timeout=20,
    )
    await local_ws.send(json.dumps({"type": "auth", "token": token}))
    return local_ws


@dataclass
class _InboundChunks:
    updated_at: float
    chunk_count: int
    total_bytes: int
    checksum: str
    parts: dict[int, bytes] = field(default_factory=dict)


class _DirectChunkReassembler:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._inflight: dict[str, _InboundChunks] = {}
        self._completed: dict[str, float] = {}

    def clear(self) -> None:
        self._inflight.clear()
        self._completed.clear()

    def accept(self, message: Any) -> str | None:
        self._expire()
        if not isinstance(message, Mapping) or set(message) != _DIRECT_CHUNK_FIELDS:
            raise WebRtcTransportError("invalid direct chunk")
        message_id = str(message.get("message_id") or "")
        checksum = str(message.get("checksum") or "")
        chunk_index = message.get("chunk_index")
        chunk_count = message.get("chunk_count")
        total_bytes = message.get("total_bytes")
        data = message.get("data")
        if (
            _HEX_32.fullmatch(message_id) is None
            or _HEX_64.fullmatch(checksum) is None
            or not isinstance(chunk_index, int)
            or isinstance(chunk_index, bool)
            or not isinstance(chunk_count, int)
            or isinstance(chunk_count, bool)
            or not isinstance(total_bytes, int)
            or isinstance(total_bytes, bool)
            or not 1 <= chunk_count <= MAX_DIRECT_CHUNKS
            or not 0 <= chunk_index < chunk_count
            or not 1 <= total_bytes <= MAX_DIRECT_ENVELOPE_BYTES
            or not isinstance(data, str)
        ):
            raise WebRtcTransportError("invalid direct chunk metadata")
        if message_id in self._completed:
            return None
        try:
            part = base64.b64decode(data.encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeError) as exc:
            raise WebRtcTransportError("invalid direct chunk encoding") from exc
        if not part or len(part) > DIRECT_CHUNK_BYTES:
            raise WebRtcTransportError("invalid direct chunk size")
        transfer = self._inflight.get(message_id)
        if transfer is None:
            if len(self._inflight) >= MAX_DIRECT_INFLIGHT:
                raise WebRtcTransportError("direct chunk capacity exceeded")
            transfer = _InboundChunks(
                updated_at=self._clock(),
                chunk_count=chunk_count,
                total_bytes=total_bytes,
                checksum=checksum,
            )
            self._inflight[message_id] = transfer
        elif (
            transfer.chunk_count != chunk_count
            or transfer.total_bytes != total_bytes
            or transfer.checksum != checksum
        ):
            raise WebRtcTransportError("conflicting direct chunk metadata")
        previous = transfer.parts.get(chunk_index)
        if previous is not None:
            if previous != part:
                raise WebRtcTransportError("conflicting duplicate direct chunk")
            return None
        transfer.parts[chunk_index] = part
        transfer.updated_at = self._clock()
        if len(transfer.parts) != transfer.chunk_count:
            return None
        combined = b"".join(transfer.parts[index] for index in range(chunk_count))
        self._inflight.pop(message_id, None)
        if (
            len(combined) != total_bytes
            or hashlib.sha256(combined).hexdigest() != checksum
        ):
            raise WebRtcTransportError("direct chunk integrity failed")
        self._completed[message_id] = self._clock()
        try:
            return combined.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebRtcTransportError("direct chunk text is invalid") from exc

    def _expire(self) -> None:
        now = self._clock()
        self._inflight = {
            key: transfer
            for key, transfer in self._inflight.items()
            if now - transfer.updated_at < DIRECT_REASSEMBLY_TIMEOUT_SECONDS
        }
        self._completed = {
            key: created_at
            for key, created_at in self._completed.items()
            if now - created_at < DIRECT_REASSEMBLY_TIMEOUT_SECONDS * 2
        }


class WebRtcTransportManager:
    """Own at most one fresh PC offerer session for the paired mobile."""

    def __init__(
        self,
        *,
        token: str,
        pair_id: str,
        enabled: bool,
        ice_servers: list[dict[str, Any]],
        send_signal: Callable[[dict[str, Any]], Awaitable[bool]],
        peer_connection_factory: Callable[[RTCConfiguration], Any] = RTCPeerConnection,
        connect_local: Callable[[str], Awaitable[Any]] = _default_connect_local,
        coordinator: ApplicationTransportCoordinator = APPLICATION_TRANSPORT_COORDINATOR,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.token = token
        self.pair_id = pair_id
        self.enabled = bool(enabled)
        self.ice_servers = validate_ice_servers(ice_servers, allow_turn=True)
        self.configuration_id = ice_configuration_id(self.enabled, self.ice_servers)
        self._send_signal = send_signal
        self._peer_connection_factory = peer_connection_factory
        self._connect_local = connect_local
        self._coordinator = coordinator
        self._clock = clock
        self._session: WebRtcDirectSession | None = None
        self._latest_generation = 0

    @property
    def active(self) -> bool:
        session = self._session
        return session is not None and session.state in {
            WebRtcSessionState.READY,
            WebRtcSessionState.ACTIVE_TURN,
        }

    @property
    def preparing(self) -> bool:
        return self._session is not None and not self.active

    def reset_signaling_epoch(self) -> None:
        """Scope generation ordering to the authenticated relay E2E epoch."""

        self._latest_generation = 0

    async def handle_signal(self, raw_frame: Any) -> bool:
        event_type = str(raw_frame.get("event_type") or "") if isinstance(raw_frame, Mapping) else ""
        if event_type not in SIGNAL_EVENT_TYPES:
            return False
        try:
            frame = validate_signal(raw_frame, destination_role="pc", clock=self._clock)
        except WebRtcSignalingError:
            session = self._session
            if session is not None:
                await session.fail("protocol_error")
            return True
        if frame["source_role"] != "mobile":
            return True
        if event_type == "webrtc_prepare":
            await self._handle_prepare(frame)
            return True
        session = self._session
        if session is None:
            return True
        if (
            frame["attempt_id"] != session.attempt_id
            or frame["generation"] != session.generation
        ):
            return True
        await session.handle_signal(frame)
        return True

    async def _handle_prepare(self, frame: dict[str, Any]) -> None:
        if not self.enabled or frame["payload"]["configuration_id"] != self.configuration_id:
            await self._send_terminal(frame, "unavailable")
            return
        generation = frame["generation"]
        current = self._session
        if current is not None and current.active:
            await self._send_terminal(frame, "busy")
            return
        if generation < self._latest_generation:
            return
        if current is not None:
            if (
                current.attempt_id == frame["attempt_id"]
                and current.generation == generation
            ):
                return
            await current.close(notify=False)
        self._latest_generation = generation
        session = WebRtcDirectSession(
            manager=self,
            attempt_id=frame["attempt_id"],
            generation=generation,
        )
        self._session = session
        try:
            await asyncio.wait_for(session.start(), timeout=CONNECT_TIMEOUT_SECONDS)
        except Exception:
            await session.fail("unavailable")

    async def _send_terminal(self, source: dict[str, Any], category: str) -> None:
        frame = make_signal(
            "webrtc_failed",
            attempt_id=source["attempt_id"],
            generation=source["generation"],
            source_role="pc",
            sequence=1,
            payload={"category": category},
            terminal=True,
            clock=self._clock,
        )
        with suppress(Exception):
            await self._send_signal(frame)

    async def cancel_preparing(self) -> None:
        session = self._session
        if session is not None and not session.active:
            await session.close(notify=False)

    async def close(self) -> None:
        session = self._session
        if session is not None:
            await session.close(notify=False)

    async def _session_closed(
        self,
        session: "WebRtcDirectSession",
        *,
        notify: bool,
        category: str,
    ) -> None:
        if self._session is session:
            self._session = None
        if notify:
            frame = make_signal(
                "webrtc_failed",
                attempt_id=session.attempt_id,
                generation=session.generation,
                source_role="pc",
                sequence=session.next_signal_sequence(),
                payload={"category": category},
                terminal=True,
                clock=self._clock,
            )
            with suppress(Exception):
                await self._send_signal(frame)


class WebRtcDirectSession:
    """One non-reusable ICE/DTLS/SCTP/E2E generation."""

    def __init__(
        self,
        *,
        manager: WebRtcTransportManager,
        attempt_id: str,
        generation: int,
    ) -> None:
        self.manager = manager
        self.attempt_id = attempt_id
        self.generation = generation
        self.state = WebRtcSessionState.IDLE
        self.pc: Any = None
        self.channel: Any = None
        self.local_ws: Any = None
        self.lease: Any = None
        self._local_reader_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._signal_sequence = 0
        self._last_remote_signal_sequence = 0
        self._remote_event_ids: set[str] = set()
        self._remote_answer_applied = False
        self._remote_end_of_candidates = False
        self._pending_candidates: list[dict[str, Any]] = []
        self._candidate_keys: set[str] = set()
        self._private_key = generate_x25519_private_key()
        self._public_key = x25519_public_key(self._private_key)
        self._nonce_base = generate_nonce_base()
        self._direct_pair_id = _direct_pair_id(
            manager.pair_id,
            attempt_id,
            generation,
        )
        self._context: RelaySessionContext | None = None
        self._keys = None
        self._peer_authenticated = False
        self._outbound_sequence = OutboundSequence()
        self._replay_guard = SequenceReplayGuard()
        self._reassembler = _DirectChunkReassembler()
        self._send_lock = asyncio.Lock()
        self._buffer_low = asyncio.Event()
        self._buffer_low.set()
        self._queued_items = 0
        self._queued_bytes = 0
        self._direct_bytes_sent = 0
        self._direct_bytes_received = 0
        self._active_request_id = ""
        self._active_turn_id = ""
        self._candidate_summary: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return self.state in {WebRtcSessionState.READY, WebRtcSessionState.ACTIVE_TURN}

    def next_signal_sequence(self) -> int:
        self._signal_sequence += 1
        return self._signal_sequence

    def _set_state(self, state: WebRtcSessionState) -> None:
        self.state = state

    def _schedule(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def settled(completed: asyncio.Task) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None and not self._closed:
                asyncio.create_task(self.fail("protocol_error"))

        task.add_done_callback(settled)

    async def start(self) -> None:
        if self.state is not WebRtcSessionState.IDLE:
            return
        self._set_state(WebRtcSessionState.PREPARING)
        self._schedule(self._authentication_timeout())
        self.pc = self.manager._peer_connection_factory(
            _rtc_configuration(self.manager.ice_servers)
        )
        channel = self.pc.createDataChannel(
            WEBRTC_DATA_CHANNEL_LABEL,
            ordered=True,
            protocol=WEBRTC_DATA_CHANNEL_PROTOCOL,
        )
        if (
            channel.label != WEBRTC_DATA_CHANNEL_LABEL
            or channel.protocol != WEBRTC_DATA_CHANNEL_PROTOCOL
            or channel.ordered is not True
            or channel.maxRetransmits is not None
            or channel.maxPacketLifeTime is not None
        ):
            raise WebRtcTransportError("invalid local DataChannel contract")
        self.channel = channel
        channel.bufferedAmountLowThreshold = DATA_CHANNEL_LOW_WATER

        @channel.on("open")
        def on_open() -> None:
            self._schedule(self._on_channel_open())

        @channel.on("message")
        def on_message(message: Any) -> None:
            self._schedule(self._on_channel_message(message))

        @channel.on("close")
        def on_close() -> None:
            if not self._closed:
                self._schedule(self.fail("unavailable"))

        @channel.on("bufferedamountlow")
        def on_buffered_low() -> None:
            self._buffer_low.set()

        peer = self.pc

        @peer.on("connectionstatechange")
        def on_connection_state() -> None:
            state = peer.connectionState
            print(
                f"[OSIA WebRTC] peer state={state} "
                f"attempt={self.attempt_id[:8]} generation={self.generation}"
            )
            if state in {"failed", "closed"} and not self._closed:
                self._schedule(self.fail("ice_failed"))
            elif state == "disconnected" and not self._closed:
                self._schedule(self._fail_if_still_disconnected())

        @peer.on("iceconnectionstatechange")
        def on_ice_state() -> None:
            print(
                f"[OSIA WebRTC] ICE state={peer.iceConnectionState} "
                f"attempt={self.attempt_id[:8]} generation={self.generation}"
            )

        self._set_state(WebRtcSessionState.OFFERING)
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self._set_state(WebRtcSessionState.GATHERING)
        local = self.pc.localDescription
        if local is None:
            raise WebRtcTransportError("local SDP unavailable")
        stripped_sdp, candidates = strip_sdp_candidates(local.sdp)
        await self._send_signal(
            "webrtc_offer",
            {"type": "offer", "sdp": stripped_sdp},
        )
        for batch in _candidate_batches(candidates):
            await self._send_signal(
                "webrtc_ice_candidates",
                {"candidates": batch},
            )
        await self._send_signal("webrtc_end_of_candidates", {})
        self._set_state(WebRtcSessionState.CONNECTING)

    async def _authentication_timeout(self) -> None:
        await asyncio.sleep(CONNECT_TIMEOUT_SECONDS)
        if not self._closed and not self.active:
            await self.fail("timeout")

    async def _fail_if_still_disconnected(self) -> None:
        await asyncio.sleep(DISCONNECTED_GRACE_SECONDS)
        if (
            not self._closed
            and self.pc is not None
            and self.pc.connectionState == "disconnected"
        ):
            await self.fail("ice_failed")

    async def _send_signal(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        frame = make_signal(
            event_type,
            attempt_id=self.attempt_id,
            generation=self.generation,
            source_role="pc",
            sequence=self.next_signal_sequence(),
            payload=payload,
            terminal=terminal,
            clock=self.manager._clock,
        )
        if not await self.manager._send_signal(frame):
            raise WebRtcTransportError("signaling channel unavailable")

    async def handle_signal(self, frame: dict[str, Any]) -> None:
        if self._closed:
            return
        sequence = frame["sequence"]
        event_id = frame["event_id"]
        if event_id in self._remote_event_ids:
            return
        if sequence <= self._last_remote_signal_sequence:
            return
        self._remote_event_ids.add(event_id)
        self._last_remote_signal_sequence = sequence
        event_type = frame["event_type"]
        if event_type == "webrtc_answer":
            if self._remote_answer_applied:
                await self.fail("protocol_error")
                return
            self._remote_answer_applied = True
            await self.pc.setRemoteDescription(
                RTCSessionDescription(
                    sdp=frame["payload"]["sdp"],
                    type="answer",
                )
            )
            for candidate in self._pending_candidates:
                await self._add_remote_candidate(candidate)
            self._pending_candidates.clear()
            if self._remote_end_of_candidates:
                await self.pc.addIceCandidate(None)
            return
        if event_type == "webrtc_ice_candidates":
            for raw_candidate in frame["payload"]["candidates"]:
                candidate = validate_candidate(raw_candidate)
                key = candidate_dedup_key(candidate)
                if key in self._candidate_keys:
                    continue
                if len(self._candidate_keys) >= MAX_CANDIDATES:
                    await self.fail("protocol_error")
                    return
                self._candidate_keys.add(key)
                if not self._remote_answer_applied:
                    self._pending_candidates.append(candidate)
                else:
                    await self._add_remote_candidate(candidate)
            return
        if event_type == "webrtc_end_of_candidates":
            if self._remote_end_of_candidates:
                return
            self._remote_end_of_candidates = True
            if self._remote_answer_applied:
                await self.pc.addIceCandidate(None)
            return
        if event_type in {"webrtc_cancel", "webrtc_failed"}:
            await self.close(notify=False)

    async def _add_remote_candidate(self, record: Mapping[str, Any]) -> None:
        parsed = candidate_from_sdp(str(record["candidate"])[10:])
        candidate = RTCIceCandidate(
            component=parsed.component,
            foundation=parsed.foundation,
            ip=parsed.ip,
            port=parsed.port,
            priority=parsed.priority,
            protocol=parsed.protocol,
            type=parsed.type,
            relatedAddress=parsed.relatedAddress,
            relatedPort=parsed.relatedPort,
            sdpMid=str(record["sdp_mid"]),
            sdpMLineIndex=int(record["sdp_mline_index"]),
            tcpType=parsed.tcpType,
        )
        await self.pc.addIceCandidate(candidate)

    async def _on_channel_open(self) -> None:
        if self._closed or self.state is not WebRtcSessionState.CONNECTING:
            return
        self._set_state(WebRtcSessionState.DATA_CHANNEL_OPEN)
        await self._send_plain({
            "type": "webrtc_e2e_hello",
            "protocol_version": WEBRTC_PROTOCOL_VERSION,
            "transport": "webrtc",
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "source_role": "pc",
            "destination_role": "mobile",
            "data_channel_label": WEBRTC_DATA_CHANNEL_LABEL,
            "data_channel_protocol": WEBRTC_DATA_CHANNEL_PROTOCOL,
            "e2e_version": E2E_VERSION,
            "alg": ALG_CHACHA20_POLY1305,
            "public_key": b64url_encode(self._public_key),
            "nonce_base": b64url_encode(self._nonce_base),
        })
        self._set_state(WebRtcSessionState.AUTHENTICATING)

    async def _on_channel_message(self, raw: Any) -> None:
        if self._closed:
            return
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > DIRECT_CHUNK_BYTES * 2:
            await self.fail("protocol_error")
            return
        self._direct_bytes_received += len(raw.encode("utf-8"))
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, Mapping) and decoded.get("type") == "WEBRTC_CHUNK":
                complete = self._reassembler.accept(decoded)
                if complete is None:
                    return
                decoded = json.loads(complete)
            if not isinstance(decoded, Mapping):
                raise WebRtcTransportError("direct message must be an object")
            message = dict(decoded)
            message_type = message.get("type")
            if message_type == "webrtc_e2e_hello":
                await self._handle_mobile_hello(message)
                return
            if message_type == "webrtc_e2e_ready":
                await self._handle_mobile_ready(message)
                return
            if message_type == "E2E_FRAME":
                await self._handle_encrypted(message)
                return
            raise WebRtcTransportError("unexpected direct message")
        except (WebRtcTransportError, WebRtcSignalingError, RelayCryptoError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            await self.fail("protocol_error")

    async def _handle_mobile_hello(self, message: dict[str, Any]) -> None:
        if (
            self._context is not None
            or set(message) != _DIRECT_HELLO_FIELDS
            or message.get("protocol_version") != WEBRTC_PROTOCOL_VERSION
            or message.get("transport") != "webrtc"
            or message.get("attempt_id") != self.attempt_id
            or message.get("generation") != self.generation
            or message.get("source_role") != "mobile"
            or message.get("destination_role") != "pc"
            or message.get("data_channel_label") != WEBRTC_DATA_CHANNEL_LABEL
            or message.get("data_channel_protocol") != WEBRTC_DATA_CHANNEL_PROTOCOL
            or message.get("e2e_version") != E2E_VERSION
            or message.get("alg") != ALG_CHACHA20_POLY1305
        ):
            raise WebRtcTransportError("invalid mobile E2E hello")
        mobile_public_key = b64url_decode(str(message.get("public_key") or ""))
        mobile_nonce_base = b64url_decode(str(message.get("nonce_base") or ""))
        if len(mobile_public_key) != 32 or len(mobile_nonce_base) != 4:
            raise WebRtcTransportError("invalid mobile E2E key material")
        context = RelaySessionContext(
            pair_id=self._direct_pair_id,
            pc_public_key=self._public_key,
            mobile_public_key=mobile_public_key,
            pc_nonce_base=self._nonce_base,
            mobile_nonce_base=mobile_nonce_base,
        )
        keys = derive_session_keys(
            token=self.manager.token,
            local_private_key=self._private_key,
            peer_public_key=mobile_public_key,
            context=context,
        )
        self._context = context
        self._keys = keys
        await self._send_plain({
            "type": "webrtc_e2e_ready",
            "protocol_version": WEBRTC_PROTOCOL_VERSION,
            "transport": "webrtc",
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "source_role": "pc",
            "destination_role": "mobile",
            "e2e_version": E2E_VERSION,
            "alg": ALG_CHACHA20_POLY1305,
            "kid": keys.kid,
            "tag": handshake_confirmation_tag(
                token=self.manager.token,
                context=context,
                sender_role="pc",
            ),
        })

    async def _handle_mobile_ready(self, message: dict[str, Any]) -> None:
        context = self._context
        keys = self._keys
        if (
            self._peer_authenticated
            or context is None
            or keys is None
            or set(message) != _DIRECT_READY_FIELDS
            or message.get("protocol_version") != WEBRTC_PROTOCOL_VERSION
            or message.get("transport") != "webrtc"
            or message.get("attempt_id") != self.attempt_id
            or message.get("generation") != self.generation
            or message.get("source_role") != "mobile"
            or message.get("destination_role") != "pc"
            or message.get("e2e_version") != E2E_VERSION
            or message.get("alg") != ALG_CHACHA20_POLY1305
            or message.get("kid") != keys.kid
            or not verify_handshake_confirmation(
                token=self.manager.token,
                context=context,
                sender_role="mobile",
                tag=str(message.get("tag") or ""),
            )
        ):
            raise WebRtcTransportError("mobile E2E identity confirmation failed")
        lease = self.manager._coordinator.try_acquire("webrtc", self)
        if lease is None:
            raise WebRtcTransportError("application transport busy")
        local_ws = None
        try:
            local_ws = await self.manager._connect_local(self.manager.token)
        except Exception:
            lease.release()
            raise
        self.lease = lease
        self.local_ws = local_ws
        self._peer_authenticated = True
        await self._send_encrypted(
            make_signal(
                "webrtc_direct_ready",
                attempt_id=self.attempt_id,
                generation=self.generation,
                source_role="pc",
                sequence=self.next_signal_sequence(),
                clock=self.manager._clock,
            )
        )

    async def _handle_encrypted(self, envelope: dict[str, Any]) -> None:
        keys = self._keys
        if (
            keys is None
            or not self._peer_authenticated
            or set(envelope) != _DIRECT_E2E_FIELDS
            or envelope.get("pair_id") != self._direct_pair_id
            or envelope.get("role") != "mobile"
        ):
            raise WebRtcTransportError("encrypted frame before direct authentication")
        frame = decrypt_frame(
            envelope,
            keys=keys,
            direction=MOBILE_TO_PC,
            replay_guard=self._replay_guard,
            replay_key="mobile",
        )
        if frame.get("event_type") == "webrtc_direct_commit":
            signal = validate_signal(
                frame,
                destination_role="pc",
                clock=self.manager._clock,
            )
            if (
                signal["attempt_id"] != self.attempt_id
                or signal["generation"] != self.generation
                or self.state is not WebRtcSessionState.AUTHENTICATING
            ):
                raise WebRtcTransportError("invalid direct commit")
            await self._send_encrypted(
                make_signal(
                    "webrtc_direct_commit_ack",
                    attempt_id=self.attempt_id,
                    generation=self.generation,
                    source_role="pc",
                    sequence=self.next_signal_sequence(),
                    clock=self.manager._clock,
                )
            )
            self._set_state(WebRtcSessionState.READY)
            self._local_reader_task = asyncio.create_task(self._local_to_direct())
            summary = self.selected_candidate_summary()
            print(
                f"[OSIA WebRTC] direct ready attempt={self.attempt_id[:8]} "
                f"generation={self.generation} local_type={summary['local_type']} "
                f"remote_type={summary['remote_type']} protocol={summary['protocol']} "
                f"direct={summary['direct']} lease=acquired bridge=1"
            )
            return
        if not self.active:
            raise WebRtcTransportError("application frame before direct commit")
        if frame.get("type") == "auth":
            raise WebRtcTransportError("auth frame is forbidden on direct transport")
        self._observe_mobile_frame(frame)
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_APP_FRAME_BYTES:
            raise WebRtcTransportError("application frame too large")
        await self.local_ws.send(encoded)

    async def _local_to_direct(self) -> None:
        local_ws = self.local_ws
        try:
            async for raw in local_ws:
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_FRAME_BYTES:
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                self._observe_local_frame(frame)
                await self._send_encrypted(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                await self.fail("bridge_unavailable")

    def _observe_mobile_frame(self, frame: Mapping[str, Any]) -> None:
        frame_type = str(frame.get("type") or "")
        starts_turn = frame_type in {"text", "audio", "speak"} or (
            frame_type == "file" and frame.get("is_image") is True
        )
        if not starts_turn:
            return
        request_id = str(frame.get("request_id") or frame.get("client_request_id") or "")
        turn_id = str(frame.get("turn_id") or "")
        if not request_id:
            raise WebRtcTransportError("application turn lacks correlation")
        if self.state is WebRtcSessionState.READY:
            self._active_request_id = request_id
            self._active_turn_id = turn_id
            self._set_state(WebRtcSessionState.ACTIVE_TURN)
            return
        if (
            self.state is not WebRtcSessionState.ACTIVE_TURN
            or request_id != self._active_request_id
            or (turn_id and self._active_turn_id and turn_id != self._active_turn_id)
        ):
            raise WebRtcTransportError("concurrent direct turn rejected")

    def _observe_local_frame(self, frame: Mapping[str, Any]) -> None:
        if self.state is not WebRtcSessionState.ACTIVE_TURN:
            return
        request_id = str(frame.get("request_id") or frame.get("client_request_id") or "")
        turn_id = str(frame.get("turn_id") or "")
        if request_id == self._active_request_id and not self._active_turn_id and turn_id:
            self._active_turn_id = turn_id
        same_turn = request_id == self._active_request_id and (
            not self._active_turn_id or not turn_id or turn_id == self._active_turn_id
        )
        terminal_error = frame.get("terminal") is True and frame.get("status") in {
            "error", "cancelled", "canceled", "rejected"
        }
        if same_turn and (terminal_error or frame.get("media_terminal") is True):
            self._active_request_id = ""
            self._active_turn_id = ""
            self._set_state(WebRtcSessionState.READY)

    async def _send_plain(self, message: Mapping[str, Any]) -> None:
        await self._send_serialized(
            json.dumps(message, ensure_ascii=True, separators=(",", ":"))
        )

    async def _send_encrypted(self, frame: Mapping[str, Any]) -> None:
        keys = self._keys
        if keys is None:
            raise WebRtcTransportError("direct E2E keys unavailable")
        encrypted = encrypt_frame(
            frame,
            keys=keys,
            direction=PC_TO_MOBILE,
            pair_id=self._direct_pair_id,
            role="pc",
            priority=relay_frame_priority(frame),
            final=relay_frame_is_final(frame),
            outbound_sequence=self._outbound_sequence,
        )
        envelope = {"type": "E2E_FRAME", **encrypted}
        encoded = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DIRECT_ENVELOPE_BYTES:
            raise WebRtcTransportError("encrypted direct envelope too large")
        await self._send_serialized(encoded)

    async def _send_serialized(self, encoded: str) -> None:
        payload = encoded.encode("utf-8")
        if len(payload) <= DIRECT_CHUNK_BYTES:
            messages = [encoded]
        else:
            count = math.ceil(len(payload) / DIRECT_CHUNK_BYTES)
            if count > MAX_DIRECT_CHUNKS:
                raise WebRtcTransportError("direct message requires too many chunks")
            message_id = secrets.token_hex(16)
            checksum = hashlib.sha256(payload).hexdigest()
            messages = []
            for index in range(count):
                part = payload[index * DIRECT_CHUNK_BYTES : (index + 1) * DIRECT_CHUNK_BYTES]
                messages.append(json.dumps({
                    "type": "WEBRTC_CHUNK",
                    "message_id": message_id,
                    "chunk_index": index,
                    "chunk_count": count,
                    "total_bytes": len(payload),
                    "checksum": checksum,
                    "data": base64.urlsafe_b64encode(part).decode("ascii"),
                }, separators=(",", ":")))
        queued_size = sum(len(message.encode("utf-8")) for message in messages)
        if (
            self._queued_items + len(messages) > MAX_SEND_QUEUE_ITEMS
            or self._queued_bytes + queued_size > MAX_SEND_QUEUE_BYTES
        ):
            raise WebRtcTransportError("direct send queue is full")
        self._queued_items += len(messages)
        self._queued_bytes += queued_size
        try:
            await asyncio.wait_for(
                self._send_messages(messages),
                timeout=min(
                    MAX_SEND_TIMEOUT_SECONDS,
                    SEND_TIMEOUT_SECONDS
                    + len(messages) * SEND_TIMEOUT_PER_CHUNK_SECONDS,
                ),
            )
        finally:
            self._queued_items -= len(messages)
            self._queued_bytes -= queued_size

    async def _send_messages(self, messages: list[str]) -> None:
        async with self._send_lock:
            channel = self.channel
            if channel is None or channel.readyState != "open":
                raise WebRtcTransportError("DataChannel is not open")
            for message in messages:
                while channel.bufferedAmount > DATA_CHANNEL_HIGH_WATER:
                    self._buffer_low.clear()
                    if channel.bufferedAmount <= DATA_CHANNEL_HIGH_WATER:
                        continue
                    await self._buffer_low.wait()
                    if channel.readyState != "open":
                        raise WebRtcTransportError("DataChannel closed during backpressure")
                channel.send(message)
                self._direct_bytes_sent += len(message.encode("utf-8"))

    def selected_candidate_summary(self) -> dict[str, Any]:
        local_type = "unknown"
        remote_type = "unknown"
        protocol = "unknown"
        try:
            connection = self.pc.sctp.transport.transport._connection
            pairs = list(connection._nominated.values())
            if pairs:
                pair = pairs[0]
                local_type = str(pair.local_candidate.type)
                remote_type = str(pair.remote_candidate.type)
                protocol = str(pair.local_candidate.transport).lower()
        except Exception:
            pass
        return {
            "local_type": local_type,
            "remote_type": remote_type,
            "protocol": protocol,
            "direct": (
                local_type in {"host", "srflx", "prflx"}
                and remote_type in {"host", "srflx", "prflx"}
            ),
            "bytes_sent": self._direct_bytes_sent,
            "bytes_received": self._direct_bytes_received,
        }

    async def fail(self, category: str) -> None:
        await self.close(notify=True, category=category)

    async def close(
        self,
        *,
        notify: bool,
        category: str = "cancelled",
    ) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._set_state(WebRtcSessionState.CLOSING)
            current = asyncio.current_task()
            tasks = [task for task in self._tasks if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            reader = self._local_reader_task
            self._local_reader_task = None
            if reader is not None and reader is not current:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            local_ws = self.local_ws
            self.local_ws = None
            if local_ws is not None:
                with suppress(Exception):
                    await local_ws.close()
            self._candidate_summary = self.selected_candidate_summary()
            pc = self.pc
            self.pc = None
            if pc is not None:
                with suppress(Exception):
                    await pc.close()
            lease = self.lease
            self.lease = None
            if lease is not None:
                lease.release()
            self._reassembler.clear()
            self._set_state(WebRtcSessionState.DISCONNECTED)
            summary = self._candidate_summary
            print(
                f"[OSIA WebRTC] cleanup complete attempt={self.attempt_id[:8]} "
                f"generation={self.generation} bytes_sent={summary['bytes_sent']} "
                f"bytes_received={summary['bytes_received']} bridge=0 lease=released"
            )
        await self.manager._session_closed(
            self,
            notify=notify,
            category=category,
        )
