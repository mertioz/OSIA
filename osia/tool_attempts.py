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
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
ANTI_REPLAY_RETENTION_SECONDS = 30 * 24 * 60 * 60
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_OPAQUE_BROWSER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


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
        if status in TERMINAL_STATUSES and not record.get("terminal_outcome"):
            raise CorruptAttemptError("terminal_outcome_missing")
        browser_state = record.get("browser_state_ref")
        if browser_state is not None and _safe_browser_state(browser_state) is None:
            raise CorruptAttemptError("invalid_browser_state_ref")
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
        remote_operation_key: str = "",
        next_check_at: float | None = None,
        deadline_at: float | None = None,
        postcondition_type: str = "",
        browser_state_ref: Mapping[str, Any] | None = None,
        repair_count: int = 0,
        final_response_message_id: str = "",
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
        if repair_count not in {0, 1}:
            raise ValueError("invalid_repair_count")
        now = _now()
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
            "deadline_at": float(deadline_at) if deadline_at is not None else None,
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
            "reconciliation_claim": "",
            "reconciliation_claimed_at": None,
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
        return self._transition(
            attempt_id, ToolAttemptStatus.RUNNING,
            expected_revision=expected_revision,
            updates={"started_at": _now(), "last_progress_at": _now(),
                     "dispatch_state": "dispatch_started"},
        )

    def mark_pending(
        self, attempt_id: str, *, next_check_at: float, deadline_at: float,
        reason_code: str = "owner_still_running",
    ) -> dict:
        return self._transition(
            attempt_id, ToolAttemptStatus.PENDING,
            updates={"next_check_at": float(next_check_at),
                     "deadline_at": float(deadline_at),
                     "dispatch_state": "worker_may_be_active",
                     "reason_code": _safe_reason(reason_code)},
        )

    def mark_waiting_verification(
        self, attempt_id: str, *, next_check_at: float, deadline_at: float,
        reason_code: str = "postcondition_not_observed",
    ) -> dict:
        return self._transition(
            attempt_id, ToolAttemptStatus.WAITING_VERIFICATION,
            updates={"next_check_at": float(next_check_at),
                     "deadline_at": float(deadline_at),
                     "reason_code": _safe_reason(reason_code)},
        )

    def record_progress(
        self, attempt_id: str, *, phase: str = "running",
        next_check_at: float | None = None,
    ) -> dict:
        record = self.get_attempt(attempt_id)
        if record is None or ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
            return record or {}
        updates = {"last_progress_at": _now(), "reason_code": _safe_reason(phase)}
        if next_check_at is not None:
            updates["next_check_at"] = float(next_check_at)
        return self._transition(attempt_id, ToolAttemptStatus(record["status"]), updates=updates)

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
            record.update({
                "status": status.value,
                "terminal_outcome": _safe_token(
                    terminal_outcome, field="terminal_outcome",
                ),
                "dispatch_state": _safe_token(dispatch_state, field="dispatch_state"),
                "reason_code": _safe_reason(reason_code),
                "completed_at": now,
                "next_check_at": None,
                "updated_at": now,
                "revision": int(record["revision"]) + 1,
            })
            self._write_atomic(self._path(attempt_id), record)
            return dict(record), True

    def request_cancellation(self, attempt_id: str) -> dict:
        record = self.get_attempt(attempt_id)
        if record is None:
            raise KeyError(attempt_id)
        if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
            return record
        return self._transition(
            attempt_id, ToolAttemptStatus.CANCELLATION_REQUESTED,
            updates={"reason_code": "cancellation_requested"},
        )

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
                    and record.get("continuation_state") == "not_claimed"
                ):
                    unfinished.append(record)
        return sorted(unfinished, key=lambda item: float(item.get("created_at") or 0))

    def claim_reconciliation(self, attempt_id: str) -> str | None:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return None
            if record.get("reconciliation_claim"):
                return None
            claim = "reconcile_" + secrets.token_urlsafe(18)
            record["reconciliation_claim"] = claim
            record["reconciliation_claimed_at"] = _now()
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
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def claim_continuation(self, attempt_id: str) -> str | None:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) not in TERMINAL_STATUSES:
                return None
            if record.get("continuation_state") != "not_claimed":
                return None
            claim = "continue_" + secrets.token_urlsafe(18)
            record["continuation_state"] = claim
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
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def find_compatible_suspended_turn(self, conversation_id: str) -> dict | None:
        conversation_id = _safe_token(conversation_id, field="conversation_id")
        with self._store_lock():
            return self._active_for_conversation_unlocked(conversation_id)

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
                return
            if runtime.terminal_payload is None:
                runtime.terminal_payload = payload
            if runtime.timer is not None:
                runtime.timer.cancel()
                runtime.timer = None
            activated = runtime.activated
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
        self._emit(runtime, attempt_id, "verifying_tool_result")
        if runtime.verify is not None:
            try:
                payload = runtime.verify(attempt_id)
            except Exception:
                payload = None
            if payload is not None:
                self.notify_terminal(attempt_id, payload)
                return
        now = _now()
        deadline = float(record.get("deadline_at") or now)
        if now >= deadline:
            terminal, created = self.store.record_terminal_result(
                attempt_id,
                status=ToolAttemptStatus.UNKNOWN_AFTER_START,
                terminal_outcome="outcome_unknown",
                dispatch_state="effect_unknown",
                reason_code="verification_deadline_expired",
            )
            if created:
                self.notify_terminal(attempt_id, terminal)
            else:
                self.notify_terminal(attempt_id, terminal)
            return
        # Bounded low-frequency fallback; delivery of an owner callback still
        # cancels this timer and claims continuation first.
        next_check = min(deadline, now + 5.0 + secrets.randbelow(1000) / 1000.0)
        try:
            current = ToolAttemptStatus(record["status"])
            if current == ToolAttemptStatus.PENDING:
                self.store.mark_pending(
                    attempt_id,
                    next_check_at=next_check,
                    deadline_at=deadline,
                    reason_code="owner_still_running",
                )
            elif current == ToolAttemptStatus.CANCELLATION_REQUESTED:
                self.store.record_progress(
                    attempt_id,
                    phase="cancellation_requested",
                    next_check_at=next_check,
                )
            else:
                self.store.mark_waiting_verification(
                    attempt_id,
                    next_check_at=next_check,
                    deadline_at=deadline,
                    reason_code="postcondition_not_observed",
                )
        except (IllegalTransition, CorruptAttemptError):
            return
        self._schedule_timer(attempt_id)

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
        self._emit(runtime, attempt_id, "resuming_tool_turn")

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

    def startup_fail_closed(self) -> list[dict]:
        """Finalize orphaned attempts without replaying their external action."""

        recovered = []
        for record in self.store.list_recoverable_attempts():
            attempt_id = record["tool_attempt_id"]
            claim = self.store.claim_reconciliation(attempt_id)
            if claim is None:
                continue
            dispatch_started = record.get("dispatch_state") != "not_dispatched"
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


def recover_orphaned_tool_attempts() -> list[dict]:
    """Fail closed on startup and finish any accepted chat turn exactly once."""

    recovered = get_tool_attempt_coordinator().startup_fail_closed()
    if not recovered:
        try:
            get_tool_attempt_store().cleanup()
        except (OSError, CorruptAttemptError):
            pass
        return []
    from osia import conversations

    for record in recovered:
        language = str((record.get("turn_context") or {}).get("language") or "en")
        status = ToolAttemptStatus(record["status"])
        unknown = status == ToolAttemptStatus.UNKNOWN_AFTER_START
        confirmed_effect = status in {
            ToolAttemptStatus.SUCCEEDED,
            ToolAttemptStatus.FAILED_AFTER_EFFECT,
        }
        recovery_reason = (
            "session_lost_after_unknown_effect" if unknown else
            "session_lost_after_terminal_receipt" if confirmed_effect else
            "session_lost_before_dispatch"
        )
        text = (
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
    "recover_orphaned_tool_attempts", "tool_attempt_scope",
]
