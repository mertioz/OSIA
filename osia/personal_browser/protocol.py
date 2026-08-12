"""Strict bounded protocol shared by broker, Native host, and MV3 extension."""

from __future__ import annotations

import json
import re
import struct
import time
from typing import Any, BinaryIO, Mapping


PROTOCOL_VERSION = "1.0"
PROTOCOL_MIN_VERSION = "1.0"
PROTOCOL_MAX_VERSION = "1.0"
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_DEADLINE_MS = 120_000
MAX_CAPABILITIES = 64
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{7,127}$")
_OP_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FAMILIES = frozenset({"chrome", "edge", "brave"})
KNOWN_OPERATIONS = frozenset({
    "activate_browser_element", "browser_back", "browser_forward",
    "close_browser_tab", "fill_form_field", "get_browser_state",
    "handle_browser_dialog", "inspect_browser_page", "inspect_form_fields",
    "list_browser_dialogs", "list_browser_tabs", "navigate_browser",
    "prepare_browser_action_authorization", "reload_browser_page",
    "return_to_browser_opener", "revoke", "select_browser_tab",
    "wait_for_browser_condition",
})
CONTROL_OPERATIONS = frozenset({"invoke", "revoke", "status"})


class ProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _object(value: Any, code: str = "malformed_message") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(code)
    return value


