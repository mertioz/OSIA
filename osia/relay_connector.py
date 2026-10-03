"""Outbound OSIA public relay connector.

This process-side bridge keeps the existing local mobile server protocol intact:
it authenticates to ``ws://127.0.0.1:8765/ws`` as a local client, then forwards
JSON frames through the public relay. The relay never receives the raw mobile
token; PC and mobile authenticate to the relay with a token-derived proof.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from osia.config import load_mobile_token
from osia.oauth_relay import (
    TYPE_OAUTH_CALLBACK,
    TYPE_OAUTH_CALLBACK_ACK,
    TYPE_OAUTH_FLOW_REGISTERED,
    coordinator as oauth_relay_coordinator,
)
from osia.stun_health import start_stun_health_probe
from osia.transport_coordinator import (
    acquire_application_transport,
    application_transport_coordination_enabled,
)
from osia.webrtc_signaling import (
    WebRtcSignalingError,
    capability_frame as webrtc_capability_frame,
    load_webrtc_ice_servers,
    webrtc_direct_enabled,
)
from osia.webrtc_transport import WebRtcTransportManager
from osia.relay_crypto import (
    ALG_CHACHA20_POLY1305,
    E2E_VERSION,
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    RelayCryptoError,
    RelayReplayError,
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
    nonce_for_seq,
    relay_frame_is_final,
    relay_frame_priority,
    verify_handshake_confirmation,
    x25519_public_key,
)

DEFAULT_RELAY_URL = "wss://relay.osia.space/ws"
LOCAL_WS_URL = "ws://127.0.0.1:8765/ws"
PROTOCOL_VERSION = 2
MAX_RELAY_FRAME_BYTES = 64 * 1024
# Keep application traffic comfortably below the Worker's hard per-message
# ceiling.  This limit is applied to the final UTF-8 relay JSON, not to the
# local plaintext frame.
MAX_SAFE_RELAY_MESSAGE_BYTES = 48 * 1024
# The trusted loopback server may send a post-turn base64 MP3 much larger than
# one public relay frame.  Keep that receive allowance bounded independently;
# eligible frames are encrypted and chunked before they reach the public relay.
MAX_LOCAL_SERVER_FRAME_BYTES = 16 * 1024 * 1024
MAX_RELAY_TRACE_INCREMENTAL = 6
MAX_RELAY_TRACE_ENVELOPES = 7
MAX_RELAY_TRACE_SNAPSHOT_EVENTS = 8
MAX_RELAY_TRACE_SNAPSHOT_BYTES = 4 * 1024
E2E_CHUNK_VERSION = 1
E2E_CHUNK_CIPHERTEXT_CHARS = 40 * 1024
MAX_E2E_CHUNK_TOTAL_BYTES = 8 * 1024 * 1024
MAX_E2E_CHUNK_COUNT = 256
MAX_E2E_CHUNK_INFLIGHT = 4
E2E_CHUNK_TIMEOUT_SECONDS = 30.0
_MAX_RECENT_CHUNK_TRANSFERS = 64
TYPE_E2E_HELLO = "E2E_HELLO"
TYPE_E2E_READY = "E2E_READY"
TYPE_E2E_FRAME = "E2E_FRAME"
TYPE_E2E_CHUNK = "E2E_CHUNK"
TYPE_TRANSPORT_COMMIT = "transport_commit"
TYPE_TRANSPORT_COMMITTED = "transport_committed"

_CHUNK_PRIORITY_VALUES = {"critical", "normal", "droppable"}
_LEGACY_IDENTITY_FIELDS = {
    "device_id", "session_id", "from", "to", "to_session_id"
}
_CONTROL_FIELDS = {"v", "type", "role", "pair_id", "seq", "ack", "payload"}
_HELLO_PAYLOAD_FIELDS = {
    "e2e_version", "alg", "public_key", "nonce_base", "e2e_chunk_version"
}
_READY_PAYLOAD_FIELDS = {"e2e_version", "alg", "kid", "tag"}
_E2E_FRAME_FIELDS = {
    "v", "type", "role", "pair_id", "seq", "ack", "e2e_version", "alg",
    "kid", "priority", "final", "nonce", "ciphertext"
}
_E2E_CHUNK_FIELDS = {
    "v", "type", "role", "pair_id", "seq", "ack", "e2e_version", "alg",
    "kid", "frame_seq", "priority", "final", "nonce", "chunk_id",
    "chunk_index", "chunk_count", "total_bytes", "ciphertext_chunk"
}
_CHUNK_SIGNATURE_FIELDS = (
    "role",
    "pair_id",
    "e2e_version",
    "alg",
    "kid",
    "frame_seq",
    "priority",
    "final",
    "nonce",
    "chunk_id",
    "chunk_count",
    "total_bytes",
)

_started = False
_started_lock = threading.Lock()


@dataclass
class _ChunkTransfer:
    signature: tuple
    metadata: dict
    chunk_id: str
    chunk_count: int
    total_bytes: int
    created_at: float
    chunks: dict[int, str] = field(default_factory=dict)
    chunk_hashes: dict[int, bytes] = field(default_factory=dict)
    outer_sequences: dict[int, int] = field(default_factory=dict)
    received_bytes: int = 0


@dataclass(frozen=True)
class _ChunkTombstone:
    signature: tuple
    chunk_hashes: dict[int, bytes]
    outer_sequences: dict[int, int]


def relay_pair_credentials(token: str) -> tuple[str, str]:
    """Return ``(pair_id, secret)`` from the mobile token without exposing it."""
    secret = hmac.new(
        token.encode("utf-8"),
        b"osia-relay-secret-v1",
        hashlib.sha256,
    ).hexdigest()
    pair_id = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return pair_id, secret


class RelayConfigurationError(RuntimeError):
    """A fail-closed relay configuration/protocol error."""


class RelaySessionSuperseded(RuntimeError):
    """The Worker replaced this PC socket with a newer PC connection."""


def build_relay_endpoint(base_url: str, pair_id: str) -> str:
    """Build the canonical v2 URL without retaining base URL metadata."""

    if "?" in base_url or "#" in base_url:
        raise RelayConfigurationError(
            "relay URL must not contain a query string or fragment"
        )
    try:
        parsed = urlsplit(base_url)
        parsed.port  # Validate malformed and out-of-range ports.
    except ValueError as exc:
        raise RelayConfigurationError("invalid relay URL") from exc
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/ws"}
        or not re.fullmatch(r"[0-9a-f]{64}", pair_id)
    ):
        raise RelayConfigurationError(
            "relay URL must be a credential-free ws/wss base without query or fragment"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "/ws", f"pair_id={pair_id}", ""))


def _relay_enabled() -> bool:
    raw = os.environ.get("OSIA_RELAY_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _relay_url() -> str:
    return os.environ.get("OSIA_RELAY_URL", DEFAULT_RELAY_URL).strip() or DEFAULT_RELAY_URL


def _relay_allow_plaintext(relay_url: str) -> bool:
    raw = os.environ.get("OSIA_RELAY_ALLOW_PLAINTEXT", "0").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return False
    mode = os.environ.get("OSIA_RELAY_MODE", "public").strip().lower()
    if mode not in {"dev", "private", "local"}:
        return False
    try:
        parsed = urlsplit(relay_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"ws", "wss"}
        and bool(host)
        and host != "relay.osia.space"
    )


def _safe_frame_type(frame) -> str:
    if isinstance(frame, dict):
        return str(frame.get("type") or "")
    return ""


class RelayBridge:
    def __init__(self, token: str, relay_url: str) -> None:
        self.token = token
        self.relay_url = relay_url
        self.pair_id, self.secret = relay_pair_credentials(token)
        self.seq = 0
        self._terminal_error_code = ""
        self.allow_plaintext = _relay_allow_plaintext(relay_url)
        self._transport_coordination_enabled = (
            application_transport_coordination_enabled()
        )
        self._reset_relay_chunk_capabilities()
        self._reset_e2e_state()
        self._relay_send_lock = asyncio.Lock()
        self._active_relay_ws = None
        self._relay_loop: asyncio.AbstractEventLoop | None = None
        self._oauth_authoritative = False
        self._local_ws = None
        self._local_reader_task: asyncio.Task | None = None
        self._application_transport_lease = None
        try:
            webrtc_enabled = webrtc_direct_enabled()
            webrtc_ice_servers = load_webrtc_ice_servers()
        except WebRtcSignalingError:
            print("[OSIA WebRTC] disabled: invalid ICE configuration")
            webrtc_enabled = False
            webrtc_ice_servers = []
        self._webrtc_manager = WebRtcTransportManager(
            token=token,
            pair_id=self.pair_id,
            enabled=webrtc_enabled,
            ice_servers=webrtc_ice_servers,
            send_signal=self._send_webrtc_signal,
        )

    def _reset_e2e_state(self) -> None:
        self.e2e_private_key = generate_x25519_private_key()
        self.e2e_public_key = x25519_public_key(self.e2e_private_key)
        self.e2e_nonce_base = generate_nonce_base()
        self.e2e_context: RelaySessionContext | None = None
        self.e2e_keys = None
        self.e2e_initial_hello_sent = False
        self.e2e_peer_hello_fingerprint: tuple[bytes, bytes] | None = None
        self.e2e_hello_sent_for_peer: tuple[bytes, bytes] | None = None
        self.e2e_ready_sent = False
        self.e2e_peer_ready = False
        self.e2e_pc_ready_sent = False
        self.lan_provisioning_sent = False
        self.webrtc_capabilities_sent = False
        self.e2e_peer_chunk_version = 0
        self.pending_e2e_ready: tuple[dict, dict] | None = None
        self.e2e_replay_guard = SequenceReplayGuard()
        self.mobile_ready = False
        self._relay_commit_attempt_id: str | None = None
        self._chunk_transfers: dict[tuple, _ChunkTransfer] = {}
        self._chunk_tombstones: dict[tuple, _ChunkTombstone] = {}
        self._chunk_tombstone_order: deque[tuple] = deque()
        self._outbound_chunk_ids: dict[str, float] = {}
        self._outbound_chunk_order: deque[str] = deque()
        self._relay_trace_state: dict[tuple[str, str, str, str], dict] = {}
        self._relay_trace_tombstones: set[tuple[str, str, str, str]] = set()
        self._relay_trace_tombstone_order: deque[
            tuple[str, str, str, str]
        ] = deque()

    @staticmethod
    def _execution_trace_key(
        frame: dict,
    ) -> tuple[str, str, str, str] | None:
        if (
            frame.get("type") != "execution_trace"
            or frame.get("schema_version") != 1
            or frame.get("redaction_class") != "public_ui"
        ):
            return None
        legacy_values = tuple(
            str(frame.get(name) or "")
            for name in ("conversation_id", "request_id", "turn_id")
        )
        if any(not value or len(value) > 256 for value in legacy_values):
            return None
        run_id = str(frame.get("run_id") or legacy_values[2])
        if not run_id or len(run_id) > 256:
            return None
        return (*legacy_values, run_id)

    def _remember_relay_trace_tombstone(
        self,
        key: tuple[str, str, str, str],
    ) -> None:
        if key in self._relay_trace_tombstones:
            return
        while len(self._relay_trace_tombstone_order) >= 64:
            expired = self._relay_trace_tombstone_order.popleft()
            self._relay_trace_tombstones.discard(expired)
        self._relay_trace_tombstones.add(key)
        self._relay_trace_tombstone_order.append(key)

    @staticmethod
    def _compact_relay_trace_snapshot(frame: dict) -> dict | None:
        snapshot = frame.get("snapshot")
        if not isinstance(snapshot, list):
            snapshot = []
        priority = {
            "terminal": 0,
            "goal": 1,
            "public_reasoning_summary": 2,
            "waiting_permission": 3,
            "reconciliation": 4,
            "result": 5,
            "continuation": 5,
            "action": 6,
        }
        valid = [
            dict(item) for item in snapshot
            if isinstance(item, dict)
            and item.get("redaction_class") == "public_ui"
            and item.get("conversation_id") == frame.get("conversation_id")
            and item.get("request_id") == frame.get("request_id")
            and item.get("turn_id") == frame.get("turn_id")
            and item.get("kind") in priority
        ]
        selected = sorted(
            valid,
            key=lambda item: (
                priority[str(item.get("kind"))],
                -int(item.get("sequence") or 0),
            ),
        )[:MAX_RELAY_TRACE_SNAPSHOT_EVENTS]
        selected.sort(key=lambda item: int(item.get("sequence") or 0))
        compact = dict(frame)
        compact["snapshot"] = selected
        while selected:
            size = len(json.dumps(
                compact, ensure_ascii=True, separators=(",", ":"),
            ).encode("utf-8"))
            if size <= MAX_RELAY_TRACE_SNAPSHOT_BYTES:
                return compact
            removable = max(
                range(len(selected)),
                key=lambda index: (
                    priority[str(selected[index].get("kind"))],
                    -int(selected[index].get("sequence") or 0),
                ),
            )
            selected.pop(removable)
        if len(json.dumps(
            compact, ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")) <= MAX_RELAY_TRACE_SNAPSHOT_BYTES:
            return compact
        return None

    def _relay_execution_trace_frame(self, frame: dict) -> dict | None:
        """Apply the optional <=6 incremental + one terminal Relay budget."""

        key = self._execution_trace_key(frame)
        if key is None or key in self._relay_trace_tombstones:
            return None
        from osia.execution_trace import validate_persisted_trace

        validated = validate_persisted_trace({
            "schema_version": frame.get("schema_version"),
            "conversation_id": frame.get("conversation_id"),
            "request_id": frame.get("request_id"),
            "turn_id": frame.get("turn_id"),
            "redaction_class": frame.get("redaction_class"),
            "events": frame.get("snapshot"),
        })
        if validated is None:
            return None
        matching = next((
            event for event in validated["events"]
            if event.get("event_id") == frame.get("event_id")
            and event.get("revision") == frame.get("revision")
        ), None)
        if (
            matching is None
            or set(frame) != set(matching) | {"type", "snapshot"}
            or any(frame.get(name) != value for name, value in matching.items())
        ):
            return None
        frame = dict(frame)
        frame["snapshot"] = validated["events"]
        if key not in self._relay_trace_state and len(self._relay_trace_state) >= 64:
            evicted = next(iter(self._relay_trace_state))
            self._relay_trace_state.pop(evicted)
            self._remember_relay_trace_tombstone(evicted)
        state = self._relay_trace_state.setdefault(
            key,
            {"incremental": 0, "signatures": set(), "envelopes": 0},
        )
        terminal = frame.get("terminal") is True and frame.get("kind") == "terminal"
        if terminal:
            if state["envelopes"] >= MAX_RELAY_TRACE_ENVELOPES:
                return None
            compact = self._compact_relay_trace_snapshot(frame)
            self._relay_trace_state.pop(key, None)
            self._remember_relay_trace_tombstone(key)
            if compact is None:
                return None
            state["envelopes"] += 1
            return compact
        if frame.get("terminal") is True or state["incremental"] >= MAX_RELAY_TRACE_INCREMENTAL:
            return None
        display = frame.get("display")
        signature = (
            str(frame.get("kind") or ""),
            str(display.get("key") or "") if isinstance(display, dict) else "",
        )
        if not signature[0] or not signature[1] or signature in state["signatures"]:
            return None
        state["signatures"].add(signature)
        state["incremental"] += 1
        state["envelopes"] += 1
        return frame

    def _reset_relay_chunk_capabilities(self) -> None:
        self.relay_chunk_version = 0
        self.relay_chunk_max_frame_bytes = 0
        self.relay_chunk_max_ciphertext_chars = 0
        self.relay_chunk_max_total_bytes = 0
        self.relay_chunk_max_count = 0

    def _begin_relay_connection(self) -> None:
        self.seq = 0
        self._terminal_error_code = ""
        self._oauth_authoritative = False
        self._reset_relay_chunk_capabilities()
        self._reset_e2e_state()

    @staticmethod
    def _bounded_chunk_limit(value, ceiling: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return 0
        return min(value, ceiling)

    def _set_relay_chunk_capabilities(self, payload: dict) -> None:
        self._reset_relay_chunk_capabilities()
        version = payload.get("e2e_chunk_version")
        if version != E2E_CHUNK_VERSION or isinstance(version, bool):
            return
        self.relay_chunk_version = E2E_CHUNK_VERSION
        self.relay_chunk_max_frame_bytes = self._bounded_chunk_limit(
            payload.get("e2e_chunk_max_frame_bytes"),
            MAX_SAFE_RELAY_MESSAGE_BYTES,
        )
        self.relay_chunk_max_ciphertext_chars = self._bounded_chunk_limit(
            payload.get("e2e_chunk_max_ciphertext_chars"),
            E2E_CHUNK_CIPHERTEXT_CHARS,
        )
        self.relay_chunk_max_total_bytes = self._bounded_chunk_limit(
            payload.get("e2e_chunk_max_total_bytes"),
            MAX_E2E_CHUNK_TOTAL_BYTES,
        )
        self.relay_chunk_max_count = self._bounded_chunk_limit(
            payload.get("e2e_chunk_max_count"),
            MAX_E2E_CHUNK_COUNT,
        )
        if not self._relay_chunk_limits_ready():
            self._reset_relay_chunk_capabilities()

    def _relay_chunk_limits_ready(self) -> bool:
        return (
            self.relay_chunk_version == E2E_CHUNK_VERSION
            and self.relay_chunk_max_frame_bytes == MAX_SAFE_RELAY_MESSAGE_BYTES
            and self.relay_chunk_max_ciphertext_chars
            == E2E_CHUNK_CIPHERTEXT_CHARS
            and self.relay_chunk_max_total_bytes == MAX_E2E_CHUNK_TOTAL_BYTES
            and self.relay_chunk_max_count == MAX_E2E_CHUNK_COUNT
        )

    def _chunking_available(self) -> bool:
        return (
            self._relay_chunk_limits_ready()
            and self.e2e_peer_chunk_version == E2E_CHUNK_VERSION
        )

    async def _prepare_mobile_session(self, msg: dict, *, ready: bool) -> bool:
        if (
            msg.get("v") != PROTOCOL_VERSION
            or str(msg.get("role") or "") != "mobile"
            or str(msg.get("pair_id") or "") != self.pair_id
            or any(field in msg for field in _LEGACY_IDENTITY_FIELDS)
        ):
            return False
        if ready:
            self.mobile_ready = True
        return True

    def _relay_endpoint(self) -> str:
        return build_relay_endpoint(self.relay_url, self.pair_id)

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _envelope(self, msg_type: str, payload: dict | None = None) -> dict:
        return {
            "v": PROTOCOL_VERSION,
            "type": msg_type,
            "role": "pc",
            "pair_id": self.pair_id,
            "seq": self._next_seq(),
            "ack": 0,
            "payload": payload or {},
        }

    @staticmethod
    def _serialize_relay_message(msg: dict) -> str:
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))

    async def _send_relay(self, relay_ws, msg_type: str, payload: dict | None = None) -> None:
        # Allocate relay sequences while holding the same lock used for the
        # network send.  Chunk series use this invariant to remain contiguous.
        async with self._relay_send_lock:
            encoded = self._serialize_relay_message(
                self._envelope(msg_type, payload)
            )
            await relay_ws.send(encoded)

    def send_oauth_control(self, msg_type: str, payload: dict) -> None:
        """Thread-safe bridge from process-local OAuth owners to relay v2."""
        loop = self._relay_loop
        relay_ws = self._active_relay_ws
        if (
            loop is None
            or loop.is_closed()
            or relay_ws is None
            or not self._oauth_authoritative
        ):
            raise RuntimeError("oauth relay unavailable")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("oauth relay control cannot block its own loop")
        future = asyncio.run_coroutine_threadsafe(
            self._send_relay(relay_ws, msg_type, payload), loop
        )
        future.result(timeout=2.0)

    async def _send_e2e_hello(
        self,
        relay_ws,
        *,
        peer_fingerprint: tuple[bytes, bytes] | None = None,
    ) -> None:
        if peer_fingerprint is None:
            if self.e2e_initial_hello_sent:
                return
        elif self.e2e_hello_sent_for_peer == peer_fingerprint:
            return
        await self._send_relay(relay_ws, TYPE_E2E_HELLO, {
            "e2e_version": E2E_VERSION,
            "alg": ALG_CHACHA20_POLY1305,
            "public_key": b64url_encode(self.e2e_public_key),
            "nonce_base": b64url_encode(self.e2e_nonce_base),
            "e2e_chunk_version": E2E_CHUNK_VERSION,
        })
        if peer_fingerprint is None:
            self.e2e_initial_hello_sent = True
        else:
            self.e2e_hello_sent_for_peer = peer_fingerprint

    async def _handle_e2e_hello(self, relay_ws, msg: dict, payload: dict) -> None:
        try:
            if (
                set(msg) != _CONTROL_FIELDS
                or msg.get("type") != TYPE_E2E_HELLO
                or msg.get("payload") != payload
                or set(payload) != _HELLO_PAYLOAD_FIELDS
                or payload.get("e2e_version") != E2E_VERSION
                or payload.get("alg") != ALG_CHACHA20_POLY1305
            ):
                raise ValueError("invalid v2 E2E hello shape")
            mobile_public_key = b64url_decode(str(payload.get("public_key") or ""))
            mobile_nonce_base = b64url_decode(str(payload.get("nonce_base") or ""))
            if len(mobile_public_key) != 32 or len(mobile_nonce_base) != 4:
                raise ValueError("invalid mobile E2E key material")
            if not await self._prepare_mobile_session(msg, ready=False):
                return
            peer_fingerprint = (mobile_public_key, mobile_nonce_base)
            mobile_chunk_version = payload.get("e2e_chunk_version")
            if (
                mobile_chunk_version != E2E_CHUNK_VERSION
                or isinstance(mobile_chunk_version, bool)
            ):
                mobile_chunk_version = 0

            current = self.e2e_context
            if current is not None:
                if self.e2e_peer_hello_fingerprint != peer_fingerprint:
                    await self._replace_mobile_crypto_context()
                    current = None
                    print("[OSIA Relay] authoritative mobile E2E peer replaced")
                else:
                    return

            # The first hello may have been sent while no mobile peer existed.
            # Resend it now that this authenticated mobile session is routable.
            self.e2e_peer_hello_fingerprint = peer_fingerprint
            await self._send_e2e_hello(
                relay_ws,
                peer_fingerprint=peer_fingerprint,
            )
            context = RelaySessionContext(
                pair_id=self.pair_id,
                pc_public_key=self.e2e_public_key,
                mobile_public_key=mobile_public_key,
                pc_nonce_base=self.e2e_nonce_base,
                mobile_nonce_base=mobile_nonce_base,
            )
            keys = derive_session_keys(
                token=self.token,
                local_private_key=self.e2e_private_key,
                peer_public_key=mobile_public_key,
                context=context,
            )
        except Exception as exc:
            print(f"[OSIA Relay] E2E hello rejected: {type(exc).__name__}")
            await relay_ws.close()
            return

        self.e2e_context = context
        self.e2e_keys = keys
        self.e2e_peer_chunk_version = mobile_chunk_version
        await self._send_e2e_ready(relay_ws)
        pending_ready = self.pending_e2e_ready
        if pending_ready is not None:
            self.pending_e2e_ready = None
            await self._handle_e2e_ready(relay_ws, *pending_ready)
        await self._maybe_start_e2e_bridge(relay_ws)

    async def _replace_mobile_crypto_context(self) -> None:
        """Discard only peer-bound state; keep this socket's seq/key material."""

        await self._close_local()
        # A replacement peer invalidates the old WebRTC E2E session too. Close
        # it before the new peer can commit relay, so its application lease is
        # released at this peer-replacement boundary.
        await self._webrtc_manager.close()
        self._webrtc_manager.reset_signaling_epoch()
        self.e2e_context = None
        self.e2e_keys = None
        self.e2e_peer_hello_fingerprint = None
        self.e2e_hello_sent_for_peer = None
        self.e2e_ready_sent = False
        self.e2e_peer_ready = False
        self.e2e_pc_ready_sent = False
        self.lan_provisioning_sent = False
        self.webrtc_capabilities_sent = False
        self.e2e_peer_chunk_version = 0
        self.pending_e2e_ready = None
        self.e2e_replay_guard = SequenceReplayGuard()
        self.mobile_ready = False
        self._relay_commit_attempt_id = None
        self._chunk_transfers.clear()
        self._chunk_tombstones.clear()
        self._chunk_tombstone_order.clear()
        self._outbound_chunk_ids.clear()
        self._outbound_chunk_order.clear()

    async def _send_e2e_ready(self, relay_ws) -> None:
        if (
            self.e2e_ready_sent
            or self.e2e_context is None
            or self.e2e_keys is None
        ):
            return
        await self._send_relay(relay_ws, TYPE_E2E_READY, {
            "e2e_version": E2E_VERSION,
            "alg": ALG_CHACHA20_POLY1305,
            "kid": self.e2e_keys.kid,
            "tag": handshake_confirmation_tag(
                token=self.token,
                context=self.e2e_context,
                sender_role="pc",
            ),
        })
        self.e2e_ready_sent = True

    async def _handle_e2e_ready(self, relay_ws, msg: dict, payload: dict) -> None:
        if (
            msg.get("v") != PROTOCOL_VERSION
            or str(msg.get("role") or "") != "mobile"
            or str(msg.get("pair_id") or "") != self.pair_id
            or any(field in msg for field in _LEGACY_IDENTITY_FIELDS)
            or set(msg) != _CONTROL_FIELDS
            or msg.get("type") != TYPE_E2E_READY
            or msg.get("payload") != payload
            or set(payload) != _READY_PAYLOAD_FIELDS
            or payload.get("e2e_version") != E2E_VERSION
            or payload.get("alg") != ALG_CHACHA20_POLY1305
        ):
            return
        context = self.e2e_context
        keys = self.e2e_keys
        if context is None or keys is None:
            self.pending_e2e_ready = (dict(msg), dict(payload))
            return
        if str(payload.get("kid") or "") != keys.kid:
            print("[OSIA Relay] E2E auth mismatch: key id")
            await relay_ws.close()
            return
        tag = str(payload.get("tag") or "")
        if not verify_handshake_confirmation(
            token=self.token,
            context=context,
            sender_role="mobile",
            tag=tag,
        ):
            print("[OSIA Relay] E2E auth mismatch: confirmation tag")
            await relay_ws.close()
            return
        if self.e2e_peer_ready:
            return
        self.e2e_peer_ready = True
        await self._maybe_start_e2e_bridge(relay_ws)

    async def _maybe_start_e2e_bridge(self, relay_ws) -> None:
        # A verified E2E_READY is stronger than the routable MOBILE_READY hint,
        # which may have been sent before this PC joined the relay session.
        if not self.e2e_peer_ready or self.e2e_keys is None:
            return
        if self.e2e_pc_ready_sent:
            return
        if self._transport_coordination_enabled:
            # Remain E2E-authenticated but application-idle until mobile has
            # completed its LAN-first attempt and explicitly commits to relay.
            await self._send_relay(
                relay_ws,
                "PC_READY",
                {"e2e": True, "standby": True},
            )
            try:
                from osia.lan_gateway import lan_provisioning_snapshot

                provisioning = lan_provisioning_snapshot()
            except Exception:
                provisioning = None
            if provisioning is not None and not self.lan_provisioning_sent:
                alias = str(provisioning.get("instance_alias") or "")
                pin = str(provisioning.get("certificate_sha256") or "")
                if (
                    re.fullmatch(r"[0-9a-f]{16}", alias)
                    and re.fullmatch(r"[0-9a-f]{64}", pin)
                    and provisioning.get("protocol_version") == 1
                    and provisioning.get("port") == 8766
                    and provisioning.get("path") == "/lan"
                ):
                    sent = await self._send_e2e_frame(
                        relay_ws,
                        {"type": "lan_provisioning", **provisioning},
                        priority="critical",
                        final=False,
                    )
                    self.lan_provisioning_sent = bool(sent)
            if not self.webrtc_capabilities_sent:
                sent = await self._send_e2e_frame(
                    relay_ws,
                    webrtc_capability_frame(
                        self._webrtc_manager.enabled,
                        self._webrtc_manager.ice_servers,
                    ),
                    priority="critical",
                    final=False,
                )
                self.webrtc_capabilities_sent = bool(sent)
        else:
            # Preserve the established relay-v2 path byte-for-byte in behavior
            # when LAN-direct coordination is disabled.
            if self._local_ws is None:
                await self._connect_local(relay_ws)
            await self._send_relay(relay_ws, "PC_READY", {"e2e": True})
        self.e2e_pc_ready_sent = True

    async def _handle_transport_commit(self, relay_ws, frame: dict) -> None:
        if (
            set(frame) != {"type", "transport", "attempt_id"}
            or frame.get("type") != TYPE_TRANSPORT_COMMIT
            or frame.get("transport") != "relay"
        ):
            await relay_ws.close()
            return
        attempt_id = str(frame.get("attempt_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            await relay_ws.close()
            return
        current = self._relay_commit_attempt_id
        if current is not None and current != attempt_id:
            await relay_ws.close()
            return
        if self._local_ws is None:
            await self._connect_local(relay_ws, start_reader=False)
        self._relay_commit_attempt_id = attempt_id
        await self._send_e2e_frame(
            relay_ws,
            {
                "type": TYPE_TRANSPORT_COMMITTED,
                "transport": "relay",
                "attempt_id": attempt_id,
            },
            priority="critical",
            final=False,
        )
        local_ws = self._local_ws
        if local_ws is not None and self._local_reader_task is None:
            self._local_reader_task = asyncio.create_task(
                self._local_to_relay(local_ws, relay_ws)
            )

    async def _send_webrtc_signal(self, frame: dict) -> bool:
        relay_ws = self._active_relay_ws
        if relay_ws is None or not self.e2e_peer_ready or self.e2e_keys is None:
            return False
        return bool(
            await self._send_e2e_frame(
                relay_ws,
                frame,
                priority="critical",
                final=bool(frame.get("terminal")),
            )
        )

    async def _connect_local(self, relay_ws, *, start_reader: bool = True) -> None:
        if self._local_ws is not None:
            return
        import websockets

        lease = acquire_application_transport("relay", self)
        if lease is None:
            raise RuntimeError("another application transport owns the local bridge")
        local_ws = None
        try:
            local_ws = await websockets.connect(
                LOCAL_WS_URL,
                max_size=MAX_LOCAL_SERVER_FRAME_BYTES,
                ping_interval=20,
                ping_timeout=20,
            )
            await local_ws.send(json.dumps({"type": "auth", "token": self.token}))
            self._local_ws = local_ws
            self._application_transport_lease = lease
            if start_reader:
                self._local_reader_task = asyncio.create_task(
                    self._local_to_relay(local_ws, relay_ws)
                )
            print("[OSIA Relay] local OSIA websocket connected")
        except Exception:
            if local_ws is not None:
                try:
                    await local_ws.close()
                except Exception:
                    pass
            lease.release()
            raise

    async def _close_local(self) -> None:
        local_ws = self._local_ws
        self._local_ws = None
        task = self._local_reader_task
        self._local_reader_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if local_ws is not None:
            try:
                await local_ws.close()
            except Exception:
                pass
        lease = self._application_transport_lease
        self._application_transport_lease = None
        if lease is not None:
            lease.release()

    async def _local_to_relay(self, local_ws, relay_ws) -> None:
        try:
            async for raw in local_ws:
                if not isinstance(raw, str):
                    continue
                if len(raw.encode("utf-8")) > MAX_LOCAL_SERVER_FRAME_BYTES:
                    print("[OSIA Relay] dropped oversized local frame")
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "execution_trace":
                    frame = self._relay_execution_trace_frame(frame)
                    if frame is None:
                        continue
                await self._send_e2e_frame(relay_ws, frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[OSIA Relay] local reader stopped: {exc}")
        finally:
            if self._local_ws is local_ws:
                self._local_ws = None
                lease = self._application_transport_lease
                self._application_transport_lease = None
                if lease is not None:
                    lease.release()
                print("[OSIA Relay] local bridge ended; reconnecting relay")
                try:
                    await relay_ws.close()
                except Exception:
                    pass

    async def _send_e2e_frame(
        self,
        relay_ws,
        frame: dict,
        *,
        priority: str | None = None,
        final: bool | None = None,
    ) -> bool:
        keys = self.e2e_keys
        if keys is None or not self.e2e_peer_ready:
            print("[OSIA Relay] dropped local frame before E2E was ready")
            return False
        async with self._relay_send_lock:
            return await self._send_e2e_frame_locked(
                relay_ws,
                frame,
                priority=priority,
                final=final,
                allow_chunking=True,
            )

    async def _send_e2e_frame_locked(
        self,
        relay_ws,
        frame: dict,
        *,
        priority: str | None,
        final: bool | None,
        allow_chunking: bool,
    ) -> bool:
        """Encrypt and send one app frame while ``_relay_send_lock`` is held."""
        keys = self.e2e_keys
        if keys is None:
            return False
        frame_seq = self._next_seq()
        frame_priority = priority or relay_frame_priority(frame)
        frame_final = relay_frame_is_final(frame) if final is None else final
        encrypted = encrypt_frame(
            frame,
            keys=keys,
            direction=PC_TO_MOBILE,
            pair_id=self.pair_id,
            role="pc",
            priority=frame_priority,
            final=frame_final,
            seq=frame_seq,
        )
        msg = {
            "v": PROTOCOL_VERSION,
            "type": TYPE_E2E_FRAME,
            "role": "pc",
            "pair_id": self.pair_id,
            "seq": frame_seq,
            "ack": 0,
            **encrypted,
        }
        encoded = self._serialize_relay_message(msg)
        if len(encoded.encode("utf-8")) <= MAX_SAFE_RELAY_MESSAGE_BYTES:
            await relay_ws.send(encoded)
            return True

        if not allow_chunking:
            print("[OSIA Relay] encrypted control exceeded safe relay size")
            return False

        chunk_id = b64url_encode(secrets.token_bytes(16))
        ciphertext = str(encrypted["ciphertext"])
        if not self._chunking_available():
            print(
                "[OSIA Relay] oversized encrypted frame requires chunk-capable "
                "relay and peer"
            )
            await self._send_chunk_error_locked(
                relay_ws,
                chunk_id,
                "unsupported_chunk",
            )
            return False

        total_bytes = len(ciphertext)
        if total_bytes > self.relay_chunk_max_total_bytes:
            print(
                "[OSIA Relay] encrypted frame exceeds bounded chunk transfer cap "
                f"({total_bytes} bytes)"
            )
            await self._send_chunk_error_locked(
                relay_ws,
                chunk_id,
                "frame_too_large",
            )
            return False

        pieces = [
            ciphertext[offset : offset + E2E_CHUNK_CIPHERTEXT_CHARS]
            for offset in range(0, total_bytes, E2E_CHUNK_CIPHERTEXT_CHARS)
        ]
        if not pieces or len(pieces) > self.relay_chunk_max_count:
            print("[OSIA Relay] encrypted frame requires too many relay chunks")
            await self._send_chunk_error_locked(
                relay_ws,
                chunk_id,
                "frame_too_large",
            )
            return False

        encoded_chunks: list[str] = []
        chunk_count = len(pieces)
        for chunk_index, ciphertext_chunk in enumerate(pieces):
            outer_seq = frame_seq if chunk_index == 0 else self._next_seq()
            chunk_msg = {
                "v": PROTOCOL_VERSION,
                "type": TYPE_E2E_CHUNK,
                "role": "pc",
                "pair_id": self.pair_id,
                "seq": outer_seq,
                "ack": 0,
                "e2e_version": encrypted["e2e_version"],
                "alg": encrypted["alg"],
                "kid": encrypted["kid"],
                "frame_seq": frame_seq,
                "priority": encrypted["priority"],
                "final": encrypted["final"],
                "nonce": encrypted["nonce"],
                "chunk_id": chunk_id,
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
                "total_bytes": total_bytes,
                "ciphertext_chunk": ciphertext_chunk,
            }
            chunk_encoded = self._serialize_relay_message(chunk_msg)
            if (
                len(chunk_encoded.encode("utf-8"))
                > self.relay_chunk_max_frame_bytes
            ):
                print("[OSIA Relay] serialized chunk exceeds safe relay size")
                await self._send_chunk_error_locked(
                    relay_ws,
                    chunk_id,
                    "frame_too_large",
                )
                return False
            encoded_chunks.append(chunk_encoded)

        self._remember_outbound_chunk(chunk_id)
        print(
            f"[OSIA Relay] sending E2E_CHUNK role=pc chunks={chunk_count} "
            f"total={total_bytes} priority={frame_priority} final={frame_final} "
            f"chunk={chunk_id[:8]}"
        )
        for chunk_encoded in encoded_chunks:
            await relay_ws.send(chunk_encoded)
        return True

    async def _send_chunk_error_locked(
        self,
        relay_ws,
        chunk_id: str,
        code: str,
    ) -> bool:
        return await self._send_e2e_frame_locked(
            relay_ws,
            {
                "type": "relay_chunk_error",
                "chunk_id": chunk_id,
                "code": code,
            },
            priority="critical",
            final=False,
            allow_chunking=False,
        )

    async def _send_chunk_error(
        self,
        relay_ws,
        chunk_id: str,
        code: str,
    ) -> bool:
        if self.e2e_keys is None or not self.e2e_peer_ready:
            return False
        async with self._relay_send_lock:
            return await self._send_chunk_error_locked(relay_ws, chunk_id, code)

    def _remember_outbound_chunk(self, chunk_id: str) -> None:
        now = time.monotonic()
        self._prune_outbound_chunks(now)
        while len(self._outbound_chunk_ids) >= _MAX_RECENT_CHUNK_TRANSFERS:
            oldest = self._outbound_chunk_order.popleft()
            self._outbound_chunk_ids.pop(oldest, None)
        self._outbound_chunk_ids[chunk_id] = (
            now + E2E_CHUNK_TIMEOUT_SECONDS * 4
        )
        self._outbound_chunk_order.append(chunk_id)

    def _prune_outbound_chunks(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        while self._outbound_chunk_order:
            chunk_id = self._outbound_chunk_order[0]
            expires_at = self._outbound_chunk_ids.get(chunk_id)
            if expires_at is not None and expires_at > current:
                break
            self._outbound_chunk_order.popleft()
            self._outbound_chunk_ids.pop(chunk_id, None)

    def _discard_outbound_chunk(self, chunk_id: str) -> None:
        self._outbound_chunk_ids.pop(chunk_id, None)
        try:
            self._outbound_chunk_order.remove(chunk_id)
        except ValueError:
            pass

    def _current_mobile_transport(self, msg: dict, *, chunk: bool) -> bool:
        keys = self.e2e_keys
        if keys is None or not self.e2e_peer_ready:
            print("[OSIA Relay] dropped encrypted frame before E2E was ready")
            return False
        if (
            msg.get("v") != PROTOCOL_VERSION
            or str(msg.get("role") or "") != "mobile"
            or str(msg.get("pair_id") or "") != self.pair_id
            or msg.get("ack") != 0
            or any(field in msg for field in _LEGACY_IDENTITY_FIELDS)
            or set(msg) != (_E2E_CHUNK_FIELDS if chunk else _E2E_FRAME_FIELDS)
        ):
            print("[OSIA Relay] ignored encrypted frame outside the current peer role")
            return False
        return True

    async def _handle_e2e_frame(self, relay_ws, msg: dict) -> None:
        if not self._current_mobile_transport(msg, chunk=False):
            return
        await self._decrypt_and_dispatch(relay_ws, msg)

    async def _decrypt_and_dispatch(
        self,
        relay_ws,
        envelope: dict,
        *,
        chunk_id: str | None = None,
    ) -> None:
        keys = self.e2e_keys
        if keys is None:
            return
        try:
            frame = decrypt_frame(
                envelope,
                keys=keys,
                direction=MOBILE_TO_PC,
                replay_guard=self.e2e_replay_guard,
                replay_key="mobile",
            )
        except RelayReplayError as exc:
            print(f"[OSIA Relay] dropped invalid encrypted frame: {exc}")
            return
        except (RelayCryptoError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"[OSIA Relay] dropped invalid encrypted frame: {exc}")
            if chunk_id is not None:
                await self._send_chunk_error(
                    relay_ws,
                    chunk_id,
                    "chunk_auth_failed",
                )
            await relay_ws.close()
            return
        if _safe_frame_type(frame) == "relay_chunk_error":
            self._handle_chunk_error_frame(frame)
            return
        if self._transport_coordination_enabled:
            try:
                if await self._webrtc_manager.handle_signal(frame):
                    return
            except Exception as exc:
                print(f"[OSIA WebRTC] signaling failed: {type(exc).__name__}")
                await relay_ws.close()
                return
        if (
            self._transport_coordination_enabled
            and _safe_frame_type(frame) == TYPE_TRANSPORT_COMMIT
        ):
            try:
                await self._handle_transport_commit(relay_ws, frame)
            except Exception as exc:
                print(f"[OSIA Relay] relay activation failed: {type(exc).__name__}")
                await relay_ws.close()
            return
        if self._transport_coordination_enabled and self._local_ws is None:
            print("[OSIA Relay] app frame rejected before relay transport commit")
            await relay_ws.close()
            return
        if not await self._forward_mobile_frame(frame):
            print("[OSIA Relay] local bridge unavailable; closing relay")
            await relay_ws.close()

    def _handle_chunk_error_frame(self, frame: dict) -> None:
        chunk_id = str(frame.get("chunk_id") or "")
        code = str(frame.get("code") or "")
        allowed_codes = {
            "chunk_timeout",
            "chunk_conflict",
            "chunk_auth_failed",
            "chunk_invalid",
            "chunk_capacity",
            "frame_too_large",
            "unsupported_chunk",
        }
        if not self._valid_chunk_id(chunk_id) or code not in allowed_codes:
            print("[OSIA Relay] ignored malformed encrypted chunk error")
            return
        self._prune_outbound_chunks()
        if chunk_id not in self._outbound_chunk_ids:
            status = (
                "peer reported"
                if code in {"unsupported_chunk", "frame_too_large"}
                else "ignored stale"
            )
            print(
                f"[OSIA Relay] {status} chunk error code={code} "
                f"chunk={chunk_id[:8]}"
            )
            return
        self._discard_outbound_chunk(chunk_id)
        print(
            f"[OSIA Relay] outbound chunk transfer failed code={code} "
            f"chunk={chunk_id[:8]}"
        )

    @staticmethod
    def _valid_chunk_id(chunk_id: str) -> bool:
        try:
            decoded = b64url_decode(chunk_id)
        except Exception:
            return False
        return len(decoded) == 16 and b64url_encode(decoded) == chunk_id

    @staticmethod
    def _chunk_int(msg: dict, field_name: str) -> int:
        value = msg.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field_name} must be an integer")
        return value

    def _validate_e2e_chunk(self, msg: dict) -> dict:
        keys = self.e2e_keys
        if keys is None:
            raise ValueError("E2E keys unavailable")
        if msg.get("v") != PROTOCOL_VERSION or msg.get("ack") != 0:
            raise ValueError("invalid v2 chunk envelope")
        if msg.get("e2e_version") != E2E_VERSION:
            raise ValueError("unsupported E2E version")
        if str(msg.get("alg") or "") != ALG_CHACHA20_POLY1305:
            raise ValueError("unsupported E2E algorithm")
        if str(msg.get("kid") or "") != keys.kid:
            raise ValueError("unknown key id")

        frame_seq = self._chunk_int(msg, "frame_seq")
        outer_seq = self._chunk_int(msg, "seq")
        chunk_index = self._chunk_int(msg, "chunk_index")
        chunk_count = self._chunk_int(msg, "chunk_count")
        total_bytes = self._chunk_int(msg, "total_bytes")
        if not 0 < frame_seq < 2**64:
            raise ValueError("frame_seq out of range")
        if not 0 <= chunk_index < chunk_count <= MAX_E2E_CHUNK_COUNT:
            raise ValueError("chunk index/count out of range")
        if outer_seq != frame_seq + chunk_index or outer_seq >= 2**64:
            raise ValueError("chunk relay sequence mismatch")
        if not 0 < total_bytes <= MAX_E2E_CHUNK_TOTAL_BYTES:
            raise ValueError("chunk total out of range")
        expected_count = (
            total_bytes + E2E_CHUNK_CIPHERTEXT_CHARS - 1
        ) // E2E_CHUNK_CIPHERTEXT_CHARS
        if chunk_count != expected_count:
            raise ValueError("chunk count does not match total")

        ciphertext_chunk = msg.get("ciphertext_chunk")
        if not isinstance(ciphertext_chunk, str) or not ciphertext_chunk:
            raise ValueError("ciphertext chunk must be a non-empty string")
        expected_size = (
            E2E_CHUNK_CIPHERTEXT_CHARS
            if chunk_index < chunk_count - 1
            else total_bytes - E2E_CHUNK_CIPHERTEXT_CHARS * (chunk_count - 1)
        )
        if len(ciphertext_chunk) != expected_size:
            raise ValueError("ciphertext chunk size mismatch")
        if not ciphertext_chunk.isascii() or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for char in ciphertext_chunk
        ):
            raise ValueError("ciphertext chunk is not base64url")

        chunk_id = str(msg.get("chunk_id") or "")
        if not self._valid_chunk_id(chunk_id):
            raise ValueError("invalid chunk id")
        priority = msg.get("priority")
        if priority not in _CHUNK_PRIORITY_VALUES:
            raise ValueError("invalid chunk priority")
        final = msg.get("final")
        if not isinstance(final, bool):
            raise ValueError("invalid chunk final flag")
        nonce_text = str(msg.get("nonce") or "")
        try:
            nonce = b64url_decode(nonce_text)
        except Exception as exc:
            raise ValueError("invalid chunk nonce") from exc
        if nonce != nonce_for_seq(keys.direction_nonce_base(MOBILE_TO_PC), frame_seq):
            raise ValueError("chunk nonce does not match frame sequence")

        key = (
            MOBILE_TO_PC,
            str(msg.get("pair_id") or ""),
            str(msg.get("role") or ""),
            str(msg.get("kid") or ""),
            frame_seq,
            chunk_id,
        )
        signature = tuple(msg.get(field) for field in _CHUNK_SIGNATURE_FIELDS)
        metadata = {
            "e2e_version": msg["e2e_version"],
            "alg": msg["alg"],
            "kid": msg["kid"],
            "pair_id": msg["pair_id"],
            "role": msg["role"],
            "seq": frame_seq,
            "priority": priority,
            "final": final,
            "nonce": nonce_text,
        }
        return {
            "key": key,
            "signature": signature,
            "metadata": metadata,
            "chunk_id": chunk_id,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "total_bytes": total_bytes,
            "outer_seq": outer_seq,
            "ciphertext_chunk": ciphertext_chunk,
        }

    def _remember_chunk_tombstone(
        self,
        key: tuple,
        transfer: _ChunkTransfer,
    ) -> None:
        tombstone = _ChunkTombstone(
            signature=transfer.signature,
            chunk_hashes=dict(transfer.chunk_hashes),
            outer_sequences=dict(transfer.outer_sequences),
        )
        if key not in self._chunk_tombstones:
            while len(self._chunk_tombstones) >= _MAX_RECENT_CHUNK_TRANSFERS:
                oldest = self._chunk_tombstone_order.popleft()
                self._chunk_tombstones.pop(oldest, None)
            self._chunk_tombstone_order.append(key)
        self._chunk_tombstones[key] = tombstone

    async def _fail_chunk_transfer(
        self,
        relay_ws,
        *,
        key: tuple | None,
        transfer: _ChunkTransfer | None,
        chunk_id: str,
        code: str,
        close_relay: bool,
    ) -> None:
        if key is not None and transfer is not None:
            self._chunk_transfers.pop(key, None)
            self._remember_chunk_tombstone(key, transfer)
        print(
            f"[OSIA Relay] chunk transfer failed code={code} "
            f"chunk={chunk_id[:8]}"
        )
        try:
            await self._send_chunk_error(relay_ws, chunk_id, code)
        finally:
            if close_relay:
                await relay_ws.close()

    async def _handle_e2e_chunk(self, relay_ws, msg: dict) -> None:
        if not self._current_mobile_transport(msg, chunk=True):
            return
        raw_chunk_id = str(msg.get("chunk_id") or "")
        if not self._chunking_available():
            print("[OSIA Relay] rejected E2E_CHUNK without negotiated capability")
            if self._valid_chunk_id(raw_chunk_id):
                await self._send_chunk_error(
                    relay_ws,
                    raw_chunk_id,
                    "unsupported_chunk",
                )
            await relay_ws.close()
            return
        try:
            validated = self._validate_e2e_chunk(msg)
        except Exception as exc:
            print(f"[OSIA Relay] rejected invalid E2E_CHUNK: {exc}")
            if self._valid_chunk_id(raw_chunk_id):
                await self._fail_chunk_transfer(
                    relay_ws,
                    key=None,
                    transfer=None,
                    chunk_id=raw_chunk_id,
                    code="chunk_invalid",
                    close_relay=True,
                )
            else:
                await relay_ws.close()
            return

        key = validated["key"]
        signature = validated["signature"]
        chunk_id = validated["chunk_id"]
        chunk_index = validated["chunk_index"]
        outer_seq = validated["outer_seq"]
        ciphertext_chunk = validated["ciphertext_chunk"]
        chunk_hash = hashlib.sha256(ciphertext_chunk.encode("ascii")).digest()

        tombstone = self._chunk_tombstones.get(key)
        if tombstone is not None:
            known_hash = tombstone.chunk_hashes.get(chunk_index)
            known_seq = tombstone.outer_sequences.get(chunk_index)
            if (
                tombstone.signature != signature
                or (known_hash is not None and known_hash != chunk_hash)
                or (known_seq is not None and known_seq != outer_seq)
            ):
                await self._fail_chunk_transfer(
                    relay_ws,
                    key=None,
                    transfer=None,
                    chunk_id=chunk_id,
                    code="chunk_conflict",
                    close_relay=True,
                )
            return

        transfer = self._chunk_transfers.get(key)
        if transfer is None:
            if len(self._chunk_transfers) >= MAX_E2E_CHUNK_INFLIGHT:
                await self._fail_chunk_transfer(
                    relay_ws,
                    key=None,
                    transfer=None,
                    chunk_id=chunk_id,
                    code="chunk_capacity",
                    close_relay=True,
                )
                return
            transfer = _ChunkTransfer(
                signature=signature,
                metadata=validated["metadata"],
                chunk_id=chunk_id,
                chunk_count=validated["chunk_count"],
                total_bytes=validated["total_bytes"],
                created_at=time.monotonic(),
            )
            self._chunk_transfers[key] = transfer
            print(
                f"[OSIA Relay] receiving E2E_CHUNK role=mobile "
                f"chunks={transfer.chunk_count} total={transfer.total_bytes} "
                f"priority={transfer.metadata['priority']} "
                f"final={transfer.metadata['final']} chunk={chunk_id[:8]}"
            )
        elif transfer.signature != signature:
            await self._fail_chunk_transfer(
                relay_ws,
                key=key,
                transfer=transfer,
                chunk_id=chunk_id,
                code="chunk_conflict",
                close_relay=True,
            )
            return

        known_hash = transfer.chunk_hashes.get(chunk_index)
        known_seq = transfer.outer_sequences.get(chunk_index)
        if known_hash is not None:
            if known_hash == chunk_hash and known_seq == outer_seq:
                return
            await self._fail_chunk_transfer(
                relay_ws,
                key=key,
                transfer=transfer,
                chunk_id=chunk_id,
                code="chunk_conflict",
                close_relay=True,
            )
            return

        if transfer.received_bytes + len(ciphertext_chunk) > transfer.total_bytes:
            await self._fail_chunk_transfer(
                relay_ws,
                key=key,
                transfer=transfer,
                chunk_id=chunk_id,
                code="chunk_invalid",
                close_relay=True,
            )
            return
        transfer.chunks[chunk_index] = ciphertext_chunk
        transfer.chunk_hashes[chunk_index] = chunk_hash
        transfer.outer_sequences[chunk_index] = outer_seq
        transfer.received_bytes += len(ciphertext_chunk)
        if len(transfer.chunks) != transfer.chunk_count:
            return
        if transfer.received_bytes != transfer.total_bytes:
            await self._fail_chunk_transfer(
                relay_ws,
                key=key,
                transfer=transfer,
                chunk_id=chunk_id,
                code="chunk_invalid",
                close_relay=True,
            )
            return

        ciphertext = "".join(
            transfer.chunks[index] for index in range(transfer.chunk_count)
        )
        self._chunk_transfers.pop(key, None)
        self._remember_chunk_tombstone(key, transfer)
        envelope = {**transfer.metadata, "ciphertext": ciphertext}
        print(
            f"[OSIA Relay] reassembled E2E_CHUNK role=mobile "
            f"chunks={transfer.chunk_count} total={transfer.total_bytes} "
            f"chunk={chunk_id[:8]}"
        )
        await self._decrypt_and_dispatch(
            relay_ws,
            envelope,
            chunk_id=chunk_id,
        )

    def _expire_chunk_transfers(self, now: float | None = None) -> list[str]:
        current = time.monotonic() if now is None else now
        expired: list[str] = []
        for key, transfer in list(self._chunk_transfers.items()):
            if current - transfer.created_at < E2E_CHUNK_TIMEOUT_SECONDS:
                continue
            self._chunk_transfers.pop(key, None)
            self._remember_chunk_tombstone(key, transfer)
            expired.append(transfer.chunk_id)
        return expired

    async def _chunk_expiry_loop(self, relay_ws) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                for chunk_id in self._expire_chunk_transfers():
                    print(
                        f"[OSIA Relay] chunk transfer timed out "
                        f"chunk={chunk_id[:8]}"
                    )
                    await self._send_chunk_error(
                        relay_ws,
                        chunk_id,
                        "chunk_timeout",
                    )
        except asyncio.CancelledError:
            raise

    async def _handle_relay_frame(self, relay_ws, msg: dict) -> None:
        msg_type = str(msg.get("type") or "")
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}

        if msg_type == "PAIR_CONFIRM":
            self._set_relay_chunk_capabilities(payload)
            if (
                msg.get("v") != PROTOCOL_VERSION
                or str(msg.get("role") or "") != "pc"
                or str(msg.get("pair_id") or "") != self.pair_id
                or payload.get("relay_protocol_version") != PROTOCOL_VERSION
                or payload.get("identity_mode") != "role_authority"
                or not isinstance(payload.get("max_frame_bytes"), int)
                or isinstance(payload.get("max_frame_bytes"), bool)
                or payload.get("max_frame_bytes") < MAX_RELAY_FRAME_BYTES
                or not isinstance(payload.get("max_batch_items"), int)
                or isinstance(payload.get("max_batch_items"), bool)
                or payload.get("max_batch_items") <= 0
                or not self._relay_chunk_limits_ready()
            ):
                self._terminal_error_code = "update_required"
                print("[OSIA Relay] incompatible relay protocol; update required")
                await relay_ws.close()
                return
            print("[OSIA Relay] relay pair confirmed")
            self._oauth_authoritative = True
            oauth_relay_coordinator.mark_authoritative(self)
            if self._relay_chunk_limits_ready():
                print("[OSIA Relay] relay E2E chunk transport available")
            await self._send_e2e_hello(relay_ws)
            return
        if msg_type == TYPE_OAUTH_FLOW_REGISTERED:
            oauth_relay_coordinator.handle_flow_registered(msg, self.pair_id)
            return
        if msg_type == TYPE_OAUTH_CALLBACK:
            ack, completion, _reason = oauth_relay_coordinator.handle_callback(
                msg, self.pair_id
            )
            try:
                if ack is not None:
                    await self._send_relay(relay_ws, TYPE_OAUTH_CALLBACK_ACK, ack)
            finally:
                oauth_relay_coordinator.dispatch_completion(completion)
            return
        if msg_type == "MOBILE_READY":
            if not await self._prepare_mobile_session(msg, ready=True):
                return
            await self._maybe_start_e2e_bridge(relay_ws)
            return
        if msg_type == TYPE_E2E_HELLO:
            await self._handle_e2e_hello(relay_ws, msg, payload)
            return
        if msg_type == TYPE_E2E_READY:
            await self._handle_e2e_ready(relay_ws, msg, payload)
            return
        if msg_type == TYPE_E2E_FRAME:
            await self._handle_e2e_frame(relay_ws, msg)
            return
        if msg_type == TYPE_E2E_CHUNK:
            await self._handle_e2e_chunk(relay_ws, msg)
            return
        if msg_type == "PING":
            await self._send_relay(relay_ws, "PONG", {"t": int(time.time() * 1000)})
            return
        if msg_type == "DISCONNECT":
            await self._close_local()
            self._reset_e2e_state()
            return
        if msg_type == "ERROR":
            code = str(payload.get("code") or "")
            oauth_relay_coordinator.handle_control_error(code)
            if code and code != "peer_unavailable":
                if code in {
                    "e2e_required",
                    "update_required",
                    "pair_proof_mismatch",
                    "invalid_pair_route",
                }:
                    self._terminal_error_code = code
                    print(f"[OSIA Relay] fail-closed relay error: {code}")
                    await relay_ws.close()
                elif code == "session_superseded":
                    self._terminal_error_code = code
                    print("[OSIA Relay] relay connection was replaced")
                    await relay_ws.close()
                elif code == "resume_not_supported":
                    print("[OSIA Relay] relay resume unsupported; reconnecting cleanly")
                    await relay_ws.close()
                else:
                    print(f"[OSIA Relay] relay error: {code}")
            return
        if msg_type in {"FRAME", "BATCH"} and self.e2e_keys is not None:
            print("[OSIA Relay] dropped plaintext relay frame after E2E was active")
            return
        if not self.allow_plaintext:
            if msg_type in {"FRAME", "BATCH"}:
                print("[OSIA Relay] dropped plaintext relay frame; E2E is required")
            return
        if msg_type == "BATCH":
            frames = payload.get("frames")
            if isinstance(frames, list):
                for frame in frames:
                    if not await self._forward_mobile_frame(frame):
                        print("[OSIA Relay] local bridge unavailable; closing relay")
                        await relay_ws.close()
                        return
            return
        if msg_type == "FRAME":
            if not await self._forward_mobile_frame(payload.get("frame")):
                print("[OSIA Relay] local bridge unavailable; closing relay")
                await relay_ws.close()

    async def _forward_mobile_frame(self, frame) -> bool:
        if not isinstance(frame, dict):
            return True
        if _safe_frame_type(frame) == "auth":
            return True
        local_ws = self._local_ws
        if local_ws is None:
            return False
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_LOCAL_SERVER_FRAME_BYTES:
            print("[OSIA Relay] dropped oversized mobile frame")
            return True
        await local_ws.send(encoded)
        return True

    async def run_once(self) -> None:
        import websockets

        await self._close_local()
        self._begin_relay_connection()
        self._relay_loop = asyncio.get_running_loop()
        oauth_relay_coordinator.bind_transport(self)
        endpoint = self._relay_endpoint()
        async with websockets.connect(
            endpoint,
            max_size=MAX_RELAY_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        ) as relay_ws:
            self._active_relay_ws = relay_ws
            print("[OSIA Relay] connected to relay")
            await self._send_relay(relay_ws, "PAIR_START", {"secret": self.secret})
            expiry_task = asyncio.create_task(self._chunk_expiry_loop(relay_ws))
            try:
                async for raw in relay_ws:
                    if not isinstance(raw, str):
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_relay_frame(relay_ws, msg)
            finally:
                expiry_task.cancel()
                try:
                    await expiry_task
                except asyncio.CancelledError:
                    pass
                if self._active_relay_ws is relay_ws:
                    self._active_relay_ws = None
                self._oauth_authoritative = False
                oauth_relay_coordinator.mark_disconnected(self)
                await self._webrtc_manager.cancel_preparing()
        if self._terminal_error_code == "session_superseded":
            raise RelaySessionSuperseded("relay connection replaced")
        if self._terminal_error_code in {
            "e2e_required",
            "update_required",
            "pair_proof_mismatch",
            "invalid_pair_route",
        }:
            raise RelayConfigurationError(self._terminal_error_code)

    async def run_forever(self) -> None:
        delay = 1.0
        superseded_retries = 0
        while True:
            try:
                await self.run_once()
                delay = 1.0
                superseded_retries = 0
            except RelaySessionSuperseded:
                superseded_retries += 1
                if superseded_retries > 3:
                    raise RelayConfigurationError("session_superseded")
                print("[OSIA Relay] waiting before bounded ownership retry")
                await self._close_local()
                await asyncio.sleep(30.0 * superseded_retries)
                delay = 1.0
            except RelayConfigurationError:
                await self._close_local()
                raise
            except Exception as exc:
                print(f"[OSIA Relay] connector retry after error: {exc}")
                await self._close_local()
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 30.0)


def _thread_main() -> None:
    while _relay_enabled():
        token = load_mobile_token()
        if not token:
            print("[OSIA Relay] no mobile token yet; waiting")
            time.sleep(5)
            continue
        try:
            asyncio.run(RelayBridge(token, _relay_url()).run_forever())
        except RelayConfigurationError as exc:
            print(f"[OSIA Relay] connector stopped: {exc}")
            return
        except Exception as exc:
            print(f"[OSIA Relay] connector loop crashed: {exc}")
            time.sleep(5)


def start_background_connector() -> None:
    """Start the relay connector once, if enabled. Best-effort and non-blocking."""
    global _started
    if not _relay_enabled():
        return
    try:
        if webrtc_direct_enabled():
            records = load_webrtc_ice_servers()
            urls = [
                str(url)
                for record in records
                for url in record.get("urls", [])
                if str(url).startswith("stun:")
            ]
            start_stun_health_probe(urls)
    except WebRtcSignalingError:
        pass
    with _started_lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_thread_main,
        name="osia-relay-connector",
        daemon=True,
    ).start()
