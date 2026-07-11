"""Endpoint-only relay protocol-v2 E2E crypto helpers for WOR-104."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


E2E_VERSION = 1
ALG_CHACHA20_POLY1305 = "CHACHA20-POLY1305"
PC_TO_MOBILE = "pc_to_mobile"
MOBILE_TO_PC = "mobile_to_pc"
PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"
PRIORITY_DROPPABLE = "droppable"

_LEGACY_PAIR_SECRET_LABEL = b"osia-relay-secret-v1"
_E2E_PSK_LABEL = b"osia-e2e-psk-v1"
_SESSION_INFO = b"osia-relay-e2e-session-v1"
_READY_TAG_LABEL = "osia-e2e-ready-v1"
_KEY_MATERIAL_LEN = 80
_KEY_LEN = 32
_KID_LEN = 16
_NONCE_BASE_LEN = 4
_NONCE_LEN = 12
_TAG_LEN = 16

_AAD_FIELDS = (
    "e2e_version",
    "alg",
    "kid",
    "pair_id",
    "role",
    "seq",
    "priority",
    "final",
)
_LEGACY_IDENTITY_FIELDS = {"device_id", "session_id", "from", "to", "to_session_id"}


class RelayCryptoError(Exception):
    """Base exception for relay crypto validation/decryption failures."""


class RelayReplayError(RelayCryptoError):
    """Raised when a repeated or old sequence number is observed."""


def canonical_json(value: Any) -> bytes:
    """Encode JSON with stable map ordering and no insignificant whitespace."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def derive_pair_secret(token: str) -> bytes:
    """Derive the legacy relay-pair secret bytes without exposing the token."""

    return hmac.new(
        token.encode("utf-8"),
        _LEGACY_PAIR_SECRET_LABEL,
        hashlib.sha256,
    ).digest()


def derive_e2e_psk(token: str) -> bytes:
    """Derive the endpoint-only E2E PSK. This value must never go to the relay."""

    return hmac.new(
        token.encode("utf-8"),
        _E2E_PSK_LABEL,
        hashlib.sha256,
    ).digest()


def generate_x25519_private_key() -> bytes:
    return secrets.token_bytes(32)


def generate_nonce_base() -> bytes:
    return secrets.token_bytes(_NONCE_BASE_LEN)


def x25519_public_key(private_key_bytes: bytes) -> bytes:
    private_key = x25519.X25519PrivateKey.from_private_bytes(private_key_bytes)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


@dataclass(frozen=True)
class RelaySessionContext:
    pair_id: str
    pc_public_key: bytes
    mobile_public_key: bytes
    pc_nonce_base: bytes
    mobile_nonce_base: bytes
    e2e_version: int = E2E_VERSION
    alg: str = ALG_CHACHA20_POLY1305

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.pair_id):
            raise ValueError("pair_id must be lowercase 64-hex")
        _require_len("pc_public_key", self.pc_public_key, 32)
        _require_len("mobile_public_key", self.mobile_public_key, 32)
        _require_len("pc_nonce_base", self.pc_nonce_base, _NONCE_BASE_LEN)
        _require_len("mobile_nonce_base", self.mobile_nonce_base, _NONCE_BASE_LEN)

    def transcript(self) -> dict[str, Any]:
        return {
            "e2e_version": self.e2e_version,
            "alg": self.alg,
            "pair_id": self.pair_id,
            "roles": ["pc", "mobile"],
            "pc_public_key": b64url_encode(self.pc_public_key),
            "mobile_public_key": b64url_encode(self.mobile_public_key),
            "pc_nonce_base": b64url_encode(self.pc_nonce_base),
            "mobile_nonce_base": b64url_encode(self.mobile_nonce_base),
        }

    def transcript_bytes(self) -> bytes:
        return canonical_json(self.transcript())


@dataclass(frozen=True)
class RelaySessionKeys:
    pc_to_mobile_key: bytes
    mobile_to_pc_key: bytes
    pc_nonce_base: bytes
    mobile_nonce_base: bytes
    kid: str

    def direction_key(self, direction: str) -> bytes:
        if direction == PC_TO_MOBILE:
            return self.pc_to_mobile_key
        if direction == MOBILE_TO_PC:
            return self.mobile_to_pc_key
        raise ValueError(f"Unknown relay crypto direction: {direction}")

    def direction_nonce_base(self, direction: str) -> bytes:
        if direction == PC_TO_MOBILE:
            return self.pc_nonce_base
        if direction == MOBILE_TO_PC:
            return self.mobile_nonce_base
        raise ValueError(f"Unknown relay crypto direction: {direction}")


