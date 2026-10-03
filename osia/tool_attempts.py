"""Durable, privacy-bounded tool-attempt lifecycle for normal chat (WOR-227).

The store is intentionally independent from the agent, browser and Autonomous
modules.  It persists only correlation and safety metadata; tool arguments,
tool results, page content and browser credentials never cross this boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
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

from osia.action_truth import (
    ActionState,
    CanonicalEffectClass,
    ReconciliationStatus,
    action_identity,
    canonical_effect_class,
    evidence_reference,
    new_action_truth,
    normalize_action_truth,
    semantic_effect_identity,
    settle_policy_for,
    transition_action_truth,
)
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
_PRIMITIVE_LIFECYCLES = frozenset({
    "prepared", "dispatch_started", "dispatch_returned", "receipt_bound",
    "verified", "not_applied", "unknown",
})
_PRIMITIVE_EFFECT_CLASSES = frozenset({
    "impossible", "effect_possible", "verified", "not_applied", "unknown",
})
# Compatibility defaults for journals created before action-level settle
# policies existed. New actions persist an effect-aware policy in action_truth.
_LEGACY_SEMANTIC_SETTLE_CHECKS = 3
_LEGACY_SEMANTIC_SETTLE_INTERVAL_SECONDS = 1.0
_SEMANTIC_SETTLE_STATES = frozenset({
    "inactive", "active", "proof_observed", "exhausted", "cancelled",
    "stale_target", "observation_failed", "evidence_unresolved", "aborted",
})
_SEMANTIC_SETTLE_PROBE_CLASSIFICATIONS = frozenset({
    "completed_match", "completed_nonmatch", "completed_inconclusive",
    "probe_failed", "stale_or_invalid",
})
_ATTEMPT_AUTHORITY_STATES = frozenset({
    "active", "cancelled", "terminal", "superseded",
})
_PROVIDER_REFERENCE_KEYS = frozenset({
    "provider_request_id", "provider_response_id", "tool_call_id", "call_id",
    "function_call_id", "continuation_id", "session_id",
    "external_operation_id", "status_id", "idempotency_key", "thread_id",
    "turn_id", "item_id", "provider_native_call_id", "native_thread_id",
    "native_turn_id",
})
_PROVIDER_OPERATION_REFERENCE_PRIORITY = (
    "external_operation_id", "status_id", "function_call_id", "call_id",
    "provider_request_id", "provider_response_id", "provider_native_call_id",
    "tool_call_id", "item_id",
    "continuation_id", "session_id",
)


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


class SemanticSettleProbeClassification(str, Enum):
    """Typed outcome of one bounded read-only semantic settle probe."""

    COMPLETED_MATCH = "completed_match"
    COMPLETED_NONMATCH = "completed_nonmatch"
    COMPLETED_INCONCLUSIVE = "completed_inconclusive"
    PROBE_FAILED = "probe_failed"
    STALE_OR_INVALID = "stale_or_invalid"


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

# Stable, value-free wrapper results used only when the durable dispatch
# boundary itself blocks the external callable.  The agent converts the
# cancellation result into its typed CANCELLED_PROVEN outcome before anything
# user-facing is committed.
TOOL_DISPATCH_CANCELLED_RESULT = (
    "ERROR: action cancelled before dispatch; no effect occurred"
)
TOOL_DISPATCH_BOUNDARY_UNAVAILABLE_RESULT = (
    "ERROR: durable tool dispatch boundary unavailable; "
    "the tool was not executed"
)


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


@dataclass(frozen=True)
class SemanticSettleProbeResult:
    """Sanitized evidence contract returned by one settle observer invocation."""

    classification: SemanticSettleProbeClassification
    observer_mechanism: str
    observation_started_at: float
    observation_completed_at: float = 0.0
    target_application: str = ""
    surface_ref: str = ""
    native_window_id: str = ""
    window_generation: str = ""
    exact_postcondition_match: bool | None = None
    reason_code: str = ""
    evidence_ref: str = ""
    probe_invoked: bool = True
    terminal_payload: Any = None


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


def _created_at_sort_key(item: Mapping[str, Any]) -> float:
    try:
        return max(0.0, float(item.get("created_at") or 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


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


def _live_process_identity(value: Any) -> tuple[bool, str]:
    """Return liveness plus a PID-reuse-safe identity when available."""

    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return False, ""
    if pid <= 0:
        return False, ""
    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False, ""
        return True, f"{pid}:{process.create_time():.6f}"
    except (ImportError, OSError):
        return _pid_alive(pid), ""
    except Exception as exc:
        try:
            import psutil

            if isinstance(exc, psutil.NoSuchProcess):
                return False, ""
        except ImportError:
            pass
        # AccessDenied and indeterminate probes are not proof of death.
        return True, ""


def _continuation_owner_alive(record: Mapping[str, Any]) -> bool:
    """Reject only the exact still-live owner of a continuation claim."""

    alive, live_identity = _live_process_identity(
        record.get("continuation_claimed_pid")
    )
    if not alive:
        return False
    claimed_identity = str(
        record.get("continuation_claimed_process_identity") or ""
    )
    return bool(
        not claimed_identity
        or not live_identity
        or claimed_identity == live_identity
    )


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


def _safe_primitive_text(value: Any, *, maximum: int = 160) -> str:
    """Keep only bounded identifier-like primitive metadata, never UI text."""

    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.:@!+/-]+", text):
        return ""
    return text


def _safe_primitive_journal(value: Any) -> list[dict[str, Any]]:
    entries = []
    for raw in value if isinstance(value, list) else ():
        if not isinstance(raw, Mapping):
            continue
        lifecycle = str(raw.get("lifecycle") or "prepared")
        effect_class = str(raw.get("effect_classification") or "impossible")
        if lifecycle not in _PRIMITIVE_LIFECYCLES:
            lifecycle = "unknown"
        if effect_class not in _PRIMITIVE_EFFECT_CLASSES:
            effect_class = "unknown"
        timestamps = {}
        for name in (
            "prepared_at", "dispatch_started_at", "dispatch_returned_at",
            "receipt_bound_at", "verified_at", "not_applied_at",
            "unknown_at",
        ):
            try:
                number = max(0.0, float(raw.get(name) or 0.0))
            except (TypeError, ValueError, OverflowError):
                number = 0.0
            if number:
                timestamps[name] = number
        entry = {
            "schema_version": 1,
            "primitive_id": _safe_primitive_text(raw.get("primitive_id")),
            "action_id": _safe_primitive_text(raw.get("action_id")),
            "semantic_effect_id": _safe_primitive_text(
                raw.get("semantic_effect_id")
            ),
            "effect_role": (
                "preparatory"
                if raw.get("effect_role") == "preparatory" else "semantic"
            ),
            "checkpoint_role": (
                "intermediate"
                if raw.get("checkpoint_role") == "intermediate" else "final"
            ),
            "semantic_batch_id": _safe_primitive_text(
                raw.get("semantic_batch_id")
            ),
            "run_id": _safe_primitive_text(raw.get("run_id")),
            "task_id": _safe_primitive_text(raw.get("task_id")),
            "semantic_node_id": _safe_primitive_text(
                raw.get("semantic_node_id")
            ),
            "semantic_substep_id": _safe_primitive_text(
                raw.get("semantic_substep_id")
            ),
            "strategy_attempt_id": _safe_primitive_text(
                raw.get("strategy_attempt_id")
            ),
            "operation_id": _safe_primitive_text(raw.get("operation_id")),
            "tool_attempt_id": _safe_primitive_text(
                raw.get("tool_attempt_id")
            ),
            "application_identity": _safe_primitive_text(
                raw.get("application_identity")
            ),
            "surface_ref": _safe_primitive_text(raw.get("surface_ref")),
            "native_window_id": _safe_primitive_text(
                raw.get("native_window_id")
            ),
            "window_generation": _safe_primitive_text(
                raw.get("window_generation")
            ),
            "runtime_identity": _safe_primitive_text(
                raw.get("runtime_identity")
            ),
            "automation_id": _safe_primitive_text(raw.get("automation_id")),
            "uia_pattern": _safe_primitive_text(raw.get("uia_pattern")),
            "attempted_effect": _safe_primitive_text(
                raw.get("attempted_effect")
            ),
            "semantic_postcondition": _safe_primitive_text(
                raw.get("semantic_postcondition")
            ),
            "primitive_receipt_id": _safe_primitive_text(
                raw.get("primitive_receipt_id")
            ),
            "dispatch_state": _safe_primitive_text(
                raw.get("dispatch_state")
            ),
            "outcome": _safe_primitive_text(raw.get("outcome")),
            "reason_code": _safe_reason(raw.get("reason_code")),
            "failure_domain": _safe_reason(raw.get("failure_domain")),
            "failure_class": _safe_reason(raw.get("failure_class")),
            "stale_reason": _safe_reason(raw.get("stale_reason")),
            "dispatch_preflight_result": _safe_reason(
                raw.get("dispatch_preflight_result")
            ),
            "failed_before_effect_reason": _safe_reason(
                raw.get("failed_before_effect_reason")
            ),
            "stale_rebind_status": _safe_reason(
                raw.get("stale_rebind_status")
            ),
            "selected_pattern_available": (
                raw.get("selected_pattern_available") is True
            ),
            "candidate_applicable": raw.get("candidate_applicable") is True,
            "lifecycle": lifecycle,
            "effect_classification": effect_class,
            **timestamps,
        }
        try:
            entry["batch_index"] = max(0, int(raw.get("batch_index") or 0))
            entry["batch_size"] = max(0, int(raw.get("batch_size") or 0))
        except (TypeError, ValueError, OverflowError):
            entry["batch_index"] = 0
            entry["batch_size"] = 0
        if entry["primitive_id"]:
            entries.append(entry)
        if len(entries) >= 32:
            break
    return entries


def _safe_semantic_settle_receipts(
    value: Any, *, max_checks: int,
) -> list[dict[str, Any]]:
    receipts = []
    seen_slots: set[int] = set()
    for raw in (value if isinstance(value, list) else []):
        if not isinstance(raw, Mapping):
            continue
        classification = str(raw.get("result_classification") or "")
        if classification not in _SEMANTIC_SETTLE_PROBE_CLASSIFICATIONS:
            continue
        try:
            slot_index = int(raw.get("slot_index") or 0)
            started_at = max(
                0.0, float(raw.get("observation_started_at") or 0.0),
            )
            completed_at = max(
                0.0, float(raw.get("observation_completed_at") or 0.0),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not 1 <= slot_index <= max_checks
            or slot_index in seen_slots
            or not started_at
        ):
            continue
        probe_invoked = raw.get("probe_invoked") is True
        if (
            classification in {
                "completed_match", "completed_nonmatch",
                "completed_inconclusive",
            }
            and (not probe_invoked or not completed_at)
        ):
            classification = "probe_failed"
            completed_at = 0.0
        evidence_ref = str(raw.get("evidence_ref") or "")
        if evidence_ref and not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_ref):
            evidence_ref = ""
        exact_match = (
            True
            if classification == "completed_match" else
            False
            if classification == "completed_nonmatch" else
            None
        )
        receipts.append({
            "schema_version": 1,
            "checkpoint_id": _safe_primitive_text(raw.get("checkpoint_id")),
            "semantic_attempt_id": _safe_primitive_text(
                raw.get("semantic_attempt_id")
            ),
            "semantic_substep_id": _safe_primitive_text(
                raw.get("semantic_substep_id")
            ),
            "slot_index": slot_index,
            "observer_mechanism": _safe_reason(raw.get("observer_mechanism")),
            "probe_invoked": probe_invoked,
            "observation_started_at": started_at,
            "observation_completed_at": completed_at,
            "target_application": _safe_primitive_text(
                raw.get("target_application")
            ),
            "surface_ref": _safe_primitive_text(raw.get("surface_ref")),
            "native_window_id": _safe_primitive_text(
                raw.get("native_window_id")
            ),
            "window_generation": _safe_primitive_text(
                raw.get("window_generation")
            ),
            "result_classification": classification,
            "exact_postcondition_match": exact_match,
            "reason_code": _safe_reason(raw.get("reason_code")),
            "evidence_ref": evidence_ref,
        })
        seen_slots.add(slot_index)
        if len(receipts) >= max_checks:
            break
    receipts.sort(key=lambda item: int(item["slot_index"]))
    return receipts


def _safe_semantic_settle(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    state = str(source.get("state") or "inactive")
    if state not in _SEMANTIC_SETTLE_STATES:
        state = "aborted"
    try:
        max_checks = max(1, min(
            32,
            int(
                source.get("max_local_checks")
                or _LEGACY_SEMANTIC_SETTLE_CHECKS
            ),
        ))
    except (TypeError, ValueError, OverflowError):
        max_checks = _LEGACY_SEMANTIC_SETTLE_CHECKS
    probe_delays = []
    for raw_delay in source.get("probe_delays_seconds") or ():
        try:
            delay = max(0.0, min(120.0, float(raw_delay)))
        except (TypeError, ValueError, OverflowError):
            continue
        probe_delays.append(delay)
        if len(probe_delays) >= max_checks:
            break
    if not probe_delays:
        probe_delays = [
            _LEGACY_SEMANTIC_SETTLE_INTERVAL_SECONDS
        ] * max_checks
    elif len(probe_delays) < max_checks:
        probe_delays.extend([probe_delays[-1]] * (max_checks - len(probe_delays)))
    receipts = _safe_semantic_settle_receipts(
        source.get("observation_receipts"), max_checks=max_checks,
    )
    try:
        slot_count = max(0, min(
            max_checks,
            int(
                source.get("settle_slot_count")
                if source.get("settle_slot_count") is not None
                else source.get("local_check_count") or 0
            ),
        ))
        probe_attempt_count = max(0, min(
            slot_count,
            int(source.get("probe_attempt_count") or 0),
        ))
        completed_observation_count = max(0, min(
            probe_attempt_count,
            int(source.get("completed_observation_count") or 0),
        ))
        provider_checks = max(0, int(source.get("provider_check_count") or 0))
        started_at = max(0.0, float(source.get("started_at") or 0.0))
    except (TypeError, ValueError, OverflowError):
        state = "aborted"
        slot_count = 0
        probe_attempt_count = 0
        completed_observation_count = 0
        provider_checks = 0
        started_at = 0.0
    if receipts:
        slot_count = max(
            slot_count,
            max(int(item["slot_index"]) for item in receipts),
        )
        probe_attempt_count = max(
            probe_attempt_count,
            sum(item["probe_invoked"] is True for item in receipts),
        )
        completed_observation_count = max(
            completed_observation_count,
            sum(
                item["result_classification"] in {
                    "completed_match", "completed_nonmatch",
                    "completed_inconclusive",
                }
                for item in receipts
            ),
        )
    check_times = []
    for item in source.get("local_check_times") or ():
        try:
            timestamp = max(0.0, float(item))
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp:
            check_times.append(timestamp)
        if len(check_times) >= max_checks:
            break
    return {
        "schema_version": 2,
        "checkpoint_id": _safe_primitive_text(source.get("checkpoint_id")),
        "state": state,
        "max_local_checks": max_checks,
        "interval_seconds": probe_delays[0],
        "probe_delays_seconds": probe_delays,
        "settle_slot_count": slot_count,
        "probe_attempt_count": probe_attempt_count,
        "completed_observation_count": completed_observation_count,
        # Compatibility aliases.  They describe bounded sleep slots, not proof
        # that a valid observation completed; use the typed counters above.
        "local_check_count": slot_count,
        "provider_check_count": provider_checks,
        "started_at": started_at,
        "local_check_times": check_times,
        "observation_receipts": receipts,
    }


def _safe_semantic_batch_record(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    state = str(source.get("state") or "inactive")
    if state not in {
        "inactive", "prepared", "dispatching", "checkpoint_pending",
        "verified", "stopped", "resume_ready", "outcome_unknown",
    }:
        state = "outcome_unknown"
    primitive_ids = [
        safe for item in (source.get("primitive_ids") or [])[:32]
        if (safe := _safe_primitive_text(item))
    ]
    partial_batch_state = str(source.get("partial_batch_state") or "")
    if partial_batch_state not in {
        "", "completed_batch", "partial_prefix_dispatched",
        "ambiguous_in_batch", "failed_before_any_effect",
    }:
        partial_batch_state = "ambiguous_in_batch"
    try:
        checkpoint_count = max(0, min(1, int(
            source.get("semantic_checkpoint_count") or 0
        )))
        dispatched_count = max(0, min(
            len(primitive_ids), int(source.get("dispatched_count") or 0),
        ))
        completed_count = max(0, min(
            dispatched_count, int(source.get("completed_dispatch_count") or 0),
        ))
        resume_from = source.get("resume_from_index")
        resume_from = (
            None if resume_from is None else
            max(0, min(len(primitive_ids), int(resume_from)))
        )
        rejected_index = source.get("rejected_primitive_index")
        rejected_index = (
            None if rejected_index is None or not primitive_ids else
            max(0, min(len(primitive_ids) - 1, int(rejected_index)))
        )
        checkpoint_prefix_length = max(0, min(
            len(primitive_ids), int(source.get("checkpoint_prefix_length") or 0),
        ))
    except (TypeError, ValueError, OverflowError):
        state = "outcome_unknown"
        checkpoint_count = 0
        dispatched_count = 0
        completed_count = 0
        resume_from = None
        rejected_index = None
        checkpoint_prefix_length = 0
    return {
        "schema_version": 1,
        "semantic_batch_id": _safe_primitive_text(
            source.get("semantic_batch_id")
        ),
        "state": state,
        "partial_batch_state": partial_batch_state,
        "semantic_checkpoint_count": checkpoint_count,
        "primitive_ids": primitive_ids,
        "dispatched_count": dispatched_count,
        "completed_dispatch_count": completed_count,
        "stop_reason": _safe_reason(source.get("stop_reason")),
        "resume_from_index": resume_from,
        "rejected_primitive_index": rejected_index,
        "rejected_primitive_id": _safe_primitive_text(
            source.get("rejected_primitive_id")
        ),
        "rejected_receipt_id": _safe_primitive_text(
            source.get("rejected_receipt_id")
        ),
        "rejection_reason": _safe_reason(source.get("rejection_reason")),
        "checkpoint_id": _safe_primitive_text(source.get("checkpoint_id")),
        "checkpoint_prefix_length": checkpoint_prefix_length,
        "checkpoint_postcondition": _safe_primitive_text(
            source.get("checkpoint_postcondition")
        ),
    }


def _semantic_settle_active(record: Mapping[str, Any]) -> bool:
    settle = _safe_semantic_settle(record.get("semantic_settle"))
    return bool(
        settle["state"] == "active"
        and settle["checkpoint_id"]
        and settle["settle_slot_count"] < settle["max_local_checks"]
    )


def _semantic_settle_configuration(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    truth = record.get("action_truth")
    truth = truth if isinstance(truth, Mapping) else {}
    policy = truth.get("settle_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    try:
        max_checks = max(1, min(
            32,
            int(policy.get("max_probes") or _LEGACY_SEMANTIC_SETTLE_CHECKS),
        ))
    except (TypeError, ValueError, OverflowError):
        max_checks = _LEGACY_SEMANTIC_SETTLE_CHECKS
    delays = policy.get("probe_delays_seconds")
    return {
        "max_local_checks": max_checks,
        "probe_delays_seconds": (
            list(delays)[:max_checks]
            if isinstance(delays, (list, tuple)) else []
        ),
    }


def _semantic_dispatch_started(record: Mapping[str, Any]) -> bool:
    return any(
        item.get("effect_role") == "semantic"
        and item.get("lifecycle") in {
            "dispatch_started", "dispatch_returned", "receipt_bound",
            "verified", "unknown",
        }
        for item in _safe_primitive_journal(record.get("primitive_journal"))
    )


def _any_primitive_dispatch_started(record: Mapping[str, Any]) -> bool:
    return any(
        item.get("lifecycle") in {
            "dispatch_started", "dispatch_returned", "receipt_bound",
            "verified", "unknown",
        }
        for item in _safe_primitive_journal(record.get("primitive_journal"))
    )


def _exact_correlated_primitive_no_effect(record: Mapping[str, Any]) -> bool:
    """Return True only for one fully correlated exact no-dispatch proof."""

    attempt_id = str(record.get("tool_attempt_id") or "")
    execution_epoch = str(record.get("execution_epoch") or "")
    truth = normalize_action_truth(
        record.get("action_truth"), legacy_record=record,
    )
    action_id = str(truth.get("action_id") or "")
    semantic_effect_id = str(truth.get("semantic_effect_id") or "")
    if not all((attempt_id, execution_epoch, action_id, semantic_effect_id)):
        return False
    if (
        truth.get("current_state") != ActionState.NOT_APPLIED_VERIFIED.value
        or truth.get("effect_truth") != "not_applied"
        or float(truth.get("dispatch_started_at") or 0.0) != 0.0
        or float(truth.get("dispatch_returned_at") or 0.0) != 0.0
        or truth.get("current_reason")
        != "action_primitive_not_applied_verified"
        or str(truth.get("operation") or "")
        != str(record.get("tool_name") or "")
    ):
        return False

    provider_references = record.get("provider_references")
    provider_references = (
        provider_references if isinstance(provider_references, Mapping) else {}
    )
    provider_operation_ref = next((
        provider_references.get(key)
        for key in _PROVIDER_OPERATION_REFERENCE_PRIORITY
        if provider_references.get(key)
    ), "")
    if provider_operation_ref and (
        str(truth.get("provider_operation_ref") or "")
        != str(provider_operation_ref)
    ):
        return False

    journal = _safe_primitive_journal(record.get("primitive_journal"))
    if (
        not journal
        or _any_primitive_dispatch_started(record)
        or any(
            item.get("tool_attempt_id") != attempt_id
            or item.get("lifecycle") not in {"prepared", "not_applied"}
            for item in journal
        )
        or not any(
            item.get("lifecycle") == "not_applied"
            and item.get("effect_classification") == "not_applied"
            for item in journal
        )
    ):
        return False

    semantic_node_id = str(truth.get("semantic_node_id") or "")
    if semantic_node_id and any(
        item.get("semantic_node_id")
        and str(item.get("semantic_node_id")) != semantic_node_id
        for item in journal
    ):
        return False
    if not any(
        item.get("exact") is True
        and item.get("reason_code")
        == "action_primitive_not_applied_verified"
        for item in truth.get("evidence") or []
        if isinstance(item, Mapping)
    ):
        return False
    return any(
        isinstance(event, Mapping)
        and event.get("attempt_id") == attempt_id
        and event.get("execution_epoch") == execution_epoch
        and event.get("action_id") == action_id
        and event.get("semantic_effect_id") == semantic_effect_id
        and event.get("state") == ActionState.NOT_APPLIED_VERIFIED.value
        and event.get("effect_truth") == "not_applied"
        and event.get("reason_code")
        == "action_primitive_not_applied_verified"
        for event in record.get("action_outbox") or []
    )


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


def _safe_provider_references(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in _PROVIDER_REFERENCE_KEYS:
        text = _safe_primitive_text(value.get(key), maximum=256)
        if text:
            result[key] = text
    return result


def _safe_identity_receipts(value: Any, *, maximum: int = 128) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for raw in value[-maximum:]:
        if not isinstance(raw, Mapping):
            continue
        receipt_id = _safe_primitive_text(raw.get("receipt_id"), maximum=256)
        if not receipt_id or receipt_id in seen:
            continue
        seen.add(receipt_id)
        try:
            received_at = max(0.0, float(raw.get("received_at") or 0.0))
        except (TypeError, ValueError, OverflowError):
            received_at = 0.0
        result.append({
            "receipt_id": receipt_id,
            "classification": _safe_reason(raw.get("classification")),
            "reason_code": _safe_reason(raw.get("reason_code")),
            "failure_domain": _safe_reason(raw.get("failure_domain")),
            "execution_epoch": _safe_primitive_text(
                raw.get("execution_epoch"), maximum=256,
            ),
            "target_generation": _safe_primitive_text(
                raw.get("target_generation"), maximum=256,
            ),
            "stale": raw.get("stale") is True,
            "received_at": received_at,
        })
    return result


def _safe_visual_action_receipt(value: Any) -> dict[str, Any]:
    """Privacy-bounded durable metadata for one canonical visual action."""

    source = value if isinstance(value, Mapping) else {}
    result = {}
    for key in (
        "action_id", "attempt_id", "semantic_effect_id", "source_frame_id",
        "fresh_frame_id", "post_frame_id", "proposal_id", "surface_ref",
        "surface_generation", "window_generation", "native_window_id",
        "application", "dispatch_state",
        "outcome", "reason_code", "reconciliation_status", "actuator",
    ):
        safe = _safe_primitive_text(source.get(key), maximum=512)
        if safe:
            result[key] = safe
    expected = _safe_primitive_text(
        source.get("expected_postcondition"), maximum=800,
    )
    if expected:
        result["expected_postcondition"] = expected
    if "dispatch_count" in source:
        try:
            dispatch_count = int(source.get("dispatch_count") or 0)
        except (TypeError, ValueError, OverflowError):
            dispatch_count = 0
        result["dispatch_count"] = max(0, min(1, dispatch_count))
    frame_dimensions = source.get("frame_dimensions")
    frame_dimensions = (
        frame_dimensions if isinstance(frame_dimensions, Mapping) else {}
    )
    try:
        frame_width = max(0, int(frame_dimensions.get("width") or 0))
        frame_height = max(0, int(frame_dimensions.get("height") or 0))
    except (TypeError, ValueError, OverflowError):
        frame_width, frame_height = 0, 0
    if frame_width and frame_height:
        result["frame_dimensions"] = {
            "width": frame_width,
            "height": frame_height,
        }
    lease = source.get("actuation_lease")
    lease = lease if isinstance(lease, Mapping) else {}
    lease_state = _safe_reason(lease.get("state"))
    try:
        acknowledged_at = max(0.0, float(lease.get("acknowledged_at") or 0.0))
        lease_expires_at = max(0.0, float(lease.get("expires_at") or 0.0))
    except (TypeError, ValueError, OverflowError):
        acknowledged_at = 0.0
        lease_expires_at = 0.0
    if lease_state:
        result["actuation_lease"] = {
            "state": lease_state,
            "acknowledged_at": acknowledged_at,
            "expires_at": lease_expires_at,
        }
    normalized_bbox = source.get("normalized_bbox")
    normalized_bbox = (
        normalized_bbox if isinstance(normalized_bbox, Mapping) else {}
    )
    try:
        safe_bbox = {
            key: float(normalized_bbox[key])
            for key in ("left", "top", "right", "bottom")
        }
        if not all(math.isfinite(value) for value in safe_bbox.values()):
            safe_bbox = {}
        elif not (
            0.0 <= safe_bbox["left"] < safe_bbox["right"] <= 1.0
            and 0.0 <= safe_bbox["top"] < safe_bbox["bottom"] <= 1.0
        ):
            safe_bbox = {}
    except (KeyError, TypeError, ValueError, OverflowError):
        safe_bbox = {}
    if safe_bbox:
        result["normalized_bbox"] = safe_bbox
    transform = source.get("coordinate_transform")
    transform = transform if isinstance(transform, Mapping) else {}

    def _number_list(name: str, maximum: int) -> list[float]:
        values = transform.get(name)
        if not isinstance(values, (list, tuple)):
            return []
        safe_values = []
        for item in list(values)[:maximum]:
            try:
                number = float(item)
            except (TypeError, ValueError, OverflowError):
                return []
            if not math.isfinite(number):
                return []
            safe_values.append(number)
        return safe_values

    dimensions = transform.get("source_dimensions")
    dimensions = dimensions if isinstance(dimensions, Mapping) else {}
    try:
        width = max(0, int(dimensions.get("width") or 0))
        height = max(0, int(dimensions.get("height") or 0))
    except (TypeError, ValueError, OverflowError):
        width, height = 0, 0
    safe_transform = {
        "source_dimensions": {"width": width, "height": height},
        "source_box": _number_list("source_box", 4),
        "screen_bounds": _number_list("screen_bounds", 4),
        "client_origin_screen": _number_list("client_origin_screen", 2),
        "source_to_screen_affine": _number_list(
            "source_to_screen_affine", 6,
        ),
        "mapping_chain": _safe_primitive_text(
            transform.get("mapping_chain"), maximum=120,
        ),
    }
    if safe_transform["source_box"] and safe_transform[
        "source_to_screen_affine"
    ]:
        result["coordinate_transform"] = safe_transform
    return result


def _action_outbox_event(record: Mapping[str, Any]) -> dict | None:
    truth = record.get("action_truth")
    if not isinstance(truth, Mapping) or not truth.get("action_id"):
        return None
    timeline = truth.get("timeline")
    latest = timeline[-1] if isinstance(timeline, list) and timeline else {}
    if not isinstance(latest, Mapping):
        latest = {}
    material = "\0".join((
        str(record.get("tool_attempt_id") or ""),
        str(truth.get("action_id") or ""),
        str(latest.get("state") or truth.get("current_state") or ""),
        str(latest.get("reason_code") or truth.get("current_reason") or ""),
        str(latest.get("occurred_at") or record.get("updated_at") or ""),
        str(len(timeline) if isinstance(timeline, list) else 0),
    ))
    event_id = "action_event_" + hashlib.sha256(
        material.encode("utf-8", errors="replace")
    ).hexdigest()[:40]
    try:
        created_at = max(
            0.0,
            float(latest.get("occurred_at") or record.get("updated_at") or 0.0),
        )
    except (TypeError, ValueError, OverflowError):
        created_at = 0.0
    return {
        "event_id": event_id,
        "action_id": str(truth.get("action_id") or ""),
        "semantic_effect_id": str(truth.get("semantic_effect_id") or ""),
        "attempt_id": str(record.get("tool_attempt_id") or ""),
        "execution_epoch": str(record.get("execution_epoch") or ""),
        "state": str(truth.get("current_state") or ""),
        "effect_truth": str(truth.get("effect_truth") or ""),
        "effect_class": str(truth.get("effect_class") or ""),
        "reconciliation_status": str(
            truth.get("reconciliation_status") or ""
        ),
        "reason_code": str(truth.get("current_reason") or "")[:96],
        "created_at": created_at,
    }


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
        self._lock_state = threading.local()
        self._publish_transport_events = root is None

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
            self._lock_state.depth = int(
                getattr(self._lock_state, "depth", 0)
            ) + 1
            try:
                yield
            finally:
                depth = int(getattr(self._lock_state, "depth", 1)) - 1
                if depth > 0:
                    self._lock_state.depth = depth
                else:
                    try:
                        del self._lock_state.depth
                    except AttributeError:
                        pass
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _diagnostic_file_state(path: Path, *, validate_json: bool) -> dict:
        state: dict[str, Any] = {
            "exists": False,
            "size": None,
            "mode": None,
            "file_attributes": None,
            "readable": False,
            "json_valid": None,
            "open_errno": None,
            "open_winerror": None,
        }
        try:
            stat_result = path.stat()
            state.update({
                "exists": True,
                "size": int(stat_result.st_size),
                "mode": int(stat_result.st_mode),
                "file_attributes": getattr(
                    stat_result, "st_file_attributes", None,
                ),
            })
            with path.open("rb") as handle:
                if validate_json:
                    json.load(handle)
                else:
                    handle.read(1)
            state["readable"] = True
            if validate_json:
                state["json_valid"] = True
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            state["open_errno"] = getattr(exc, "errno", None)
            state["open_winerror"] = getattr(exc, "winerror", None)
            if validate_json:
                state["json_valid"] = False
        return state

    def _replace_failure_diagnostic(
        self,
        *,
        operation_id: str,
        path: Path,
        tmp: Path,
        error: OSError,
        closed_at: float,
    ) -> dict:
        normalized_path = os.path.normcase(str(path.resolve(strict=False)))
        lock_path = self.root / ".store.lock"
        lock_owned = getattr(self._lock, "_is_owned", None)
        lock_depth = int(getattr(self._lock_state, "depth", 0))
        path_kind = (
            "attempts" if path.parent == self.attempts_dir else
            "barriers" if path.parent == self.barriers_dir else
            "store"
        )
        return {
            "operation_id": operation_id,
            "path_hash": hashlib.sha256(
                normalized_path.encode("utf-8", errors="replace")
            ).hexdigest(),
            "path_kind": path_kind,
            "timestamp_unix": time.time(),
            "elapsed_since_close_ms": round(
                max(0.0, time.perf_counter() - closed_at) * 1000.0, 3,
            ),
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "thread_lock_depth": lock_depth,
            "thread_lock_owned": bool(lock_owned()) if lock_owned else None,
            "interprocess_lock_file_exists": lock_path.exists(),
            "interprocess_lock_held_by_thread": lock_depth > 0,
            "errno": getattr(error, "errno", None),
            "winerror": getattr(error, "winerror", None),
            "temporary": self._diagnostic_file_state(
                tmp, validate_json=True,
            ),
            "destination": self._diagnostic_file_state(
                path, validate_json=False,
            ),
            "cleanup_policy": "delete_temporary_in_finally",
        }

    @staticmethod
    def _prepare_action_outbox(value: Mapping[str, Any]) -> None:
        if not isinstance(value, dict) or not value.get("tool_attempt_id"):
            return
        status = str(value.get("status") or "")
        if status in {
            ToolAttemptStatus.CANCELLATION_REQUESTED.value,
            ToolAttemptStatus.CANCELLED_PROVEN.value,
        }:
            value["authority_state"] = "cancelled"
            value["tombstoned_at"] = value.get("tombstoned_at") or _now()
        elif status in {item.value for item in TERMINAL_STATUSES}:
            if value.get("authority_state") not in {"cancelled", "superseded"}:
                value["authority_state"] = "terminal"
            value["tombstoned_at"] = value.get("tombstoned_at") or _now()
        event = _action_outbox_event(value)
        if event is None:
            return
        existing = value.get("action_outbox")
        outbox = [
            dict(item) for item in existing[-128:]
            if isinstance(item, Mapping) and item.get("event_id")
        ] if isinstance(existing, list) else []
        if not any(item.get("event_id") == event["event_id"] for item in outbox):
            outbox.append(event)
        value["action_outbox"] = outbox[-128:]

    def _publish_action_outbox(self, value: Mapping[str, Any]) -> None:
        if not self._publish_transport_events:
            return
        from osia.durable_transport import get_durable_transport_store

        transport = get_durable_transport_store()
        attempt_id = str(value.get("tool_attempt_id") or "")
        conversation_id = str(value.get("conversation_id") or "")
        turn_id = str(value.get("turn_id") or "")
        request_id = str(value.get("request_id") or "") or (
            "request_" + hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]
        )
        run_id = str(value.get("autonomous_run_id") or turn_id)
        if not all((attempt_id, conversation_id, turn_id, request_id, run_id)):
            return
        for event in value.get("action_outbox") or []:
            if not isinstance(event, Mapping) or not event.get("event_id"):
                continue
            state = str(event.get("state") or "")
            failure_domain = ""
            if state == ActionState.OBSERVER_FAILED.value:
                failure_domain = "observer_failure"
            elif state == ActionState.TARGET_STALE.value:
                failure_domain = "stale_target"
            elif str(event.get("effect_truth") or "") in {"possible", "observed"}:
                failure_domain = "semantic_effect_uncertainty"
            elif state in {
                ActionState.PRECONDITION_FAILED.value,
                ActionState.FAILED_BEFORE_EFFECT.value,
            }:
                failure_domain = "semantic_precondition_failure"
            transport.append_event(
                conversation_id=conversation_id,
                request_id=request_id,
                turn_id=turn_id,
                run_id=run_id,
                event_type="action_truth_changed",
                payload={
                    "action_state": state,
                    "effect_truth": str(event.get("effect_truth") or ""),
                    "effect_class": str(event.get("effect_class") or ""),
                    "reconciliation_status": str(
                        event.get("reconciliation_status") or ""
                    ),
                    "reason_code": str(event.get("reason_code") or ""),
                    "failure_domain": failure_domain,
                    "action_execution_epoch": str(
                        event.get("execution_epoch") or ""
                    ),
                },
                action_id=str(event.get("action_id") or ""),
                attempt_id=attempt_id,
                semantic_effect_id=str(event.get("semantic_effect_id") or ""),
                idempotency_key=str(event.get("event_id") or ""),
                state_updates={
                    "action_state": state,
                    "effect_truth": str(event.get("effect_truth") or ""),
                },
            )

    def _write_atomic(self, path: Path, value: Mapping[str, Any]) -> None:
        self._prepare_action_outbox(value)
        operation_id = "atomic_replace_" + uuid.uuid4().hex
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            closed_at = time.perf_counter()
            try:
                os.replace(tmp, path)
            except OSError as exc:
                try:
                    diagnostic = self._replace_failure_diagnostic(
                        operation_id=operation_id,
                        path=path,
                        tmp=tmp,
                        error=exc,
                        closed_at=closed_at,
                    )
                except Exception as diagnostic_error:
                    # Diagnostic collection must never mask the durable write
                    # failure it is meant to classify.
                    diagnostic = {
                        "operation_id": operation_id,
                        "timestamp_unix": time.time(),
                        "pid": os.getpid(),
                        "thread_id": threading.get_ident(),
                        "errno": getattr(exc, "errno", None),
                        "winerror": getattr(exc, "winerror", None),
                        "diagnostic_error": type(diagnostic_error).__name__,
                    }
                try:
                    setattr(exc, "osia_durable_diagnostic", diagnostic)
                except (AttributeError, TypeError):
                    pass
                try:
                    print(
                        "[OSIA Durable Store] atomic_replace_failure "
                        + json.dumps(diagnostic, sort_keys=True),
                        file=sys.stderr,
                    )
                except Exception:
                    pass
                raise
            if os.name != "nt":
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            try:
                self._publish_action_outbox(value)
            except Exception:
                # The embedded outbox is already durable in the canonical
                # attempt file; startup/live drains can publish it later.
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
        record.setdefault("continuation_claimed_process_identity", None)
        continuation_identity = record.get(
            "continuation_claimed_process_identity"
        )
        if continuation_identity is not None and (
            not isinstance(continuation_identity, str)
            or len(continuation_identity) > 192
            or any(ord(char) < 32 for char in continuation_identity)
        ):
            raise CorruptAttemptError("invalid_continuation_process_identity")
        record.setdefault("reconciliation_claimed_pid", None)
        record.setdefault("owner_dispatch_boundary_armed_at", None)
        record.setdefault("owner_dispatch_claimed_at", None)
        record.setdefault(
            "owner_dispatch_timed_out_after_claim",
            bool(
                record.get("owner_dispatch_claimed_at")
                and record.get("dispatch_state") == "worker_may_be_active"
            ),
        )
        if type(record["owner_dispatch_timed_out_after_claim"]) is not bool:
            raise CorruptAttemptError("invalid_owner_dispatch_timeout_state")
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
        record["primitive_journal"] = _safe_primitive_journal(
            record.get("primitive_journal")
        )
        record["semantic_settle"] = _safe_semantic_settle(
            record.get("semantic_settle")
        )
        record["semantic_batch"] = _safe_semantic_batch_record(
            record.get("semantic_batch")
        )
        record["action_truth"] = normalize_action_truth(
            record.get("action_truth"), legacy_record=record,
        )
        if record.get("process_facts") is not None:
            from osia.tool_contracts import normalize_process_facts

            process_facts = normalize_process_facts(record["process_facts"])
            if process_facts is None:
                raise CorruptAttemptError("invalid_process_facts")
            record["process_facts"] = process_facts
        execution_epoch = _safe_primitive_text(
            record.get("execution_epoch"), maximum=256,
        )
        if not execution_epoch:
            execution_epoch = "attempt_epoch_legacy_" + hashlib.sha256(
                str(record.get("tool_attempt_id") or "legacy").encode("utf-8")
            ).hexdigest()[:32]
        record["execution_epoch"] = execution_epoch
        authority_state = str(record.get("authority_state") or "")
        if status in {
            ToolAttemptStatus.CANCELLATION_REQUESTED,
            ToolAttemptStatus.CANCELLED_PROVEN,
        }:
            authority_state = "cancelled"
        elif status in TERMINAL_STATUSES and authority_state not in {
            "cancelled", "superseded",
        }:
            authority_state = "terminal"
        elif authority_state not in _ATTEMPT_AUTHORITY_STATES:
            authority_state = "active"
        record["authority_state"] = authority_state
        try:
            tombstoned_at = float(record.get("tombstoned_at") or 0.0)
        except (TypeError, ValueError, OverflowError):
            tombstoned_at = 0.0
        record["tombstoned_at"] = tombstoned_at or None
        refs = _safe_provider_references(record.get("provider_references"))
        if not refs and record.get("tool_call_id"):
            refs["tool_call_id"] = str(record["tool_call_id"])
        record["provider_references"] = refs
        callbacks = record.get("processed_provider_callbacks")
        record["processed_provider_callbacks"] = [
            item for raw in (callbacks if isinstance(callbacks, list) else [])[-128:]
            if (item := _safe_primitive_text(raw, maximum=256))
        ]
        record["observer_receipts"] = _safe_identity_receipts(
            record.get("observer_receipts")
        )
        record["visual_action_receipt"] = _safe_visual_action_receipt(
            record.get("visual_action_receipt")
        )
        history = record.get("continuation_history")
        record["continuation_history"] = _safe_identity_receipts(
            history, maximum=64,
        )
        outbox = record.get("action_outbox")
        record["action_outbox"] = [
            dict(item) for item in (outbox if isinstance(outbox, list) else [])[-128:]
            if isinstance(item, Mapping)
            and _safe_primitive_text(item.get("event_id"), maximum=256)
        ]
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
                # Legacy journals can await proof after the worker has settled.
                # They remain observable, but do not own a new dispatch.
                and record.get("dispatch_state") != "owner_settled_unverified"
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
        primitive_binding: Mapping[str, Any] | None = None,
        action_binding: Mapping[str, Any] | None = None,
        provider_references: Mapping[str, Any] | None = None,
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
        action_source = (
            dict(action_binding) if isinstance(action_binding, Mapping) else {}
        )
        idempotency_identity = str(
            action_source.get("idempotency_identity")
            or remote_operation_key
            or ""
        )
        effect_class = canonical_effect_class(
            side_effect,
            idempotence,
            idempotency_identity=idempotency_identity,
        )
        action_target = action_source.get("target")
        action_target = (
            dict(action_target) if isinstance(action_target, Mapping) else {}
        )
        semantic_effect_id = str(
            action_source.get("semantic_effect_id") or ""
        ) or semantic_effect_identity(
            turn_id=turn_id,
            semantic_node_id=str(action_source.get("semantic_node_id") or ""),
            attempted_effect=str(action_source.get("attempted_effect") or ""),
            operation=str(action_source.get("operation") or tool_name),
            target=action_target,
            equivalence_fingerprint=equivalence_fingerprint,
            argument_fingerprint=argument_fingerprint,
        )
        durable_action_id = str(action_source.get("action_id") or "") or action_identity(
            run_id=autonomous_run_id or turn_id,
            turn_id=turn_id,
            semantic_effect_id=semantic_effect_id,
        )
        action_truth = new_action_truth(
            action_id=durable_action_id,
            semantic_effect_id=semantic_effect_id,
            effect_class=effect_class,
            operation=str(action_source.get("operation") or tool_name),
            target=action_target,
            goal_contract_ref=str(action_source.get("goal_contract_ref") or ""),
            semantic_node_id=str(action_source.get("semantic_node_id") or ""),
            attempted_effect=str(action_source.get("attempted_effect") or ""),
            idempotency_identity=idempotency_identity,
            dependencies=(action_source.get("dependencies") or ()),
            conflict_scope=str(action_source.get("conflict_scope") or ""),
            expected_postcondition_ref=str(
                action_source.get("expected_postcondition_ref") or ""
            ),
            effect_role=str(action_source.get("effect_role") or "semantic"),
            settle_policy=settle_policy_for(
                operation=str(action_source.get("operation") or tool_name),
                family=str(persistence.get("family") or "generic"),
                effect_class=effect_class,
                effect_role=str(
                    action_source.get("effect_role") or "semantic"
                ),
            ),
            occurred_at=now,
        )
        safe_provider_references = _safe_provider_references(
            provider_references
        )
        provider_operation_ref = next((
            safe_provider_references.get(key)
            for key in _PROVIDER_OPERATION_REFERENCE_PRIORITY
            if safe_provider_references.get(key)
        ), "")
        if provider_operation_ref:
            action_truth["provider_operation_ref"] = provider_operation_ref
        primitive_journal = []
        semantic_batch_record = _safe_semantic_batch_record(None)
        if isinstance(primitive_binding, Mapping):
            supplied_batch = primitive_binding.get("batch_primitives")
            bindings = (
                list(supplied_batch)[:32]
                if isinstance(supplied_batch, list) else [primitive_binding]
            )
            batch_id = _safe_primitive_text(
                primitive_binding.get("semantic_batch_id")
            )
            prepared_entries = []
            for index, binding in enumerate(bindings):
                if not isinstance(binding, Mapping):
                    continue
                effect_role = (
                    "preparatory"
                    if binding.get("effect_role") == "preparatory"
                    else "semantic"
                )
                material = "|".join((
                    attempt_id,
                    str(binding.get("strategy_attempt_id") or ""),
                    str(binding.get("semantic_substep_id") or ""),
                    str(binding.get("surface_ref") or ""),
                    str(index),
                ))
                primitive_id = "primitive_" + hashlib.sha256(
                    material.encode("utf-8", errors="replace")
                ).hexdigest()[:20]
                primitive_target = {
                    "kind": "desktop",
                    "application": str(
                        binding.get("application_identity") or ""
                    ),
                    "surface_ref": str(binding.get("surface_ref") or ""),
                    "native_window_id": str(
                        binding.get("native_window_id") or ""
                    ),
                    "window_generation": str(
                        binding.get("window_generation") or ""
                    ),
                    "conflict_scope": str(
                        binding.get("surface_ref")
                        or binding.get("application_identity")
                        or ""
                    ),
                }
                primitive_effect_id = semantic_effect_identity(
                    turn_id=turn_id,
                    semantic_node_id=str(
                        binding.get("semantic_substep_id")
                        or binding.get("semantic_node_id")
                        or ""
                    ),
                    attempted_effect=str(
                        binding.get("attempted_effect") or ""
                    ),
                    operation=str(binding.get("uia_pattern") or tool_name),
                    target=primitive_target,
                    argument_fingerprint=argument_fingerprint,
                )
                prepared_entries.append({
                    **dict(binding),
                    "primitive_id": primitive_id,
                    "semantic_effect_id": primitive_effect_id,
                    "action_id": action_identity(
                        run_id=autonomous_run_id or turn_id,
                        turn_id=turn_id,
                        semantic_effect_id=primitive_effect_id,
                    ),
                    "effect_role": effect_role,
                    "checkpoint_role": (
                        "intermediate"
                        if index < len(bindings) - 1 else "final"
                    ),
                    "semantic_batch_id": batch_id,
                    "batch_index": index,
                    "batch_size": len(bindings),
                    "tool_attempt_id": attempt_id,
                    "run_id": autonomous_run_id,
                    "task_id": autonomous_task_id,
                    "operation_id": autonomous_operation_id,
                    "lifecycle": "prepared",
                    "effect_classification": "impossible",
                    "prepared_at": now,
                })
            primitive_journal = _safe_primitive_journal(prepared_entries)
            if batch_id and len(primitive_journal) >= 2:
                semantic_batch_record = _safe_semantic_batch_record({
                    "semantic_batch_id": batch_id,
                    "state": "prepared",
                    "semantic_checkpoint_count": 1,
                    "primitive_ids": [
                        item["primitive_id"] for item in primitive_journal
                    ],
                })
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
            "owner_dispatch_boundary_armed_at": None,
            "owner_dispatch_claimed_at": None,
            "owner_dispatch_timed_out_after_claim": False,
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
            "continuation_claimed_process_identity": None,
            "reconciliation_claim": "",
            "reconciliation_claimed_at": None,
            "reconciliation_claimed_pid": None,
            "owner_scope": owner_scope,
            "autonomous_run_id": str(autonomous_run_id or ""),
            "autonomous_task_id": str(autonomous_task_id or ""),
            "autonomous_operation_id": str(autonomous_operation_id or ""),
            "operation_revision": operation_revision,
            "permission_epoch": permission_epoch,
            "execution_epoch": "attempt_epoch_" + secrets.token_urlsafe(24),
            "authority_state": "active",
            "tombstoned_at": None,
            "provider_references": {
                **safe_provider_references,
                "tool_call_id": tool_call_id,
                **({"idempotency_key": str(remote_operation_key)}
                   if remote_operation_key else {}),
            },
            "processed_provider_callbacks": [],
            "observer_receipts": [],
            "visual_action_receipt": {},
            "continuation_history": [],
            "action_outbox": [],
            "primitive_journal": primitive_journal,
            "semantic_settle": _safe_semantic_settle(None),
            "semantic_batch": semantic_batch_record,
            "action_truth": action_truth,
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
            if (
                status is ToolAttemptStatus.PENDING
                and record.get("owner_dispatch_claimed_at")
                and record.get("dispatch_state") == "worker_may_be_active"
            ):
                record["owner_dispatch_timed_out_after_claim"] = True
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def mark_accepted(self, attempt_id: str, *, expected_revision: int | None = None) -> dict:
        return self._transition(
            attempt_id, ToolAttemptStatus.ACCEPTED,
            expected_revision=expected_revision,
            updates={"accepted_at": _now()},
        )

    def arm_owner_dispatch_boundary(self, attempt_id: str) -> dict:
        """Bind an attempt to the guarded owner-dispatch handshake."""

        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            if current in TERMINAL_STATUSES:
                return dict(record)
            if current not in {
                ToolAttemptStatus.ACCEPTED,
                ToolAttemptStatus.RUNNING,
            }:
                raise IllegalTransition(
                    f"owner_dispatch_boundary_not_armable:{current.value}"
                )
            if record.get("owner_dispatch_boundary_armed_at"):
                return dict(record)
            now = _now()
            record.update({
                "owner_dispatch_boundary_armed_at": now,
                "updated_at": now,
                "revision": int(record["revision"]) + 1,
            })
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

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
                # RUNNING means the tool owner/wrapper started.  Semantic OS
                # dispatch is recorded only by the primitive journal below.
                "dispatch_state": "owner_started",
                "updated_at": now,
                "revision": int(record["revision"]) + 1,
            })
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def claim_owner_dispatch(
        self,
        attempt_id: str,
        *,
        owner_settled: bool = False,
        allow_pending_owner: bool = False,
    ) -> tuple[dict, bool]:
        """Atomically choose cancellation or the external-call boundary.

        ``RUNNING`` records initially mean only that the local owner/wrapper is
        active.  This claim is made at the last boundary before the external
        callable.  If cancellation already won while no primitive dispatch is
        possible, it is durably proven under the same lock.  Otherwise the
        boundary records ``dispatch_started`` before allowing the caller to
        continue, so a later cancellation can never be misreported as a proven
        pre-dispatch stop.
        """

        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            pending_owner_deadline = min(
                float(record.get("hard_deadline_at") or 0.0),
                float(
                    record.get("inactivity_deadline_at")
                    or record.get("hard_deadline_at")
                    or 0.0
                ),
            )
            pending_owner_active = bool(
                allow_pending_owner
                and current is ToolAttemptStatus.PENDING
                and record.get("dispatch_state") == "worker_may_be_active"
                and pending_owner_deadline > _now()
            )
            if current is ToolAttemptStatus.CANCELLED_PROVEN:
                return dict(record), False
            if current is ToolAttemptStatus.CANCELLATION_REQUESTED:
                dispatch_impossible = bool(
                    record.get("owner_dispatch_boundary_armed_at")
                    and not record.get("owner_dispatch_claimed_at")
                    and not _any_primitive_dispatch_started(record)
                    and (
                        record.get("dispatch_state") in {
                            "not_dispatched", "owner_started",
                        }
                        or (
                            allow_pending_owner
                            and record.get("dispatch_state")
                            == "worker_may_be_active"
                        )
                        or owner_settled
                    )
                )
                if dispatch_impossible:
                    now = _now()
                    record.update({
                        "status": ToolAttemptStatus.CANCELLED_PROVEN.value,
                        "terminal_outcome": "cancelled_proven",
                        "dispatch_state": "cancelled_before_effect",
                        "reason_code": (
                            "cancellation_acknowledged_before_effect"
                        ),
                        "completed_at": now,
                        "next_check_at": None,
                        "reconciliation_claim": "",
                        "reconciliation_claimed_at": None,
                        "reconciliation_claimed_pid": None,
                        "persistence_state": "terminal",
                        "terminal_state": "terminal",
                        "updated_at": now,
                        "revision": int(record["revision"]) + 1,
                    })
                    record["action_truth"] = transition_action_truth(
                        record["action_truth"],
                        state=ActionState.CANCELLED,
                        reason_code="cancellation_acknowledged_before_effect",
                        occurred_at=now,
                    )
                    self._write_atomic(self._path(attempt_id), record)
                return dict(record), False
            if (
                owner_settled
                or not record.get("owner_dispatch_boundary_armed_at")
                or not (
                    (
                        current is ToolAttemptStatus.RUNNING
                        and record.get("dispatch_state") == "owner_started"
                    )
                    or pending_owner_active
                )
                or _any_primitive_dispatch_started(record)
            ):
                return dict(record), False
            now = _now()
            record.update({
                "dispatch_state": "dispatch_started",
                "owner_dispatch_claimed_at": now,
                "updated_at": now,
                "revision": int(record["revision"]) + 1,
            })
            # A primitive-bound Desktop action has a narrower semantic dispatch
            # boundary below.  The owner/wrapper claim must not contaminate its
            # effect truth (for example, a focus/preparation failure).
            if not record.get("primitive_journal"):
                record["action_truth"] = transition_action_truth(
                    record["action_truth"],
                    state=ActionState.DISPATCH_STARTED,
                    reason_code="semantic_dispatch_started",
                    occurred_at=now,
                )
            self._write_atomic(self._path(attempt_id), record)
            return dict(record), True

    @staticmethod
    def _journal_entry_index(
        record: Mapping[str, Any], primitive_id: str = "",
    ) -> int:
        journal = list(record.get("primitive_journal") or [])
        if primitive_id:
            for index in range(len(journal) - 1, -1, -1):
                if journal[index].get("primitive_id") == primitive_id:
                    return index
            return -1
        return len(journal) - 1

    def record_primitive_dispatch_started(
        self,
        attempt_id: str,
        *,
        primitive_id: str = "",
        application_identity: str = "",
        surface_ref: str = "",
        native_window_id: str = "",
        window_generation: str = "",
        runtime_identity: str = "",
        automation_id: str = "",
        uia_pattern: str = "",
    ) -> dict:
        """Atomically witness the narrow semantic OS/UIA call boundary."""

        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return record
            journal = list(record.get("primitive_journal") or [])
            index = self._journal_entry_index(record, primitive_id)
            if index < 0 or journal[index].get("lifecycle") != "prepared":
                raise IllegalTransition("primitive_dispatch_not_prepared")
            entry = dict(journal[index])
            for name, value in (
                ("application_identity", application_identity),
                ("surface_ref", surface_ref),
                ("native_window_id", native_window_id),
                ("window_generation", window_generation),
                ("runtime_identity", runtime_identity),
                ("automation_id", automation_id),
                ("uia_pattern", uia_pattern),
            ):
                safe = _safe_primitive_text(value)
                if safe:
                    entry[name] = safe
            now = _now()
            semantic_effect = entry.get("effect_role") == "semantic"
            tracked_action_effect = bool(
                semantic_effect
                or (
                    entry.get("effect_role") == "preparatory"
                    and (record.get("action_truth") or {}).get("effect_role")
                    == "preparatory"
                )
            )
            entry.update({
                "lifecycle": "dispatch_started",
                "effect_classification": (
                    "effect_possible" if tracked_action_effect else "impossible"
                ),
                "dispatch_started_at": now,
            })
            journal[index] = entry
            record["primitive_journal"] = _safe_primitive_journal(journal)
            batch = _safe_semantic_batch_record(record.get("semantic_batch"))
            if batch["state"] != "inactive" and entry.get(
                "semantic_batch_id"
            ) == batch["semantic_batch_id"]:
                batch.update({
                    "state": "dispatching",
                    "dispatched_count": max(
                        batch["dispatched_count"],
                        int(entry.get("batch_index") or 0) + 1,
                    ),
                })
                record["semantic_batch"] = _safe_semantic_batch_record(batch)
            if (
                entry.get("effect_role") == "semantic"
                and entry.get("checkpoint_role") != "intermediate"
            ):
                record["semantic_settle"] = _safe_semantic_settle({
                    **_semantic_settle_configuration(record),
                    "checkpoint_id": (
                        entry.get("semantic_substep_id")
                        or entry.get("primitive_id")
                    ),
                    "state": "active",
                    "settle_slot_count": 0,
                    "probe_attempt_count": 0,
                    "completed_observation_count": 0,
                    "local_check_count": 0,
                    "provider_check_count": 0,
                    "started_at": now,
                    "local_check_times": [],
                    "observation_receipts": [],
                })
            if tracked_action_effect:
                record["action_truth"] = transition_action_truth(
                    record["action_truth"],
                    state=ActionState.DISPATCH_STARTED,
                    reason_code="action_primitive_dispatch_started",
                    evidence={
                        "evidence_ref": evidence_reference({
                            "primitive_id": entry.get("primitive_id"),
                            "semantic_substep_id": entry.get(
                                "semantic_substep_id"
                            ),
                        }),
                    },
                    occurred_at=now,
                )
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = now
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def record_primitive_dispatch_returned(
        self, attempt_id: str, *, primitive_id: str = "",
    ) -> dict:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return record
            journal = list(record.get("primitive_journal") or [])
            index = self._journal_entry_index(record, primitive_id)
            if index < 0 or journal[index].get("lifecycle") not in {
                "dispatch_started", "dispatch_returned",
            }:
                raise IllegalTransition("primitive_dispatch_not_started")
            entry = dict(journal[index])
            now = _now()
            semantic_effect = entry.get("effect_role") == "semantic"
            tracked_action_effect = bool(
                semantic_effect
                or (
                    entry.get("effect_role") == "preparatory"
                    and (record.get("action_truth") or {}).get("effect_role")
                    == "preparatory"
                )
            )
            entry.update({
                "lifecycle": "dispatch_returned",
                "effect_classification": (
                    "effect_possible" if tracked_action_effect else "impossible"
                ),
                "dispatch_returned_at": now,
            })
            journal[index] = entry
            record["primitive_journal"] = _safe_primitive_journal(journal)
            batch = _safe_semantic_batch_record(record.get("semantic_batch"))
            if batch["state"] != "inactive" and entry.get(
                "semantic_batch_id"
            ) == batch["semantic_batch_id"]:
                batch.update({
                    "completed_dispatch_count": max(
                        batch["completed_dispatch_count"],
                        int(entry.get("batch_index") or 0) + 1,
                    ),
                    "state": (
                        "checkpoint_pending"
                        if entry.get("checkpoint_role") == "final" else
                        "dispatching"
                    ),
                })
                record["semantic_batch"] = _safe_semantic_batch_record(batch)
            if tracked_action_effect:
                record["action_truth"] = transition_action_truth(
                    record["action_truth"],
                    state=ActionState.DISPATCH_RETURNED,
                    reason_code="action_primitive_dispatch_returned",
                    occurred_at=now,
                )
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = now
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def bind_primitive_receipt(
        self,
        attempt_id: str,
        *,
        primitive_id: str = "",
        outcome: str = "success_unverified",
    ) -> dict:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return record
            journal = list(record.get("primitive_journal") or [])
            index = self._journal_entry_index(record, primitive_id)
            if index < 0 or journal[index].get("lifecycle") not in {
                "dispatch_started", "dispatch_returned", "receipt_bound",
            }:
                raise IllegalTransition("primitive_receipt_without_dispatch")
            entry = dict(journal[index])
            now = _now()
            semantic_effect = entry.get("effect_role") == "semantic"
            tracked_action_effect = bool(
                semantic_effect
                or (
                    entry.get("effect_role") == "preparatory"
                    and (record.get("action_truth") or {}).get("effect_role")
                    == "preparatory"
                )
            )
            entry.update({
                "lifecycle": "receipt_bound",
                "effect_classification": (
                    "unknown" if outcome == "outcome_unknown"
                    else "effect_possible" if tracked_action_effect else "impossible"
                ),
                "receipt_bound_at": now,
            })
            journal[index] = entry
            record["primitive_journal"] = _safe_primitive_journal(journal)
            if tracked_action_effect:
                state = (
                    ActionState.EFFECT_POSSIBLE
                    if outcome == "outcome_unknown"
                    else ActionState.DISPATCH_RETURNED
                )
                record["action_truth"] = transition_action_truth(
                    record["action_truth"],
                    state=state,
                    reason_code=(
                        "primitive_receipt_outcome_unknown"
                        if outcome == "outcome_unknown"
                        else "action_primitive_receipt_bound"
                    ),
                    evidence={
                        "evidence_ref": evidence_reference({
                            "primitive_id": entry.get("primitive_id"),
                            "outcome": outcome,
                        }),
                    },
                    occurred_at=now,
                )
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = now
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def bind_visual_action_receipt(
        self,
        attempt_id: str,
        receipt: Mapping[str, Any],
    ) -> dict:
        """Persist canonical visual proposal/transform identity, never pixels."""

        safe = _safe_visual_action_receipt(receipt)
        if not safe.get("proposal_id") or not safe.get("source_frame_id"):
            raise IllegalTransition("visual_action_identity_incomplete")
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = _safe_visual_action_receipt(
                record.get("visual_action_receipt")
            )
            if current and current.get("proposal_id") != safe.get("proposal_id"):
                raise IllegalTransition("visual_action_proposal_conflict")
            record["visual_action_receipt"] = safe
            truth = dict(record.get("action_truth") or {})
            target = dict(truth.get("target") or {})
            target["frame_id"] = safe["source_frame_id"]
            if safe.get("surface_ref"):
                target["surface_ref"] = safe["surface_ref"]
            if safe.get("surface_generation") or safe.get("window_generation"):
                target["window_generation"] = (
                    safe.get("surface_generation")
                    or safe.get("window_generation")
                )
            truth["target"] = target
            record["action_truth"] = normalize_action_truth(
                truth, legacy_record=record,
            )
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def record_semantic_batch_primitive_receipt(
        self,
        attempt_id: str,
        *,
        semantic_batch_id: str,
        batch_index: int,
        receipt: Mapping[str, Any],
    ) -> dict:
        """Persist one typed batch result before the batch is aggregated."""

        if not isinstance(receipt, Mapping):
            raise TypeError("typed_semantic_batch_primitive_receipt_required")
        try:
            item_index = int(batch_index)
        except (TypeError, ValueError, OverflowError) as exc:
            raise IllegalTransition("semantic_batch_primitive_index_invalid") from exc
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if ToolAttemptStatus(record["status"]) in TERMINAL_STATUSES:
                return record
            batch = _safe_semantic_batch_record(record.get("semantic_batch"))
            journal = list(record.get("primitive_journal") or [])
            safe_batch_id = _safe_primitive_text(semantic_batch_id)
            if (
                not safe_batch_id
                or batch["semantic_batch_id"] != safe_batch_id
                or item_index < 0
                or item_index >= len(journal)
                or journal[item_index].get("semantic_batch_id") != safe_batch_id
            ):
                raise IllegalTransition("semantic_batch_primitive_identity_mismatch")

            dispatch_state = _safe_primitive_text(
                receipt.get("dispatch_state")
            ) or "not_dispatched"
            outcome = _safe_primitive_text(receipt.get("outcome"))
            reason_code = _safe_reason(receipt.get("reason_code"))
            failure_class = _safe_reason(receipt.get("failure_class"))
            stale_reason = _safe_reason(receipt.get("stale_reason"))
            actionability = receipt.get("actionability")
            actionability = (
                actionability if isinstance(actionability, Mapping) else {}
            )
            candidate_identity = actionability.get("candidate_identity")
            candidate_identity = (
                candidate_identity
                if isinstance(candidate_identity, Mapping) else {}
            )
            receipt_projection = {
                "semantic_batch_id": safe_batch_id,
                "batch_index": item_index,
                "dispatch_state": dispatch_state,
                "outcome": outcome,
                "reason_code": reason_code,
                "failure_class": failure_class,
                "stale_reason": stale_reason,
                "application_identity": _safe_primitive_text(
                    receipt.get("application_identity")
                ),
                "surface_ref": _safe_primitive_text(receipt.get("surface_ref")),
                "native_window_id": _safe_primitive_text(
                    receipt.get("native_window_id")
                ),
                "window_generation": _safe_primitive_text(
                    receipt.get("window_generation")
                ),
                "runtime_identity": _safe_primitive_text(
                    receipt.get("runtime_identity")
                    or candidate_identity.get("runtime_signature")
                ),
                "automation_id": _safe_primitive_text(
                    receipt.get("automation_id")
                    or actionability.get("automation_id")
                ),
                "uia_pattern": _safe_primitive_text(
                    receipt.get("uia_pattern")
                    or actionability.get("selected_pattern")
                ),
                "structural_epoch": str(receipt.get("structural_epoch") or ""),
                "dispatch_preflight_result": _safe_reason(
                    actionability.get("dispatch_preflight_result")
                ),
                "failed_before_effect_reason": _safe_reason(
                    actionability.get("failed_before_effect_reason")
                ),
                "stale_rebind_status": _safe_reason(
                    actionability.get("stale_rebind_status")
                ),
                "selected_pattern_available": (
                    actionability.get("selected_pattern_available") is True
                ),
                "candidate_applicable": (
                    actionability.get("candidate_applicable") is True
                ),
            }
            receipt_id = "receipt_" + hashlib.sha256(
                json.dumps(
                    receipt_projection,
                    sort_keys=True,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8", errors="replace")
            ).hexdigest()[:24]
            entry = dict(journal[item_index])
            entry.update({
                "primitive_receipt_id": receipt_id,
                "dispatch_state": dispatch_state,
                "outcome": outcome,
                "reason_code": reason_code,
                "failure_class": failure_class,
                "stale_reason": stale_reason,
                "dispatch_preflight_result": receipt_projection[
                    "dispatch_preflight_result"
                ],
                "failed_before_effect_reason": receipt_projection[
                    "failed_before_effect_reason"
                ],
                "stale_rebind_status": receipt_projection[
                    "stale_rebind_status"
                ],
                "selected_pattern_available": receipt_projection[
                    "selected_pattern_available"
                ],
                "candidate_applicable": receipt_projection[
                    "candidate_applicable"
                ],
            })
            for identity_field in (
                "application_identity", "surface_ref", "native_window_id",
                "window_generation", "runtime_identity", "automation_id",
                "uia_pattern",
            ):
                if receipt_projection[identity_field]:
                    entry[identity_field] = receipt_projection[identity_field]
            now = _now()
            if dispatch_state == "not_dispatched":
                if entry.get("lifecycle") not in {"prepared", "not_applied"}:
                    raise IllegalTransition(
                        "semantic_batch_not_dispatched_after_dispatch_start"
                    )
                entry.update({
                    "lifecycle": "not_applied",
                    "effect_classification": "not_applied",
                    "not_applied_at": float(entry.get("not_applied_at") or now),
                })
            else:
                if entry.get("lifecycle") not in {
                    "dispatch_started", "dispatch_returned", "receipt_bound",
                    "unknown", "verified",
                }:
                    raise IllegalTransition(
                        "semantic_batch_receipt_without_dispatch"
                    )
                if entry.get("lifecycle") not in {"unknown", "verified"}:
                    entry.update({
                        "lifecycle": "receipt_bound",
                        "effect_classification": (
                            "unknown"
                            if dispatch_state == "effect_unknown"
                            or outcome == "outcome_unknown" else
                            "effect_possible"
                        ),
                        "receipt_bound_at": float(
                            entry.get("receipt_bound_at") or now
                        ),
                    })
            journal[item_index] = entry

            prefix_entries = journal[:item_index]
            prefix_is_dispatched = bool(prefix_entries) and all(
                item.get("lifecycle") in {"receipt_bound", "verified"}
                and item.get("dispatch_state") in {
                    "dispatched", "effect_confirmed",
                }
                and item.get("outcome") in {
                    "success_unverified", "success_verified",
                    "confirmed_success",
                }
                for item in prefix_entries
            )
            no_later_dispatch = all(
                item.get("lifecycle") in {"prepared", "not_applied"}
                for item in journal[item_index + 1:]
            )
            if dispatch_state == "not_dispatched":
                batch.update({
                    "state": (
                        "checkpoint_pending"
                        if prefix_is_dispatched and no_later_dispatch else "stopped"
                    ),
                    "partial_batch_state": (
                        "partial_prefix_dispatched"
                        if prefix_is_dispatched and no_later_dispatch else
                        "failed_before_any_effect"
                        if not prefix_entries else "ambiguous_in_batch"
                    ),
                    "stop_reason": (
                        "semantic_batch_prefix_checkpoint_pending"
                        if prefix_is_dispatched and no_later_dispatch else
                        reason_code or "semantic_batch_dispatch_unavailable"
                    ),
                    "rejected_primitive_index": item_index,
                    "rejected_primitive_id": entry.get("primitive_id"),
                    "rejected_receipt_id": receipt_id,
                    "rejection_reason": (
                        reason_code or "semantic_batch_dispatch_unavailable"
                    ),
                    "resume_from_index": None,
                })
                if prefix_is_dispatched and no_later_dispatch:
                    checkpoint_id = (
                        f"{safe_batch_id}.prefix.{item_index}"
                    )
                    checkpoint_entry = journal[item_index - 1]
                    batch.update({
                        "checkpoint_id": checkpoint_id,
                        "checkpoint_prefix_length": item_index,
                        "checkpoint_postcondition": checkpoint_entry.get(
                            "semantic_postcondition"
                        ),
                    })
                    settle = _safe_semantic_settle(record.get("semantic_settle"))
                    if settle["state"] == "inactive":
                        record["semantic_settle"] = _safe_semantic_settle({
                            **_semantic_settle_configuration(record),
                            "checkpoint_id": checkpoint_id,
                            "state": "active",
                            "settle_slot_count": 0,
                            "probe_attempt_count": 0,
                            "completed_observation_count": 0,
                            "local_check_count": 0,
                            "provider_check_count": 0,
                            "started_at": now,
                            "local_check_times": [],
                            "observation_receipts": [],
                        })
            elif dispatch_state == "effect_unknown" or outcome == "outcome_unknown":
                batch.update({
                    "state": "outcome_unknown",
                    "partial_batch_state": "ambiguous_in_batch",
                    "stop_reason": (
                        reason_code or "semantic_batch_primitive_outcome_unknown"
                    ),
                    "resume_from_index": None,
                })
            elif item_index == len(journal) - 1 and all(
                item.get("lifecycle") in {"receipt_bound", "verified"}
                for item in journal
            ):
                batch.update({
                    "state": "checkpoint_pending",
                    "partial_batch_state": "completed_batch",
                    "stop_reason": "semantic_batch_dispatched",
                })

            record["primitive_journal"] = _safe_primitive_journal(journal)
            record["semantic_batch"] = _safe_semantic_batch_record(batch)
            record["updated_at"] = now
            record["revision"] = int(record["revision"]) + 1
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def record_primitive_outcome(
        self,
        attempt_id: str,
        *,
        outcome: str,
        primitive_id: str = "",
    ) -> dict:
        lifecycle, effect_class, timestamp = {
            "verified": ("verified", "verified", "verified_at"),
            "not_applied": ("not_applied", "not_applied", "not_applied_at"),
            "unknown": ("unknown", "unknown", "unknown_at"),
        }.get(str(outcome or ""), ("unknown", "unknown", "unknown_at"))
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if (
                lifecycle == "unknown"
                and normalize_action_truth(
                    record.get("action_truth"), legacy_record=record,
                ).get("effect_truth") == "verified"
            ):
                return record
            journal = list(record.get("primitive_journal") or [])
            index = self._journal_entry_index(record, primitive_id)
            if index < 0:
                return record
            entry = dict(journal[index])
            if (
                lifecycle in {"verified", "unknown"}
                and entry.get("lifecycle") == "prepared"
            ):
                raise IllegalTransition("primitive_outcome_without_dispatch")
            now = _now()
            entry.update({
                "lifecycle": lifecycle,
                "effect_classification": effect_class,
                timestamp: now,
            })
            journal[index] = entry
            record["primitive_journal"] = _safe_primitive_journal(journal)
            if (
                entry.get("effect_role") == "semantic"
                or (
                    entry.get("effect_role") == "preparatory"
                    and (record.get("action_truth") or {}).get("effect_role")
                    == "preparatory"
                )
            ):
                action_state = {
                    "verified": ActionState.EFFECT_VERIFIED,
                    "not_applied": ActionState.NOT_APPLIED_VERIFIED,
                    "unknown": ActionState.EFFECT_POSSIBLE,
                }[lifecycle]
                record["action_truth"] = transition_action_truth(
                    record["action_truth"],
                    state=action_state,
                    reason_code={
                        "verified": "action_primitive_effect_verified",
                        "not_applied": "action_primitive_not_applied_verified",
                        "unknown": "action_primitive_outcome_unknown",
                    }[lifecycle],
                    evidence={
                        "evidence_ref": evidence_reference({
                            "primitive_id": entry.get("primitive_id"),
                            "outcome": lifecycle,
                        }),
                    },
                    exact=lifecycle in {"verified", "not_applied"},
                    occurred_at=now,
                )
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = now
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def reconcile_semantic_batch_prefix(
        self,
        attempt_id: str,
        *,
        semantic_batch_id: str,
        applied_count: int | None,
        exact_non_applied_suffix: bool,
        applied_subset_ambiguous: bool = False,
    ) -> dict:
        """Bind exact read-only prefix evidence without replaying the batch."""

        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            batch = _safe_semantic_batch_record(record.get("semantic_batch"))
            journal = list(record.get("primitive_journal") or [])
            if (
                not batch["semantic_batch_id"]
                or batch["semantic_batch_id"]
                != _safe_primitive_text(semantic_batch_id)
                or len(journal) != len(batch["primitive_ids"])
            ):
                raise IllegalTransition("semantic_batch_identity_mismatch")
            if applied_subset_ambiguous or applied_count is None:
                batch.update({
                    "state": "outcome_unknown",
                    "stop_reason": "semantic_batch_applied_prefix_ambiguous",
                    "resume_from_index": None,
                })
                record["semantic_batch"] = _safe_semantic_batch_record(batch)
                record["updated_at"] = _now()
                record["revision"] = int(record["revision"]) + 1
                self._write_atomic(self._path(attempt_id), record)
                return dict(record)
            try:
                prefix_size = int(applied_count)
            except (TypeError, ValueError, OverflowError) as exc:
                raise IllegalTransition("semantic_batch_prefix_invalid") from exc
            if (
                prefix_size < 0
                or prefix_size > len(journal)
                or not exact_non_applied_suffix
            ):
                raise IllegalTransition("semantic_batch_prefix_invalid")
            now = _now()
            reconciled = []
            for index, raw in enumerate(journal):
                item = dict(raw)
                if index < prefix_size:
                    if item.get("lifecycle") == "prepared":
                        raise IllegalTransition(
                            "semantic_batch_prefix_without_dispatch"
                        )
                    item.update({
                        "lifecycle": "verified",
                        "effect_classification": "verified",
                        "verified_at": now,
                    })
                else:
                    if item.get("lifecycle") not in {
                        "prepared", "not_applied",
                    }:
                        raise IllegalTransition(
                            "semantic_batch_suffix_effect_possible"
                        )
                    item.update({
                        "lifecycle": "not_applied",
                        "effect_classification": "not_applied",
                        "not_applied_at": now,
                    })
                reconciled.append(item)
            record["primitive_journal"] = _safe_primitive_journal(reconciled)
            batch.update({
                "state": (
                    "verified"
                    if prefix_size == len(journal) else "resume_ready"
                ),
                "stop_reason": "semantic_batch_exact_prefix_reconciled",
                "resume_from_index": prefix_size,
            })
            record["semantic_batch"] = _safe_semantic_batch_record(batch)
            settle = _safe_semantic_settle(record.get("semantic_settle"))
            if (
                settle["state"] == "active"
                and settle["checkpoint_id"] == batch.get("checkpoint_id")
            ):
                settle["state"] = "proof_observed"
                record["semantic_settle"] = _safe_semantic_settle(settle)
            prefix_evidence = {
                "evidence_ref": evidence_reference({
                    "semantic_batch_id": batch["semantic_batch_id"],
                    "applied_count": prefix_size,
                    "primitive_count": len(journal),
                    "resume_from_index": prefix_size,
                }),
            }
            if prefix_size == 0:
                action_state = ActionState.NOT_APPLIED_VERIFIED
                reconciliation_state = ReconciliationStatus.NOT_APPLIED
            elif prefix_size == len(journal):
                action_state = ActionState.EFFECT_VERIFIED
                reconciliation_state = ReconciliationStatus.VERIFIED
            else:
                action_state = ActionState.EFFECT_OBSERVED
                reconciliation_state = ReconciliationStatus.PENDING
            record["action_truth"] = transition_action_truth(
                record["action_truth"],
                state=action_state,
                reason_code="semantic_batch_exact_prefix_reconciled",
                evidence=prefix_evidence,
                reconciliation_status=reconciliation_state,
                exact=True,
                occurred_at=now,
            )
            record["updated_at"] = now
            record["revision"] = int(record["revision"]) + 1
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
        record = self._transition(
            attempt_id, ToolAttemptStatus.PENDING,
            updates=updates,
        )
        if (record.get("action_truth") or {}).get("dispatch_started_at"):
            record = self.update_action_truth(
                attempt_id,
                state=ActionState.STILL_RUNNING.value,
                reason_code=_safe_reason(reason_code) or "owner_still_running",
                reconciliation_status=(
                    ReconciliationStatus.PROPAGATION_PENDING.value
                ),
            )
        return record

    def mark_waiting_verification(
        self, attempt_id: str, *, next_check_at: float, deadline_at: float,
        reason_code: str = "postcondition_not_observed",
        owner_settled: bool = False,
        check_index: int | None = None,
    ) -> dict:
        semantic_next = self.semantic_settle_next_check_at(attempt_id)
        updates = {
            "next_check_at": float(
                semantic_next if semantic_next is not None else next_check_at
            ),
            "deadline_at": float(deadline_at),
            "hard_deadline_at": float(deadline_at),
            "reason_code": _safe_reason(reason_code),
            "persistence_state": "waiting",
        }
        if owner_settled:
            updates["dispatch_state"] = "owner_settled_unverified"
        if check_index is not None:
            updates["reconciliation_check_index"] = max(0, int(check_index))
        record = self._transition(
            attempt_id, ToolAttemptStatus.WAITING_VERIFICATION,
            updates=updates,
        )
        if (record.get("action_truth") or {}).get("dispatch_started_at"):
            record = self.update_action_truth(
                attempt_id,
                state=ActionState.RECONCILING.value,
                reason_code=(
                    _safe_reason(reason_code) or "reconciliation_pending"
                ),
                reconciliation_status=ReconciliationStatus.PENDING.value,
            )
        return record

    def semantic_settle_next_check_at(
        self, attempt_id: str, *, now: float | None = None,
    ) -> float | None:
        """Return the next exact local-check deadline for one checkpoint."""

        record = self.get_attempt(attempt_id)
        if not record or not _semantic_settle_active(record):
            return None
        settle = _safe_semantic_settle(record.get("semantic_settle"))
        anchor = (
            settle["local_check_times"][-1]
            if settle["local_check_times"] else settle["started_at"]
        )
        current = _now() if now is None else float(now)
        delay_index = min(
            int(settle["settle_slot_count"]),
            len(settle["probe_delays_seconds"]) - 1,
        )
        return max(
            current + 0.05,
            anchor + float(settle["probe_delays_seconds"][delay_index]),
        )

    def record_semantic_settle_probe_result(
        self,
        attempt_id: str,
        *,
        result: SemanticSettleProbeResult,
    ) -> dict:
        """Persist one typed probe receipt before advancing its settle slot."""

        if not isinstance(result, SemanticSettleProbeResult):
            raise TypeError("typed_semantic_settle_probe_result_required")

        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            settle = _safe_semantic_settle(record.get("semantic_settle"))
            if (
                not settle["checkpoint_id"]
                or settle["settle_slot_count"] >= settle["max_local_checks"]
            ):
                return record
            classification = result.classification
            if not isinstance(classification, SemanticSettleProbeClassification):
                raise TypeError("typed_semantic_settle_classification_required")
            timestamp = max(0.0, float(result.observation_started_at))
            if not timestamp:
                raise ValueError("semantic_settle_probe_start_required")
            completed_at = max(0.0, float(result.observation_completed_at or 0.0))
            completed = classification in {
                SemanticSettleProbeClassification.COMPLETED_MATCH,
                SemanticSettleProbeClassification.COMPLETED_NONMATCH,
                SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE,
            }
            if completed and not completed_at:
                raise ValueError("completed_settle_observation_time_required")
            if completed and not result.probe_invoked:
                raise ValueError("completed_settle_probe_must_be_invoked")
            observer_mechanism = _safe_reason(result.observer_mechanism)
            if not observer_mechanism:
                raise ValueError("semantic_settle_observer_mechanism_required")
            exact_match = (
                True
                if classification is SemanticSettleProbeClassification.COMPLETED_MATCH
                else False
                if classification is SemanticSettleProbeClassification.COMPLETED_NONMATCH
                else None
            )
            slot_index = int(settle["settle_slot_count"]) + 1
            journal_entry = next(
                (
                    item for item in reversed(record.get("primitive_journal") or [])
                    if item.get("effect_role") == "semantic"
                    and (
                        item.get("semantic_substep_id") == settle["checkpoint_id"]
                        or item.get("primitive_id") == settle["checkpoint_id"]
                    )
                ),
                {},
            )
            if not journal_entry:
                batch = _safe_semantic_batch_record(
                    record.get("semantic_batch")
                )
                prefix_length = int(batch["checkpoint_prefix_length"] or 0)
                journal = list(record.get("primitive_journal") or [])
                if (
                    settle["checkpoint_id"] == batch["checkpoint_id"]
                    and 0 < prefix_length <= len(journal)
                ):
                    journal_entry = journal[prefix_length - 1]
            receipt = {
                "schema_version": 1,
                "checkpoint_id": settle["checkpoint_id"],
                "semantic_attempt_id": str(
                    journal_entry.get("strategy_attempt_id") or ""
                ),
                "semantic_substep_id": str(
                    journal_entry.get("semantic_substep_id") or ""
                ),
                "slot_index": slot_index,
                "observer_mechanism": observer_mechanism,
                "probe_invoked": result.probe_invoked is True,
                "observation_started_at": timestamp,
                "observation_completed_at": completed_at,
                "target_application": (
                    result.target_application
                    or journal_entry.get("application_identity")
                    or ""
                ),
                "surface_ref": (
                    result.surface_ref or journal_entry.get("surface_ref") or ""
                ),
                "native_window_id": (
                    result.native_window_id
                    or journal_entry.get("native_window_id")
                    or ""
                ),
                "window_generation": (
                    result.window_generation
                    or journal_entry.get("window_generation")
                    or ""
                ),
                "result_classification": classification.value,
                "exact_postcondition_match": exact_match,
                "reason_code": (
                    result.reason_code or "settle_probe_" + classification.value
                ),
                "evidence_ref": result.evidence_ref,
            }
            receipts = list(settle["observation_receipts"])
            receipts.append(receipt)
            checks = list(settle["local_check_times"])
            checks.append(max(0.0, timestamp))
            settle.update({
                "settle_slot_count": slot_index,
                "probe_attempt_count": min(
                    settle["max_local_checks"],
                    int(settle["probe_attempt_count"])
                    + (1 if result.probe_invoked else 0),
                ),
                "completed_observation_count": min(
                    settle["max_local_checks"],
                    int(settle["completed_observation_count"])
                    + (1 if completed else 0),
                ),
                "local_check_count": min(
                    settle["max_local_checks"],
                    slot_index,
                ),
                "local_check_times": checks[-settle["max_local_checks"]:],
                "observation_receipts": receipts[-settle["max_local_checks"]:],
            })
            record["semantic_settle"] = _safe_semantic_settle(settle)
            record["reconciliation_check_index"] = int(
                record["semantic_settle"]["settle_slot_count"]
            )
            record["updated_at"] = max(timestamp, completed_at)
            record["revision"] = int(record["revision"]) + 1
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

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
        process_facts: Mapping[str, Any] | None = None,
    ) -> tuple[dict, bool]:
        status = ToolAttemptStatus(status)
        if status not in TERMINAL_STATUSES:
            raise ValueError("terminal_status_required")
        from osia.tool_contracts import normalize_process_facts

        completed_process = normalize_process_facts(process_facts)
        if process_facts is not None and completed_process is None:
            raise ValueError("invalid_completed_process_facts")
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            current = ToolAttemptStatus(record["status"])
            if current in TERMINAL_STATUSES:
                return record, False
            reported_status = status
            reported_terminal_outcome = terminal_outcome
            reported_dispatch_state = dispatch_state
            reported_reason_code = reason_code
            cancellation_authoritative = (
                current is ToolAttemptStatus.CANCELLATION_REQUESTED
            )
            if cancellation_authoritative:
                # A worker may settle after Stop won. Preserve its semantic
                # evidence below, but terminalize orchestration as cancelled.
                status = ToolAttemptStatus.CANCELLED_PROVEN
                terminal_outcome = "cancelled_proven"
                reason_code = "late_result_after_cancellation"
            # Once the owner timed out after atomically claiming dispatch, a
            # late callback can settle the worker but cannot rewind the durable
            # boundary to "not_dispatched".  Keep ordinary synchronous
            # pre-dispatch failures unchanged; this guard applies only after
            # the explicit worker-may-still-be-active timeout was persisted.
            if (
                record.get("owner_dispatch_claimed_at")
                and record.get("owner_dispatch_timed_out_after_claim") is True
                and dispatch_state in {
                    "not_dispatched", "cancelled_before_effect",
                }
                and not (
                    status is ToolAttemptStatus.FAILED_BEFORE_EFFECT
                    and terminal_outcome in {
                        "confirmed_failure_before_effect",
                        "failed_before_effect",
                        "not_applied_verified",
                    }
                    and dispatch_state == "not_dispatched"
                    and _exact_correlated_primitive_no_effect(record)
                )
            ):
                status = ToolAttemptStatus.UNKNOWN_AFTER_START
                terminal_outcome = "outcome_unknown"
                dispatch_state = "effect_unknown"
                reason_code = "owner_timeout_after_dispatch_claim"
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
            settle = _safe_semantic_settle(record.get("semantic_settle"))
            if settle["state"] == "active":
                settle["state"] = (
                    "proof_observed"
                    if (
                        status == ToolAttemptStatus.SUCCEEDED
                        or safe_reason
                        == "semantic_batch_exact_prefix_reconciled"
                        or (
                            status == ToolAttemptStatus.FAILED_BEFORE_EFFECT
                            and safe_reason
                            == "local_reconciliation_not_applied"
                        )
                    ) else
                    "cancelled"
                    if status == ToolAttemptStatus.CANCELLED_PROVEN else
                    "stale_target"
                    if safe_reason in {
                        "semantic_settle_target_invalid",
                        "stale_desktop_target",
                    } else
                    "observation_failed"
                    if safe_reason == "semantic_settle_observation_failed" else
                    "evidence_unresolved"
                    if safe_reason == "semantic_settle_evidence_unresolved" else
                    "exhausted"
                    if (
                        status == ToolAttemptStatus.UNKNOWN_AFTER_START
                        and safe_reason == "semantic_settle_exhausted"
                        and settle["completed_observation_count"]
                        >= settle["max_local_checks"]
                    ) else
                    "aborted"
                )
                record["semantic_settle"] = _safe_semantic_settle(settle)
            batch = _safe_semantic_batch_record(record.get("semantic_batch"))
            if batch["state"] != "inactive":
                exact_batch_verified = (
                    normalize_action_truth(
                        record.get("action_truth"), legacy_record=record,
                    ).get("effect_truth") == "verified"
                )
                batch_fully_dispatched = bool(
                    record.get("primitive_journal")
                ) and all(
                    item.get("lifecycle") != "prepared"
                    for item in record.get("primitive_journal") or []
                )
                if batch_fully_dispatched and (
                    exact_batch_verified
                    or status == ToolAttemptStatus.SUCCEEDED
                ):
                    verified_journal = []
                    for raw in record.get("primitive_journal") or []:
                        item = dict(raw)
                        item.update({
                            "lifecycle": "verified",
                            "effect_classification": "verified",
                            "verified_at": now,
                        })
                        verified_journal.append(item)
                    record["primitive_journal"] = _safe_primitive_journal(
                        verified_journal
                    )
                batch["state"] = (
                    "resume_ready"
                    if (
                        batch["state"] == "resume_ready"
                        and safe_reason
                        == "semantic_batch_exact_prefix_reconciled"
                    ) else
                    "verified"
                    if (
                        (
                            exact_batch_verified
                            or status == ToolAttemptStatus.SUCCEEDED
                        )
                        and batch_fully_dispatched
                    ) else
                    "outcome_unknown"
                    if (
                        status == ToolAttemptStatus.SUCCEEDED
                        or status in {
                        ToolAttemptStatus.UNKNOWN_AFTER_START,
                        ToolAttemptStatus.FAILED_AFTER_EFFECT,
                        }
                    ) else
                    "stopped"
                )
                batch["stop_reason"] = safe_reason
                record["semantic_batch"] = _safe_semantic_batch_record(batch)
            truth = normalize_action_truth(
                record.get("action_truth"), legacy_record=record,
            )
            semantic_started = bool(truth.get("dispatch_started_at"))
            if (
                not semantic_started
                and not record.get("primitive_journal")
                and dispatch_state not in {
                    "not_dispatched", "cancelled_before_effect",
                }
            ):
                truth = transition_action_truth(
                    truth,
                    state=ActionState.DISPATCH_STARTED,
                    reason_code="terminal_receipt_confirms_dispatch_started",
                    occurred_at=now,
                )
                semantic_started = True
            action_evidence = {
                "evidence_ref": evidence_reference({
                    "terminal_outcome": reported_terminal_outcome,
                    "dispatch_state": reported_dispatch_state,
                    "reason_code": reported_reason_code,
                }),
            }
            if status is ToolAttemptStatus.CANCELLED_PROVEN:
                if cancellation_authoritative:
                    if reported_status is ToolAttemptStatus.SUCCEEDED:
                        verified = reported_terminal_outcome in {
                            "success_verified", "confirmed_success",
                        } or truth.get("effect_class") == "read_only"
                        truth = transition_action_truth(
                            truth,
                            state=(
                                ActionState.EFFECT_VERIFIED
                                if verified else ActionState.EFFECT_OBSERVED
                            ),
                            reason_code="late_result_evidence_after_cancellation",
                            evidence=action_evidence,
                            reconciliation_status=(
                                ReconciliationStatus.VERIFIED
                                if verified else ReconciliationStatus.PENDING
                            ),
                            exact=verified,
                            occurred_at=now,
                        )
                    elif (
                        reported_status is ToolAttemptStatus.FAILED_BEFORE_EFFECT
                        and not semantic_started
                    ):
                        truth = transition_action_truth(
                            truth,
                            state=ActionState.FAILED_BEFORE_EFFECT,
                            reason_code="late_non_application_after_cancellation",
                            occurred_at=now,
                        )
                    elif semantic_started:
                        truth = transition_action_truth(
                            truth,
                            state=ActionState.EFFECT_POSSIBLE,
                            reason_code="late_uncertain_result_after_cancellation",
                            evidence=action_evidence,
                            reconciliation_status=ReconciliationStatus.PENDING,
                            occurred_at=now,
                        )
                truth = transition_action_truth(
                    truth,
                    state=ActionState.CANCELLED,
                    reason_code=safe_reason or "cancellation_proven",
                    occurred_at=now,
                )
            elif truth.get("effect_truth") in {
                "verified", "not_applied",
            }:
                # Exact child/reconciliation evidence dominates a later lossy
                # parent callback or terminal summary.
                pass
            elif (
                safe_reason == "local_reconciliation_not_applied"
                and status is ToolAttemptStatus.FAILED_BEFORE_EFFECT
            ):
                truth = transition_action_truth(
                    truth,
                    state=ActionState.NOT_APPLIED_VERIFIED,
                    reason_code="local_reconciliation_not_applied",
                    evidence=action_evidence,
                    reconciliation_status=ReconciliationStatus.NOT_APPLIED,
                    exact=True,
                    occurred_at=now,
                )
            elif (
                status is ToolAttemptStatus.FAILED_BEFORE_EFFECT
                and not semantic_started
            ):
                truth = transition_action_truth(
                    truth,
                    state=ActionState.FAILED_BEFORE_EFFECT,
                    reason_code=safe_reason or "failed_before_effect",
                    occurred_at=now,
                )
            elif safe_reason == "semantic_batch_exact_prefix_reconciled":
                # reconcile_semantic_batch_prefix already persisted the exact
                # applied prefix and resume frontier.  A parent terminal
                # summary must not collapse that child truth back to generic
                # effect_possible.
                pass
            elif (
                terminal_outcome == "command_completed"
                and completed_process is not None
                and completed_process["source"] == "local_subprocess"
                and record.get("tool_name") in {"bash", "run_command"}
                and status in {ToolAttemptStatus.SUCCEEDED, ToolAttemptStatus.FAILED_AFTER_EFFECT}
                and dispatch_state == "effect_confirmed"
                and semantic_started
            ):
                truth = transition_action_truth(
                    truth, state=ActionState.EFFECT_OBSERVED,
                    reason_code=safe_reason or "completed_process_result",
                    evidence=action_evidence,
                    reconciliation_status=ReconciliationStatus.PENDING,
                    exact=False, occurred_at=now,
                )
            elif (
                terminal_outcome == "command_completed"
                and truth.get("current_state") == ActionState.EFFECT_OBSERVED.value
                and any(
                    item.get("classification") == "command_completed"
                    and item.get("execution_epoch") == record.get("execution_epoch")
                    and item.get("stale") is False
                    for item in record.get("continuation_history") or []
                )
            ):
                # The fenced provider callback already observed process
                # completion. Its nonzero exit proves neither the requested
                # external effect nor non-application; retain that exact truth.
                pass
            elif status is ToolAttemptStatus.SUCCEEDED:
                verified = terminal_outcome in {
                    "success_verified", "confirmed_success",
                } or truth.get("effect_class") == "read_only"
                truth = transition_action_truth(
                    truth,
                    state=(
                        ActionState.EFFECT_VERIFIED
                        if verified else ActionState.EFFECT_OBSERVED
                    ),
                    reason_code=(
                        safe_reason
                        or "effect_verified" if verified
                        else "effect_observed_unverified"
                    ),
                    evidence=action_evidence,
                    reconciliation_status=(
                        ReconciliationStatus.VERIFIED
                        if verified else ReconciliationStatus.PENDING
                    ),
                    exact=verified,
                    occurred_at=now,
                )
            else:
                truth = transition_action_truth(
                    truth,
                    state=ActionState.EFFECT_POSSIBLE,
                    reason_code=safe_reason or "effect_possible_after_dispatch",
                    evidence=action_evidence,
                    reconciliation_status=ReconciliationStatus.PENDING,
                    occurred_at=now,
                )
            record["action_truth"] = truth
            if (
                completed_process is not None
                and record.get("tool_name") in {"bash", "run_command"}
                and status in {ToolAttemptStatus.SUCCEEDED, ToolAttemptStatus.FAILED_AFTER_EFFECT}
                and terminal_outcome in {"success_verified", "success_unverified", "command_completed", "failed_after_effect"}
                and dispatch_state in {"effect_confirmed", "effect_applied"}
                and semantic_started
                and record.get("authority_state") not in {"cancelled", "superseded"}
            ):
                record["process_facts"] = completed_process
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
                record["action_truth"] = transition_action_truth(
                    record["action_truth"],
                    state=ActionState.CANCELLED,
                    reason_code="cancellation_acknowledged_before_effect",
                    occurred_at=now,
                )
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

    def list_recoverable_attempts(
        self,
        *,
        now: float | None = None,
        include_not_started: bool = False,
    ) -> list[dict]:
        del now  # callers decide whether next_check_at/deadline_at is due
        recoverable: list[dict] = []
        with self._store_lock():
            for path in self.attempts_dir.glob("attempt_*.json"):
                try:
                    record = self._validate(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, CorruptAttemptError):
                    # Corruption is never interpreted as permission to replay.
                    continue
                status = ToolAttemptStatus(record["status"])
                if (
                    status in RECOVERABLE_STATUSES
                    or (
                        include_not_started
                        and status is ToolAttemptStatus.NOT_STARTED
                    )
                ):
                    recoverable.append(record)
        return sorted(recoverable, key=_created_at_sort_key)

    def list_action_attempts(self, action_id: str) -> list[dict[str, Any]]:
        """Return every durable attempt/barrier for one semantic action."""

        action_id = str(action_id or "")
        if not action_id:
            return []
        found: list[dict[str, Any]] = []
        with self._store_lock():
            for directory in (self.attempts_dir, self.barriers_dir):
                for path in directory.glob("*.json"):
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(raw, dict):
                        continue
                    truth = normalize_action_truth(
                        raw.get("action_truth"), legacy_record=raw,
                    )
                    if truth.get("action_id") == action_id:
                        item = dict(raw)
                        item["action_truth"] = truth
                        found.append(item)
        return sorted(found, key=_created_at_sort_key)

    def list_unresolved_action_attempts(
        self, *, conversation_id: str = "", turn_id: str = "",
    ) -> list[dict[str, Any]]:
        """Project possible effects without treating terminality as truth."""

        found: list[dict[str, Any]] = []
        with self._store_lock():
            for directory in (self.attempts_dir, self.barriers_dir):
                for path in directory.glob("*.json"):
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(raw, dict):
                        continue
                    if conversation_id and raw.get("conversation_id") != conversation_id:
                        continue
                    if turn_id and raw.get("turn_id") != turn_id:
                        continue
                    truth = normalize_action_truth(
                        raw.get("action_truth"), legacy_record=raw,
                    )
                    if truth.get("effect_truth") not in {"possible", "observed"}:
                        continue
                    item = dict(raw)
                    item["action_truth"] = truth
                    found.append(item)
        return sorted(found, key=_created_at_sort_key)

    def update_action_truth(
        self,
        attempt_id: str,
        *,
        state: str,
        reason_code: str,
        evidence: Mapping[str, Any] | None = None,
        observer: Mapping[str, Any] | None = None,
        reconciliation_status: str = "",
        exact: bool = False,
        occurred_at: float | None = None,
        observer_attempt_id: str = "",
        execution_epoch: str = "",
        target_generation: str = "",
    ) -> dict[str, Any]:
        """Persist an observer/action transition under the attempt lock."""

        with self._store_lock():
            attempt_path = self._path(attempt_id)
            barrier_path = self.barriers_dir / f"{attempt_id}.json"
            is_barrier = not attempt_path.exists() and barrier_path.exists()
            if is_barrier:
                try:
                    record = json.loads(barrier_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CorruptAttemptError(
                        "attempt_barrier_unreadable"
                    ) from exc
                if not isinstance(record, dict):
                    raise CorruptAttemptError("invalid_attempt_barrier")
                record["action_truth"] = normalize_action_truth(
                    record.get("action_truth"), legacy_record=record,
                )
            else:
                record = self._read_unlocked(attempt_id)
            observer_source = (
                dict(observer) if isinstance(observer, Mapping) else {}
            )
            observer_attempt_id = _safe_primitive_text(
                observer_attempt_id
                or observer_source.get("observer_attempt_id"),
                maximum=256,
            )
            execution_epoch = _safe_primitive_text(
                execution_epoch or observer_source.get("execution_epoch"),
                maximum=256,
            )
            target_generation = _safe_primitive_text(
                target_generation
                or observer_source.get("target_generation"),
                maximum=256,
            )
            receipts = _safe_identity_receipts(record.get("observer_receipts"))
            if observer_attempt_id and any(
                item.get("receipt_id") == observer_attempt_id
                for item in receipts
            ):
                return dict(record)
            stale = bool(
                execution_epoch
                and execution_epoch != str(record.get("execution_epoch") or "")
            )
            expected_generation = str(
                ((record.get("action_truth") or {}).get("target") or {}).get(
                    "window_generation"
                )
                or ((record.get("action_truth") or {}).get("target") or {}).get(
                    "page_generation"
                )
                or ""
            )
            if (
                target_generation
                and expected_generation
                and target_generation != expected_generation
            ):
                stale = True
            if observer_attempt_id:
                receipts.append({
                    "receipt_id": observer_attempt_id,
                    "classification": str(
                        observer_source.get("status") or state
                    ),
                    "reason_code": (
                        "stale_observer_generation"
                        if stale else reason_code
                    ),
                    "execution_epoch": execution_epoch,
                    "target_generation": target_generation,
                    "stale": stale,
                    "received_at": (
                        _now() if occurred_at is None else float(occurred_at)
                    ),
                })
                record["observer_receipts"] = receipts[-128:]
            if stale:
                record["updated_at"] = (
                    _now() if occurred_at is None else float(occurred_at)
                )
                record["revision"] = int(record.get("revision") or 0) + 1
                self._write_atomic(
                    barrier_path if is_barrier else attempt_path,
                    record,
                )
                return dict(record)
            current_truth = normalize_action_truth(
                record.get("action_truth"), legacy_record=record,
            )
            authority_state = str(record.get("authority_state") or "active")
            try:
                target_state = ActionState(state)
            except ValueError:
                target_state = ActionState.EFFECT_POSSIBLE
            definitive_state = target_state in {
                ActionState.EFFECT_VERIFIED,
                ActionState.NOT_APPLIED_VERIFIED,
            }
            current_effect = str(current_truth.get("effect_truth") or "")
            incoming_effect = (
                "verified"
                if target_state is ActionState.EFFECT_VERIFIED else
                "not_applied"
                if target_state is ActionState.NOT_APPLIED_VERIFIED else ""
            )
            immutable_exact_conflict = bool(
                current_effect in {"verified", "not_applied"}
                and incoming_effect != current_effect
            )
            retain_only = bool(
                authority_state == "superseded"
                or (
                    authority_state in {"cancelled", "terminal"}
                    and not definitive_state
                )
                or immutable_exact_conflict
            )
            if retain_only:
                record["updated_at"] = (
                    _now() if occurred_at is None else float(occurred_at)
                )
                record["revision"] = int(record.get("revision") or 0) + 1
                self._write_atomic(
                    barrier_path if is_barrier else attempt_path,
                    record,
                )
                return dict(record)
            batch_before = _safe_semantic_batch_record(
                record.get("semantic_batch")
            )
            journal_before = list(record.get("primitive_journal") or [])
            if (
                target_state is ActionState.EFFECT_VERIFIED
                and exact
                and batch_before.get("state") != "inactive"
                and journal_before
                and any(
                    item.get("lifecycle") == "prepared"
                    for item in journal_before
                )
            ):
                record["updated_at"] = (
                    _now() if occurred_at is None else float(occurred_at)
                )
                record["revision"] = int(record.get("revision") or 0) + 1
                self._write_atomic(
                    barrier_path if is_barrier else attempt_path,
                    record,
                )
                return dict(record)
            updated_truth = transition_action_truth(
                current_truth,
                state=state,
                reason_code=reason_code,
                evidence=evidence,
                observer=observer,
                reconciliation_status=(reconciliation_status or None),
                exact=exact,
                occurred_at=occurred_at,
            )
            batch = _safe_semantic_batch_record(record.get("semantic_batch"))
            journal = list(record.get("primitive_journal") or [])
            if (
                target_state is ActionState.EFFECT_VERIFIED
                and exact
                and batch.get("state") != "inactive"
                and journal
                and all(
                    item.get("lifecycle") in {
                        "dispatch_started", "dispatch_returned",
                        "receipt_bound", "unknown", "verified",
                    }
                    for item in journal
                )
            ):
                verified_at = (
                    _now() if occurred_at is None else float(occurred_at)
                )
                record["primitive_journal"] = _safe_primitive_journal([
                    {
                        **item,
                        "lifecycle": "verified",
                        "effect_classification": "verified",
                        "verified_at": verified_at,
                    }
                    for item in journal
                ])
                batch.update({
                    "state": "verified",
                    "partial_batch_state": "completed_batch",
                    "completed_dispatch_count": len(journal),
                    "dispatched_count": len(journal),
                    "stop_reason": reason_code,
                })
                record["semantic_batch"] = _safe_semantic_batch_record(batch)
            if authority_state == "cancelled":
                updated_truth = transition_action_truth(
                    updated_truth,
                    state=ActionState.CANCELLED,
                    reason_code="authoritative_user_cancellation",
                    occurred_at=occurred_at,
                )
            record["action_truth"] = updated_truth
            record["updated_at"] = (
                _now() if occurred_at is None else float(occurred_at)
            )
            record["revision"] = int(record.get("revision") or 0) + 1
            self._write_atomic(
                barrier_path if is_barrier else attempt_path,
                record,
            )
            return dict(record)

    def bind_provider_references(
        self,
        attempt_id: str,
        references: Mapping[str, Any],
        *,
        expected_execution_epoch: str = "",
    ) -> dict[str, Any]:
        """Bind opaque native operation IDs without persisting provider content."""

        safe = _safe_provider_references(references)
        if not safe:
            raise ValueError("provider_reference_missing")
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if (
                expected_execution_epoch
                and expected_execution_epoch != record.get("execution_epoch")
            ):
                raise IllegalTransition("stale_execution_epoch")
            current = dict(record.get("provider_references") or {})
            for key, value in safe.items():
                existing = current.get(key)
                if existing and existing != value:
                    raise IllegalTransition("provider_reference_conflict")
                current[key] = value
            record["provider_references"] = current
            provider_operation_ref = next((
                current.get(key)
                for key in _PROVIDER_OPERATION_REFERENCE_PRIORITY
                if current.get(key)
            ), "")
            if provider_operation_ref:
                truth = dict(record.get("action_truth") or {})
                truth["provider_operation_ref"] = provider_operation_ref
                record["action_truth"] = normalize_action_truth(
                    truth, legacy_record=record,
                )
            record["updated_at"] = _now()
            record["revision"] = int(record.get("revision") or 0) + 1
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def apply_provider_callback(
        self,
        attempt_id: str,
        *,
        callback_id: str,
        execution_epoch: str,
        outcome: str,
        provider_references: Mapping[str, Any] | None = None,
        reason_code: str = "provider_continuation",
        evidence_ref: str = "",
        failure_domain: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Apply one fenced provider receipt; return ``(record, authoritative)``.

        A stale/duplicate/late receipt is retained diagnostically but never
        regains execution authority.  Definitive evidence may strengthen
        ActionTruth after cancellation without changing the cancelled attempt.
        """

        callback_id = _safe_primitive_text(callback_id, maximum=256)
        execution_epoch = _safe_primitive_text(execution_epoch, maximum=256)
        if not callback_id or not execution_epoch:
            raise ValueError("provider_callback_identity_missing")
        outcome = _safe_reason(outcome)
        reason_code = _safe_reason(reason_code) or "provider_continuation"
        failure_domain = _safe_reason(failure_domain)
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            processed = list(record.get("processed_provider_callbacks") or [])
            if callback_id in processed:
                return dict(record), False
            processed.append(callback_id)
            record["processed_provider_callbacks"] = processed[-128:]
            stale_epoch = execution_epoch != record.get("execution_epoch")
            authority_state = str(record.get("authority_state") or "active")
            late = authority_state in {"cancelled", "terminal", "superseded"}
            refs = dict(record.get("provider_references") or {})
            supplied_refs = _safe_provider_references(provider_references)
            invalid_command_binding = bool(
                outcome == "command_completed" and (
                    record.get("tool_name") != "run_command"
                    or not evidence_ref
                    or not (record.get("action_truth") or {}).get("dispatch_started_at")
                    or any(
                        not refs.get(key)
                        or supplied_refs.get(key) != refs.get(key)
                        for key in ("thread_id", "turn_id", "item_id", "tool_call_id")
                    )
                )
            )
            if not invalid_command_binding:
                for key, value in supplied_refs.items():
                    existing = refs.get(key)
                    if not existing or existing == value:
                        refs[key] = value
            record["provider_references"] = refs
            history = _safe_identity_receipts(
                record.get("continuation_history"), maximum=64,
            )
            history.append({
                "receipt_id": callback_id,
                "classification": outcome,
                "reason_code": (
                    "stale_execution_epoch" if stale_epoch
                    else "invalid_command_callback_binding" if invalid_command_binding
                    else "late_provider_evidence" if late
                    else reason_code
                ),
                "failure_domain": failure_domain,
                "execution_epoch": execution_epoch,
                "target_generation": "",
                "stale": stale_epoch or invalid_command_binding,
                "received_at": _now(),
            })
            record["continuation_history"] = history[-64:]
            definitive = outcome in {
                "completed_with_receipt", "explicit_not_applied",
                "failed_before_effect",
            }
            authoritative = not stale_epoch and not late and not invalid_command_binding
            current_truth = normalize_action_truth(
                record.get("action_truth"), legacy_record=record,
            )
            current_effect = str(current_truth.get("effect_truth") or "")
            incoming_effect = (
                "verified" if outcome == "completed_with_receipt" else
                "not_applied" if outcome in {
                    "explicit_not_applied", "failed_before_effect",
                } else ""
            )
            should_apply = not stale_epoch and not invalid_command_binding and (
                authoritative or definitive
            ) and not (
                current_effect in {"verified", "not_applied"}
                and incoming_effect != current_effect
            )
            if should_apply:
                evidence = {
                    "evidence_ref": evidence_ref or (
                        "provider_receipt:" + hashlib.sha256(
                            callback_id.encode("utf-8")
                        ).hexdigest()[:32]
                    ),
                    "provider_operation_ref": next((
                        refs.get(key)
                        for key in _PROVIDER_OPERATION_REFERENCE_PRIORITY
                        if refs.get(key)
                    ), ""),
                }
                dispatch_started = bool(current_truth.get("dispatch_started_at"))
                if outcome == "completed_with_receipt":
                    target_state = ActionState.EFFECT_VERIFIED
                    exact = True
                elif outcome == "command_completed" and dispatch_started:
                    target_state = ActionState.EFFECT_OBSERVED
                    exact = False
                elif outcome == "explicit_not_applied" or (
                    outcome == "failed_before_effect" and dispatch_started
                ):
                    target_state = ActionState.NOT_APPLIED_VERIFIED
                    exact = True
                elif outcome == "failed_before_effect":
                    target_state = ActionState.FAILED_BEFORE_EFFECT
                    exact = True
                elif outcome == "still_running" and dispatch_started:
                    target_state = ActionState.STILL_RUNNING
                    exact = False
                elif dispatch_started:
                    target_state = ActionState.RECONCILING
                    exact = False
                else:
                    # A missing/unsupported provider lookup is transport
                    # evidence only.  Without the exact semantic dispatch
                    # boundary it cannot manufacture action uncertainty.
                    should_apply = False
                    target_state = ActionState.FAILED_BEFORE_EFFECT
                    exact = False
                if late and target_state not in {
                    ActionState.EFFECT_VERIFIED,
                    ActionState.NOT_APPLIED_VERIFIED,
                }:
                    should_apply = False
                if should_apply:
                    updated_truth = transition_action_truth(
                        current_truth,
                        state=target_state,
                        reason_code=reason_code,
                        evidence=(evidence if definitive or outcome == "command_completed" else None),
                        reconciliation_status=(
                            ReconciliationStatus.VERIFIED
                            if target_state is ActionState.EFFECT_VERIFIED else
                            ReconciliationStatus.NOT_APPLIED
                            if target_state is ActionState.NOT_APPLIED_VERIFIED else
                            ReconciliationStatus.PENDING
                            if target_state is ActionState.EFFECT_OBSERVED else
                            ReconciliationStatus.RUNNING
                        ),
                        exact=exact,
                    )
                    if authority_state == "cancelled":
                        updated_truth = transition_action_truth(
                            updated_truth,
                            state=ActionState.CANCELLED,
                            reason_code="authoritative_user_cancellation",
                        )
                    record["action_truth"] = updated_truth
            record["updated_at"] = _now()
            record["revision"] = int(record.get("revision") or 0) + 1
            self._write_atomic(self._path(attempt_id), record)
            return dict(record), authoritative

    def supersede_attempt(
        self, attempt_id: str, *, execution_epoch: str,
    ) -> dict[str, Any]:
        with self._store_lock():
            record = self._read_unlocked(attempt_id)
            if execution_epoch != record.get("execution_epoch"):
                raise IllegalTransition("stale_execution_epoch")
            if record.get("authority_state") == "cancelled":
                return dict(record)
            record["authority_state"] = "superseded"
            record["tombstoned_at"] = _now()
            record["updated_at"] = _now()
            record["revision"] = int(record.get("revision") or 0) + 1
            self._write_atomic(self._path(attempt_id), record)
            return dict(record)

    def drain_action_outbox(self) -> int:
        """Publish embedded state+event transactions after a prior crash."""

        published = 0
        with self._store_lock():
            records = []
            for directory in (self.attempts_dir, self.barriers_dir):
                for path in directory.glob("*.json"):
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if isinstance(raw, dict) and raw.get("action_truth"):
                        self._prepare_action_outbox(raw)
                        records.append(raw)
        for record in records:
            before = len(record.get("action_outbox") or [])
            try:
                self._publish_action_outbox(record)
            except Exception:
                continue
            published += before
        return published

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
                        or not _continuation_owner_alive(record)
                    )
                ):
                    unfinished.append(record)
        return sorted(unfinished, key=_created_at_sort_key)

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
                and _continuation_owner_alive(record)
            ):
                return None
            claim = "continue_" + secrets.token_urlsafe(18)
            _alive, process_identity = _live_process_identity(os.getpid())
            record["continuation_state"] = claim
            record["continuation_claimed_at"] = _now()
            record["continuation_claimed_pid"] = os.getpid()
            record["continuation_claimed_process_identity"] = (
                process_identity or None
            )
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
            record["continuation_claimed_process_identity"] = None
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
            record["continuation_claimed_process_identity"] = None
            record["revision"] = int(record["revision"]) + 1
            record["updated_at"] = _now()
            self._write_atomic(self._path(attempt_id), record)
            return True

    def find_compatible_suspended_turn(self, conversation_id: str) -> dict | None:
        conversation_id = _safe_token(conversation_id, field="conversation_id")
        with self._store_lock():
            return self._active_for_conversation_unlocked(conversation_id)

    def find_resumable_chat_turn(self, conversation_id: str) -> dict | None:
        """Return one exact Chat receipt that an owner may safely reattach.

        Normal suspended attempts remain eligible.  A terminal receipt is also
        eligible only while its correlated continuation is unfinished and no
        live process owns that continuation claim.  Multiple candidates are
        contradictory durable state, so recovery fails closed instead of
        choosing one by filesystem order or recency.
        """

        conversation_id = _safe_token(conversation_id, field="conversation_id")
        candidates: list[dict] = []
        with self._store_lock():
            for path in self.attempts_dir.glob("attempt_*.json"):
                try:
                    record = self._validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except (OSError, json.JSONDecodeError, CorruptAttemptError):
                    continue
                if (
                    record.get("conversation_id") != conversation_id
                    or str(record.get("owner_scope") or "chat") != "chat"
                ):
                    continue
                status = ToolAttemptStatus(record["status"])
                if status in RECOVERABLE_STATUSES:
                    candidates.append(record)
                    continue
                if status in TERMINAL_STATUSES and (
                    record.get("continuation_state") != "complete"
                    and (
                        record.get("continuation_state") == "not_claimed"
                        or not _continuation_owner_alive(record)
                    )
                ):
                    candidates.append(record)
        return candidates[0] if len(candidates) == 1 else None

    def has_terminal_barrier(
        self, *, conversation_id: str, turn_id: str, tool_name: str,
        argument_fingerprint: str,
    ) -> bool:
        """Legacy API: persisted attempts never block a new dispatch."""
        return False

    def has_dispatch_barrier(
        self, *, conversation_id: str, turn_id: str, tool_name: str,
        argument_fingerprint: str, equivalence_fingerprint: str = "",
        block_unknown_effect: bool = True,
    ) -> bool:
        """Legacy API: persisted attempts never block a new dispatch."""
        return False

    def has_equivalent_terminal_barrier(
        self, *, conversation_id: str, turn_id: str,
        equivalence_fingerprint: str,
    ) -> bool:
        """Legacy API: persisted attempts never block a new dispatch."""
        return False

    def has_unknown_effect_barrier(
        self, *, conversation_id: str, turn_id: str,
    ) -> bool:
        """Legacy API: persisted attempts never block a new dispatch."""
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
                    "action_truth": record.get("action_truth"),
                    "primitive_journal": record.get("primitive_journal", []),
                    "semantic_batch": record.get("semantic_batch", {}),
                    "request_id": record.get("request_id", ""),
                    "autonomous_run_id": record.get("autonomous_run_id", ""),
                    "completed_at": completed,
                    "created_at": record.get("created_at", completed),
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

    def has_registered_runtime(self, attempt_id: str) -> bool:
        """Prove one exact in-process suspended continuation exists."""

        with self._lock:
            runtime = self._runtimes.get(str(attempt_id or ""))
            return bool(runtime is not None and not runtime.activated)

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
    def _coerce_settle_probe_result(
        payload: Any,
        *,
        record: Mapping[str, Any],
        started_at: float,
        probe_invoked: bool,
        failure_reason: str = "",
    ) -> SemanticSettleProbeResult:
        if isinstance(payload, SemanticSettleProbeResult):
            return payload
        journal_entry = next(
            (
                item for item in reversed(record.get("primitive_journal") or [])
                if item.get("effect_role") == "semantic"
            ),
            {},
        )
        batch = _safe_semantic_batch_record(record.get("semantic_batch"))
        journal = list(record.get("primitive_journal") or [])
        prefix_length = int(batch["checkpoint_prefix_length"] or 0)
        settle = _safe_semantic_settle(record.get("semantic_settle"))
        if (
            settle["checkpoint_id"] == batch["checkpoint_id"]
            and 0 < prefix_length <= len(journal)
        ):
            journal_entry = journal[prefix_length - 1]
        status = str(record.get("status") or "")
        reason_code = str(record.get("reason_code") or failure_reason or "")
        if payload is None:
            classification = SemanticSettleProbeClassification.PROBE_FAILED
            reason_code = failure_reason or "settle_probe_result_missing"
            completed_at = 0.0
            exact_match = None
        elif status == ToolAttemptStatus.SUCCEEDED.value:
            classification = SemanticSettleProbeClassification.COMPLETED_MATCH
            completed_at = _now()
            exact_match = True
        elif reason_code in {
            "semantic_settle_target_invalid", "stale_desktop_target",
        }:
            classification = SemanticSettleProbeClassification.STALE_OR_INVALID
            completed_at = _now()
            exact_match = None
        elif status == ToolAttemptStatus.FAILED_BEFORE_EFFECT.value:
            classification = SemanticSettleProbeClassification.COMPLETED_NONMATCH
            completed_at = _now()
            exact_match = False
        else:
            classification = (
                SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE
            )
            completed_at = _now()
            exact_match = None
        evidence_ref = ""
        if payload is not None:
            projection = {
                "status": status,
                "reason_code": reason_code,
                "dispatch_state": str(record.get("dispatch_state") or ""),
                "terminal_outcome": str(record.get("terminal_outcome") or ""),
            }
            evidence_ref = "sha256:" + hashlib.sha256(
                json.dumps(
                    projection, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        return SemanticSettleProbeResult(
            classification=classification,
            observer_mechanism="legacy_local_verifier",
            observation_started_at=started_at,
            observation_completed_at=completed_at,
            target_application=str(
                journal_entry.get("application_identity") or ""
            ),
            surface_ref=str(journal_entry.get("surface_ref") or ""),
            native_window_id=str(
                journal_entry.get("native_window_id") or ""
            ),
            window_generation=str(
                journal_entry.get("window_generation") or ""
            ),
            exact_postcondition_match=exact_match,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
            probe_invoked=probe_invoked,
            terminal_payload=payload,
        )

    @staticmethod
    def _next_progressive_check(record: Mapping[str, Any], now: float) -> tuple[float, int]:
        if _semantic_settle_active(record):
            settle = _safe_semantic_settle(record.get("semantic_settle"))
            delay_index = min(
                int(settle["settle_slot_count"]),
                len(settle["probe_delays_seconds"]) - 1,
            )
            return (
                now + float(settle["probe_delays_seconds"][delay_index]),
                int(settle["local_check_count"]),
            )
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
            try:
                record = self.store.get_attempt(attempt_id) or {}
            except CorruptAttemptError:
                record = {}
            self._schedule_timer(
                attempt_id, immediate=not _semantic_settle_active(record),
            )
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
            settle_due = bool(
                _semantic_settle_active(record)
                and str(record.get("dispatch_state") or "") not in {
                    "owner_started", "dispatch_started",
                    "worker_may_be_active",
                }
            )
            progress_before = int(record.get("useful_progress_count") or 0)
            if settle_due:
                probe_started_at = _now()
                if runtime.verify is None:
                    probe_result = self._coerce_settle_probe_result(
                        None,
                        record=record,
                        started_at=probe_started_at,
                        probe_invoked=False,
                        failure_reason="settle_probe_callback_missing",
                    )
                else:
                    try:
                        payload = runtime.verify(attempt_id)
                        latest = self.store.get_attempt(attempt_id) or record
                        probe_result = self._coerce_settle_probe_result(
                            payload,
                            record=latest,
                            started_at=probe_started_at,
                            probe_invoked=True,
                        )
                    except Exception:
                        probe_result = self._coerce_settle_probe_result(
                            None,
                            record=record,
                            started_at=probe_started_at,
                            probe_invoked=True,
                            failure_reason="settle_probe_callback_failed",
                        )
                try:
                    record = self.store.record_semantic_settle_probe_result(
                        attempt_id,
                        result=probe_result,
                    )
                except (TypeError, ValueError, OverflowError):
                    probe_result = self._coerce_settle_probe_result(
                        None,
                        record=record,
                        started_at=probe_started_at,
                        probe_invoked=runtime.verify is not None,
                        failure_reason="settle_probe_result_invalid",
                    )
                    record = self.store.record_semantic_settle_probe_result(
                        attempt_id,
                        result=probe_result,
                    )
                if probe_result.terminal_payload is not None:
                    self.notify_terminal(
                        attempt_id, probe_result.terminal_payload,
                    )
                    return
                classification = probe_result.classification
                if classification is SemanticSettleProbeClassification.COMPLETED_MATCH:
                    self.store.record_primitive_outcome(
                        attempt_id, outcome="verified",
                    )
                    terminal, _created = self.store.record_terminal_result(
                        attempt_id,
                        status=ToolAttemptStatus.SUCCEEDED,
                        terminal_outcome="success_verified",
                        dispatch_state="effect_confirmed",
                        reason_code="postcondition_verified_read_only",
                    )
                    self.notify_terminal(attempt_id, terminal)
                    return
                if classification is SemanticSettleProbeClassification.STALE_OR_INVALID:
                    terminal, _created = self.store.record_terminal_result(
                        attempt_id,
                        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
                        terminal_outcome="outcome_unknown",
                        dispatch_state="effect_unknown",
                        reason_code="semantic_settle_target_invalid",
                    )
                    self.notify_terminal(attempt_id, terminal)
                    return
            elif runtime.verify is not None:
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
            settle = _safe_semantic_settle(
                current_record.get("semantic_settle")
            )
            if (
                settle["state"] == "active"
                and settle["settle_slot_count"] >= settle["max_local_checks"]
            ):
                for item in current_record.get("primitive_journal") or []:
                    if (
                        item.get("effect_role") == "semantic"
                        and item.get("lifecycle") in {
                            "dispatch_started", "dispatch_returned",
                            "receipt_bound",
                        }
                    ):
                        self.store.record_primitive_outcome(
                            attempt_id,
                            primitive_id=str(item.get("primitive_id") or ""),
                            outcome="unknown",
                        )
                terminal, _created = self.store.record_terminal_result(
                    attempt_id,
                    status=ToolAttemptStatus.UNKNOWN_AFTER_START,
                    terminal_outcome="outcome_unknown",
                    dispatch_state="effect_unknown",
                    reason_code=(
                        "semantic_settle_exhausted"
                        if settle["completed_observation_count"]
                        >= settle["max_local_checks"]
                        else "semantic_settle_evidence_unresolved"
                        if settle["completed_observation_count"] > 0
                        else "semantic_settle_observation_failed"
                    ),
                )
                self.notify_terminal(attempt_id, terminal)
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
                    and (
                        _semantic_dispatch_started(current_record)
                        if current_record.get("tool_name")
                        == "activate_desktop_surface"
                        else dispatch_state not in {
                            "not_dispatched", "cancelled_before_effect",
                        }
                    )
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
            if (
                progress_after > progress_before
                and not _semantic_settle_active(current_record)
            ):
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
        for record in self.store.list_recoverable_attempts(
            include_not_started=True,
        ):
            attempt_id = record["tool_attempt_id"]
            claim = self.store.claim_reconciliation(attempt_id)
            if claim is None:
                continue
            dispatch_started = (
                _semantic_dispatch_started(record)
                if record.get("tool_name") == "activate_desktop_surface"
                else record.get("dispatch_state") != "not_dispatched"
            )
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
_ACTIVE_ATTEMPT_STORE: ContextVar[ToolAttemptStore | None] = ContextVar(
    "osia_active_tool_attempt_store", default=None,
)
_EVENT_SINK: ContextVar[Callable[[ToolAttemptEvent], None] | None] = ContextVar(
    "osia_tool_attempt_event_sink", default=None,
)


@contextmanager
def tool_attempt_scope(
    attempt_id: str,
    event_sink: Callable[[ToolAttemptEvent], None] | None = None,
    store: ToolAttemptStore | None = None,
):
    attempt_token = _ACTIVE_ATTEMPT_ID.set(str(attempt_id or ""))
    store_token = _ACTIVE_ATTEMPT_STORE.set(store)
    sink_token = _EVENT_SINK.set(event_sink)
    try:
        yield
    finally:
        _EVENT_SINK.reset(sink_token)
        _ACTIVE_ATTEMPT_STORE.reset(store_token)
        _ACTIVE_ATTEMPT_ID.reset(attempt_token)


def current_tool_attempt_id() -> str:
    return _ACTIVE_ATTEMPT_ID.get()


def current_tool_attempt_store() -> ToolAttemptStore | None:
    return _ACTIVE_ATTEMPT_STORE.get()


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

    def _canonical_chat_binding(
        record: Mapping[str, Any],
    ) -> tuple[bool, dict | None]:
        if str(record.get("owner_scope") or "chat") != "chat":
            return False, None
        try:
            from osia.turn_terminality import (
                load_persisted_execution_binding,
                persisted_turn_state_exists,
            )

            turn_id = str(record.get("turn_id") or "")
            canonical_state_exists = persisted_turn_state_exists(turn_id)
            binding = (
                load_persisted_execution_binding(turn_id)
                if canonical_state_exists else None
            )
        except Exception:
            # An indeterminate durable-state probe is canonical and fail-closed;
            # it must never enter the legacy direct-commit path.
            return True, None
        if not canonical_state_exists:
            return False, None
        expected = {
            "conversation_id": str(record.get("conversation_id") or ""),
            "request_id": str(record.get("request_id") or ""),
            "turn_id": str(record.get("turn_id") or ""),
            "provider_id": str(record.get("provider_id") or ""),
            "model_id": str(record.get("model_id") or ""),
        }
        if (
            not isinstance(binding, dict)
            or any(not value for value in expected.values())
            or any(
                str(binding.get(key) or "") != value
                for key, value in expected.items()
            )
            or not str(binding.get("run_id") or "")
            or not str(binding.get("effective_reasoning") or "")
        ):
            return True, None
        return True, dict(binding)

    def _canonical_final_already_committed(
        record: Mapping[str, Any], binding: Mapping[str, Any],
    ) -> bool:
        message_id = str(record.get("final_response_message_id") or "")
        turn_id = str(record.get("turn_id") or "")
        if not message_id or not turn_id:
            return False
        try:
            from osia.turn_terminality import load_persisted_terminal_result

            terminal = load_persisted_terminal_result(turn_id)
            conversation = conversations.get_conversation(
                str(record.get("conversation_id") or "")
            )
        except Exception:
            return False
        if (
            not isinstance(terminal, dict)
            or terminal.get("execution_binding") != dict(binding)
        ):
            return False
        reserved_text = str(terminal.get("final_text") or "")
        for message in list((conversation or {}).get("messages") or []):
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and str(message.get("_message_id") or "") == message_id
                and str(message.get("content") or "") == reserved_text
                and all(
                    str(message.get("_" + key) or "") == str(value or "")
                    for key, value in binding.items()
                )
            ):
                return True
        return False

    for record in recovered:
        canonical_state_exists, canonical_binding = _canonical_chat_binding(record)
        if canonical_state_exists:
            if canonical_binding is None:
                # A partial, corrupt, or mismatched canonical receipt is never
                # downgraded to legacy recovery metadata.
                continue
            # Unified Chat finals belong to the exact desktop/server owner,
            # because only that owner can reserve terminality and persist the
            # seven-field route. Feed the receipt back through the coordinator
            # instead of writing a parallel assistant message here.
            attempt_id = str(record.get("tool_attempt_id") or "")
            if _canonical_final_already_committed(record, canonical_binding):
                claim = get_tool_attempt_store().claim_continuation(attempt_id)
                if claim is not None:
                    try:
                        completed = get_tool_attempt_store().complete_continuation(
                            attempt_id,
                            claim,
                            final_response_message_id=str(
                                record.get("final_response_message_id") or ""
                            ),
                        )
                    except Exception:
                        completed = False
                    if not completed:
                        try:
                            get_tool_attempt_store().release_continuation(
                                attempt_id, claim,
                            )
                        except Exception:
                            pass
            else:
                get_tool_attempt_coordinator().notify_terminal(
                    attempt_id, record,
                )
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
        recovery_text = (
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
        try:
            from osia.turn_terminality import load_persisted_terminal_result

            persisted_terminal = load_persisted_terminal_result(turn_id)
        except Exception:
            persisted_terminal = None
        terminal_binding = (
            persisted_terminal.get("execution_binding")
            if isinstance(persisted_terminal, dict) else None
        )
        exact_terminal_binding = bool(
            isinstance(terminal_binding, dict)
            and str(terminal_binding.get("conversation_id") or "")
            == str(record.get("conversation_id") or "")
            and str(terminal_binding.get("request_id") or "") == request_id
            and str(terminal_binding.get("turn_id") or "") == turn_id
            and str(terminal_binding.get("run_id") or "") == turn_id
            and str(terminal_binding.get("provider_id") or "")
            == str(record.get("provider_id") or "")
            and str(terminal_binding.get("model_id") or "")
            == str(record.get("model_id") or "")
        )
        reserved_final = (
            str(persisted_terminal.get("final_text") or "")
            if exact_terminal_binding else ""
        )
        if is_internal_tool_control_text(reserved_final):
            reserved_final = ""
        text = reserved_final or recovery_text
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
            from osia.execution_trace import (
                ExecutionTraceLedger,
                terminal_display_key,
                validate_persisted_trace,
            )
            from osia.turn_terminality import load_persisted_execution_binding

            exact_binding = load_persisted_execution_binding(turn_id)
            expected = {
                "conversation_id": str(record.get("conversation_id") or ""),
                "request_id": request_id,
                "turn_id": turn_id,
                "run_id": turn_id,
                "provider_id": str(record.get("provider_id") or ""),
                "model_id": str(record.get("model_id") or ""),
            }
            if not isinstance(exact_binding, dict) or any(
                str(exact_binding.get(key) or "") != value
                for key, value in expected.items()
            ):
                exact_binding = {
                    **expected,
                    "effective_reasoning": "unknown",
                }
            recovery_trace = ExecutionTraceLedger(
                **exact_binding,
            )
            terminal_key = terminal_display_key(
                str(persisted_terminal.get("terminal_state") or "")
                if reserved_final else
                "blocked_verified"
            )
            if not terminal_key:
                raise ValueError("terminal recovery trace key is unavailable")
            recovery_trace.record(
                kind="terminal",
                display_key=terminal_key,
                terminal=True,
                dedupe_key="terminal",
            )
            safe_recovery_trace = validate_persisted_trace(
                recovery_trace.snapshot()
            )
        except Exception:
            safe_recovery_trace = None
        try:
            recovery_metadata = {
                "_conversation_id": record.get("conversation_id"),
                "_request_id": request_id,
                "_client_request_id": request_id,
                "_turn_id": turn_id,
                "_run_id": turn_id,
                "_message_id": message_id,
                "_operation_id": message_id,
                "_response_event_id": event_id,
                "_response_sequence": 1,
                "_terminal_event_id": terminal_id,
                "_terminal_sequence": 2,
                "_terminal": True,
                "_language": language,
                "_terminal_status": "done" if reserved_final else "error",
                "_stage": (
                    "terminal_recovery" if reserved_final else "tool_recovery"
                ),
                "_error_category": "" if reserved_final else recovery_reason,
                "_reason_code": "" if reserved_final else recovery_reason,
            }
            if safe_recovery_trace is not None:
                recovery_metadata["_execution_trace_v1"] = safe_recovery_trace
            receipt = conversations.commit_message(
                str(record.get("conversation_id") or ""),
                "assistant",
                text,
                metadata=recovery_metadata,
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
    "TOOL_DISPATCH_BOUNDARY_UNAVAILABLE_RESULT",
    "TOOL_DISPATCH_CANCELLED_RESULT",
    "SemanticSettleProbeClassification", "SemanticSettleProbeResult",
    "ToolAttemptCoordinator", "ToolAttemptEvent", "ToolAttemptStatus",
    "ToolAttemptStore",
    "WAITING_TOOL_RESULT", "current_tool_attempt_id",
    "current_tool_attempt_store", "get_tool_attempt_store",
    "get_tool_attempt_coordinator", "publish_tool_attempt_event",
    "is_internal_tool_control_text", "recover_orphaned_tool_attempts",
    "tool_attempt_scope",
]