def _exact(value: Mapping[str, Any], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise ProtocolError("unknown_fields")


def _identifier(value: Any, code: str = "invalid_identifier") -> str:
    text = str(value or "")
    if not _ID_RE.fullmatch(text):
        raise ProtocolError(code)
    return text


def _bounded_json(value: Any) -> Any:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError("malformed_message") from exc
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("message_too_large")
    return value


def encode_message(message: Mapping[str, Any]) -> bytes:
    validated = validate_message(message)
    payload = json.dumps(
        validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("message_too_large")
    return payload


def decode_message(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("message_too_large" if payload else "malformed_message")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("malformed_message") from exc
    return validate_message(value)


def validate_message(raw: Any) -> dict[str, Any]:
    value = dict(_object(raw))
    kind = value.get("message_type")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("protocol_mismatch")
    if kind == "hello":
        _exact(value, {
            "protocol_version", "message_type", "protocol_min", "protocol_max",
            "connection_id", "install_instance_id", "browser_session_id",
            "browser_family", "caller_origin", "capabilities",
        })
        if value.get("protocol_min") != PROTOCOL_MIN_VERSION or value.get("protocol_max") != PROTOCOL_MAX_VERSION:
            raise ProtocolError("protocol_mismatch")
        for key in ("connection_id", "install_instance_id", "browser_session_id"):
            _identifier(value.get(key))
        if value.get("browser_family") not in _FAMILIES:
            raise ProtocolError("incompatible")
        origin = str(value.get("caller_origin") or "")
        if not re.fullmatch(r"chrome-extension://[a-p]{32}/", origin):
            raise ProtocolError("origin_not_allowed")
        capabilities = value.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > MAX_CAPABILITIES
            or any(not isinstance(item, str) or item not in KNOWN_OPERATIONS for item in capabilities)
        ):
            raise ProtocolError("invalid_capabilities")
    elif kind == "target_update":
        _exact(value, {"protocol_version", "message_type", "connection_id", "target"})
        _identifier(value.get("connection_id"))
        validate_target(value.get("target"))
    elif kind == "request":
        _exact(value, {
            "protocol_version", "message_type", "request_id", "connection_id",
            "target_handle", "operation", "parameters", "deadline_ms",
        })
        _identifier(value.get("request_id"))
        _identifier(value.get("connection_id"))
        _identifier(value.get("target_handle"))
        if not isinstance(value.get("operation"), str) or value["operation"] not in KNOWN_OPERATIONS:
            raise ProtocolError("unknown_operation")
        _object(value.get("parameters"))
        deadline = value.get("deadline_ms")
        now_ms = int(time.time() * 1000)
        if (
            not isinstance(deadline, int) or isinstance(deadline, bool)
            or deadline < now_ms - 1_000 or deadline > now_ms + MAX_DEADLINE_MS
        ):
            raise ProtocolError("invalid_deadline")
    elif kind == "dispatch_started":
        _exact(value, {
            "protocol_version", "message_type", "request_id", "connection_id",
            "target_handle",
        })
        for key in ("request_id", "connection_id", "target_handle"):
            _identifier(value.get(key))
    elif kind == "response":
        _exact(value, {
            "protocol_version", "message_type", "request_id", "connection_id",
            "status", "error", "result",
        })
        _identifier(value.get("request_id"))
        _identifier(value.get("connection_id"))
        if value.get("status") not in {"ok", "error"}:
            raise ProtocolError("malformed_message")
        error = value.get("error")
        if error is not None:
            error_obj = _object(error)
            _exact(error_obj, {"code"})
            if not isinstance(error_obj.get("code"), str) or not _OP_RE.fullmatch(error_obj["code"]):
                raise ProtocolError("malformed_message")
        if value.get("status") == "ok" and not isinstance(value.get("result"), Mapping):
            raise ProtocolError("malformed_message")
    elif kind == "cancel":
        _exact(value, {"protocol_version", "message_type", "request_id", "connection_id"})
        _identifier(value.get("request_id"))
        _identifier(value.get("connection_id"))
    elif kind == "control_request":
        _exact(value, {
            "protocol_version", "message_type", "request_id",
            "control_operation", "browser_operation", "arguments", "turn_binding",
        })
        _identifier(value.get("request_id"))
        control_operation = value.get("control_operation")
        if control_operation not in CONTROL_OPERATIONS:
            raise ProtocolError("unknown_operation")
        browser_operation = value.get("browser_operation")
        if control_operation == "invoke":
            if browser_operation not in KNOWN_OPERATIONS:
                raise ProtocolError("unknown_operation")
            binding = _object(value.get("turn_binding"))
            _exact(binding, {
                "personal_connection_id", "personal_session_id",
                "personal_target_handle",
            })
            identities = [
                str(binding.get("personal_connection_id") or ""),
                str(binding.get("personal_session_id") or ""),
                str(binding.get("personal_target_handle") or ""),
            ]
            if any(identities) and not all(identities):
                raise ProtocolError("invalid_identifier")
            for identity in identities:
                if identity:
                    _identifier(identity)
        elif browser_operation is not None:
            raise ProtocolError("unknown_operation")
        elif value.get("turn_binding") is not None:
            raise ProtocolError("unknown_fields")
        _object(value.get("arguments"))
    elif kind == "control_response":
        _exact(value, {
            "protocol_version", "message_type", "request_id", "status",
            "error", "result",
        })
        _identifier(value.get("request_id"))
        if value.get("status") not in {"ok", "error"}:
            raise ProtocolError("malformed_message")
        if not isinstance(value.get("result"), Mapping):
            raise ProtocolError("malformed_message")
        error = value.get("error")
        if error is not None:
            error_obj = _object(error)
            _exact(error_obj, {"code"})
            if not isinstance(error_obj.get("code"), str) or not _OP_RE.fullmatch(error_obj["code"]):
                raise ProtocolError("malformed_message")
    elif kind == "control_progress":
        _exact(value, {
            "protocol_version", "message_type", "request_id", "phase",
        })
        _identifier(value.get("request_id"))
        if value.get("phase") != "waiting":
            raise ProtocolError("malformed_message")
    else:
        raise ProtocolError("unknown_message_type")
    _bounded_json(value)
    return value


def validate_target(raw: Any) -> dict[str, Any]:
    value = dict(_object(raw, "invalid_target"))
    _exact(value, {
        "target_handle", "install_instance_id", "browser_session_id", "browser_family",
        "window_id", "tab_id", "safe_origin", "safe_url", "safe_title",
        "authorization_epoch", "document_nonce", "state_ref", "authorized",
        "state",
    })
    for key in ("target_handle", "install_instance_id", "browser_session_id", "document_nonce"):
        _identifier(value.get(key), "invalid_target")
    if value.get("browser_family") not in _FAMILIES:
        raise ProtocolError("invalid_target")
    for key in ("window_id", "tab_id", "authorization_epoch"):
        if not isinstance(value.get(key), int) or isinstance(value.get(key), bool) or value[key] < 0:
            raise ProtocolError("invalid_target")
    if not isinstance(value.get("authorized"), bool):
        raise ProtocolError("invalid_target")
    for key, maximum in (("safe_origin", 512), ("safe_url", 1024), ("safe_title", 160), ("state", 64)):
        if not isinstance(value.get(key), str) or len(value[key]) > maximum:
            raise ProtocolError("invalid_target")
    if value.get("state_ref") is not None and not isinstance(value.get("state_ref"), Mapping):
        raise ProtocolError("invalid_target")
    return value


def request_message(
    *, request_id: str, connection_id: str, target_handle: str,
    operation: str, parameters: Mapping[str, Any], timeout_seconds: float,
) -> dict[str, Any]:
    timeout_ms = min(MAX_DEADLINE_MS, max(100, int(float(timeout_seconds) * 1000)))
    return validate_message({
        "protocol_version": PROTOCOL_VERSION,
        "message_type": "request",
        "request_id": request_id,
        "connection_id": connection_id,
        "target_handle": target_handle,
        "operation": operation,
        "parameters": dict(parameters),
        "deadline_ms": int(time.time() * 1000) + timeout_ms,
    })


def read_framed(stream: BinaryIO, *, native_endian: bool = False) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ProtocolError("malformed_frame")
    size = struct.unpack("=I" if native_endian else "<I", header)[0]
    if size <= 0 or size > MAX_ENVELOPE_BYTES:
        raise ProtocolError("message_too_large")
    payload = stream.read(size)
    if len(payload) != size:
        raise ProtocolError("malformed_frame")
    return decode_message(payload)


def write_framed(stream: BinaryIO, message: Mapping[str, Any], *, native_endian: bool = False) -> None:
    payload = encode_message(message)
    stream.write(struct.pack("=I" if native_endian else "<I", len(payload)))
    stream.write(payload)
    stream.flush()


__all__ = [
    "CONTROL_OPERATIONS", "KNOWN_OPERATIONS", "MAX_ENVELOPE_BYTES", "PROTOCOL_MAX_VERSION", "PROTOCOL_MIN_VERSION",
    "PROTOCOL_VERSION", "ProtocolError", "decode_message", "encode_message",
    "read_framed", "request_message", "validate_message", "validate_target",
    "write_framed",
]