def derive_session_keys(
    *,
    token: str,
    local_private_key: bytes,
    peer_public_key: bytes,
    context: RelaySessionContext,
) -> RelaySessionKeys:
    """Derive direction-separated session keys from X25519 + endpoint-only PSK."""

    private_key = x25519.X25519PrivateKey.from_private_bytes(local_private_key)
    peer_key = x25519.X25519PublicKey.from_public_bytes(peer_public_key)
    shared_secret = private_key.exchange(peer_key)
    transcript_hash = hashlib.sha256(context.transcript_bytes()).digest()
    ikm = shared_secret + derive_e2e_psk(token)
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_MATERIAL_LEN,
        salt=transcript_hash,
        info=_SESSION_INFO,
    ).derive(ikm)

    pc_to_mobile_key = key_material[:_KEY_LEN]
    mobile_to_pc_key = key_material[_KEY_LEN : _KEY_LEN * 2]
    kid = b64url_encode(key_material[_KEY_LEN * 2 : _KEY_LEN * 2 + _KID_LEN])
    return RelaySessionKeys(
        pc_to_mobile_key=pc_to_mobile_key,
        mobile_to_pc_key=mobile_to_pc_key,
        pc_nonce_base=context.pc_nonce_base,
        mobile_nonce_base=context.mobile_nonce_base,
        kid=kid,
    )


def handshake_confirmation_tag(
    *,
    token: str,
    context: RelaySessionContext,
    sender_role: str,
) -> str:
    body = {
        "label": _READY_TAG_LABEL,
        "sender_role": sender_role,
        "transcript": context.transcript(),
    }
    tag = hmac.new(derive_e2e_psk(token), canonical_json(body), hashlib.sha256).digest()
    return b64url_encode(tag)


def verify_handshake_confirmation(
    *,
    token: str,
    context: RelaySessionContext,
    sender_role: str,
    tag: str,
) -> bool:
    expected = handshake_confirmation_tag(
        token=token,
        context=context,
        sender_role=sender_role,
    )
    return hmac.compare_digest(expected, tag)


def nonce_for_seq(nonce_base: bytes, seq: int) -> bytes:
    _require_len("nonce_base", nonce_base, _NONCE_BASE_LEN)
    if not 0 <= seq < 2**64:
        raise ValueError("seq must fit uint64")
    return nonce_base + seq.to_bytes(8, "big")


class SequenceReplayGuard:
    """Strict monotonic replay guard keyed by the active direction context."""

    def __init__(self) -> None:
        self._last_seen: dict[str, int] = {}

    def accept(self, key: str, seq: int) -> None:
        last_seen = self._last_seen.get(key, -1)
        if seq <= last_seen:
            raise RelayReplayError(f"replayed or old sequence: {seq}")
        self._last_seen[key] = seq


class OutboundSequence:
    """Monotonic outbound sequence allocator for normal encryption mode."""

    def __init__(self, start: int = 0) -> None:
        if start < 0:
            raise ValueError("start must be non-negative")
        self._last = start

    def next(self) -> int:
        return self.reserve(self._last + 1)

    def reserve(self, seq: int) -> int:
        if seq <= self._last:
            raise RelayReplayError(f"sequence already used: {seq}")
        if seq >= 2**64:
            raise ValueError("seq must fit uint64")
        self._last = seq
        return seq


def aad_metadata(
    *,
    keys: RelaySessionKeys,
    pair_id: str,
    role: str,
    seq: int,
    priority: str,
    final: bool,
) -> dict[str, Any]:
    if role not in {"pc", "mobile"}:
        raise ValueError("role must be pc or mobile")
    if not re.fullmatch(r"[0-9a-f]{64}", pair_id):
        raise ValueError("pair_id must be lowercase 64-hex")
    return {
        "e2e_version": E2E_VERSION,
        "alg": ALG_CHACHA20_POLY1305,
        "kid": keys.kid,
        "pair_id": pair_id,
        "role": role,
        "seq": seq,
        "priority": priority,
        "final": final,
    }


