"""Canonical public commentary and structured execution facts (WOR-254/340).

This module never accepts raw reasoning, tool arguments/results, paths,
provider payloads, or exception text. Producers select one deterministic
display key from the allowlist below. Public assistant commentary and a
provider-designated public summary use the same dedicated text sanitizer rather
than the general event API.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
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

_SENSITIVE_PUBLIC_TEXT_PATTERNS = (
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|"
        r"secret|password|passphrase|credential|private[_ -]?key)\b"
        r"\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\w)\+?(?:\d[\s().-]?){8,}\d(?!\w)"),
    re.compile(
        r"<\s*/?\s*(?:system|developer)\b|"
        r"\b(?:system|developer)\s+prompt\s*[:=]",
        re.IGNORECASE,
    ),
)

EVENT_KINDS = frozenset({
    "goal",
    "action",
    "waiting_permission",
    "result",
    "continuation",
    "reconciliation",
    "public_reasoning_summary",
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
    "execution.result.observed": ("result", frozenset()),
    "execution.result.needs_attention": ("result", frozenset()),
    "execution.result.cancelled": ("result", frozenset()),
    "execution.continuation.new_strategy": ("continuation", frozenset()),
    "execution.reconciliation.verifying": ("reconciliation", frozenset()),
    "execution.reasoning.summary": ("public_reasoning_summary", frozenset()),
    "execution.terminal.success": ("terminal", frozenset()),
    "execution.terminal.blocked": ("terminal", frozenset()),
    "execution.terminal.cancelled": ("terminal", frozenset()),
    "execution.terminal.safety_block": ("terminal", frozenset()),
}

_LEGACY_EVENT_REQUIRED_FIELDS = frozenset({
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
_TRACE_BINDING_FIELDS = frozenset({
    "run_id",
    "provider_id",
    "model_id",
    "effective_reasoning",
})
_EVENT_REQUIRED_FIELDS = _LEGACY_EVENT_REQUIRED_FIELDS | _TRACE_BINDING_FIELDS
_EVENT_OPTIONAL_FIELDS = frozenset({
    "invocation_id",
    "tool_attempt_id",
    "permission_request_id",
    "summary",
})
_LEGACY_SNAPSHOT_FIELDS = frozenset({
    "schema_version",
    "conversation_id",
    "request_id",
    "turn_id",
    "redaction_class",
    "events",
})

_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_PRIORITY = {
    "terminal": 0,
    "goal": 1,
    "public_reasoning_summary": 2,
    "waiting_permission": 3,
    "reconciliation": 4,
    "result": 5,
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


def _safe_binding_label(
    value: object,
    *,
    maximum: int,
    fallback: str,
) -> str:
    text = str(value or fallback).strip()
    if not text:
        text = fallback
    if len(text) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("invalid execution trace binding label")
    return text


def sanitize_public_reasoning_summary(value: object) -> str:
    """Sanitize a provider-designated public summary, never raw CoT."""

    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        char
        for char in text
        if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    )
    if re.search(
        r"<\s*/?\s*(?:think(?:ing)?|reasoning|analysis|"
        r"tool(?:[_-]?(?:call|result|code))?|function_calls)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return ""
    if re.search(r"<[|｜]|[|｜]DSML[|｜]", text, flags=re.IGNORECASE):
        return ""
    if any(pattern.search(text) for pattern in _SENSITIVE_PUBLIC_TEXT_PATTERNS):
        return ""
    # Directional overrides/isolates can make a benign-looking UI string render
    # differently from its stored value. They are never needed in a summary.
    text = text.translate({
        codepoint: None
        for codepoint in (
            0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
            0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
        )
    })
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    compacted = []
    for line in lines:
        if not line and (not compacted or not compacted[-1]):
            continue
        compacted.append(line)
    text = "\n".join(compacted)
    return text


def sanitize_public_model_narration(value: object) -> str:
    """Sanitize optional public progress text before it enters a turn trace."""

    text = sanitize_public_reasoning_summary(value)
    normalized = re.sub(r"[.!…]+$", "", text.strip()).casefold()
    if normalized in {
        "thinking", "planning", "analyzing", "analysing", "working",
        "je réfléchis", "analyse", "en cours", "je travaille",
    }:
        return ""
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
    if name in {"search_public_web", "read_public_web_page"}:
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
    run_id: str
    provider_id: str
    model_id: str
    effective_reasoning: str
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
    summary: str = ""

    def public_dict(self) -> dict:
        value = {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "effective_reasoning": self.effective_reasoning,
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
        if self.kind == "public_reasoning_summary" and self.summary:
            value["summary"] = self.summary
        return value


class ExecutionTraceLedger:
    """One canonical public trace with compact diagnostics for an exact turn."""

    def __init__(
        self,
        *,
        conversation_id: str,
        request_id: str,
        turn_id: str,
        run_id: str = "",
        provider_id: str = "unknown",
        model_id: str = "unknown",
        effective_reasoning: str = "auto",
        callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.conversation_id = _safe_id(conversation_id)
        self.request_id = _safe_id(request_id)
        self.turn_id = _safe_id(turn_id)
        self.run_id = _safe_id(run_id or turn_id)
        self.provider_id = _safe_binding_label(
            provider_id, maximum=64, fallback="unknown",
        )
        self.model_id = _safe_binding_label(
            model_id, maximum=160, fallback="unknown",
        )
        self.effective_reasoning = _safe_binding_label(
            effective_reasoning, maximum=64, fallback="auto",
        )
        self._callback = callback if callable(callback) else None
        self._events: list[SafeExecutionTraceEvent] = []
        self._by_slot: dict[str, int] = {}
        self._sequence = 0
        self._live_frames = 0
        self._terminal = False
        self._lock = threading.RLock()

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.conversation_id,
            self.request_id,
            self.turn_id,
            self.run_id,
        )

    def binding_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "effective_reasoning": self.effective_reasoning,
        }

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
        _public_reasoning_summary: str = "",
    ) -> SafeExecutionTraceEvent | None:
        with self._lock:
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
            summary = ""
            if kind == "public_reasoning_summary":
                summary = sanitize_public_reasoning_summary(
                    _public_reasoning_summary
                )
                if not summary:
                    return None
            elif _public_reasoning_summary:
                return None

            slot = str(dedupe_key or "")
            existing_index = self._by_slot.get(slot) if slot else None
            if existing_index is not None and kind == "public_reasoning_summary":
                prior_summary = self._events[existing_index].summary
                if prior_summary and not summary.startswith(prior_summary):
                    if prior_summary.startswith(summary):
                        return self._events[existing_index]
                    # A distinct public statement is an append, never a rewrite
                    # of wording that was already shown to the user.
                    slot += ":" + hashlib.sha256(summary.encode("utf-8")).hexdigest()
                    existing_index = self._by_slot.get(slot)
            if existing_index is not None:
                previous = self._events[existing_index]
                if (
                    previous.kind == kind
                    and previous.display_key == display_key
                    and previous.display_args == safe_args
                    and previous.invocation_id == invocation
                    and previous.tool_attempt_id == attempt
                    and previous.permission_request_id == permission
                    and previous.summary == summary
                ):
                    return previous
                event = SafeExecutionTraceEvent(
                    event_id=previous.event_id,
                    conversation_id=self.conversation_id,
                    request_id=self.request_id,
                    turn_id=self.turn_id,
                    run_id=self.run_id,
                    provider_id=self.provider_id,
                    model_id=self.model_id,
                    effective_reasoning=self.effective_reasoning,
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
                    summary=summary,
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
                    run_id=self.run_id,
                    provider_id=self.provider_id,
                    model_id=self.model_id,
                    effective_reasoning=self.effective_reasoning,
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
                    summary=summary,
                )
                self._events.append(event)
                if slot:
                    self._by_slot[slot] = len(self._events) - 1

            if terminal:
                self._terminal = True
            self._publish(event)
            return event

    def record_public_reasoning_summary(
        self,
        summary: str,
        *,
        source_id: str = "",
    ) -> SafeExecutionTraceEvent | None:
        """Record only an explicit provider public-summary aggregate."""

        return self.record(
            kind="public_reasoning_summary",
            display_key="execution.reasoning.summary",
            dedupe_key="provider_public_reasoning_summary:" + source_id,
            _public_reasoning_summary=summary,
        )

    def record_public_narration(
        self,
        narration: str,
    ) -> SafeExecutionTraceEvent | None:
        """Append a deduplicated public model statement without shortening it."""

        summary = sanitize_public_model_narration(narration)
        if not summary:
            return None
        slot = "model_public_narration:" + hashlib.sha256(
            summary.encode("utf-8")
        ).hexdigest()[:32]
        with self._lock:
            for event in self._events:
                if (
                    event.kind == "public_reasoning_summary"
                    and event.summary == summary
                ):
                    return event
            return self.record(
                kind="public_reasoning_summary",
                display_key="execution.reasoning.summary",
                dedupe_key=slot,
                _public_reasoning_summary=summary,
            )

    def _publish(self, event: SafeExecutionTraceEvent) -> None:
        if self._callback is None:
            return
        # Reserve the final activity slot for the terminal tombstone. Revisions
        # of public model text must not freeze when an activity
        # budget is exhausted. Relay applies its own coalescing/budget later.
        if event.terminal:
            if self._live_frames >= MAX_LIVE_FRAMES:
                return
        elif (
            event.kind != "public_reasoning_summary"
            and self._live_frames >= MAX_LIVE_FRAMES - 1
        ):
            return
        frame = {"type": "execution_trace", **event.public_dict()}
        frame["snapshot"] = self.snapshot()["events"]
        if event.kind != "public_reasoning_summary":
            self._live_frames += 1
        try:
            self._callback(frame)
        except Exception:
            # UI projection is optional and must never alter execution.
            pass

    def _compacted_events(self) -> list[SafeExecutionTraceEvent]:
        events = list(self._events)
        commentary = [item for item in events if item.kind == "public_reasoning_summary"]
        diagnostics = [item for item in events if item.kind != "public_reasoning_summary"]
        if len(events) > MAX_PERSISTED_EVENTS:
            selected = sorted(
                diagnostics,
                key=lambda item: (_PRIORITY[item.kind], -item.sequence),
            )[:max(1, MAX_PERSISTED_EVENTS - len(commentary))]
            events = sorted(commentary + selected, key=lambda item: item.sequence)
        while events:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "conversation_id": self.conversation_id,
                "request_id": self.request_id,
                "turn_id": self.turn_id,
                "run_id": self.run_id,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "effective_reasoning": self.effective_reasoning,
                "redaction_class": REDACTION_CLASS,
                "events": [item.public_dict() for item in events],
            }
            encoded = json.dumps(
                payload, ensure_ascii=True, separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) <= MAX_SERIALIZED_BYTES:
                return events
            removable_indexes = [
                index for index, item in enumerate(events)
                if item.kind not in {"public_reasoning_summary", "terminal"}
            ]
            if not removable_indexes:
                return events
            removable = max(
                removable_indexes,
                key=lambda index: (_PRIORITY[events[index].kind], -events[index].sequence),
            )
            events.pop(removable)
        return []

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "conversation_id": self.conversation_id,
                "request_id": self.request_id,
                "turn_id": self.turn_id,
                "run_id": self.run_id,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "effective_reasoning": self.effective_reasoning,
                "redaction_class": REDACTION_CLASS,
                "events": [
                    item.public_dict() for item in self._compacted_events()
                ],
            }


def validate_persisted_trace(value: object) -> dict | None:
    """Validate canonical metadata without applying transport retention limits."""

    if not isinstance(value, Mapping):
        return None
    value_keys = set(value)
    if value_keys not in {
        _LEGACY_SNAPSHOT_FIELDS,
        _LEGACY_SNAPSHOT_FIELDS | _TRACE_BINDING_FIELDS,
    }:
        return None
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("redaction_class") != REDACTION_CLASS
    ):
        return None
    events = value.get("events")
    if not isinstance(events, list):
        return None
    extended_envelope = _TRACE_BINDING_FIELDS.issubset(value_keys)
    try:
        conversation_id = _safe_id(value.get("conversation_id"))
        request_id = _safe_id(value.get("request_id"))
        turn_id = _safe_id(value.get("turn_id"))
        envelope_binding = None
        if extended_envelope:
            envelope_binding = {
                "run_id": _safe_id(value.get("run_id")),
                "provider_id": _safe_binding_label(
                    value.get("provider_id"), maximum=64, fallback="unknown",
                ),
                "model_id": _safe_binding_label(
                    value.get("model_id"), maximum=160, fallback="unknown",
                ),
                "effective_reasoning": _safe_binding_label(
                    value.get("effective_reasoning"), maximum=64, fallback="auto",
                ),
            }
            if any(
                value.get(key) != item
                for key, item in envelope_binding.items()
            ):
                return None
    except ValueError:
        return None
    last_sequence = 0
    terminal_seen = False
    validated: list[dict] = []
    inferred_binding = None
    for raw in events:
        if not isinstance(raw, Mapping):
            return None
        raw_keys = set(raw)
        extended_event = _TRACE_BINDING_FIELDS.issubset(raw_keys)
        required_fields = (
            _EVENT_REQUIRED_FIELDS
            if extended_event else
            _LEGACY_EVENT_REQUIRED_FIELDS
        )
        if not required_fields.issubset(raw_keys) or (
            raw_keys - required_fields - _EVENT_OPTIONAL_FIELDS
        ):
            return None
        if extended_envelope and not extended_event:
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
            or raw.get("conversation_id") != conversation_id
            or raw.get("request_id") != request_id
            or raw.get("turn_id") != turn_id
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
            event_binding = None
            if extended_event:
                event_binding = {
                    "run_id": _safe_id(raw.get("run_id")),
                    "provider_id": _safe_binding_label(
                        raw.get("provider_id"), maximum=64, fallback="unknown",
                    ),
                    "model_id": _safe_binding_label(
                        raw.get("model_id"), maximum=160, fallback="unknown",
                    ),
                    "effective_reasoning": _safe_binding_label(
                        raw.get("effective_reasoning"), maximum=64, fallback="auto",
                    ),
                }
                if any(
                    raw.get(binding_key) != item
                    for binding_key, item in event_binding.items()
                ):
                    return None
        except ValueError:
            return None
        if event_binding is not None:
            if envelope_binding is not None and event_binding != envelope_binding:
                return None
            if inferred_binding is None:
                inferred_binding = event_binding
            elif event_binding != inferred_binding:
                return None
        elif inferred_binding is not None:
            return None
        if kind == "public_reasoning_summary":
            summary = raw.get("summary")
            if (
                not extended_event
                or not isinstance(summary, str)
                or sanitize_public_reasoning_summary(summary) != summary
                or not summary
            ):
                return None
        elif "summary" in raw:
            return None
        is_terminal = raw.get("terminal") is True
        if is_terminal != (kind == "terminal"):
            return None
        terminal_seen = is_terminal
        last_sequence = sequence
        validated.append(dict(raw))
    result = {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": conversation_id,
        "request_id": request_id,
        "turn_id": turn_id,
        "redaction_class": REDACTION_CLASS,
        "events": validated,
    }
    binding = envelope_binding or inferred_binding
    if binding is not None:
        result.update(binding)
    return result


def validate_execution_trace_frame(value: object) -> dict | None:
    """Validate an existing public wire event and its correlated snapshot."""
    if not isinstance(value, Mapping) or value.get("type") != "execution_trace":
        return None
    snapshot = validate_persisted_trace({
        name: value.get(name)
        for name in _LEGACY_SNAPSHOT_FIELDS | _TRACE_BINDING_FIELDS
        if name != "events"
    } | {"events": value.get("snapshot")})
    if snapshot is None:
        return None
    event = next((
        item for item in snapshot["events"]
        if item["event_id"] == value.get("event_id")
        and item["revision"] == value.get("revision")
    ), None)
    if event is None or set(value) != set(event) | {"type", "snapshot"}:
        return None
    if any(value.get(name) != item for name, item in event.items()):
        return None
    return {"type": "execution_trace", **event, "snapshot": snapshot["events"]}
