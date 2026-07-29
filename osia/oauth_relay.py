"""Process-local coordinator for OAuth callbacks delivered by the relay.

Only routing metadata lives here.  OAuth state, authorization codes and PKCE
verifiers are never persisted or logged; the Gmail and MCP owners remain the
authoritative local flow stores.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


OAUTH_RELAY_REDIRECT_URI = "https://relay.osia.space/oauth/callback"
OAUTH_SCHEMA_VERSION = 1
OAUTH_DEFAULT_TTL_SECONDS = 165
OAUTH_MIN_TTL_SECONDS = 30
OAUTH_MAX_TTL_SECONDS = 300
OAUTH_REGISTRATION_TIMEOUT_SECONDS = 5.0
OAUTH_MAX_LOCAL_FLOWS = 4
_JS_MAX_SAFE_INTEGER = (1 << 53) - 1

TYPE_OAUTH_FLOW_REGISTER = "OAUTH_FLOW_REGISTER"
TYPE_OAUTH_FLOW_REGISTERED = "OAUTH_FLOW_REGISTERED"
TYPE_OAUTH_CALLBACK = "OAUTH_CALLBACK"
TYPE_OAUTH_CALLBACK_ACK = "OAUTH_CALLBACK_ACK"
TYPE_OAUTH_FLOW_CANCEL = "OAUTH_FLOW_CANCEL"

_CALLBACK_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ERROR_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_FLOW_KINDS = frozenset({"gmail", "mcp"})
_TERMINAL_LIFECYCLES = frozenset(
    {"cancelled", "expired", "claimed", "rejected"}
)
_SAFE_REJECTION_CODES = frozenset(
    {
        "oauth_flow_quota",
        "oauth_flow_conflict",
        "oauth_flow_gone",
        "oauth_temporarily_unavailable",
        "oauth_rate_limited",
        "invalid_oauth_control",
    }
)


class OAuthRelayError(RuntimeError):
    """Token-safe typed failure returned to an OAuth orchestration call site."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OAuthRelayUnavailable(OAuthRelayError):
    def __init__(self) -> None:
        super().__init__(
            "relay_unavailable",
            "The secure OAuth relay is unavailable. Reconnect OSIA and try again.",
        )


class OAuthRelayRegistrationTimeout(OAuthRelayError):
    def __init__(self) -> None:
        super().__init__(
            "relay_registration_timeout",
            "The secure OAuth relay did not confirm the flow in time.",
        )


class OAuthRelayRegistrationRejected(OAuthRelayError):
    def __init__(self, reason: str = "oauth_temporarily_unavailable") -> None:
        self.reason = reason if reason in _SAFE_REJECTION_CODES else "oauth_temporarily_unavailable"
        super().__init__(
            "relay_registration_rejected",
            "The secure OAuth relay rejected the authorization flow.",
        )


class OAuthRelayValidationError(OAuthRelayError):
    def __init__(self, code: str = "invalid_oauth_flow") -> None:
        super().__init__(code, "The OAuth relay flow is invalid.")


@dataclass(frozen=True)
class OAuthRelayRegistration:
    callback_id: str
    expires_at: float


@dataclass
class _LocalFlow:
    callback_id: str
    state: str
    flow_kind: str
    owner: str
    expires_at: float
    lifecycle: str
    claim_callback: Callable[[str, str | None, str | None], Any]
    confirmation: threading.Event
    worker_expires_at_ms: int | None = None
    rejection_reason: str = ""
    ack_disposition: str = ""


def callback_id_for_state(state: str) -> str:
    """Return the Worker's exact lowercase-hex SHA-256 callback identifier."""
    if not isinstance(state, str):
        raise OAuthRelayValidationError("invalid_oauth_state")
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def is_valid_callback_id(value: Any) -> bool:
    return isinstance(value, str) and _CALLBACK_ID_RE.fullmatch(value) is not None


def _is_safe_integer(value: Any, *, minimum: int = 0) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= _JS_MAX_SAFE_INTEGER
    )


