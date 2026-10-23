"""Durable application-event transport for correlated OSIA execution.

Semantic truth remains owned by ActionTruth and the durable execution stores.
This module owns only distribution history, delivery cursors, command
deduplication and projection fencing.  A missing ACK therefore never changes
an ActionRecord and can never authorize a tool replay.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from osia.config import CONFIG_DIR


SCHEMA_VERSION = 1
PAYLOAD_VERSION = 1
PRODUCER_ID = "osia-pc"
MAX_EVENTS_PER_STREAM = 2048
MAX_INBOX_RECORDS = 4096
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_SAFE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "api_key", "authorization", "base64", "cookie", "credential", "data",
    "hidden_reasoning", "password", "raw_screenshot", "reasoning_signature",
    "secret", "token",
})


class FailureDomain(str, Enum):
    TRANSPORT_FAILURE = "transport_failure"
    PROVIDER_TRANSPORT_FAILURE = "provider_transport_failure"
    PROVIDER_OPERATION_FAILURE = "provider_operation_failure"
    SEMANTIC_PRECONDITION_FAILURE = "semantic_precondition_failure"
    SEMANTIC_EFFECT_UNCERTAINTY = "semantic_effect_uncertainty"
    OBSERVER_FAILURE = "observer_failure"
    PROJECTION_FAILURE = "projection_failure"
    STALE_TARGET = "stale_target"


class InboxDisposition(str, Enum):
    CLAIMED = "claimed"
    DUPLICATE_IN_PROGRESS = "duplicate_in_progress"
    DUPLICATE_COMPLETED = "duplicate_completed"
    CONFLICT = "conflict"


class ProjectionDisposition(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    GAP = "gap"
    HISTORICAL = "historical"
    STALE_GENERATION = "stale_generation"
    INVALID = "invalid"


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    stream_id: str
    sequence: int
    conversation_id: str
    request_id: str
    turn_id: str
    run_id: str
    event_type: str
    created_at: float
    producer_id: str = PRODUCER_ID
    payload_version: int = PAYLOAD_VERSION
    action_id: str = ""
    attempt_id: str = ""
    semantic_effect_id: str = ""
    execution_epoch: str = ""
    terminal: bool = False
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["payload"] = dict(self.payload or {})
        return value

    def to_frame(self, *, replayed: bool = False, historical: bool = False) -> dict:
        frame = dict(self.payload or {})
        frame.update({
            "type": self.event_type,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "client_request_id": self.request_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "producer_id": self.producer_id,
            "payload_version": self.payload_version,
            "terminal": self.terminal,
        })
        for key in (
            "action_id", "attempt_id", "semantic_effect_id", "execution_epoch",
        ):
            value = getattr(self, key)
            if value:
                frame[key] = value
        if replayed:
            frame["replayed"] = True
        if historical:
            frame["historical"] = True
        return frame


@dataclass(frozen=True)
class InboxClaim:
    disposition: InboxDisposition
    command_id: str
    execution_epoch: str
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProjectionResult:
    disposition: ProjectionDisposition
    stream_id: str = ""
    expected_sequence: int = 0
    received_sequence: int = 0


def _safe_id(value: Any, field: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"invalid_{field}")
    return text


def _bounded_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return max(-(2 ** 63), min(2 ** 63 - 1, value))
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else 0.0
    if isinstance(value, str):
        return value[:32_000]
    return str(value)[:512]


def sanitize_payload(value: Mapping[str, Any] | None, *, _depth: int = 0) -> dict:
    """Return a bounded event payload without secrets, binary data or reasoning."""

    if not isinstance(value, Mapping) or _depth > 3:
        return {}
    result: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:128]:
        key = str(raw_key or "")
        if not _SAFE_KEY.fullmatch(key) or key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
            continue
        lowered = key.lower()
        if any(marker in lowered for marker in (
            "password", "secret", "signature", "screenshot", "base64",
        )):
            continue
        if isinstance(raw_value, Mapping):
            result[key] = sanitize_payload(raw_value, _depth=_depth + 1)
        elif isinstance(raw_value, (list, tuple)):
            items = []
            for item in list(raw_value)[:64]:
                items.append(
                    sanitize_payload(item, _depth=_depth + 1)
                    if isinstance(item, Mapping) else _bounded_scalar(item)
                )
            result[key] = items
        else:
            result[key] = _bounded_scalar(raw_value)
    return result


def stream_identity(conversation_id: str, run_id: str) -> str:
    conversation = _safe_id(conversation_id, "conversation_id")
    run = _safe_id(run_id, "run_id")
    digest = hashlib.sha256(f"{conversation}\0{run}".encode("utf-8")).hexdigest()
    return "stream_" + digest[:40]


def command_fingerprint(command_type: str, binding: Mapping[str, Any]) -> str:
    safe = sanitize_payload(binding)
    encoded = json.dumps(
        {"command_type": str(command_type or ""), "binding": safe},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@contextmanager
def _process_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class DurableTransportStore:
    """File-backed outbox/inbox with per-run sequence authority."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.root = (
            Path(root) if root is not None
            else Path(CONFIG_DIR) / "durable_transport"
        )
        self.streams_dir = self.root / "streams"
        self.inbox_dir = self.root / "inbox"
        self._thread_lock = threading.RLock()
        self._clock = clock or time.time

    def _ensure_dirs(self) -> None:
        self.streams_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    def _stream_path(self, stream_id: str) -> Path:
        safe = _safe_id(stream_id, "stream_id")
        return self.streams_dir / f"{safe}.json"

    def _inbox_path(self, command_id: str) -> Path:
        safe = _safe_id(command_id, "command_id")
        digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()
        return self.inbox_dir / f"{digest}.json"

    @staticmethod
    def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            path.name + f".{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("durable_transport_record_unreadable") from exc
        if not isinstance(value, dict):
            raise RuntimeError("durable_transport_record_invalid")
        return value

    def _new_stream(self, *, conversation_id: str, run_id: str) -> dict:
        stream_id = stream_identity(conversation_id, run_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "stream_id": stream_id,
            "binding": {
                "conversation_id": _safe_id(conversation_id, "conversation_id"),
                "run_id": _safe_id(run_id, "run_id"),
            },
            "next_sequence": 1,
            "execution_epoch": "epoch_" + secrets.token_urlsafe(24),
            "authority_state": "active",
            "tombstoned_at": 0.0,
            "terminal_sequence": 0,
            "compacted_through": 0,
            "events": [],
            "idempotency_index": {},
            "compacted_events": {},
            "client_acks": {},
            "state": {},
            "updated_at": self._clock(),
        }

    def _load_stream_locked(self, *, conversation_id: str, run_id: str) -> tuple[Path, dict]:
        stream_id = stream_identity(conversation_id, run_id)
        path = self._stream_path(stream_id)
        value = self._read(path)
        if not value:
            value = self._new_stream(
                conversation_id=conversation_id,
                run_id=run_id,
            )
        expected = {
            "conversation_id": _safe_id(conversation_id, "conversation_id"),
            "run_id": _safe_id(run_id, "run_id"),
        }
        if value.get("binding") != expected or value.get("stream_id") != stream_id:
            raise ValueError("durable_stream_binding_changed")
        value.setdefault("events", [])
        value.setdefault("idempotency_index", {})
        value.setdefault("compacted_events", {})
        value.setdefault("client_acks", {})
        value.setdefault("state", {})
        value.setdefault("execution_epoch", "epoch_legacy_" + stream_id[-20:])
        value.setdefault("authority_state", "active")
        value.setdefault("tombstoned_at", 0.0)
        value.setdefault("terminal_sequence", 0)
        value.setdefault("compacted_through", 0)
        return path, value

    def append_event(
        self,
        *,
        conversation_id: str,
        request_id: str,
        turn_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        action_id: str = "",
        attempt_id: str = "",
        semantic_effect_id: str = "",
        execution_epoch: str = "",
        terminal: bool = False,
        idempotency_key: str = "",
        state_updates: Mapping[str, Any] | None = None,
    ) -> EventEnvelope:
        """Atomically update distribution state and persist its outbox event."""

        conversation_id = _safe_id(conversation_id, "conversation_id")
        request_id = _safe_id(request_id, "request_id")
        turn_id = _safe_id(turn_id, "turn_id")
        run_id = _safe_id(run_id, "run_id")
        event_type = _safe_id(event_type, "event_type")
        for field, value in (
            ("action_id", action_id), ("attempt_id", attempt_id),
            ("semantic_effect_id", semantic_effect_id),
            ("execution_epoch", execution_epoch),
            ("idempotency_key", idempotency_key),
        ):
            if value:
                _safe_id(value, field)
        safe_payload = sanitize_payload(payload)
        self._ensure_dirs()
        stream_id = stream_identity(conversation_id, run_id)
        path = self._stream_path(stream_id)
        with self._thread_lock, _process_lock(path):
            path, stream = self._load_stream_locked(
                conversation_id=conversation_id,
                run_id=run_id,
            )
            if idempotency_key:
                existing_id = stream["idempotency_index"].get(idempotency_key)
                if existing_id:
                    for raw in stream["events"]:
                        if raw.get("event_id") == existing_id:
                            return EventEnvelope(**raw)
                    compacted = (stream.get("compacted_events") or {}).get(
                        existing_id
                    )
                    if isinstance(compacted, Mapping):
                        return EventEnvelope(**dict(compacted))
                    raise RuntimeError("durable_event_index_corrupt")
            if execution_epoch and execution_epoch != stream["execution_epoch"]:
                raise ValueError("stale_execution_epoch")
            sequence = max(1, int(stream.get("next_sequence") or 1))
            envelope = EventEnvelope(
                event_id="event_" + secrets.token_urlsafe(24),
                stream_id=stream_id,
                sequence=sequence,
                conversation_id=conversation_id,
                request_id=request_id,
                turn_id=turn_id,
                run_id=run_id,
                event_type=event_type,
                created_at=float(self._clock()),
                action_id=_safe_id(action_id, "action_id", allow_empty=True),
                attempt_id=_safe_id(attempt_id, "attempt_id", allow_empty=True),
                semantic_effect_id=_safe_id(
                    semantic_effect_id, "semantic_effect_id", allow_empty=True,
                ),
                execution_epoch=(
                    execution_epoch or str(stream["execution_epoch"])
                ),
                terminal=bool(terminal),
                payload=safe_payload,
            )
            stream["events"].append(envelope.to_dict())
            stream["next_sequence"] = sequence + 1
            if idempotency_key:
                stream["idempotency_index"][idempotency_key] = envelope.event_id
            if isinstance(state_updates, Mapping):
                stream["state"].update(sanitize_payload(state_updates))
            if terminal:
                stream["authority_state"] = (
                    "cancelled"
                    if safe_payload.get("status") in {"cancelled", "canceled"}
                    else "terminal"
                )
                stream["terminal_sequence"] = sequence
                stream["tombstoned_at"] = float(self._clock())
            stream["updated_at"] = float(self._clock())
            self._write_atomic(path, stream)
            return envelope

    def get_stream(self, conversation_id: str, run_id: str) -> dict:
        self._ensure_dirs()
        path = self._stream_path(stream_identity(conversation_id, run_id))
        with self._thread_lock, _process_lock(path):
            _path, value = self._load_stream_locked(
                conversation_id=conversation_id, run_id=run_id,
            )
            return json.loads(json.dumps(value))

    @staticmethod
    def _envelope(raw: Mapping[str, Any]) -> EventEnvelope:
        allowed = set(EventEnvelope.__dataclass_fields__)
        return EventEnvelope(**{key: raw.get(key) for key in allowed})

    def replay(
        self,
        *,
        conversation_id: str,
        run_id: str,
        after_sequence: int = 0,
        through_sequence: int | None = None,
    ) -> list[dict]:
        stream = self.get_stream(conversation_id, run_id)
        terminal_sequence = int(stream.get("terminal_sequence") or 0)
        frames = []
        for raw in stream.get("events") or []:
            sequence = int(raw.get("sequence") or 0)
            if sequence <= int(after_sequence):
                continue
            if through_sequence is not None and sequence > int(through_sequence):
                continue
            event = self._envelope(raw)
            frames.append(event.to_frame(
                replayed=True,
                historical=bool(terminal_sequence),
            ))
        return frames

    def list_stream_bindings(self) -> list[dict[str, str]]:
        self._ensure_dirs()
        result = []
        for path in self.streams_dir.glob("stream_*.json"):
            try:
                value = self._read(path)
            except RuntimeError:
                continue
            binding = value.get("binding")
            if isinstance(binding, dict) and all(
                isinstance(binding.get(key), str)
                for key in ("conversation_id", "run_id")
            ):
                result.append(dict(binding))
        return result

    def binding_for_stream(self, stream_id: str) -> dict[str, str] | None:
        stream_id = _safe_id(stream_id, "stream_id")
        self._ensure_dirs()
        path = self._stream_path(stream_id)
        with self._thread_lock, _process_lock(path):
            value = self._read(path)
        binding = value.get("binding") if value else None
        if (
            value.get("stream_id") != stream_id
            or not isinstance(binding, dict)
            or not all(
                isinstance(binding.get(key), str) and binding.get(key)
                for key in ("conversation_id", "run_id")
            )
        ):
            return None
        return {
            "conversation_id": str(binding["conversation_id"]),
            "run_id": str(binding["run_id"]),
        }

    def replay_stream(self, stream_id: str, *, after_sequence: int = 0) -> list[dict]:
        binding = self.binding_for_stream(stream_id)
        if binding is None:
            return []
        return self.replay(**binding, after_sequence=after_sequence)

    def replay_for_client(
        self,
        client_id: str,
        cursors: Mapping[str, Any] | None = None,
        *,
        stream_ids: Iterable[str] | None = None,
    ) -> list[dict]:
        client_id = _safe_id(client_id, "client_id")
        supplied = dict(cursors) if isinstance(cursors, Mapping) else {}
        if stream_ids is None:
            bindings = self.list_stream_bindings()
        else:
            # Reconnect already knows its resumable streams. Do not deserialize
            # every historical stream only to discard those frames afterwards.
            bindings = []
            for stream_id in set(stream_ids):
                try:
                    binding = self.binding_for_stream(stream_id)
                except (RuntimeError, TypeError, ValueError):
                    continue
                if binding is not None:
                    bindings.append(binding)
        frames: list[dict] = []
        for binding in bindings:
            stream = self.get_stream(**binding)
            stream_id = str(stream["stream_id"])
            try:
                maximum = max(0, int(stream.get("next_sequence") or 1) - 1)
                compacted_through = max(
                    0, int(stream.get("compacted_through") or 0),
                )
                stored_cursor = max(
                    0,
                    int((stream.get("client_acks") or {}).get(client_id) or 0),
                )
            except (TypeError, ValueError, OverflowError):
                maximum = 0
                compacted_through = 0
                stored_cursor = 0
            try:
                supplied_value = supplied.get(stream_id) or 0
                if isinstance(supplied_value, bool):
                    raise ValueError("invalid_client_cursor")
                supplied_cursor = min(
                    maximum, max(0, int(supplied_value)),
                )
            except (TypeError, ValueError, OverflowError):
                supplied_cursor = 0
            client_cursor = max(
                compacted_through, stored_cursor, supplied_cursor,
            )
            frames.extend(self.replay(
                **binding,
                after_sequence=client_cursor,
            ))
        frames.sort(key=lambda item: (
            float(item.get("created_at") or 0.0),
            str(item.get("stream_id") or ""),
            int(item.get("sequence") or 0),
        ))
        return frames

    def client_cursors(self, client_id: str) -> dict[str, int]:
        client_id = _safe_id(client_id, "client_id")
        cursors: dict[str, int] = {}
        for binding in self.list_stream_bindings():
            stream = self.get_stream(**binding)
            stream_id = str(stream["stream_id"])
            try:
                sequence = max(
                    0,
                    int(stream.get("compacted_through") or 0),
                    int((stream.get("client_acks") or {}).get(client_id) or 0),
                )
            except (TypeError, ValueError, OverflowError):
                sequence = 0
            if sequence:
                cursors[stream_id] = sequence
        return cursors

    def ack(self, *, client_id: str, stream_id: str, sequence: int) -> int:
        client_id = _safe_id(client_id, "client_id")
        stream_id = _safe_id(stream_id, "stream_id")
        if isinstance(sequence, bool) or int(sequence) < 0:
            raise ValueError("invalid_ack_sequence")
        self._ensure_dirs()
        path = self._stream_path(stream_id)
        with self._thread_lock, _process_lock(path):
            stream = self._read(path)
            if not stream or stream.get("stream_id") != stream_id:
                raise ValueError("unknown_event_stream")
            maximum = max(0, int(stream.get("next_sequence") or 1) - 1)
            if int(sequence) > maximum:
                raise ValueError("ack_beyond_stream")
            current = int((stream.get("client_acks") or {}).get(client_id) or 0)
            acknowledged = max(current, int(sequence))
            stream.setdefault("client_acks", {})[client_id] = acknowledged
            stream["updated_at"] = float(self._clock())
            self._write_atomic(path, stream)
        # Keep the replay suffix bounded once every known client has crossed
        # the safe compaction floor. Idempotency tombstones remain permanent.
        self.compact(stream_id=stream_id, retain=MAX_EVENTS_PER_STREAM)
        return acknowledged

    def compact(self, *, stream_id: str, retain: int = 64) -> int:
        stream_id = _safe_id(stream_id, "stream_id")
        path = self._stream_path(stream_id)
        with self._thread_lock, _process_lock(path):
            stream = self._read(path)
            events = list(stream.get("events") or [])
            acks = list((stream.get("client_acks") or {}).values())
            if len(events) <= max(1, retain) or not acks:
                return 0
            floor = min(int(value or 0) for value in acks)
            keep_from = max(0, len(events) - max(1, retain))
            removed = [
                item for index, item in enumerate(events)
                if index < keep_from and int(item.get("sequence") or 0) <= floor
            ]
            if not removed:
                return 0
            removed_ids = {item.get("event_id") for item in removed}
            indexed_ids = {
                str(event_id)
                for event_id in (stream.get("idempotency_index") or {}).values()
                if event_id
            }
            compacted = {
                str(event_id): item
                for event_id, item in dict(
                    stream.get("compacted_events") or {}
                ).items()
                if str(event_id) in indexed_ids and isinstance(item, Mapping)
            }
            for item in removed:
                event_id = str(item.get("event_id") or "")
                if event_id in indexed_ids:
                    compacted[event_id] = item
            stream["events"] = [
                item for item in events if item.get("event_id") not in removed_ids
            ]
            # Keep the idempotency index and a compacted immutable envelope:
            # compaction must never reopen a completed state transition.
            stream["compacted_events"] = compacted
            stream["compacted_through"] = max(
                int(stream.get("compacted_through") or 0),
                max(int(item.get("sequence") or 0) for item in removed),
            )
            self._write_atomic(path, stream)
            return len(removed)

    def claim_command(
        self,
        *,
        command_id: str,
        command_type: str,
        binding: Mapping[str, Any],
        execution_epoch: str = "",
        owner_id: str = "",
        owner_pid: int = 0,
        owner_process_identity: str = "",
    ) -> InboxClaim:
        command_id = _safe_id(command_id, "command_id")
        command_type = _safe_id(command_type, "command_type")
        if execution_epoch:
            _safe_id(execution_epoch, "execution_epoch")
        fingerprint = command_fingerprint(command_type, binding)
        self._ensure_dirs()
        path = self._inbox_path(command_id)
        with self._thread_lock, _process_lock(path):
            record = self._read(path)
            if record:
                if (
                    record.get("command_id") != command_id
                    or record.get("fingerprint") != fingerprint
                ):
                    return InboxClaim(
                        InboxDisposition.CONFLICT, command_id,
                        str(record.get("execution_epoch") or ""),
                    )
                if record.get("state") == "completed":
                    return InboxClaim(
                        InboxDisposition.DUPLICATE_COMPLETED,
                        command_id,
                        str(record.get("execution_epoch") or ""),
                        dict(record.get("result") or {}),
                    )
                return InboxClaim(
                    InboxDisposition.DUPLICATE_IN_PROGRESS,
                    command_id,
                    str(record.get("execution_epoch") or ""),
                )
            epoch = execution_epoch or "epoch_" + secrets.token_urlsafe(24)
            now = float(self._clock())
            record = {
                "schema_version": SCHEMA_VERSION,
                "command_id": command_id,
                "command_type": command_type,
                "fingerprint": fingerprint,
                "binding": sanitize_payload(binding),
                "execution_epoch": epoch,
                "state": "claimed",
                "claimed_at": now,
                "owner_id": str(owner_id)[:256],
                "owner_pid": int(owner_pid),
                "owner_process_identity": str(owner_process_identity)[:128],
                "execution_boundary_version": 1,
                "completed_at": 0.0,
                "result": {},
            }
            self._write_atomic(path, record)
            return InboxClaim(InboxDisposition.CLAIMED, command_id, epoch)

    def recover_unstarted_commands(self) -> list[str]:
        """Recover proven dead pre-acceptance owners, never legacy/running ones."""
        from osia.turn_terminality import live_process_identity

        self._ensure_dirs()
        recovered = []
        for path in self.inbox_dir.glob("*.json"):
            with self._thread_lock, _process_lock(path):
                record = self._read(path)
                if (record.get("state") != "claimed"
                    or record.get("execution_boundary_version") != 1
                    or record.get("execution_binding")
                    or not record.get("owner_pid")
                    or not record.get("owner_process_identity")):
                    continue
                alive, identity = live_process_identity(record["owner_pid"])
                if alive and (not identity or identity == record["owner_process_identity"]):
                    continue
                # The process is dead (or the PID was reused), and this version
                # fences every acceptance write before execution can begin.
                record["state"] = "completed"
                record["completed_at"] = float(self._clock())
                record["result"] = {
                    "accepted": False, "state": "failed_before_execution",
                    "reason_code": "pre_turn_owner_process_exited",
                }
                self._write_atomic(path, record)
                recovered.append(str(record["command_id"]))
        return recovered

    def mark_command_running(
        self, command_id: str, *, execution_epoch: str,
        execution_binding: Mapping[str, Any], accepted: bool = False,
    ) -> None:
        """Fence execution before the canonical acceptance write can occur."""
        command_id = _safe_id(command_id, "command_id")
        execution_epoch = _safe_id(execution_epoch, "execution_epoch")
        path = self._inbox_path(command_id)
        with self._thread_lock, _process_lock(path):
            record = self._read(path)
            if (not record or record.get("command_id") != command_id
                or record.get("execution_epoch") != execution_epoch):
                raise ValueError("stale_execution_epoch")
            if record.get("state") == "completed":
                raise ValueError("command_already_completed")
            binding = sanitize_payload(execution_binding)
            previous = record.get("execution_binding")
            if previous and previous != binding:
                raise ValueError("command_execution_binding_changed")
            record["state"] = "running"
            record["execution_binding"] = binding
            record.setdefault("acceptance_started_at", float(self._clock()))
            record.setdefault("execution_stage", "accepting_turn")
            if accepted:
                record.setdefault("turn_accepted_at", float(self._clock()))
                record["execution_stage"] = "turn_accepted"
            self._write_atomic(path, record)

    def settle_unstarted_command(
        self, command_id: str, *, execution_epoch: str,
        reason_code: str = "owner_exited_before_turn_acceptance",
    ) -> bool:
        """Terminalize only a fenced owner that never entered acceptance.

        This is a failure receipt for the command transport, never a model
        receipt or permission to replay an action. Running/legacy uncertainty
        must be reconciled against canonical execution by its owner.
        """
        command_id = _safe_id(command_id, "command_id")
        execution_epoch = _safe_id(execution_epoch, "execution_epoch")
        path = self._inbox_path(command_id)
        with self._thread_lock, _process_lock(path):
            record = self._read(path)
            if (not record or record.get("command_id") != command_id
                or record.get("execution_epoch") != execution_epoch):
                raise ValueError("stale_execution_epoch")
            if record.get("state") != "claimed" or record.get("execution_binding"):
                return False
            record["state"] = "completed"
            record["completed_at"] = float(self._clock())
            record["result"] = {
                "accepted": False, "state": "failed_before_execution",
                "reason_code": str(reason_code)[:128],
            }
            self._write_atomic(path, record)
            return True

    def complete_command(
        self,
        command_id: str,
        *,
        execution_epoch: str,
        result: Mapping[str, Any] | None = None,
    ) -> InboxClaim:
        command_id = _safe_id(command_id, "command_id")
        execution_epoch = _safe_id(execution_epoch, "execution_epoch")
        path = self._inbox_path(command_id)
        with self._thread_lock, _process_lock(path):
            record = self._read(path)
            if not record or record.get("command_id") != command_id:
                raise ValueError("unknown_command")
            if record.get("execution_epoch") != execution_epoch:
                raise ValueError("stale_execution_epoch")
            if record.get("state") == "completed":
                return InboxClaim(
                    InboxDisposition.DUPLICATE_COMPLETED,
                    command_id,
                    execution_epoch,
                    dict(record.get("result") or {}),
                )
            safe_result = sanitize_payload(result)
            record["state"] = "completed"
            record["completed_at"] = float(self._clock())
            record["result"] = safe_result
            self._write_atomic(path, record)
            return InboxClaim(
                InboxDisposition.DUPLICATE_COMPLETED,
                command_id,
                execution_epoch,
                safe_result,
            )

    def list_commands(self, *, state: str | None = None) -> list[dict[str, Any]]:
        """Read command inbox records for lifecycle reconciliation.

        This is an observation API only. Callers must settle records through
        the epoch-fenced transition methods rather than rewriting inbox files.
        """

        self._ensure_dirs()
        records: list[dict[str, Any]] = []
        for path in self.inbox_dir.glob("*.json"):
            with self._thread_lock, _process_lock(path):
                record = self._read(path)
            if not record or (state is not None and record.get("state") != state):
                continue
            records.append(record)
        records.sort(key=lambda item: (
            float(item.get("claimed_at") or 0.0),
            str(item.get("command_id") or ""),
        ))
        return records

    def advance_execution_epoch(
        self, *, conversation_id: str, run_id: str,
    ) -> str:
        self._ensure_dirs()
        path = self._stream_path(stream_identity(conversation_id, run_id))
        with self._thread_lock, _process_lock(path):
            path, stream = self._load_stream_locked(
                conversation_id=conversation_id, run_id=run_id,
            )
            epoch = "epoch_" + secrets.token_urlsafe(24)
            stream["execution_epoch"] = epoch
            stream["authority_state"] = "active"
            stream["tombstoned_at"] = 0.0
            self._write_atomic(path, stream)
            return epoch