def relay_frame_is_final(frame: Mapping[str, Any]) -> bool:
    frame_type = str(frame.get("type") or "")
    if frame_type == "status":
        content = str(frame.get("content") or frame.get("status") or "")
        return content in {"done", "error"}
    if frame_type == "turn_status":
        return str(frame.get("status") or "") in {"done", "error"}
    if frame_type == "conversation_busy_changed":
        return frame.get("busy") is False
    return frame_type in {
        "response_done",
        "turn_complete",
        "done",
        "final",
        "error",
    }


def relay_frame_priority(frame: Mapping[str, Any]) -> str:
    frame_type = str(frame.get("type") or "")
    if relay_frame_is_final(frame):
        return PRIORITY_CRITICAL
    if frame_type in {
        "response",
        "turn_accepted",
        "turn_status",
        "conversation_busy",
        "conversation_busy_changed",
        "active_conversation",
        "conversation_history",
        "relay_chunk_error",
        "cron_notification",
    }:
        return PRIORITY_CRITICAL
    if frame_type in {"heartbeat_data", "mcp_status"}:
        return PRIORITY_DROPPABLE
    if frame_type == "status":
        return PRIORITY_DROPPABLE
    return PRIORITY_NORMAL


def encrypt_frame(
    frame: Mapping[str, Any],
    *,
    keys: RelaySessionKeys,
    direction: str,
    pair_id: str,
    role: str,
    priority: str,
    final: bool,
    seq: int | None = None,
    outbound_sequence: OutboundSequence | None = None,
) -> dict[str, Any]:
    if outbound_sequence is not None:
        seq = outbound_sequence.next() if seq is None else outbound_sequence.reserve(seq)
    if seq is None:
        raise ValueError("seq is required without an outbound sequence")

    metadata = aad_metadata(
        keys=keys,
        pair_id=pair_id,
        role=role,
        seq=seq,
        priority=priority,
        final=final,
    )
    nonce = nonce_for_seq(keys.direction_nonce_base(direction), seq)
    ciphertext = ChaCha20Poly1305(keys.direction_key(direction)).encrypt(
        nonce,
        canonical_json(frame),
        canonical_json(metadata),
    )
    return {
        **metadata,
        "nonce": b64url_encode(nonce),
        "ciphertext": b64url_encode(ciphertext),
    }


def decrypt_frame(
    envelope: Mapping[str, Any],
    *,
    keys: RelaySessionKeys,
    direction: str,
    replay_guard: SequenceReplayGuard | None = None,
    replay_key: str | None = None,
) -> dict[str, Any]:
    if any(field in envelope for field in _LEGACY_IDENTITY_FIELDS):
        raise RelayCryptoError("legacy relay identity metadata is not allowed")
    metadata = _metadata_from_envelope(envelope)
    if metadata["kid"] != keys.kid:
        raise RelayCryptoError("unknown key id")
    seq = _require_int("seq", metadata["seq"])
    nonce = b64url_decode(_require_str("nonce", envelope.get("nonce")))
    ciphertext = b64url_decode(_require_str("ciphertext", envelope.get("ciphertext")))
    expected_nonce = nonce_for_seq(keys.direction_nonce_base(direction), seq)
    if nonce != expected_nonce:
        raise RelayCryptoError("nonce does not match sequence")

    try:
        plaintext = ChaCha20Poly1305(keys.direction_key(direction)).decrypt(
            nonce,
            ciphertext,
            canonical_json(metadata),
        )
    except InvalidTag as exc:
        raise RelayCryptoError("frame authentication failed") from exc

    decoded = json.loads(plaintext.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise RelayCryptoError("decrypted frame is not an object")
    if replay_guard is not None:
        replay_guard.accept(replay_key or str(metadata["role"]), seq)
    return decoded


def _metadata_from_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {field: envelope[field] for field in _AAD_FIELDS}


def _require_len(name: str, value: bytes, expected: int) -> None:
    if len(value) != expected:
        raise ValueError(f"{name} must be {expected} bytes")


def _require_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RelayCryptoError(f"{name} must be an integer")
    return value


def _require_str(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise RelayCryptoError(f"{name} must be a string")
    return value
