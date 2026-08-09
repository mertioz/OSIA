"""Durable, privacy-bounded tool-attempt lifecycle for normal chat (WOR-227).

The store is intentionally independent from the agent, browser and Autonomous
modules.  It persists only correlation and safety metadata; tool arguments,
tool results, page content and browser credentials never cross this boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from osia.config import CONFIG_DIR


SCHEMA_VERSION = 1
WAITING_TOOL_RESULT = "WAITING_TOOL_RESULT"
_INTERNAL_TOOL_CONTROL_STATES = frozenset({
    WAITING_TOOL_RESULT,
    "WAITING_TOOL",
    "RECONCILING",
    "INTERNAL_RETRY",
    "TOOL_RETRY",
})
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
ANTI_REPLAY_RETENTION_SECONDS = 30 * 24 * 60 * 60
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_OPAQUE_BROWSER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_PERSISTENCE_STATES = frozenset({"waiting", "reconciling", "stalled", "terminal"})
_PERSISTENCE_FAMILIES = frozenset({"browser", "file", "api_mcp", "system", "generic"})


def is_internal_tool_control_text(value: Any) -> bool:
    """Return whether *value* is a whole internal orchestration marker.

    These values are transport/control state, never assistant-authored prose.
    Matching is deliberately restricted to the complete marker-shaped value;
    ordinary sentences that happen to discuss waiting or retries are untouched.
    """

    normalized = str(value or "").strip().upper()
    if normalized in _INTERNAL_TOOL_CONTROL_STATES:
        return True
    return bool(re.fullmatch(
        r"(?:WAITING_TOOL|RECONCILING|INTERNAL_RETRY|TOOL_RETRY)"
        r"(?:_[A-Z0-9]+)*",
        normalized,
    ))


class ToolAttemptStatus(str, Enum):
    NOT_STARTED = "not_started"
    ACCEPTED = "accepted"
    RUNNING = "running"
    PENDING = "pending"
    WAITING_VERIFICATION = "waiting_verification"
    SUCCEEDED = "succeeded"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    FAILED_AFTER_EFFECT = "failed_after_effect"
    UNKNOWN_AFTER_START = "unknown_after_start"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED_PROVEN = "cancelled_proven"


TERMINAL_STATUSES = frozenset({
    ToolAttemptStatus.SUCCEEDED,
    ToolAttemptStatus.FAILED_BEFORE_EFFECT,
    ToolAttemptStatus.FAILED_AFTER_EFFECT,
    ToolAttemptStatus.UNKNOWN_AFTER_START,
    ToolAttemptStatus.CANCELLED_PROVEN,
})
RECOVERABLE_STATUSES = frozenset({
    ToolAttemptStatus.ACCEPTED,
    ToolAttemptStatus.RUNNING,
    ToolAttemptStatus.PENDING,
    ToolAttemptStatus.WAITING_VERIFICATION,
    ToolAttemptStatus.CANCELLATION_REQUESTED,
})


class RevisionConflict(RuntimeError):
    pass


class IllegalTransition(RuntimeError):
    pass


class CorruptAttemptError(RuntimeError):
    """A record cannot be trusted and therefore must never trigger replay."""


@dataclass(frozen=True)
class ToolAttemptEvent:
    attempt_id: str
    phase: str
    reason_code: str = ""


@dataclass
class _SuspendedRuntime:
    resume: Callable[[str, Any], str]
    finalize: Callable[[str, str, BaseException | None], None]
    status: Callable[[str, str], None] | None = None
    verify: Callable[[str], Any] | None = None
    activated: bool = False
    terminal_payload: Any = None
    timer: threading.Timer | None = None
    running: bool = False


_TRANSITIONS = {
    ToolAttemptStatus.NOT_STARTED: {
        ToolAttemptStatus.ACCEPTED,
        ToolAttemptStatus.FAILED_BEFORE_EFFECT,
        ToolAttemptStatus.CANCELLED_PROVEN,
    },
    ToolAttemptStatus.ACCEPTED: {
        ToolAttemptStatus.RUNNING,
        ToolAttemptStatus.FAILED_BEFORE_EFFECT,
        ToolAttemptStatus.CANCELLATION_REQUESTED,
        ToolAttemptStatus.CANCELLED_PROVEN,
    },
    ToolAttemptStatus.RUNNING: {
        ToolAttemptStatus.PENDING,
        ToolAttemptStatus.WAITING_VERIFICATION,
        ToolAttemptStatus.SUCCEEDED,
        ToolAttemptStatus.FAILED_BEFORE_EFFECT,
        ToolAttemptStatus.FAILED_AFTER_EFFECT,
        ToolAttemptStatus.UNKNOWN_AFTER_START,
        ToolAttemptStatus.CANCELLATION_REQUESTED,
        ToolAttemptStatus.CANCELLED_PROVEN,
    },
    ToolAttemptStatus.PENDING: {
        ToolAttemptStatus.WAITING_VERIFICATION,
        ToolAttemptStatus.SUCCEEDED,
        ToolAttemptStatus.FAILED_BEFORE_EFFECT,
        ToolAttemptStatus.FAILED_AFTER_EFFECT,
        ToolAttemptStatus.UNKNOWN_AFTER_START,
        ToolAttemptStatus.CANCELLATION_REQUESTED,
        ToolAttemptStatus.CANCELLED_PROVEN,
    },
    ToolAttemptStatus.WAITING_VERIFICATION: {
        ToolAttemptStatus.PENDING,
        ToolAttemptStatus.SUCCEEDED,
        ToolAttemptStatus.FAILED_BEFORE_EFFECT,
        ToolAttemptStatus.FAILED_AFTER_EFFECT,
        ToolAttemptStatus.UNKNOWN_AFTER_START,
        ToolAttemptStatus.CANCELLATION_REQUESTED,
        ToolAttemptStatus.CANCELLED_PROVEN,
    },
    ToolAttemptStatus.CANCELLATION_REQUESTED: {
        ToolAttemptStatus.PENDING,
        ToolAttemptStatus.WAITING_VERIFICATION,
        ToolAttemptStatus.SUCCEEDED,
        ToolAttemptStatus.FAILED_AFTER_EFFECT,
        ToolAttemptStatus.UNKNOWN_AFTER_START,
        ToolAttemptStatus.CANCELLED_PROVEN,
    },
}


def _now() -> float:
    return time.time()


def _pid_alive(value: Any) -> bool:
    """Best-effort live-owner check for restart-safe durable claims."""

    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            handle = kernel32.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                pid,
            )
            if handle:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                return True
            return ctypes.get_last_error() == 5  # access denied still means alive
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_token(value: Any, *, field: str, allow_empty: bool = False) -> str:
    token = str(value or "").strip()
    if not token and allow_empty:
        return ""
    if field in {"provider_id", "model_id"}:
        if 1 <= len(token) <= 160 and not any(ord(char) < 32 for char in token):
            return token
        raise ValueError(f"invalid_{field}")
    if not _SAFE_TOKEN.fullmatch(token):
        raise ValueError(f"invalid_{field}")
    return token


def _safe_reason(value: Any) -> str:
    reason = str(value or "").strip().lower()
    if not reason:
        return ""
    return reason if _SAFE_REASON.fullmatch(reason) else "private_reason_withheld"


def _safe_turn_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    allowed = [
        str(item)[:16]
        for item in source.get("allowed_languages", ())
        if isinstance(item, str) and re.fullmatch(r"[a-z]{2,3}", item)
    ][:4]
    return {
        "language": str(source.get("language") or "")[:16],
        "source": str(source.get("source") or "")[:48],
        "turn_ref": str(source.get("turn_ref") or "")[:96],
        "mode": str(source.get("mode") or "single-language")[:32],
        "allowed_languages": allowed,
        "source_language": str(source.get("source_language") or "")[:16],
        "target_language": str(source.get("target_language") or "")[:16],
    }


def _safe_browser_state(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    string_fields = (
        "browser_session_id", "context_id", "page_id", "navigation_id",
    )
    int_fields = (
        "schema_version", "context_epoch", "page_generation", "document_epoch",
    )
    result: dict[str, Any] = {}
    for key in string_fields:
        item = str(value.get(key) or "")
        if not _OPAQUE_BROWSER_TOKEN.fullmatch(item):
            return None
        result[key] = item
    for key in int_fields:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        result[key] = item
    created_at = value.get("created_at")
    if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
        result["created_at"] = float(created_at)
    return result


def _safe_persistence_contract(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    family = str(source.get("family") or "generic")
    if family not in _PERSISTENCE_FAMILIES:
        family = "generic"

    def _seconds(name: str, default: float) -> float:
        try:
            result = float(source.get(name, default))
        except (TypeError, ValueError, OverflowError):
            result = default
        return max(0.05, min(result, 86_400.0))

    inactivity = _seconds("inactivity_timeout_seconds", 30.0)
    hard = max(inactivity, _seconds("hard_timeout_seconds", inactivity))
    delays: list[float] = []
    for item in source.get("check_delays_seconds", ()):
        try:
            delay = float(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0.05 <= delay <= 300.0:
            delays.append(delay)
        if len(delays) >= 12:
            break
    if not delays:
        delays = [2.0, 5.0]
    alternatives = []
    for item in source.get("safe_alternatives", ()):
        candidate = _safe_reason(item)
        if candidate and candidate not in alternatives:
            alternatives.append(candidate)
        if len(alternatives) >= 8:
            break
    return {
        "family": family,
        "release_after_seconds": _seconds("release_after_seconds", 2.0),
        "inactivity_timeout_seconds": inactivity,
        "hard_timeout_seconds": hard,
        "check_delays_seconds": delays,
        "safe_alternatives": alternatives,
        # Legacy WOR-227 records had no restart-reconciliation contract and
        # retain their original fail-closed behavior. New attempts opt in
        # explicitly through the family policy snapshot.
        "restart_reconciliation": source.get("restart_reconciliation") is True,
    }


class ToolAttemptStore:
    """Atomic JSON-file store with cross-process locking and revision CAS."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root) if root is not None else Path(CONFIG_DIR) / "tool_attempts"
        self.attempts_dir = self.root / "attempts"
        self.barriers_dir = self.root / "barriers"
        self._lock = threading.RLock()

    def _ensure_dirs(self) -> None:
        self.attempts_dir.mkdir(parents=True, exist_ok=True)
        self.barriers_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _store_lock(self):
        self._ensure_dirs()
        lock_path = self.root / ".store.lock"
        with self._lock, lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _path(self, attempt_id: str) -> Path:
        return self.attempts_dir / f"{_safe_token(attempt_id, field='attempt_id')}.json"

    def _key(self) -> bytes:
        path = self.root / ".fingerprint.key"
        with self._store_lock():
            if path.exists():
                key = path.read_bytes()
                if len(key) == 32:
                    return key
                raise CorruptAttemptError("invalid_fingerprint_key")
            key = secrets.token_bytes(32)
            # The store lock makes this one-time exclusive creation atomic
            # across the desktop/server processes.  Never replace an existing
            # key: doing so would invalidate durable anti-replay fingerprints.
            with path.open("xb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            return key

    def fingerprint_arguments(self, arguments: Mapping[str, Any]) -> str:
        """Return a keyed fingerprint; raw values are never written to disk."""

        encoded = json.dumps(
            arguments if isinstance(arguments, Mapping) else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda _value: "<unsupported>",
        ).encode("utf-8", errors="replace")
        return "hmac256:" + hmac.new(self._key(), encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate(record: Any) -> dict[str, Any]:
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise CorruptAttemptError("invalid_attempt_schema")
        try:
            status = ToolAttemptStatus(record["status"])
            revision = int(record["revision"])
            repair_count = int(record.get("repair_count", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptAttemptError("invalid_attempt_state") from exc
        if revision < 1 or repair_count not in {0, 1}:
            raise CorruptAttemptError("invalid_attempt_revision")
        for field in (
            "tool_attempt_id", "conversation_id", "turn_id", "request_id",
            "tool_call_id", "tool_name", "provider_id", "model_id",
        ):
            try:
                _safe_token(record.get(field), field=field, allow_empty=field == "request_id")
            except ValueError as exc:
                raise CorruptAttemptError(str(exc)) from exc
        fingerprint = str(record.get("argument_fingerprint") or "")
        if not re.fullmatch(r"hmac256:[0-9a-f]{64}", fingerprint):
            raise CorruptAttemptError("invalid_argument_fingerprint")
        equivalence = str(record.get("equivalence_fingerprint") or "")
        if equivalence and not re.fullmatch(r"hmac256:[0-9a-f]{64}", equivalence):
            raise CorruptAttemptError("invalid_equivalence_fingerprint")
        if status in TERMINAL_STATUSES and not record.get("terminal_outcome"):
            raise CorruptAttemptError("terminal_outcome_missing")
        browser_state = record.get("browser_state_ref")
        if browser_state is not None and _safe_browser_state(browser_state) is None:
            raise CorruptAttemptError("invalid_browser_state_ref")
        owner_scope = str(record.get("owner_scope") or "chat")
        if owner_scope not in {"chat", "autonomous"}:
            raise CorruptAttemptError("invalid_owner_scope")
        record["owner_scope"] = owner_scope
        for field in (
            "autonomous_run_id", "autonomous_task_id",
            "autonomous_operation_id",
        ):
            try:
                record[field] = _safe_token(
                    record.get(field), field=field, allow_empty=True,
                )
            except ValueError as exc:
                raise CorruptAttemptError(str(exc)) from exc
        for field in ("operation_revision", "permission_epoch"):
            value = record.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CorruptAttemptError("invalid_autonomous_revision")
            record[field] = value
        record.setdefault("continuation_claimed_at", None)
        record.setdefault("continuation_claimed_pid", None)
        record.setdefault("reconciliation_claimed_pid", None)
        policy = _safe_persistence_contract(record.get("persistence_contract"))
        record["persistence_contract"] = policy
        hard_deadline = record.get("hard_deadline_at", record.get("deadline_at"))
        inactivity_deadline = record.get("inactivity_deadline_at", hard_deadline)
        for field, value in (
            ("hard_deadline_at", hard_deadline),
            ("inactivity_deadline_at", inactivity_deadline),
        ):
            try:
                record[field] = float(value) if value is not None else None
            except (TypeError, ValueError, OverflowError) as exc:
                raise CorruptAttemptError("invalid_persistence_deadline") from exc
        persistence_state = str(record.get("persistence_state") or "")
        if not persistence_state:
            persistence_state = (
                "terminal" if status in TERMINAL_STATUSES else "waiting"
            )
        if persistence_state not in _PERSISTENCE_STATES:
            raise CorruptAttemptError("invalid_persistence_state")
        record["persistence_state"] = persistence_state
        record.setdefault(
            "terminal_state", "terminal" if status in TERMINAL_STATUSES else "",
        )
        record.setdefault("stalled_at", None)
        for field in ("reconciliation_check_index", "useful_progress_count"):
            try:
                number = int(record.get(field, 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise CorruptAttemptError("invalid_persistence_counter") from exc
            if number < 0 or number > 1_000_000:
                raise CorruptAttemptError("invalid_persistence_counter")
            record[field] = number
        fingerprints = record.get("useful_progress_fingerprints", [])
        if not isinstance(fingerprints, list) or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item or ""))
            for item in fingerprints
        ):
            raise CorruptAttemptError("invalid_progress_fingerprints")
        record["useful_progress_fingerprints"] = fingerprints[-32:]
        attempted = record.get("attempted_safe_alternatives", [])
        if not isinstance(attempted, list):
            raise CorruptAttemptError("invalid_safe_alternatives")
        safe_attempted = []
        for item in attempted:
            candidate = _safe_reason(item)
            if not candidate or candidate not in policy["safe_alternatives"]:
                raise CorruptAttemptError("invalid_safe_alternatives")
            if candidate not in safe_attempted:
                safe_attempted.append(candidate)
        record["attempted_safe_alternatives"] = safe_attempted
        return dict(record)

    def _read_unlocked(self, attempt_id: str) -> dict[str, Any]:
        path = self._path(attempt_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptAttemptError("attempt_record_unreadable") from exc
        return self._validate(raw)

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._store_lock():
            path = self._path(attempt_id)
            return self._read_unlocked(attempt_id) if path.exists() else None

    def _active_for_conversation_unlocked(self, conversation_id: str) -> dict | None:
        for path in self.attempts_dir.glob("attempt_*.json"):
            try:
                record = self._validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, CorruptAttemptError):
                continue
            if (
                record.get("conversation_id") == conversation_id
                and ToolAttemptStatus(record["status"]) not in TERMINAL_STATUSES
            ):
                return record
        return None

    def create_attempt(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        request_id: str = "",
        tool_call_id: str,
        tool_name: str,
        provider_id: str,
        model_id: str,
        turn_context: Mapping[str, Any] | None,
        argument_fingerprint: str,
        side_effect: str,
        idempotence: str,
        retry_policy: str,
        equivalence_fingerprint: str = "",
        remote_operation_key: str = "",
        next_check_at: float | None = None,
        deadline_at: float | None = None,
        postcondition_type: str = "",
        browser_state_ref: Mapping[str, Any] | None = None,
        repair_count: int = 0,
        final_response_message_id: str = "",
        owner_scope: str = "chat",
        autonomous_run_id: str = "",
        autonomous_task_id: str = "",
        autonomous_operation_id: str = "",
        operation_revision: int = 0,
        permission_epoch: int = 0,
        persistence_contract: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation_id = _safe_token(conversation_id, field="conversation_id")
        turn_id = _safe_token(turn_id, field="turn_id")
        request_id = _safe_token(request_id, field="request_id", allow_empty=True)
        tool_call_id = _safe_token(tool_call_id, field="tool_call_id")
        tool_name = _safe_token(tool_name, field="tool_name")
        provider_id = _safe_token(provider_id, field="provider_id")
        model_id = _safe_token(model_id, field="model_id")
        if not re.fullmatch(r"hmac256:[0-9a-f]{64}", argument_fingerprint):
            raise ValueError("invalid_argument_fingerprint")
        if equivalence_fingerprint and not re.fullmatch(
            r"hmac256:[0-9a-f]{64}", equivalence_fingerprint,
        ):
            raise ValueError("invalid_equivalence_fingerprint")
        if repair_count not in {0, 1}:
            raise ValueError("invalid_repair_count")
        owner_scope = str(owner_scope or "chat")
        if owner_scope not in {"chat", "autonomous"}:
            raise ValueError("invalid_owner_scope")
        for name, value in (
            ("autonomous_run_id", autonomous_run_id),
            ("autonomous_task_id", autonomous_task_id),
            ("autonomous_operation_id", autonomous_operation_id),
        ):
            _safe_token(value, field=name, allow_empty=True)
        if (
            isinstance(operation_revision, bool)
            or not isinstance(operation_revision, int)
            or operation_revision < 0
            or isinstance(permission_epoch, bool)
            or not isinstance(permission_epoch, int)
            or permission_epoch < 0
        ):
            raise ValueError("invalid_autonomous_revision")
        now = _now()
        persistence = _safe_persistence_contract(persistence_contract)
        hard_deadline = (
            float(deadline_at)
            if deadline_at is not None
            else now + persistence["hard_timeout_seconds"]
        )
        inactivity_deadline = min(
            hard_deadline,
            now + persistence["inactivity_timeout_seconds"],
        )
        attempt_id = "attempt_" + secrets.token_urlsafe(24)
        record = {
            "schema_version": SCHEMA_VERSION,
            "tool_attempt_id": attempt_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "provider_id": provider_id,
            "model_id": model_id,
            "turn_context": _safe_turn_context(turn_context),
            "argument_fingerprint": argument_fingerprint,
            "equivalence_fingerprint": str(equivalence_fingerprint or ""),
            "side_effect": _safe_token(side_effect, field="side_effect"),
            "idempotence": _safe_token(idempotence, field="idempotence"),
            "retry_policy": _safe_token(retry_policy, field="retry_policy"),
            "remote_operation_key": _safe_token(
                remote_operation_key, field="remote_operation_key", allow_empty=True,
            ),
            "status": ToolAttemptStatus.NOT_STARTED.value,
            "accepted_at": None,
            "started_at": None,
            "last_progress_at": None,
            "completed_at": None,
            "next_check_at": float(next_check_at) if next_check_at is not None else None,
            "deadline_at": hard_deadline,
            "hard_deadline_at": hard_deadline,
            "inactivity_deadline_at": inactivity_deadline,
            "persistence_contract": persistence,
            "persistence_state": "waiting",
            "terminal_state": "",
            "stalled_at": None,
            "reconciliation_check_index": 0,
            "useful_progress_count": 0,
            "useful_progress_fingerprints": [],
            "attempted_safe_alternatives": [],
            "postcondition_type": _safe_reason(postcondition_type),
            "browser_state_ref": _safe_browser_state(browser_state_ref),
            "dispatch_state": "not_dispatched",
            "terminal_outcome": "",
            "reason_code": "",
            "repair_count": repair_count,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "final_response_message_id": _safe_token(
                final_response_message_id,
                field="final_response_message_id",
                allow_empty=True,
            ),
            "continuation_state": "not_claimed",
            "continuation_claimed_at": None,
            "continuation_claimed_pid": None,
            "reconciliation_claim": "",
            "reconciliation_claimed_at": None,
            "reconciliation_claimed_pid": None,
            "owner_scope": owner_scope,
            "autonomous_run_id": str(autonomous_run_id or ""),
            "autonomous_task_id": str(autonomous_task_id or ""),
            "autonomous_operation_id": str(autonomous_operation_id or ""),
            "operation_revision": operation_revision,
            "permission_epoch": permission_epoch,
        }
        with self._store_lock():
            if self._active_for_conversation_unlocked(conversation_id) is not None:
                raise IllegalTransition("conversation_has_active_tool_attempt")
            self._write_atomic(self._path(attempt_id), record)
        return dict(record)

    def _transition(
        self,
        attempt_id: str,
        status: ToolAttemptStatus,
        *,
        expected_revision: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            if expected_revision is not None and int(record["revision"]) != int(expected_revision):
                raise RevisionConflict("stale_tool_attempt_revision")
            if current in TERMINAL_STATUSES:
                if current == status:
                    return record
                raise IllegalTransition("terminal_tool_attempt_is_immutable")
            if status != current and status not in _TRANSITIONS.get(current, set()):
                raise IllegalTransition(f"illegal_transition:{current.value}:{status.value}")
            now = _now()
            record["status"] = status.value
            record["updated_at"] = now
            record["revision"] = int(record["revision"]) + 1
            if updates:
                record.update(dict(updates))
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def mark_accepted(self, attempt_id: str, *, expected_revision: int | None = None) -> dict:
        return self._transition(
            attempt_id, ToolAttemptStatus.ACCEPTED,
            expected_revision=expected_revision,
            updates={"accepted_at": _now()},
        )

    def mark_running(self, attempt_id: str, *, expected_revision: int | None = None) -> dict:
        # Dispatch and a pre-dispatch cancellation race on this boundary.  Keep
        # the decision under the store lock so a proven cancellation can never
        # be overwritten by a later RUNNING transition.
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            if (
                current is ToolAttemptStatus.CANCELLED_PROVEN
                and record.get("dispatch_state") == "cancelled_before_effect"
            ):
                return record
            if current in TERMINAL_STATUSES:
                raise IllegalTransition("terminal_tool_attempt_is_immutable")
            if expected_revision is not None and int(record["revision"]) != int(expected_revision):
                raise RevisionConflict("stale_tool_attempt_revision")
            if (
                current is not ToolAttemptStatus.RUNNING
                and ToolAttemptStatus.RUNNING not in _TRANSITIONS.get(current, set())
            ):
                raise IllegalTransition(
                    f"illegal_transition:{current.value}:{ToolAttemptStatus.RUNNING.value}"
                )
            now = _now()
            record.update({
                "status": ToolAttemptStatus.RUNNING.value,
                "started_at": now,
                "last_progress_at": now,
                "dispatch_state": "dispatch_started",
                "updated_at": now,
                "revision": int(record["revision"]) + 1,
            })
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def mark_pending(
        self, attempt_id: str, *, next_check_at: float, deadline_at: float,
        reason_code: str = "owner_still_running",
        check_index: int | None = None,
    ) -> dict:
        updates = {
            "next_check_at": float(next_check_at),
            "deadline_at": float(deadline_at),
            "hard_deadline_at": float(deadline_at),
            "dispatch_state": "worker_may_be_active",
            "reason_code": _safe_reason(reason_code),
            "persistence_state": "waiting",
        }
        if check_index is not None:
            updates["reconciliation_check_index"] = max(0, int(check_index))
        return self._transition(
            attempt_id, ToolAttemptStatus.PENDING,
            updates=updates,
        )

    def mark_waiting_verification(
        self, attempt_id: str, *, next_check_at: float, deadline_at: float,
        reason_code: str = "postcondition_not_observed",
        owner_settled: bool = False,
        check_index: int | None = None,
    ) -> dict:
        updates = {
            "next_check_at": float(next_check_at),
            "deadline_at": float(deadline_at),
            "hard_deadline_at": float(deadline_at),
            "reason_code": _safe_reason(reason_code),
            "persistence_state": "waiting",
        }
        if owner_settled:
            updates["dispatch_state"] = "owner_settled_unverified"
        if check_index is not None:
            updates["reconciliation_check_index"] = max(0, int(check_index))
        return self._transition(
            attempt_id, ToolAttemptStatus.WAITING_VERIFICATION,
            updates=updates,
        )

    def record_progress(
        self, attempt_id: str, *, phase: str = "running",
        next_check_at: float | None = None,
        useful: bool = False,
        evidence_token: str = "",
    ) -> dict:
        record = self.get_attempt(attempt_id)
        if record is None or ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
            return record or {}
        now = _now()
        updates: dict[str, Any] = {"reason_code": _safe_reason(phase)}
        if useful and evidence_token:
            fingerprint = "sha256:" + hashlib.sha256(
                str(evidence_token).encode("utf-8", errors="replace")
            ).hexdigest()
            seen = list(record.get("useful_progress_fingerprints") or [])
            if fingerprint not in seen:
                seen.append(fingerprint)
                policy = record.get("persistence_contract") or {}
                inactivity = float(
                    policy.get("inactivity_timeout_seconds") or 0.0
                )
                hard_deadline = float(
                    record.get("hard_deadline_at")
                    or record.get("deadline_at")
                    or now
                )
                updates.update({
                    "last_progress_at": now,
                    "inactivity_deadline_at": min(
                        hard_deadline, now + max(0.05, inactivity),
                    ),
                    "useful_progress_count": int(
                        record.get("useful_progress_count") or 0
                    ) + 1,
                    "useful_progress_fingerprints": seen[-32:],
                    "reconciliation_check_index": 0,
                })
        if next_check_at is not None:
            updates["next_check_at"] = float(next_check_at)
        return self._transition(attempt_id, ToolAttemptStatus(record["status"]), updates=updates)

    def record_safe_alternative(self, attempt_id: str, strategy: str) -> bool:
        """Claim one configured read-only alternative at most once."""

        strategy = _safe_reason(strategy)
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return False
            allowed = list(
                (record.get("persistence_contract") or {}).get(
                    "safe_alternatives", ()
                )
            )
            attempted = list(record.get("attempted_safe_alternatives") or [])
            if not strategy or strategy not in allowed or strategy in attempted:
                return False
            attempted.append(strategy)
            record["attempted_safe_alternatives"] = attempted
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def record_terminal_result(
        self,
        attempt_id: str,
        *,
        status: ToolAttemptStatus | str,
        terminal_outcome: str,
        dispatch_state: str,
        reason_code: str = "",
        expected_revision: int | None = None,
    ) -> tuple[dict, bool]:
        status = ToolAttemptStatus(status)
        if status not in TERMINAL_STATUSES:
            raise ValueError("terminal_status_required")
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            if current in TERMINAL_STATUSES:
                return record, False
            if expected_revision is not None and int(record["revision"]) != int(expected_revision):
                raise RevisionConflict("stale_tool_attempt_revision")
            if status not in _TRANSITIONS.get(current, set()):
                raise IllegalTransition(
                    f"illegal_transition:{current.value}:{status.value}"
                )
            now = _now()
            safe_reason = _safe_reason(reason_code)
            stalled = safe_reason in {
                "reconciliation_stalled", "owner_timeout_stalled",
            }
            record.update({
                "status": status.value,
                "terminal_outcome": _safe_token(
                    terminal_outcome, field="terminal_outcome",
                ),
                "dispatch_state": _safe_token(dispatch_state, field="dispatch_state"),
                "reason_code": safe_reason,
                "completed_at": now,
                "next_check_at": None,
                "reconciliation_claim": "",
                "reconciliation_claimed_at": None,
                "reconciliation_claimed_pid": None,
                "persistence_state": "stalled" if stalled else "terminal",
                "terminal_state": "terminal",
                "stalled_at": now if stalled else record.get("stalled_at"),
                "updated_at": now,
                "revision": int(record["revision"]) + 1,
            })
            self._write_atomic(self._path(attempt_id), record)
            return dict(record), True

    def request_cancellation(self, attempt_id: str) -> dict:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            if current in TERMINAL_STATUSES:
                return record
            now = _now()
            if (
                current in {
                    ToolAttemptStatus.NOT_STARTED,
                    ToolAttemptStatus.ACCEPTED,
                }
                and record.get("dispatch_state") == "not_dispatched"
            ):
                record.update({
                    "status": ToolAttemptStatus.CANCELLED_PROVEN.value,
                    "terminal_outcome": "cancelled_proven",
                    "dispatch_state": "cancelled_before_effect",
                    "reason_code": "cancellation_acknowledged_before_effect",
                    "completed_at": now,
                    "next_check_at": None,
                    "persistence_state": "terminal",
                    "terminal_state": "terminal",
                    "updated_at": now,
                    "revision": int(record["revision"]) + 1,
                })
            else:
                if (
                    current is not ToolAttemptStatus.CANCELLATION_REQUESTED
                    and ToolAttemptStatus.CANCELLATION_REQUESTED
                    not in _TRANSITIONS.get(current, set())
                ):
                    raise IllegalTransition(
                        "illegal_transition:"
                        f"{current.value}:"
                        f"{ToolAttemptStatus.CANCELLATION_REQUESTED.value}"
                    )
                record.update({
                    "status": ToolAttemptStatus.CANCELLATION_REQUESTED.value,
                    "reason_code": "cancellation_requested",
                    "updated_at": now,
                    "revision": int(record["revision"]) + 1,
                })
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def mark_cancellation_proven(self, attempt_id: str) -> tuple[dict, bool]:
        return self.record_terminal_result(
            attempt_id,
            status=ToolAttemptStatus.CANCELLED_PROVEN,
            terminal_outcome="cancelled_proven",
            dispatch_state="cancelled_before_effect",
            reason_code="cancellation_acknowledged_before_effect",
        )

    def list_recoverable_attempts(self, *, now: float | None = None) -> list[dict]:
        del now  # callers decide whether next_check_at/deadline_at is due
        recoverable: list[dict] = []
        with self._store_lock():
            for path in self.attempts_dir.glob("attempt_*.json"):
                try:
                    record = self._validate(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, CorruptAttemptError):
                    # Corruption is never interpreted as permission to replay.
                    continue
                if ToolAttemptStatus(record["status"]) in RECOVERABLE_STATUSES:
                    recoverable.append(record)
        return sorted(recoverable, key=lambda item: float(item.get("created_at") or 0))

    def list_unfinished_terminal_continuations(self) -> list[dict]:
        """Return terminal receipts that still need one correlated final."""

        unfinished: list[dict] = []
        with self._store_lock():
            for path in self.attempts_dir.glob("attempt_*.json"):
                try:
                    record = self._validate(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, CorruptAttemptError):
                    continue
                if (
                    ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES
                    and record.get("continuation_state") != "complete"
                    and (
                        record.get("continuation_state") == "not_claimed"
                        or not _pid_alive(record.get("continuation_claimed_pid"))
                    )
                ):
                    unfinished.append(record)
        return sorted(unfinished, key=lambda item: float(item.get("created_at") or 0))

    def claim_reconciliation(self, attempt_id: str) -> str | None:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return None
            claim_pid = record.get("reconciliation_claimed_pid")
            if record.get("reconciliation_claim") and _pid_alive(claim_pid):
                return None
            claim = "reconcile_" + secrets.token_urlsafe(18)
            record["reconciliation_claim"] = claim
            record["reconciliation_claimed_at"] = _now()
            record["reconciliation_claimed_pid"] = os.getpid()
            record["persistence_state"] = "reconciling"
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return claim

    def release_reconciliation(self, attempt_id: str, claim: str) -> bool:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if not claim or record.get("reconciliation_claim") != claim:
                return False
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return False
            record["reconciliation_claim"] = ""
            record["reconciliation_claimed_at"] = None
            record["reconciliation_claimed_pid"] = None
            record["persistence_state"] = "waiting"
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def claim_continuation(self, attempt_id: str) -> str | None:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) not in TERMINAL_STATUSES:
                return None
            continuation = str(record.get("continuation_state") or "")
            if continuation == "complete":
                return None
            if (
                continuation != "not_claimed"
                and _pid_alive(record.get("continuation_claimed_pid"))
            ):
                return None
            claim = "continue_" + secrets.token_urlsafe(18)
            record["continuation_state"] = claim
            record["continuation_claimed_at"] = _now()
            record["continuation_claimed_pid"] = os.getpid()
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return claim

    def complete_continuation(
        self, attempt_id: str, claim: str, *, final_response_message_id: str,
    ) -> bool:
        final_id = _safe_token(
            final_response_message_id,
            field="final_response_message_id",
        )
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if record.get("continuation_state") == "complete":
                return False
            if not claim or record.get("continuation_state") != claim:
                return False
            record["continuation_state"] = "complete"
            record["continuation_claimed_at"] = None
            record["continuation_claimed_pid"] = None
            record["final_response_message_id"] = final_id
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def release_continuation(self, attempt_id: str, claim: str) -> bool:
        """Release an unpersisted continuation so startup can finish it safely."""

        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if not claim or record.get("continuation_state") != claim:
                return False
            record["continuation_state"] = "not_claimed"
            record["continuation_claimed_at"] = None
            record["continuation_claimed_pid"] = None
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def find_compatible_suspended_turn(self, conversation_id: str) -> dict | None:
        conversation_id = _safe_token(conversation_id, field="conversation_id")
        with self._store_lock():
            return self._active_for_conversation_unlocked(conversation_id)

    def bind_autonomous(
        self,
        attempt_id: str,
        *,
        run_id: str,
        task_id: str,
        operation_id: str,
        operation_revision: int,
        permission_epoch: int,
    ) -> dict:
        """Attach one mission checkpoint to an existing common attempt.

        The binding is value-free and immutable once established.  It never
        claims the external action and therefore cannot authorize a redispatch.
        """

        values = {
            "autonomous_run_id": _safe_token(
                run_id, field="autonomous_run_id",
            ),
            "autonomous_task_id": _safe_token(
                task_id, field="autonomous_task_id",
            ),
            "autonomous_operation_id": _safe_token(
                operation_id, field="autonomous_operation_id",
            ),
        }
        if (
            isinstance(operation_revision, bool)
            or not isinstance(operation_revision, int)
            or operation_revision < 0
            or isinstance(permission_epoch, bool)
            or not isinstance(permission_epoch, int)
            or permission_epoch < 0
        ):
            raise ValueError("invalid_autonomous_revision")
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            already_bound = bool(
                record.get("owner_scope") == "autonomous"
                or any(record.get(key) for key in values)
            )
            for key, value in values.items():
                existing = str(record.get(key) or "")
                if existing and existing != value:
                    raise IllegalTransition("tool_attempt_already_bound")
            if already_bound and (
                int(record.get("operation_revision") or 0)
                != operation_revision
                or int(record.get("permission_epoch") or 0)
                != permission_epoch
            ):
                raise IllegalTransition("tool_attempt_already_bound")
            record.update(values)
            record["owner_scope"] = "autonomous"
            record["operation_revision"] = operation_revision
            record["permission_epoch"] = permission_epoch
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def has_terminal_barrier(
        self, *, conversation_id: str, turn_id: str, tool_name: str,
        argument_fingerprint: str,
    ) -> bool:
        for record in self._terminal_records_and_barriers():
            if (
                record.get("conversation_id") == conversation_id
                and record.get("turn_id") == turn_id
                and record.get("tool_name") == tool_name
                and hmac.compare_digest(
                    str(record.get("argument_fingerprint") or ""),
                    str(argument_fingerprint or ""),
                )
                and record.get("dispatch_state") != "not_dispatched"
            ):
                return True
        return False

    def has_dispatch_barrier(
        self, *, conversation_id: str, turn_id: str, tool_name: str,
        argument_fingerprint: str, equivalence_fingerprint: str = "",
        block_unknown_effect: bool = True,
    ) -> bool:
        """Check exact, equivalent, and unknown-effect barriers in one scan."""

        for record in self._terminal_records_and_barriers():
            if (
                record.get("conversation_id") != conversation_id
                or record.get("turn_id") != turn_id
                or record.get("dispatch_state") == "not_dispatched"
            ):
                continue
            if (
                block_unknown_effect
                and record.get("terminal_outcome") == "outcome_unknown"
            ):
                return True
            if (
                record.get("tool_name") == tool_name
                and hmac.compare_digest(
                    str(record.get("argument_fingerprint") or ""),
                    str(argument_fingerprint or ""),
                )
            ):
                return True
            if equivalence_fingerprint and hmac.compare_digest(
                str(record.get("equivalence_fingerprint") or ""),
                equivalence_fingerprint,
            ):
                return True
        return False

    def has_equivalent_terminal_barrier(
        self, *, conversation_id: str, turn_id: str,
        equivalence_fingerprint: str,
    ) -> bool:
        """Block another mutation that targets the same private equivalence key."""

        if not re.fullmatch(r"hmac256:[0-9a-f]{64}", equivalence_fingerprint or ""):
            return False
        for record in self._terminal_records_and_barriers():
            if (
                record.get("conversation_id") == conversation_id
                and record.get("turn_id") == turn_id
                and hmac.compare_digest(
                    str(record.get("equivalence_fingerprint") or ""),
                    equivalence_fingerprint,
                )
                and record.get("dispatch_state") != "not_dispatched"
            ):
                return True
        return False

    def has_unknown_effect_barrier(
        self, *, conversation_id: str, turn_id: str,
    ) -> bool:
        """Return whether any started mutation in this turn has unknown outcome."""

        for record in self._terminal_records_and_barriers():
            if (
                record.get("conversation_id") == conversation_id
                and record.get("turn_id") == turn_id
                and record.get("terminal_outcome") == "outcome_unknown"
                and record.get("dispatch_state") != "not_dispatched"
            ):
                return True
        return False

    def _terminal_records_and_barriers(self):
        self._ensure_dirs()
        for directory in (self.attempts_dir, self.barriers_dir):
            for path in directory.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    yield record

    def cleanup(
        self,
        *,
        now: float | None = None,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        barrier_retention_seconds: float = ANTI_REPLAY_RETENTION_SECONDS,
    ) -> dict[str, int]:
        now = _now() if now is None else float(now)
        compacted = removed = 0
        with self._store_lock():
            for path in self.attempts_dir.glob("attempt_*.json"):
                try:
                    record = self._validate(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, CorruptAttemptError):
                    continue
                if ToolAttemptStatus(record["status"]) not in TERMINAL_STATUSES:
                    continue
                if record.get("continuation_state") != "complete":
                    continue
                completed = float(record.get("completed_at") or record.get("updated_at") or now)
                if now - completed < retention_seconds:
                    continue
                barrier = {
                    "schema_version": SCHEMA_VERSION,
                    "tool_attempt_id": record["tool_attempt_id"],
                    "conversation_id": record["conversation_id"],
                    "turn_id": record["turn_id"],
                    "tool_name": record["tool_name"],
                    "argument_fingerprint": record["argument_fingerprint"],
                    "equivalence_fingerprint": record.get(
                        "equivalence_fingerprint", "",
                    ),
                    "dispatch_state": record["dispatch_state"],
                    "terminal_outcome": record["terminal_outcome"],
                    "completed_at": completed,
                    "retain_until": completed + barrier_retention_seconds,
                }
                self._write_atomic(
                    self.barriers_dir / f"{record['tool_attempt_id']}.json", barrier,
                )
                path.unlink(missing_ok=True)
                compacted += 1
            for path in self.barriers_dir.glob("attempt_*.json"):
                try:
                    barrier = json.loads(path.read_text(encoding="utf-8"))
                    retain_until = float(barrier.get("retain_until") or 0)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                if retain_until and now >= retain_until:
                    path.unlink(missing_ok=True)
                    removed += 1
        return {"compacted": compacted, "removed": removed}


class ToolAttemptCoordinator:
    """Event-first in-memory continuation coordinator backed by the store.

    Provider requests are absent while a turn is suspended.  A terminal owner
    callback wins over the durable timer because both paths must atomically
    claim the same continuation before invoking the provider.
    """

    def __init__(self, store: ToolAttemptStore | None = None):
        self.store = store or get_tool_attempt_store()
        self._lock = threading.RLock()
        self._runtimes: dict[str, _SuspendedRuntime] = {}
        self._early_terminal: dict[str, Any] = {}
        self._terminal_observers: dict[
            str, dict[str, Callable[[str, Any], None]]
        ] = {}
        self._last_lifecycle_wall = _now()
        self._last_lifecycle_monotonic = time.monotonic()

    @staticmethod
    def _emit(runtime: _SuspendedRuntime, attempt_id: str, status: str) -> None:
        if runtime.status is None:
            return
        try:
            runtime.status(status, attempt_id)
        except Exception:
            pass

    def register_suspended(
        self,
        attempt_id: str,
        *,
        resume: Callable[[str, Any], str],
        finalize: Callable[[str, str, BaseException | None], None],
        status: Callable[[str, str], None] | None = None,
        verify: Callable[[str], Any] | None = None,
    ) -> None:
        with self._lock:
            if attempt_id in self._runtimes:
                return
            runtime = _SuspendedRuntime(
                resume=resume, finalize=finalize, status=status, verify=verify,
            )
            runtime.terminal_payload = self._early_terminal.pop(attempt_id, None)
            self._runtimes[attempt_id] = runtime

    def register_terminal_observer(
        self,
        attempt_id: str,
        observer_key: str,
        callback: Callable[[str, Any], None],
    ) -> bool:
        """Observe one common attempt without owning or repeating its action."""

        observer_key = _safe_token(observer_key, field="observer_key")
        try:
            record = self.store.get_attempt(attempt_id)
        except CorruptAttemptError:
            return False
        if not record:
            return False
        with self._lock:
            observers = self._terminal_observers.setdefault(attempt_id, {})
            observers.setdefault(observer_key, callback)
        # Close the read/register race: if terminalization happened before or
        # during registration, either notify_terminal already consumed this
        # observer or this second read consumes it exactly once.
        try:
            latest = self.store.get_attempt(attempt_id)
        except CorruptAttemptError:
            latest = None
        if latest and ToolAttemptStatus(latest["status"]) in TERMINAL_STATUSES:
            with self._lock:
                observers = self._terminal_observers.get(attempt_id, {})
                terminal_observer = observers.pop(observer_key, None)
                if not observers:
                    self._terminal_observers.pop(attempt_id, None)
            if terminal_observer is not None:
                self._invoke_observer(terminal_observer, attempt_id, latest)
        return True

    @staticmethod
    def _invoke_observer(callback, attempt_id: str, payload: Any) -> None:
        def _notify() -> None:
            try:
                callback(attempt_id, payload)
            except Exception:
                pass

        threading.Thread(
            target=_notify,
            name=f"osia-tool-observer-{attempt_id[-8:]}",
            daemon=True,
        ).start()

    def activate(self, attempt_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(attempt_id)
            if runtime is None:
                return False
            runtime.activated = True
            terminal_payload = runtime.terminal_payload
        self._emit(runtime, attempt_id, "waiting_tool_result")
        if terminal_payload is not None:
            self._start_continuation(attempt_id)
        else:
            self._schedule_timer(attempt_id)
        return True

    def notify_terminal(self, attempt_id: str, payload: Any) -> None:
        with self._lock:
            runtime = self._runtimes.get(attempt_id)
            observers = list(
                self._terminal_observers.pop(attempt_id, {}).values()
            )
            if runtime is None:
                try:
                    record = self.store.get_attempt(attempt_id)
                except CorruptAttemptError:
                    record = None
                # A callback can win the race between dispatch and runtime
                # registration.  Cache only that case; callbacks arriving
                # after continuation was claimed/completed are stale.
                if record and record.get("continuation_state") == "not_claimed":
                    self._early_terminal[attempt_id] = payload
                activated = False
            else:
                if runtime.terminal_payload is None:
                    runtime.terminal_payload = payload
                if runtime.timer is not None:
                    runtime.timer.cancel()
                    runtime.timer = None
                activated = runtime.activated
        for observer in observers:
            self._invoke_observer(observer, attempt_id, payload)
        if activated:
            self._start_continuation(attempt_id)

    def _schedule_timer(self, attempt_id: str, *, immediate: bool = False) -> None:
        try:
            record = self.store.get_attempt(attempt_id)
        except CorruptAttemptError:
            record = None
        if not record or ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
            if record:
                self.notify_terminal(attempt_id, record)
            return
        now = _now()
        next_check = float(record.get("next_check_at") or now + 2.0)
        delay = 0.0 if immediate else max(0.05, min(next_check - now, 30.0))
        timer = threading.Timer(delay, self._timer_fired, args=(attempt_id,))
        timer.daemon = True
        with self._lock:
            runtime = self._runtimes.get(attempt_id)
            if runtime is None or not runtime.activated or runtime.running:
                return
            if runtime.timer is not None:
                runtime.timer.cancel()
            runtime.timer = timer
        timer.start()

    @staticmethod
    def _effective_deadline(record: Mapping[str, Any], now: float) -> tuple[float, str]:
        hard = float(
            record.get("hard_deadline_at")
            or record.get("deadline_at")
            or now
        )
        inactivity = float(record.get("inactivity_deadline_at") or hard)
        if hard <= inactivity:
            return hard, "reconciliation_hard_deadline"
        return inactivity, "reconciliation_stalled"

    @staticmethod
    def _next_progressive_check(record: Mapping[str, Any], now: float) -> tuple[float, int]:
        policy = record.get("persistence_contract") or {}
        delays = list(policy.get("check_delays_seconds") or (2.0, 5.0))
        index = max(0, int(record.get("reconciliation_check_index") or 0))
        delay = float(delays[min(index, len(delays) - 1)])
        # Small bounded jitter avoids synchronized external probes without
        # changing the progressive contract or making tests timing-sensitive.
        jitter = min(0.5, delay * 0.1) * (secrets.randbelow(1001) / 1000.0)
        return now + delay + jitter, min(index + 1, len(delays))

    def notify_reconciliation_ready(self, attempt_id: str) -> bool:
        """Wake one read-only verifier after its owner has settled."""

        with self._lock:
            runtime = self._runtimes.get(attempt_id)
            usable = bool(
                runtime is not None and runtime.activated and not runtime.running
            )
        if usable:
            self._schedule_timer(attempt_id, immediate=True)
        return usable

    def _timer_fired(self, attempt_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.get(attempt_id)
            if runtime is None or not runtime.activated or runtime.running:
                return
            runtime.timer = None
        try:
            record = self.store.get_attempt(attempt_id)
        except CorruptAttemptError:
            return
        if not record:
            return
        if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
            self.notify_terminal(attempt_id, record)
            return
        claim = self.store.claim_reconciliation(attempt_id)
        if claim is None:
            return
        try:
            self._emit(runtime, attempt_id, "verifying_tool_result")
            progress_before = int(record.get("useful_progress_count") or 0)
            if runtime.verify is not None:
                try:
                    payload = runtime.verify(attempt_id)
                except Exception:
                    payload = None
                if payload is not None:
                    self.notify_terminal(attempt_id, payload)
                    return
            now = _now()
            try:
                current_record = self.store.get_attempt(attempt_id) or record
            except CorruptAttemptError:
                return
            if ToolAttemptStatus(current_record["status"]) in TERMINAL_STATUSES:
                self.notify_terminal(attempt_id, current_record)
                return
            effective_deadline, deadline_reason = self._effective_deadline(
                current_record, now,
            )
            if now >= effective_deadline:
                side_effect = str(current_record.get("side_effect") or "")
                dispatch_state = str(
                    current_record.get("dispatch_state") or "not_dispatched"
                )
                effect_possible = bool(
                    side_effect != "read_only"
                    and dispatch_state not in {
                        "not_dispatched", "cancelled_before_effect",
                    }
                )
                terminal, _created = self.store.record_terminal_result(
                    attempt_id,
                    status=(
                        ToolAttemptStatus.UNKNOWN_AFTER_START
                        if effect_possible
                        else ToolAttemptStatus.FAILED_BEFORE_EFFECT
                    ),
                    terminal_outcome=(
                        "outcome_unknown"
                        if effect_possible
                        else "confirmed_failure_before_effect"
                    ),
                    dispatch_state=(
                        "effect_unknown" if effect_possible else "not_dispatched"
                    ),
                    reason_code=deadline_reason,
                )
                self.notify_terminal(attempt_id, terminal)
                return
            progress_after = int(
                current_record.get("useful_progress_count") or 0
            )
            if progress_after > progress_before:
                current_record["reconciliation_check_index"] = 0
            next_check, check_index = self._next_progressive_check(
                current_record, now,
            )
            next_check = min(effective_deadline, next_check)
            try:
                current = ToolAttemptStatus(current_record["status"])
                if current == ToolAttemptStatus.PENDING:
                    self.store.mark_pending(
                        attempt_id,
                        next_check_at=next_check,
                        deadline_at=float(
                            current_record.get("hard_deadline_at")
                            or current_record.get("deadline_at")
                            or effective_deadline
                        ),
                        reason_code="owner_still_running",
                        check_index=check_index,
                    )
                elif current == ToolAttemptStatus.CANCELLATION_REQUESTED:
                    self.store.record_progress(
                        attempt_id,
                        phase="cancellation_requested",
                        next_check_at=next_check,
                    )
                elif current in RECOVERABLE_STATUSES:
                    self.store.mark_waiting_verification(
                        attempt_id,
                        next_check_at=next_check,
                        deadline_at=float(
                            current_record.get("hard_deadline_at")
                            or current_record.get("deadline_at")
                            or effective_deadline
                        ),
                        reason_code="postcondition_not_observed",
                        owner_settled=(
                            current_record.get("dispatch_state")
                            == "owner_settled_unverified"
                        ),
                        check_index=check_index,
                    )
                else:
                    self.notify_terminal(attempt_id, current_record)
                    return
            except (IllegalTransition, CorruptAttemptError):
                return
            self._schedule_timer(attempt_id)
        finally:
            try:
                self.store.release_reconciliation(attempt_id, claim)
            except (IllegalTransition, CorruptAttemptError):
                pass

    def _start_continuation(self, attempt_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.get(attempt_id)
            if (
                runtime is None or not runtime.activated or runtime.running
                or runtime.terminal_payload is None
            ):
                return
            claim = self.store.claim_continuation(attempt_id)
            if claim is None:
                return
            runtime.running = True
        try:
            terminal_record = self.store.get_attempt(attempt_id) or {}
            terminal_status = ToolAttemptStatus(terminal_record.get("status"))
        except (CorruptAttemptError, TypeError, ValueError):
            terminal_status = ToolAttemptStatus.UNKNOWN_AFTER_START
        self._emit(
            runtime,
            attempt_id,
            (
                "blocked_unknown_effect"
                if terminal_status in {
                    ToolAttemptStatus.FAILED_AFTER_EFFECT,
                    ToolAttemptStatus.UNKNOWN_AFTER_START,
                }
                else "resuming_tool_turn"
            ),
        )

        def _continue() -> None:
            error: BaseException | None = None
            final_text = ""
            finalized = False
            continuation_completed = False
            try:
                final_text = runtime.resume(attempt_id, runtime.terminal_payload)
            except BaseException as exc:  # finalizer must release busy state
                error = exc
            try:
                # A resumed provider round may itself suspend on a new durable
                # attempt.  That is a continuation hand-off, not assistant
                # content: the agent has already registered/activated the new
                # attempt and its typed status keeps the same Chat turn active.
                # Never send the internal marker through the final callback.
                if error is None and final_text == WAITING_TOOL_RESULT:
                    current_record = self.store.get_attempt(attempt_id) or {}
                    chained = self.store.find_compatible_suspended_turn(
                        str(current_record.get("conversation_id") or "")
                    )
                    if (
                        isinstance(chained, dict)
                        and str(chained.get("tool_attempt_id") or "")
                        and str(chained.get("tool_attempt_id") or "") != attempt_id
                    ):
                        finalized = True
                    else:
                        error = RuntimeError(
                            "tool continuation returned a wait marker without "
                            "an active correlated hand-off"
                        )
                        final_text = ""
                if not finalized:
                    finalized = runtime.finalize(
                        attempt_id, final_text, error,
                    ) is not False
                if finalized:
                    record = self.store.get_attempt(attempt_id)
                    final_message_id = str(
                        (record or {}).get("final_response_message_id") or ""
                    )
                    if final_message_id:
                        continuation_completed = self.store.complete_continuation(
                            attempt_id,
                            claim,
                            final_response_message_id=final_message_id,
                        )
            finally:
                if not continuation_completed:
                    self.store.release_continuation(attempt_id, claim)
                with self._lock:
                    current = self._runtimes.pop(attempt_id, None)
                    if current is not None and current.timer is not None:
                        current.timer.cancel()

        thread = threading.Thread(
            target=_continue,
            name=f"osia-tool-continuation-{attempt_id[-8:]}",
            daemon=True,
        )
        thread.start()

    def request_reconciliation(self, conversation_id: str) -> bool:
        record = self.store.find_compatible_suspended_turn(conversation_id)
        if not record:
            return False
        attempt_id = record["tool_attempt_id"]
        with self._lock:
            runtime = self._runtimes.get(attempt_id)
        if runtime is None:
            return False
        self._emit(runtime, attempt_id, "verifying_tool_result")
        self._schedule_timer(attempt_id, immediate=True)
        return True

    def request_cancellation(self, conversation_id: str) -> bool:
        record = self.store.find_compatible_suspended_turn(conversation_id)
        if not record:
            return False
        self.store.request_cancellation(record["tool_attempt_id"])
        with self._lock:
            runtime = self._runtimes.get(record["tool_attempt_id"])
        if runtime is not None:
            self._emit(runtime, record["tool_attempt_id"], "cancellation_requested")
        return True

    def trigger_recovery(
        self,
        *,
        reason: str,
        now: float | None = None,
        overdue_only: bool = True,
    ) -> list[str]:
        """Wake due read-only reconciliation paths, never original dispatch."""

        del reason  # safe trigger category is intentionally not persisted
        now = _now() if now is None else float(now)
        triggered: list[str] = []
        for record in self.store.list_recoverable_attempts(now=now):
            attempt_id = str(record.get("tool_attempt_id") or "")
            next_check = float(record.get("next_check_at") or 0.0)
            if overdue_only and next_check > now:
                continue
            with self._lock:
                runtime = self._runtimes.get(attempt_id)
                usable = bool(
                    runtime is not None
                    and runtime.activated
                    and not runtime.running
                )
            if not usable:
                continue
            self._schedule_timer(attempt_id, immediate=True)
            triggered.append(attempt_id)
        return triggered

    def observe_lifecycle_tick(
        self,
        *,
        expected_interval: float,
        gap_multiplier: float = 3.0,
    ) -> bool:
        """Detect a suspend/resume-sized gap inside an existing lifecycle loop."""

        wall = _now()
        monotonic = time.monotonic()
        with self._lock:
            wall_delta = max(0.0, wall - self._last_lifecycle_wall)
            monotonic_delta = max(
                0.0, monotonic - self._last_lifecycle_monotonic,
            )
            self._last_lifecycle_wall = wall
            self._last_lifecycle_monotonic = monotonic
        threshold = max(15.0, float(expected_interval) * gap_multiplier)
        discontinuity = (
            monotonic_delta >= threshold
            or wall_delta >= threshold
            or abs(wall_delta - monotonic_delta) >= threshold
        )
        if discontinuity:
            self.trigger_recovery(
                reason="lifecycle_gap", now=wall, overdue_only=True,
            )
        return discontinuity

    def startup_fail_closed(self, *, now: float | None = None) -> list[dict]:
        """Recover orphaned attempts without replaying their external action.

        A process restart no longer turns a still-live durable budget into an
        immediate unknown result.  The attempt remains waiting for bounded
        read-only reattachment until its persisted inactivity/hard boundary.
        """

        now = _now() if now is None else float(now)
        recovered = []
        for record in self.store.list_recoverable_attempts():
            attempt_id = record["tool_attempt_id"]
            claim = self.store.claim_reconciliation(attempt_id)
            if claim is None:
                continue
            dispatch_started = record.get("dispatch_state") != "not_dispatched"
            if (
                dispatch_started
                and (record.get("persistence_contract") or {}).get(
                    "restart_reconciliation", True,
                )
            ):
                effective_deadline, deadline_reason = self._effective_deadline(
                    record, now,
                )
                if now < effective_deadline:
                    next_check, check_index = self._next_progressive_check(
                        record, now,
                    )
                    try:
                        self.store.mark_waiting_verification(
                            attempt_id,
                            next_check_at=min(next_check, effective_deadline),
                            deadline_at=float(
                                record.get("hard_deadline_at")
                                or record.get("deadline_at")
                                or effective_deadline
                            ),
                            reason_code="restart_reconciliation_pending",
                            owner_settled=True,
                            check_index=check_index,
                        )
                    except (IllegalTransition, CorruptAttemptError):
                        pass
                    finally:
                        try:
                            self.store.release_reconciliation(attempt_id, claim)
                        except (IllegalTransition, CorruptAttemptError):
                            pass
                    continue
                side_effect = str(record.get("side_effect") or "")
                effect_possible = bool(
                    side_effect != "read_only"
                    and record.get("dispatch_state") not in {
                        "not_dispatched", "cancelled_before_effect",
                    }
                )
                terminal, created = self.store.record_terminal_result(
                    attempt_id,
                    status=(
                        ToolAttemptStatus.UNKNOWN_AFTER_START
                        if effect_possible else
                        ToolAttemptStatus.FAILED_BEFORE_EFFECT
                    ),
                    terminal_outcome=(
                        "outcome_unknown"
                        if effect_possible else
                        "confirmed_failure_before_effect"
                    ),
                    dispatch_state=(
                        "effect_unknown" if effect_possible else "not_dispatched"
                    ),
                    reason_code=deadline_reason,
                )
                if created:
                    recovered.append(terminal)
                continue
            status = (
                ToolAttemptStatus.UNKNOWN_AFTER_START
                if dispatch_started else ToolAttemptStatus.FAILED_BEFORE_EFFECT
            )
            terminal, created = self.store.record_terminal_result(
                attempt_id,
                status=status,
                terminal_outcome=(
                    "outcome_unknown" if dispatch_started else "confirmed_failure_before_effect"
                ),
                dispatch_state=("effect_unknown" if dispatch_started else "not_dispatched"),
                reason_code=(
                    "session_lost_after_unknown_effect"
                    if dispatch_started else "session_lost_before_dispatch"
                ),
            )
            if created:
                recovered.append(terminal)
        known = {item["tool_attempt_id"] for item in recovered}
        for record in self.store.list_unfinished_terminal_continuations():
            if record["tool_attempt_id"] not in known:
                recovered.append(record)
        return recovered


_ACTIVE_ATTEMPT_ID: ContextVar[str] = ContextVar(
    "osia_active_tool_attempt_id", default="",
)
_EVENT_SINK: ContextVar[Callable[[ToolAttemptEvent], None] | None] = ContextVar(
    "osia_tool_attempt_event_sink", default=None,
)


@contextmanager
def tool_attempt_scope(
    attempt_id: str,
    event_sink: Callable[[ToolAttemptEvent], None] | None = None,
):
    attempt_token = _ACTIVE_ATTEMPT_ID.set(str(attempt_id or ""))
    sink_token = _EVENT_SINK.set(event_sink)
    try:
        yield
    finally:
        _EVENT_SINK.reset(sink_token)
        _ACTIVE_ATTEMPT_ID.reset(attempt_token)


def current_tool_attempt_id() -> str:
    return _ACTIVE_ATTEMPT_ID.get()


def publish_tool_attempt_event(phase: str, reason_code: str = "") -> None:
    attempt_id = current_tool_attempt_id()
    sink = _EVENT_SINK.get()
    if attempt_id and sink is not None:
        try:
            sink(ToolAttemptEvent(attempt_id, _safe_reason(phase), _safe_reason(reason_code)))
        except Exception:
            pass


_DEFAULT_STORE: ToolAttemptStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()
_DEFAULT_COORDINATOR: ToolAttemptCoordinator | None = None
_ORPHAN_TIMER_LOCK = threading.Lock()
_ORPHAN_TIMERS: dict[str, threading.Timer] = {}


def get_tool_attempt_store() -> ToolAttemptStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = ToolAttemptStore()
    return _DEFAULT_STORE


def get_tool_attempt_coordinator() -> ToolAttemptCoordinator:
    global _DEFAULT_COORDINATOR
    if _DEFAULT_COORDINATOR is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_COORDINATOR is None:
                # Avoid recursively acquiring _DEFAULT_STORE_LOCK.
                store = _DEFAULT_STORE or ToolAttemptStore()
                globals()["_DEFAULT_STORE"] = store
                _DEFAULT_COORDINATOR = ToolAttemptCoordinator(store)
    return _DEFAULT_COORDINATOR


def _schedule_orphan_recovery_checks() -> None:
    """Arm one daemon deadline wake-up per unowned durable attempt."""

    store = get_tool_attempt_store()
    coordinator = get_tool_attempt_coordinator()
    now = _now()
    for record in store.list_recoverable_attempts():
        attempt_id = str(record.get("tool_attempt_id") or "")
        if not attempt_id:
            continue
        deadline, _reason = coordinator._effective_deadline(record, now)
        delay = max(0.05, min(deadline - now, 86_400.0))

        def _wake(target=attempt_id):
            with _ORPHAN_TIMER_LOCK:
                _ORPHAN_TIMERS.pop(target, None)
            try:
                recover_orphaned_tool_attempts()
            except Exception:
                pass

        with _ORPHAN_TIMER_LOCK:
            existing = _ORPHAN_TIMERS.get(attempt_id)
            if existing is not None and existing.is_alive():
                continue
            timer = threading.Timer(delay, _wake)
            timer.daemon = True
            _ORPHAN_TIMERS[attempt_id] = timer
            timer.start()


def recover_orphaned_tool_attempts() -> list[dict]:
    """Fail closed on startup and finish any accepted chat turn exactly once."""

    recovered = get_tool_attempt_coordinator().startup_fail_closed()
    _schedule_orphan_recovery_checks()
    if not recovered:
        try:
            get_tool_attempt_store().cleanup()
        except (OSError, CorruptAttemptError):
            pass
        return []
    from osia import conversations

    for record in recovered:
        if record.get("owner_scope") == "autonomous":
            # The Autonomous checkpoint owns its exact task continuation.  A
            # normal-chat assistant message here would duplicate that mission.
            continue
        language = str((record.get("turn_context") or {}).get("language") or "en")
        status = ToolAttemptStatus(record["status"])
        unknown = status == ToolAttemptStatus.UNKNOWN_AFTER_START
        confirmed_effect = status in {
            ToolAttemptStatus.SUCCEEDED,
            ToolAttemptStatus.FAILED_AFTER_EFFECT,
        }
        recovery_reason = (
            str(record.get("reason_code") or "")
            if record.get("reason_code") in {
                "reconciliation_stalled", "reconciliation_hard_deadline",
            } else
            "session_lost_after_unknown_effect" if unknown else
            "session_lost_after_terminal_receipt" if confirmed_effect else
            "session_lost_before_dispatch"
        )
        text = (
            "La v\u00e9rification en lecture seule n'a montr\u00e9 aucun nouveau "
            "progr\u00e8s avant sa limite d'inactivit\u00e9. OSIA n'a pas relanc\u00e9 l'action."
            if language == "fr" and recovery_reason == "reconciliation_stalled" else
            "La v\u00e9rification en lecture seule a atteint sa limite totale. "
            "OSIA n'a pas relanc\u00e9 l'action."
            if language == "fr" and recovery_reason == "reconciliation_hard_deadline" else
            "Read-only verification made no new progress before its bounded "
            "inactivity limit. OSIA did not repeat the action."
            if recovery_reason == "reconciliation_stalled" else
            "Read-only verification reached its hard limit. OSIA did not "
            "repeat the action."
            if recovery_reason == "reconciliation_hard_deadline" else
            "La session de l'outil a \u00e9t\u00e9 perdue apr\u00e8s le d\u00e9but "
            "possible de l'action. OSIA ne l'a pas relanc\u00e9e."
            if language == "fr" and unknown else
            "Le r\u00e9sultat de l'action a \u00e9t\u00e9 enregistr\u00e9, mais la "
            "reprise du tour a \u00e9t\u00e9 perdue. OSIA n'a pas relanc\u00e9 l'action."
            if language == "fr" and confirmed_effect else
            "La session de l'outil a \u00e9t\u00e9 perdue avant toute ex\u00e9cution "
            "confirm\u00e9e."
            if language == "fr" else
            "The tool session was lost after the action may have started. "
            "OSIA did not repeat it."
            if unknown else
            "The action receipt was recorded, but turn continuation was lost. "
            "OSIA did not repeat the action."
            if confirmed_effect else
            "The tool session was lost before any execution was confirmed."
        )
        request_id = str(record.get("request_id") or "")
        turn_id = str(record.get("turn_id") or "")
        message_id = str(record.get("final_response_message_id") or "")
        attempt_id = record["tool_attempt_id"]
        claim = get_tool_attempt_store().claim_continuation(attempt_id)
        if claim is None:
            continue
        if not request_id or not turn_id or not message_id:
            get_tool_attempt_store().release_continuation(attempt_id, claim)
            continue
        event_id = "event_" + secrets.token_hex(16)
        terminal_id = "event_" + secrets.token_hex(16)
        try:
            receipt = conversations.commit_message(
                str(record.get("conversation_id") or ""),
                "assistant",
                text,
                metadata={
                    "_conversation_id": record.get("conversation_id"),
                    "_request_id": request_id,
                    "_client_request_id": request_id,
                    "_turn_id": turn_id,
                    "_message_id": message_id,
                    "_operation_id": message_id,
                    "_response_event_id": event_id,
                    "_response_sequence": 1,
                    "_terminal_event_id": terminal_id,
                    "_terminal_sequence": 2,
                    "_terminal": True,
                    "_language": language,
                    "_terminal_status": "error",
                    "_stage": "tool_recovery",
                    "_error_category": recovery_reason,
                    "_reason_code": recovery_reason,
                },
            )
        except (OSError, RuntimeError, ValueError):
            get_tool_attempt_store().release_continuation(attempt_id, claim)
            continue
        if receipt is not None:
            get_tool_attempt_store().complete_continuation(
                attempt_id,
                claim,
                final_response_message_id=message_id,
            )
        else:
            get_tool_attempt_store().release_continuation(attempt_id, claim)
    try:
        get_tool_attempt_store().cleanup()
    except (OSError, CorruptAttemptError):
        pass
    return recovered


__all__ = [
    "ANTI_REPLAY_RETENTION_SECONDS", "CorruptAttemptError", "IllegalTransition",
    "RECOVERABLE_STATUSES", "RevisionConflict", "TERMINAL_STATUSES",
    "ToolAttemptCoordinator", "ToolAttemptEvent", "ToolAttemptStatus",
    "ToolAttemptStore",
    "WAITING_TOOL_RESULT", "current_tool_attempt_id", "get_tool_attempt_store",
    "get_tool_attempt_coordinator", "publish_tool_attempt_event",
    "is_internal_tool_control_text", "recover_orphaned_tool_attempts",
    "tool_attempt_scope",
]