class OAuthRelayCoordinator:
    """Bounded process-local lifecycle map shared by Gmail and MCP OAuth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._registration_lock = threading.Lock()
        self._flows: dict[str, _LocalFlow] = {}
        self._transport: Any = None
        self._authoritative = False
        self._registered_any = False

    def bind_transport(self, transport: Any) -> None:
        with self._lock:
            self._transport = transport
            self._authoritative = False

    def mark_authoritative(self, transport: Any) -> None:
        with self._lock:
            if self._transport is transport:
                self._authoritative = True

    def mark_disconnected(self, transport: Any) -> None:
        with self._lock:
            if self._transport is transport:
                self._authoritative = False

    def _transport_for_send(self) -> Any:
        with self._lock:
            if not self._authoritative or self._transport is None:
                raise OAuthRelayUnavailable()
            return self._transport

    def _send_control(self, message_type: str, payload: dict) -> None:
        transport = self._transport_for_send()
        try:
            transport.send_oauth_control(message_type, payload)
        except OAuthRelayError:
            raise
        except Exception as exc:
            raise OAuthRelayUnavailable() from exc

    def _prune_locked(self, now: float) -> None:
        for flow in self._flows.values():
            if flow.lifecycle in {"pending_registration", "registered"} and flow.expires_at <= now:
                flow.lifecycle = "expired"
                flow.rejection_reason = "callback_expired"
                flow.state = ""
                flow.confirmation.set()
        stale = [
            callback_id
            for callback_id, flow in self._flows.items()
            if flow.lifecycle in _TERMINAL_LIFECYCLES and flow.expires_at + 30.0 <= now
        ]
        for callback_id in stale:
            self._flows.pop(callback_id, None)

    def register_flow(
        self,
        *,
        state: str,
        flow_kind: str,
        owner: str,
        claim_callback: Callable[[str, str | None, str | None], Any],
        ttl_seconds: int = OAUTH_DEFAULT_TTL_SECONDS,
        timeout: float = OAUTH_REGISTRATION_TIMEOUT_SECONDS,
    ) -> OAuthRelayRegistration:
        # Worker ERROR frames intentionally contain no callback id. Serialize the
        # short registration/confirmation exchange so an error always belongs to
        # the sole pending registration.
        with self._registration_lock:
            return self._register_flow(
                state=state,
                flow_kind=flow_kind,
                owner=owner,
                claim_callback=claim_callback,
                ttl_seconds=ttl_seconds,
                timeout=timeout,
            )

    def _register_flow(
        self,
        *,
        state: str,
        flow_kind: str,
        owner: str,
        claim_callback: Callable[[str, str | None, str | None], Any],
        ttl_seconds: int = OAUTH_DEFAULT_TTL_SECONDS,
        timeout: float = OAUTH_REGISTRATION_TIMEOUT_SECONDS,
    ) -> OAuthRelayRegistration:
        if (
            not isinstance(state, str)
            or _STATE_RE.fullmatch(state) is None
            or flow_kind not in _FLOW_KINDS
            or not isinstance(owner, str)
            or not owner
            or not callable(claim_callback)
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not OAUTH_MIN_TTL_SECONDS <= ttl_seconds <= OAUTH_MAX_TTL_SECONDS
            or not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise OAuthRelayValidationError()

        callback_id = callback_id_for_state(state)
        if not is_valid_callback_id(callback_id):
            raise OAuthRelayValidationError("invalid_callback_id")

        now = time.time()
        with self._lock:
            self._prune_locked(now)
            existing = self._flows.get(callback_id)
            if existing is not None:
                if (
                    existing.state == state
                    and existing.flow_kind == flow_kind
                    and existing.owner == owner
                    and existing.lifecycle == "registered"
                    and existing.expires_at > now
                ):
                    return OAuthRelayRegistration(callback_id, existing.expires_at)
                raise OAuthRelayValidationError("oauth_flow_conflict")
            if len(self._flows) >= OAUTH_MAX_LOCAL_FLOWS:
                terminal = next(
                    (
                        key
                        for key, value in self._flows.items()
                        if value.lifecycle in _TERMINAL_LIFECYCLES
                    ),
                    None,
                )
                if terminal is not None:
                    self._flows.pop(terminal, None)
            if len(self._flows) >= OAUTH_MAX_LOCAL_FLOWS:
                raise OAuthRelayValidationError("oauth_flow_quota")
            if not self._authoritative or self._transport is None:
                raise OAuthRelayUnavailable()
            flow = _LocalFlow(
                callback_id=callback_id,
                state=state,
                flow_kind=flow_kind,
                owner=owner,
                expires_at=now + ttl_seconds,
                lifecycle="pending_registration",
                claim_callback=claim_callback,
                confirmation=threading.Event(),
            )
            self._flows[callback_id] = flow
            self._registered_any = True

        try:
            self._send_control(
                TYPE_OAUTH_FLOW_REGISTER,
                {
                    "schema_version": OAUTH_SCHEMA_VERSION,
                    "callback_id": callback_id,
                    "flow_kind": flow_kind,
                    "ttl_seconds": ttl_seconds,
                },
            )
        except Exception:
            with self._lock:
                current = self._flows.get(callback_id)
                if current is flow:
                    current.lifecycle = "rejected"
                    current.rejection_reason = "relay_unavailable"
                    current.state = ""
                    current.confirmation.set()
            raise

        if not flow.confirmation.wait(timeout=float(timeout)):
            with self._lock:
                if flow.lifecycle == "pending_registration":
                    flow.lifecycle = "rejected"
                    flow.rejection_reason = "relay_registration_timeout"
                    flow.state = ""
            self._cancel_control_best_effort(callback_id)
            raise OAuthRelayRegistrationTimeout()

        with self._lock:
            if flow.lifecycle != "registered" or flow.worker_expires_at_ms is None:
                reason = flow.rejection_reason or "oauth_temporarily_unavailable"
                raise OAuthRelayRegistrationRejected(reason)
            return OAuthRelayRegistration(
                callback_id=callback_id,
                expires_at=flow.worker_expires_at_ms / 1000.0,
            )

    def _cancel_control_best_effort(self, callback_id: str) -> None:
        try:
            self._send_control(
                TYPE_OAUTH_FLOW_CANCEL,
                {
                    "schema_version": OAUTH_SCHEMA_VERSION,
                    "callback_id": callback_id,
                },
            )
        except OAuthRelayError:
            pass

    def cancel_flow(self, *, state: str | None = None, callback_id: str | None = None) -> bool:
        resolved_id = callback_id or (callback_id_for_state(state) if state else "")
        if not is_valid_callback_id(resolved_id):
            return False
        with self._lock:
            flow = self._flows.get(resolved_id)
            if flow is None or flow.lifecycle in _TERMINAL_LIFECYCLES:
                return False
            flow.lifecycle = "cancelled"
            flow.rejection_reason = "callback_cancelled"
            flow.state = ""
            flow.confirmation.set()
        self._cancel_control_best_effort(resolved_id)
        return True

    def handle_flow_registered(self, message: dict, pair_id: str) -> bool:
        payload = self._validated_envelope(message, TYPE_OAUTH_FLOW_REGISTERED, pair_id)
        if payload is None or set(payload) != {"schema_version", "callback_id", "expires_at"}:
            return False
        callback_id = payload.get("callback_id")
        expires_at = payload.get("expires_at")
        if (
            payload.get("schema_version") != OAUTH_SCHEMA_VERSION
            or not is_valid_callback_id(callback_id)
            or not _is_safe_integer(expires_at, minimum=1)
        ):
            return False
        with self._lock:
            flow = self._flows.get(callback_id)
            if flow is None or flow.lifecycle != "pending_registration":
                return False
            if expires_at <= int(time.time() * 1000):
                flow.lifecycle = "expired"
                flow.rejection_reason = "callback_expired"
                flow.state = ""
            else:
                flow.lifecycle = "registered"
                flow.worker_expires_at_ms = expires_at
                flow.expires_at = expires_at / 1000.0
            flow.confirmation.set()
        return True

    def handle_control_error(self, code: str) -> bool:
        if code not in _SAFE_REJECTION_CODES:
            return False
        with self._lock:
            pending = next(
                (flow for flow in self._flows.values() if flow.lifecycle == "pending_registration"),
                None,
            )
            if pending is None:
                return False
            pending.lifecycle = "rejected"
            pending.rejection_reason = code
            pending.state = ""
            pending.confirmation.set()
        return True

    @staticmethod
    def _validated_envelope(message: Any, expected_type: str, pair_id: str) -> dict | None:
        if not isinstance(message, dict) or set(message) != {
            "v", "type", "role", "pair_id", "seq", "ack", "payload"
        }:
            return None
        if (
            message.get("v") != 2
            or message.get("type") != expected_type
            or message.get("role") != "pc"
            or message.get("pair_id") != pair_id
            or message.get("seq") != 0
            or not _is_safe_integer(message.get("ack"))
            or not isinstance(message.get("payload"), dict)
        ):
            return None
        return message["payload"]

    def handle_callback(
        self, message: dict, pair_id: str
    ) -> tuple[dict | None, Callable[[], Any] | None, str]:
        payload = self._validated_callback_payload(message, pair_id)
        if payload is None:
            basic = self._validated_envelope(message, TYPE_OAUTH_CALLBACK, pair_id)
            callback_id = basic.get("callback_id") if basic is not None else None
            if is_valid_callback_id(callback_id):
                return (
                    {
                        "schema_version": OAUTH_SCHEMA_VERSION,
                        "callback_id": callback_id,
                        "disposition": "rejected",
                    },
                    None,
                    "malformed_callback",
                )
            return None, None, "malformed_callback"
        callback_id = payload["callback_id"]
        disposition = "rejected"
        completion: Callable[[], Any] | None = None
        reason = "unknown_callback" if self._registered_any else "callback_after_restart"
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            flow = self._flows.get(callback_id)
            if flow is not None:
                if flow.lifecycle == "claimed":
                    disposition = flow.ack_disposition or "consumed"
                    reason = "callback_replayed"
                elif flow.lifecycle == "cancelled":
                    reason = "callback_cancelled"
                elif flow.lifecycle == "expired" or flow.expires_at <= now:
                    flow.lifecycle = "expired"
                    reason = "callback_expired"
                elif flow.lifecycle != "registered":
                    reason = "callback_not_registered"
                elif flow.flow_kind != payload["flow_kind"]:
                    flow.lifecycle = "rejected"
                    flow.rejection_reason = "wrong_flow_kind"
                    flow.state = ""
                    reason = "wrong_flow_kind"
                elif flow.worker_expires_at_ms != payload["expires_at"]:
                    flow.lifecycle = "rejected"
                    flow.rejection_reason = "callback_expiry_mismatch"
                    flow.state = ""
                    reason = "callback_expiry_mismatch"
                else:
                    try:
                        claimed = flow.claim_callback(
                            flow.state,
                            payload.get("code"),
                            payload.get("error"),
                        )
                    except Exception:
                        claimed = None
                    if claimed is not None and claimed is not False:
                        flow.lifecycle = "claimed"
                        flow.ack_disposition = "consumed"
                        flow.state = ""
                        disposition = "consumed"
                        completion = claimed if callable(claimed) else None
                        reason = "accepted"
                    else:
                        flow.lifecycle = "rejected"
                        flow.rejection_reason = "local_flow_unavailable"
                        flow.state = ""
                        reason = "local_flow_unavailable"
        return (
            {
                "schema_version": OAUTH_SCHEMA_VERSION,
                "callback_id": callback_id,
                "disposition": disposition,
            },
            completion,
            reason,
        )

    @staticmethod
    def _validated_callback_payload(message: Any, pair_id: str) -> dict | None:
        payload = OAuthRelayCoordinator._validated_envelope(
            message, TYPE_OAUTH_CALLBACK, pair_id
        )
        if payload is None:
            return None
        has_code = "code" in payload
        has_error = "error" in payload
        allowed = (
            {"schema_version", "callback_id", "flow_kind", "code", "expires_at"}
            if has_code
            else {
                "schema_version", "callback_id", "flow_kind", "error",
                "error_description", "expires_at",
            }
        )
        if (
            has_code == has_error
            or not set(payload).issubset(allowed)
            or (has_code and set(payload) != allowed)
            or (has_error and not {
                "schema_version", "callback_id", "flow_kind", "error", "expires_at"
            }.issubset(payload))
            or payload.get("schema_version") != OAUTH_SCHEMA_VERSION
            or not is_valid_callback_id(payload.get("callback_id"))
            or payload.get("flow_kind") not in _FLOW_KINDS
            or not _is_safe_integer(payload.get("expires_at"), minimum=1)
        ):
            return None
        if has_code:
            code = payload.get("code")
            if not isinstance(code, str) or not code or len(code.encode("utf-8")) > 4096:
                return None
        else:
            error = payload.get("error")
            description = payload.get("error_description")
            if not isinstance(error, str) or _ERROR_RE.fullmatch(error) is None:
                return None
            if description is not None and (
                not isinstance(description, str)
                or len(description) > 256
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in description)
            ):
                return None
        return payload

    @staticmethod
    def dispatch_completion(completion: Callable[[], Any] | None) -> None:
        if completion is None:
            return

        def _run() -> None:
            try:
                completion()
            except Exception:
                return

        threading.Thread(
            target=_run,
            name="osia-oauth-relay-completion",
            daemon=True,
        ).start()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._flows.clear()
            self._transport = None
            self._authoritative = False
            self._registered_any = False


coordinator = OAuthRelayCoordinator()
