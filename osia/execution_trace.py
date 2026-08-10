"""Bounded public execution-status projection for Mobile (WOR-254).

This module never accepts model prose, tool arguments/results, paths, provider
payloads, or exception text.  Producers select one deterministic display key
from the allowlist below; only the resulting safe projection may be persisted
or sent to a client.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping


SCHEMA_VERSION = 1
REDACTION_CLASS = "public_ui"
MAX_PERSISTED_EVENTS = 24
MAX_LIVE_FRAMES = 32
MAX_SERIALIZED_BYTES = 12 * 1024
MAX_RELAY_SNAPSHOT_EVENTS = 8
MAX_RELAY_SNAPSHOT_BYTES = 4 * 1024

EVENT_KINDS = frozenset({
    "goal",
    "action",
    "waiting_permission",
    "result",
    "continuation",
    "reconciliation",
    "terminal",
})

# Values are ``(kind, allowed argument names)``.  No argument accepts arbitrary
# strings; currently the UI needs only deterministic labels.
DISPLAY_SPECS: dict[str, tuple[str, frozenset[str]]] = {
    "execution.goal.answer": ("goal", frozenset()),
    "execution.goal.complete_task": ("goal", frozenset()),
    "execution.action.preparing": ("action", frozenset()),
    "execution.action.browser": ("action", frozenset()),
    "execution.action.document": ("action", frozenset()),
    "execution.action.filesystem": ("action", frozenset()),
    "execution.action.shell": ("action", frozenset()),
    "execution.action.connector": ("action", frozenset()),
    "execution.action.memory": ("action", frozenset()),
    "execution.action.scheduled_task": ("action", frozenset()),
    "execution.action.capability": ("action", frozenset()),
    "execution.permission.waiting": ("waiting_permission", frozenset()),
    "execution.result.completed": ("result", frozenset()),
    "execution.result.needs_attention": ("result", frozenset()),
    "execution.result.cancelled": ("result", frozenset()),
    "execution.continuation.new_strategy": ("continuation", frozenset()),
    "execution.reconciliation.verifying": ("reconciliation", frozenset()),
    "execution.terminal.success": ("terminal", frozenset()),
    "execution.terminal.blocked": ("terminal", frozenset()),
    "execution.terminal.cancelled": ("terminal", frozenset()),
    "execution.terminal.safety_block": ("terminal", frozenset()),
}

_EVENT_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "event_id",
    "conversation_id",
    "request_id",
    "turn_id",
    "sequence",
    "revision",
    "kind",
    "display",
    "timestamp",
    "terminal",
    "redaction_class",
})
_EVENT_OPTIONAL_FIELDS = frozenset({
    "invocation_id",
    "tool_attempt_id",
    "permission_request_id",
})

_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_PRIORITY = {
    "terminal": 0,
    "goal": 1,
    "waiting_permission": 2,
    "reconciliation": 3,
    "result": 4,
    "continuation": 5,
    "action": 6,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: object, *, maximum: int = 256, required: bool = True) -> str:
    text = str(value or "")
    if (
        len(text) > maximum
        or (required and not text)
        or any(char not in _ID_CHARS for char in text)
    ):
        raise ValueError("invalid execution trace correlation identifier")
    return text


def _safe_args(display_key: str, value: object) -> dict:
    spec = DISPLAY_SPECS.get(display_key)
    if spec is None or not isinstance(value, Mapping):
        raise ValueError("execution trace display key is not allowlisted")
    allowed = spec[1]
    if set(value) - allowed:
        raise ValueError("execution trace display arguments are not allowlisted")
    # Reserved for future deterministic numeric/enum arguments.  Fail closed
    # until a key explicitly defines and validates one.
    if value:
        raise ValueError("execution trace display arguments are unsupported")
    return {}


def display_key_for_tool(tool_name: object) -> str:
    """Return a coarse safe capability label without exposing arguments."""

    name = str(tool_name or "").strip().lower()
    if not name:
        return "execution.action.capability"
    if any(part in name for part in ("browser", "navigate", "page", "tab", "form")):
        return "execution.action.browser"
    if any(part in name for part in ("document", "pdf", "docx", "spreadsheet", "slide")):
        return "execution.action.document"
    if any(part in name for part in ("file", "folder", "directory", "path")):
        return "execution.action.filesystem"
    if any(part in name for part in ("shell", "command", "terminal", "process")):
        return "execution.action.shell"
    if any(part in name for part in ("mcp", "connector", "gmail", "calendar", "oauth")):
        return "execution.action.connector"
    if "memory" in name:
        return "execution.action.memory"
    if any(part in name for part in ("schedule", "cron", "reminder")):
        return "execution.action.scheduled_task"
    return "execution.action.capability"


def terminal_display_key(terminal_state: object) -> str:
    return {
        "success_verified": "execution.terminal.success",
        "blocked_verified": "execution.terminal.blocked",
        "user_cancelled": "execution.terminal.cancelled",
        "hard_safety_block": "execution.terminal.safety_block",
    }.get(str(terminal_state or ""), "")


@dataclass(frozen=True)
class SafeExecutionTraceEvent:
    event_id: str
    conversation_id: str
    request_id: str
    turn_id: str
    sequence: int
    revision: int
    kind: str
    display_key: str
    display_args: dict
    timestamp: str
    terminal: bool
    invocation_id: str = ""
    tool_attempt_id: str = ""
    permission_request_id: str = ""

    def public_dict(self) -> dict:
        value = {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "revision": self.revision,
            "kind": self.kind,
            "display": {"key": self.display_key, "args": dict(self.display_args)},
            "timestamp": self.timestamp,
            "terminal": self.terminal,
            "redaction_class": REDACTION_CLASS,
        }
        for key, item in (
            ("invocation_id", self.invocation_id),
            ("tool_attempt_id", self.tool_attempt_id),
            ("permission_request_id", self.permission_request_id),
        ):
            if item:
                value[key] = item
        return value


class ExecutionTraceLedger:
    """One authoritative, bounded trace for one correlated user turn."""

    def __init__(
        self,
        *,
        conversation_id: str,
        request_id: str,
        turn_id: str,
        callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.conversation_id = _safe_id(conversation_id)
        self.request_id = _safe_id(request_id)
        self.turn_id = _safe_id(turn_id)
        self._callback = callback if callable(callback) else None
        self._events: list[SafeExecutionTraceEvent] = []
        self._by_slot: dict[str, int] = {}
        self._sequence = 0
        self._live_frames = 0
        self._terminal = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.conversation_id, self.request_id, self.turn_id

    def record(
        self,
        *,
        kind: str,
        display_key: str,
        display_args: Mapping[str, object] | None = None,
        terminal: bool = False,
        invocation_id: str = "",
        tool_attempt_id: str = "",
        permission_request_id: str = "",
        dedupe_key: str = "",
    ) -> SafeExecutionTraceEvent | None:
        if self._terminal:
            return None
        spec = DISPLAY_SPECS.get(display_key)
        if kind not in EVENT_KINDS or spec is None or spec[0] != kind:
            return None
        try:
            safe_args = _safe_args(display_key, display_args or {})
            invocation = _safe_id(invocation_id, maximum=256, required=False)
            attempt = _safe_id(tool_attempt_id, maximum=256, required=False)
            permission = _safe_id(
                permission_request_id, maximum=256, required=False,
            )
        except ValueError:
            return None
        if terminal != (kind == "terminal"):
            return None

        slot = str(dedupe_key or "")
        existing_index = self._by_slot.get(slot) if slot else None
        if existing_index is not None:
            previous = self._events[existing_index]
            if (
                previous.kind == kind
                and previous.display_key == display_key
                and previous.display_args == safe_args
                and previous.invocation_id == invocation
                and previous.tool_attempt_id == attempt
                and previous.permission_request_id == permission
            ):
                return previous
            event = SafeExecutionTraceEvent(
                event_id=previous.event_id,
                conversation_id=self.conversation_id,
                request_id=self.request_id,
                turn_id=self.turn_id,
                sequence=previous.sequence,
                revision=previous.revision + 1,
                kind=kind,
                display_key=display_key,
                display_args=safe_args,
                timestamp=_now(),
                terminal=terminal,
                invocation_id=invocation,
                tool_attempt_id=attempt,
                permission_request_id=permission,
            )
            self._events[existing_index] = event
        else:
            self._sequence += 1
            stable_slot = slot or f"sequence:{self._sequence}"
            digest = hashlib.sha256(
                "\0".join((*self.key, stable_slot)).encode("utf-8")
            ).hexdigest()[:32]
            event = SafeExecutionTraceEvent(
                event_id="trace_" + digest,
                conversation_id=self.conversation_id,
                request_id=self.request_id,
                turn_id=self.turn_id,
                sequence=self._sequence,
                revision=1,
                kind=kind,
                display_key=display_key,
                display_args=safe_args,
                timestamp=_now(),
                terminal=terminal,
                invocation_id=invocation,
                tool_attempt_id=attempt,
                permission_request_id=permission,
            )
            self._events.append(event)
            if slot:
                self._by_slot[slot] = len(self._events) - 1

        if terminal:
            self._terminal = True
        self._publish(event)
        return event

    def _publish(self, event: SafeExecutionTraceEvent) -> None:
        if self._callback is None:
            return
        # Reserve the final live slot for the terminal tombstone.
        if event.terminal:
            if self._live_frames >= MAX_LIVE_FRAMES:
                return
        elif self._live_frames >= MAX_LIVE_FRAMES - 1:
            return
        frame = {"type": "execution_trace", **event.public_dict()}
        frame["snapshot"] = self.snapshot()["events"]
        if len(json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > MAX_SERIALIZED_BYTES:
            return
        self._live_frames += 1
        try:
            self._callback(frame)
        except Exception:
            # UI projection is optional and must never alter execution.
            pass

    def _compacted_events(self) -> list[SafeExecutionTraceEvent]:
        events = list(self._events)
        if len(events) > MAX_PERSISTED_EVENTS:
            selected = sorted(
                events,
                key=lambda item: (_PRIORITY[item.kind], -item.sequence),
            )[:MAX_PERSISTED_EVENTS]
            events = sorted(selected, key=lambda item: item.sequence)
        while events:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "conversation_id": self.conversation_id,
                "request_id": self.request_id,
                "turn_id": self.turn_id,
                "redaction_class": REDACTION_CLASS,
                "events": [item.public_dict() for item in events],
            }
            encoded = json.dumps(
                payload, ensure_ascii=True, separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) <= MAX_SERIALIZED_BYTES:
                return events
            removable = max(
                range(len(events)),
                key=lambda index: (_PRIORITY[events[index].kind], -events[index].sequence),
            )
            events.pop(removable)
        return []

    def snapshot(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "redaction_class": REDACTION_CLASS,
            "events": [item.public_dict() for item in self._compacted_events()],
        }


def validate_persisted_trace(value: object) -> dict | None:
    """Fail-closed validator used before exposing stored trace metadata."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "conversation_id",
        "request_id",
        "turn_id",
        "redaction_class",
        "events",
    }:
        return None
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("redaction_class") != REDACTION_CLASS
    ):
        return None
    events = value.get("events")
    if not isinstance(events, list) or len(events) > MAX_PERSISTED_EVENTS:
        return None
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_SERIALIZED_BYTES:
        return None
    try:
        ledger = ExecutionTraceLedger(
            conversation_id=value.get("conversation_id"),
            request_id=value.get("request_id"),
            turn_id=value.get("turn_id"),
        )
    except ValueError:
        return None
    last_sequence = 0
    terminal_seen = False
    validated: list[dict] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            return None
        raw_keys = set(raw)
        if (
            not _EVENT_REQUIRED_FIELDS.issubset(raw_keys)
            or raw_keys - _EVENT_REQUIRED_FIELDS - _EVENT_OPTIONAL_FIELDS
        ):
            return None
        display = raw.get("display")
        kind = raw.get("kind")
        if not isinstance(display, Mapping) or set(display) != {"key", "args"}:
            return None
        key = display.get("key")
        spec = DISPLAY_SPECS.get(str(key or ""))
        sequence = raw.get("sequence")
        revision = raw.get("revision")
        timestamp = raw.get("timestamp")
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("redaction_class") != REDACTION_CLASS
            or raw.get("conversation_id") != ledger.conversation_id
            or raw.get("request_id") != ledger.request_id
            or raw.get("turn_id") != ledger.turn_id
            or spec is None
            or spec[0] != kind
            or type(sequence) is not int
            or sequence <= last_sequence
            or type(revision) is not int
            or revision < 1
            or not isinstance(timestamp, str)
            or len(timestamp) > 64
            or not isinstance(raw.get("terminal"), bool)
            or terminal_seen
        ):
            return None
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            _safe_id(raw.get("event_id"), maximum=256)
            _safe_args(str(key), display.get("args"))
            for optional in (
                "invocation_id", "tool_attempt_id", "permission_request_id",
            ):
                _safe_id(raw.get(optional), maximum=256, required=False)
        except ValueError:
            return None
        is_terminal = raw.get("terminal") is True
        if is_terminal != (kind == "terminal"):
            return None
        terminal_seen = is_terminal
        last_sequence = sequence
        validated.append(dict(raw))
    return {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": ledger.conversation_id,
        "request_id": ledger.request_id,
        "turn_id": ledger.turn_id,
        "redaction_class": REDACTION_CLASS,
        "events": validated,
    }