class CanonicalProjection:
    """Projection-only dedup/gap/fencing ledger shared by PC-side tests."""

    def __init__(self) -> None:
        self._sequences: dict[str, int] = {}
        self._event_ids: dict[str, tuple[str, int]] = {}
        self._terminal_epochs: set[str] = set()
        self._values: dict[str, list[dict]] = {}

    def accept(self, frame: Mapping[str, Any]) -> ProjectionResult:
        try:
            stream_id = _safe_id(frame.get("stream_id"), "stream_id")
            event_id = _safe_id(frame.get("event_id"), "event_id")
            sequence = int(frame.get("sequence") or 0)
            epoch = _safe_id(
                frame.get("execution_epoch"), "execution_epoch", allow_empty=True,
            )
        except (TypeError, ValueError, OverflowError):
            return ProjectionResult(ProjectionDisposition.INVALID)
        if sequence <= 0:
            return ProjectionResult(ProjectionDisposition.INVALID)
        seen_identity = self._event_ids.get(event_id)
        if seen_identity is not None:
            if seen_identity != (stream_id, sequence):
                return ProjectionResult(
                    ProjectionDisposition.INVALID, stream_id, sequence, sequence,
                )
            return ProjectionResult(
                ProjectionDisposition.DUPLICATE, stream_id, sequence, sequence,
            )
        previous = self._sequences.get(stream_id, 0)
        if sequence > previous + 1:
            return ProjectionResult(
                ProjectionDisposition.GAP,
                stream_id,
                previous + 1,
                sequence,
            )
        if sequence <= previous:
            return ProjectionResult(
                ProjectionDisposition.DUPLICATE, stream_id, previous, sequence,
            )
        self._event_ids[event_id] = (stream_id, sequence)
        self._sequences[stream_id] = sequence
        if epoch and epoch in self._terminal_epochs and not frame.get("terminal"):
            return ProjectionResult(
                ProjectionDisposition.STALE_GENERATION,
                stream_id,
                sequence,
                sequence,
            )
        if frame.get("terminal") is True and epoch:
            self._terminal_epochs.add(epoch)
        if frame.get("historical") is True:
            return ProjectionResult(
                ProjectionDisposition.HISTORICAL,
                stream_id,
                sequence,
                sequence,
            )
        self._values.setdefault(stream_id, []).append(dict(frame))
        return ProjectionResult(
            ProjectionDisposition.APPLIED,
            stream_id,
            sequence,
            sequence,
        )

    def projected(self, stream_id: str) -> list[dict]:
        return [dict(item) for item in self._values.get(stream_id, ())]


_DEFAULT_STORE: DurableTransportStore | None = None
_DEFAULT_LOCK = threading.Lock()


def get_durable_transport_store() -> DurableTransportStore:
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = DurableTransportStore()
        return _DEFAULT_STORE


__all__ = [
    "CanonicalProjection", "DurableTransportStore", "EventEnvelope",
    "FailureDomain", "InboxClaim", "InboxDisposition", "PAYLOAD_VERSION",
    "PRODUCER_ID", "ProjectionDisposition", "ProjectionResult",
    "command_fingerprint", "get_durable_transport_store", "sanitize_payload",
    "stream_identity",
]
