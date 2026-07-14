"""Validated endpoint-only signaling contract for WebRTC direct transport.

These frames are application payloads of the existing relay-v2 E2E channel.
The public relay therefore sees only an ordinary opaque ``E2E_FRAME``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from typing import Any, Callable, Mapping, Sequence

from osia.config import load_settings


WEBRTC_PROTOCOL_VERSION = 1
WEBRTC_DATA_CHANNEL_LABEL = "osia-app-v1"
WEBRTC_DATA_CHANNEL_PROTOCOL = "osia.e2e.v1"
MAX_ICE_SERVERS = 4
MAX_ICE_URLS = 8
MAX_SDP_BYTES = 64 * 1024
MAX_CANDIDATE_BYTES = 2048
MAX_CANDIDATES = 64
MAX_CANDIDATES_PER_EVENT = 16
SIGNAL_TTL_SECONDS = 30

SIGNAL_EVENT_TYPES = frozenset({
    "webrtc_prepare",
    "webrtc_offer",
    "webrtc_answer",
    "webrtc_ice_candidates",
    "webrtc_end_of_candidates",
    "webrtc_cancel",
    "webrtc_failed",
    "webrtc_direct_ready",
    "webrtc_direct_commit",
    "webrtc_direct_commit_ack",
})

_SIGNAL_FIELDS = frozenset({
    "protocol_version",
    "transport",
    "attempt_id",
    "generation",
    "source_role",
    "destination_role",
    "event_id",
    "event_type",
    "sequence",
    "created_at",
    "terminal",
    "payload",
})
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_ICE_URL = re.compile(
    r"^(?P<scheme>stun|turn|turns):"
    r"(?P<host>\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)"
    r"(?::(?P<port>[0-9]{1,5}))?"
    r"(?:\?transport=(?P<transport>udp|tcp))?$"
)
_MID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_FAILURE_CATEGORIES = frozenset({
    "busy",
    "cancelled",
    "expired",
    "ice_failed",
    "signaling_failed",
    "authentication_failed",
    "bridge_unavailable",
    "protocol_error",
    "timeout",
    "unavailable",
})


class WebRtcSignalingError(ValueError):
    """A signaling/configuration frame failed bounded validation."""


def webrtc_direct_enabled() -> bool:
    raw = os.getenv("OSIA_WEBRTC_DIRECT_V1")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return load_settings().get("webrtc_direct_v1") is True


def load_webrtc_ice_servers() -> list[dict[str, Any]]:
    """Load validated ICE records without ever sourcing secrets from settings.

    ``webrtc_stun_urls`` is the non-secret persisted setting. Optional TURN
    records are accepted only from the protected process environment.
    """

    raw_secret = os.getenv("OSIA_WEBRTC_ICE_SERVERS_JSON")
    if raw_secret is not None:
        try:
            records = json.loads(raw_secret)
        except json.JSONDecodeError as exc:
            raise WebRtcSignalingError("invalid protected ICE configuration") from exc
        return validate_ice_servers(records, allow_turn=True)

    raw_urls = load_settings().get("webrtc_stun_urls", [])
    if raw_urls in (None, ""):
        raw_urls = []
    if not isinstance(raw_urls, list):
        raise WebRtcSignalingError("webrtc_stun_urls must be a list")
    return validate_ice_servers([{"urls": raw_urls}], allow_turn=False) if raw_urls else []


def validate_ice_servers(
    records: Any,
    *,
    allow_turn: bool,
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or len(records) > MAX_ICE_SERVERS:
        raise WebRtcSignalingError("invalid ICE server list")
    normalized: list[dict[str, Any]] = []
    total_urls = 0
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            raise WebRtcSignalingError("invalid ICE server record")
        allowed = {"urls", "username", "credential"}
        if not set(raw_record).issubset(allowed) or "urls" not in raw_record:
            raise WebRtcSignalingError("invalid ICE server fields")
        raw_urls = raw_record["urls"]
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        if not isinstance(raw_urls, list) or not raw_urls:
            raise WebRtcSignalingError("ICE URLs must be a non-empty list")
        urls: list[str] = []
        seen_urls: set[str] = set()
        has_turn = False
        for raw_url in raw_urls:
            if not isinstance(raw_url, str) or len(raw_url) > 512:
                raise WebRtcSignalingError("invalid ICE URL")
            url = raw_url.strip()
            match = _ICE_URL.fullmatch(url)
            if match is None:
                raise WebRtcSignalingError("invalid ICE URL")
            scheme = match.group("scheme")
            port = match.group("port")
            transport = match.group("transport")
            if port is not None and not 1 <= int(port) <= 65535:
                raise WebRtcSignalingError("invalid ICE URL port")
            if scheme == "stun" and transport is not None:
                raise WebRtcSignalingError("STUN transport queries are not supported")
            if scheme != "stun":
                if not allow_turn:
                    raise WebRtcSignalingError("TURN is not allowed in persisted settings")
                has_turn = True
            if url not in seen_urls:
                seen_urls.add(url)
                urls.append(url)
        total_urls += len(urls)
        if total_urls > MAX_ICE_URLS:
            raise WebRtcSignalingError("too many ICE URLs")
        username = raw_record.get("username")
        credential = raw_record.get("credential")
        if has_turn:
            if (
                not isinstance(username, str)
                or not username
                or len(username) > 256
                or not isinstance(credential, str)
                or not credential
                or len(credential) > 512
            ):
                raise WebRtcSignalingError("TURN credentials are required")
        elif username is not None or credential is not None:
            raise WebRtcSignalingError("credentials are valid only for TURN")
        record: dict[str, Any] = {"urls": urls}
        if has_turn:
            record["username"] = username
            record["credential"] = credential
        normalized.append(record)
    return normalized


def ice_configuration_id(enabled: bool, ice_servers: Sequence[Mapping[str, Any]]) -> str:
    body = json.dumps(
        {"enabled": bool(enabled), "ice_servers": list(ice_servers)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:32]


def capability_frame(enabled: bool, ice_servers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = validate_ice_servers(list(ice_servers), allow_turn=True)
    return {
        "type": "webrtc_capabilities",
        "protocol_version": WEBRTC_PROTOCOL_VERSION,
        "enabled": bool(enabled),
        "configuration_id": ice_configuration_id(enabled, normalized),
        "ice_servers": normalized,
    }


def make_signal(
    event_type: str,
    *,
    attempt_id: str,
    generation: int,
    source_role: str,
    sequence: int,
    payload: Mapping[str, Any] | None = None,
    terminal: bool = False,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    destination_role = "mobile" if source_role == "pc" else "pc"
    frame = {
        "protocol_version": WEBRTC_PROTOCOL_VERSION,
        "transport": "webrtc",
        "attempt_id": attempt_id,
        "generation": generation,
        "source_role": source_role,
        "destination_role": destination_role,
        "event_id": secrets.token_hex(16),
        "event_type": event_type,
        "sequence": sequence,
        "created_at": int(clock()),
        "terminal": bool(terminal),
        "payload": dict(payload or {}),
    }
    return validate_signal(frame, destination_role=destination_role, clock=clock)


def validate_signal(
    frame: Any,
    *,
    destination_role: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(frame, Mapping) or set(frame) != _SIGNAL_FIELDS:
        raise WebRtcSignalingError("invalid signaling envelope")
    event_type = frame.get("event_type")
    source_role = frame.get("source_role")
    if (
        frame.get("protocol_version") != WEBRTC_PROTOCOL_VERSION
        or frame.get("transport") != "webrtc"
        or event_type not in SIGNAL_EVENT_TYPES
        or destination_role not in {"pc", "mobile"}
        or frame.get("destination_role") != destination_role
        or source_role not in {"pc", "mobile"}
        or source_role == destination_role
        or not _HEX_32.fullmatch(str(frame.get("attempt_id") or ""))
        or not _HEX_32.fullmatch(str(frame.get("event_id") or ""))
    ):
        raise WebRtcSignalingError("invalid signaling metadata")
    generation = frame.get("generation")
    sequence = frame.get("sequence")
    created_at = frame.get("created_at")
    terminal = frame.get("terminal")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not 1 <= generation < 2**31
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence < 2**31
        or not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or not isinstance(terminal, bool)
    ):
        raise WebRtcSignalingError("invalid signaling sequence")
    now = int(clock())
    if created_at > now + 5 or now - created_at > SIGNAL_TTL_SECONDS:
        raise WebRtcSignalingError("expired signaling event")
    payload = frame.get("payload")
    if not isinstance(payload, Mapping):
        raise WebRtcSignalingError("invalid signaling payload")
    _validate_payload(str(event_type), payload, terminal)
    return dict(frame)


def _validate_payload(event_type: str, payload: Mapping[str, Any], terminal: bool) -> None:
    if event_type == "webrtc_prepare":
        if set(payload) != {"configuration_id"} or not _HEX_32.fullmatch(
            str(payload.get("configuration_id") or "")
        ) or terminal:
            raise WebRtcSignalingError("invalid prepare payload")
        return
    if event_type in {"webrtc_offer", "webrtc_answer"}:
        expected_type = "offer" if event_type.endswith("offer") else "answer"
        if set(payload) != {"type", "sdp"} or payload.get("type") != expected_type:
            raise WebRtcSignalingError("invalid SDP payload")
        validate_data_only_sdp(payload.get("sdp"))
        if terminal:
            raise WebRtcSignalingError("SDP cannot be terminal")
        return
    if event_type == "webrtc_ice_candidates":
        candidates = payload.get("candidates")
        if set(payload) != {"candidates"} or not isinstance(candidates, list) or not (
            1 <= len(candidates) <= MAX_CANDIDATES_PER_EVENT
        ) or terminal:
            raise WebRtcSignalingError("invalid candidate batch")
        for candidate in candidates:
            validate_candidate(candidate)
        return
    if event_type == "webrtc_end_of_candidates":
        if payload or terminal:
            raise WebRtcSignalingError("invalid end-of-candidates payload")
        return
    if event_type in {
        "webrtc_direct_ready",
        "webrtc_direct_commit",
        "webrtc_direct_commit_ack",
    }:
        if payload or terminal:
            raise WebRtcSignalingError("invalid direct-control payload")
        return
    if event_type == "webrtc_cancel":
        if set(payload) != {"category"} or payload.get("category") not in {
            "cancelled", "superseded"
        } or not terminal:
            raise WebRtcSignalingError("invalid cancel payload")
        return
    if event_type == "webrtc_failed":
        if set(payload) != {"category"} or payload.get("category") not in _FAILURE_CATEGORIES or not terminal:
            raise WebRtcSignalingError("invalid failure payload")
        return
    raise WebRtcSignalingError("unsupported signaling event")


def validate_data_only_sdp(value: Any) -> str:
    if not isinstance(value, str):
        raise WebRtcSignalingError("SDP must be text")
    encoded = value.encode("utf-8")
    normalized = value.replace("\r\n", "\n")
    if (
        not encoded
        or len(encoded) > MAX_SDP_BYTES
        or not normalized.startswith("v=0\n")
        or "\nm=application " not in normalized
        or "\nm=audio " in normalized
        or "\nm=video " in normalized
    ):
        raise WebRtcSignalingError("invalid data-only SDP")
    return value


def validate_candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "candidate", "sdp_mid", "sdp_mline_index"
    }:
        raise WebRtcSignalingError("invalid ICE candidate fields")
    candidate = value.get("candidate")
    mid = value.get("sdp_mid")
    mline = value.get("sdp_mline_index")
    if (
        not isinstance(candidate, str)
        or not candidate.startswith("candidate:")
        or len(candidate.encode("utf-8")) > MAX_CANDIDATE_BYTES
        or not isinstance(mid, str)
        or _MID.fullmatch(mid) is None
        or not isinstance(mline, int)
        or isinstance(mline, bool)
        or not 0 <= mline <= 8
    ):
        raise WebRtcSignalingError("invalid ICE candidate")
    fields = candidate.split()
    if (
        len(fields) < 8
        or fields[1] != "1"
        or fields[2].lower() not in {"udp", "tcp"}
        or fields[6] != "typ"
        or fields[7] not in {"host", "srflx", "prflx", "relay"}
    ):
        raise WebRtcSignalingError("unsupported ICE candidate")
    try:
        port = int(fields[5])
    except (TypeError, ValueError) as exc:
        raise WebRtcSignalingError("invalid ICE candidate port") from exc
    if not 1 <= port <= 65535:
        raise WebRtcSignalingError("invalid ICE candidate port")
    return dict(value)


def strip_sdp_candidates(sdp: str) -> tuple[str, list[dict[str, Any]]]:
    """Return candidate-free SDP plus bounded candidates for E2E signaling."""

    validate_data_only_sdp(sdp)
    lines = sdp.replace("\r\n", "\n").split("\n")
    output: list[str] = []
    candidates: list[dict[str, Any]] = []
    mline_index = -1
    current_mid = ""
    for line in lines:
        if line.startswith("m="):
            mline_index += 1
        elif line.startswith("a=mid:"):
            current_mid = line[6:]
        if line.startswith("a=candidate:"):
            candidate = {
                "candidate": line[2:],
                "sdp_mid": current_mid,
                "sdp_mline_index": mline_index,
            }
            candidates.append(validate_candidate(candidate))
            if len(candidates) > MAX_CANDIDATES:
                raise WebRtcSignalingError("too many local ICE candidates")
            continue
        if line == "a=end-of-candidates":
            continue
        output.append(line)
    stripped = "\r\n".join(output)
    if not stripped.endswith("\r\n"):
        stripped += "\r\n"
    validate_data_only_sdp(stripped)
    return stripped, candidates


def candidate_dedup_key(candidate: Mapping[str, Any]) -> str:
    stable = validate_candidate(candidate)
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
