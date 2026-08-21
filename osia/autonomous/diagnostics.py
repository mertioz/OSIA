#!/usr/bin/env python3
"""Typed, sanitized Autonomous failure diagnostics (WOR-173).

This module is the single Python source of truth for failure categories, stages,
bounded diagnostic persistence and additive transport projections.  It accepts
only allow-listed structured metadata; raw prompts, exceptions, tool arguments,
paths and provider payloads never enter a diagnostic record.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum


class AutonomousStage(str, Enum):
    PLANNING = "planning"
    TASK_EXECUTION = "task_execution"
    VERIFICATION = "verification"
    RETRY = "retry"
    REPLAN = "replan"
    RESUME = "resume"
    CONTROL = "control"
    PERSISTENCE = "persistence"
    ORCHESTRATOR = "orchestrator"


class FailureCategory(str, Enum):
    PLANNING_PROVIDER_FAILURE = "planning_provider_failure"
    PLANNING_AUTHENTICATION_FAILURE = "planning_authentication_failure"
    PLANNING_RATE_LIMIT = "planning_rate_limit"
    PLANNING_TIMEOUT = "planning_timeout"
    INVALID_PLANNING_OUTPUT = "invalid_planning_output"
    TASK_EXECUTION_FAILURE = "task_execution_failure"
    TASK_TIMEOUT = "task_timeout"
    STAGNATION_TIMEOUT = "stagnation_timeout"
    HARD_TOTAL_CEILING = "hard_total_ceiling"
    REPEATED_IDENTICAL_CALL = "repeated_identical_call"
    REPEATED_CYCLE = "repeated_cycle"
    CONSECUTIVE_TOOL_ERRORS = "consecutive_tool_errors"
    PROVIDER_TIMEOUT = "provider_timeout"
    TOOL_TIMEOUT = "tool_timeout"
    VERIFICATION_ERROR = "verification_error"
    VERIFICATION_NOT_COMPLETED = "verification_not_completed"
    TASK_ATTEMPTS_EXHAUSTED = "task_attempts_exhausted"
    REPLAN_FAILURE = "replan_failure"
    REPLANS_EXHAUSTED = "replans_exhausted"
    INVALID_RESUME_STATE = "invalid_resume_state"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"
    ORCHESTRATOR_FAILURE = "orchestrator_failure"
    CHECKPOINT_FAILURE = "checkpoint_failure"
    UNKNOWN_FAILURE = "unknown_failure"


MAX_DIAGNOSTIC_HISTORY = 20

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$")
_SAFE_RUN_REF = re.compile(r"^run_[0-9a-f]{12}$")
_SAFE_TIMESTAMP = re.compile(r"^[0-9T:+.Z-]{10,40}$")

_KNOWN_STAGES = {item.value for item in AutonomousStage}
_KNOWN_CATEGORIES = {item.value for item in FailureCategory}
_KNOWN_PLANNING_OUTPUT_CATEGORIES = {
    "NO_PLANNING_PAYLOAD",
    "MALFORMED_JSON",
    "WRAPPED_JSON",
    "MULTIPLE_PAYLOADS",
    "TRUNCATED_JSON",
    "VALID_JSON_INVALID_SCHEMA",
    "WRONG_CONTENT_PART",
    "PROVIDER_EVENT_AGGREGATION",
    "OTHER_EXACT_CAUSE",
}
_KNOWN_REASON_CODES = {
    "provider_authentication",
    "provider_rate_limit",
    "provider_timeout",
    "provider_error",
    "invalid_json",
    "invalid_schema",
    "blank_objective",
    "planning_internal_failure",
    "model_setup_failure",
    "task_execution_failure",
    "task_timeout",
    "stagnation_timeout",
    "hard_total_ceiling",
    "repeated_identical_call",
    "repeated_cycle",
    "consecutive_tool_errors",
    "tool_timeout",
    "task_error_result",
    "missing_success_criterion",
    "file_criterion_satisfied",
    "file_criterion_not_met",
    "verification_error",
    "empty_verification_reply",
    "verification_not_completed",
    "verification_completed",
    "ambiguous_verification_reply",
    "document_artifact_missing",
    "document_artifact_expired",
    "document_artifact_corrupted",
    "document_artifact_unreadable",
    "document_artifact_unauthorized",
    "document_artifact_invalid",
    "document_extraction_failure",
    "file_identity_mismatch",
    "unsupported_format",
    "unsupported_legacy_format",
    "encrypted_document",
    "drm_protected",
    "protected_document",
    "corrupted_document",
    "active_content_detected",
    "external_reference_detected",
    "resource_limit_exceeded",
    "task_attempts_exhausted",
    "replan_failure",
    "replans_exhausted",
    "invalid_resume_state",
    "resume_state_unavailable",
    "resume_worker_failure",
    "user_interrupted",
    "user_aborted",
    "interrupt_stop_failure",
    "abort_stop_failure",
    "orchestrator_failure",
    "orchestrator_unavailable",
    "start_worker_failure",
    "progress_callback_failure",
    "checkpoint_failure",
    "legacy_failure",
}

_PLANNING_REASON_CATEGORIES = {
    "provider_authentication": FailureCategory.PLANNING_AUTHENTICATION_FAILURE,
    "provider_rate_limit": FailureCategory.PLANNING_RATE_LIMIT,
    "provider_timeout": FailureCategory.PLANNING_TIMEOUT,
    "provider_error": FailureCategory.PLANNING_PROVIDER_FAILURE,
    "invalid_json": FailureCategory.INVALID_PLANNING_OUTPUT,
    "invalid_schema": FailureCategory.INVALID_PLANNING_OUTPUT,
    "blank_objective": FailureCategory.INVALID_PLANNING_OUTPUT,
}


def planning_category(reason: str) -> FailureCategory:
    """Map the planner's legacy safe reason code onto the stable taxonomy."""
    return _PLANNING_REASON_CATEGORIES.get(
        str(reason or ""), FailureCategory.PLANNING_PROVIDER_FAILURE,
    )


def failure_message_key(category) -> str:
    """Return the PC i18n key for a category, with a future-safe fallback."""
    value = _enum_value(category)
    if value in _KNOWN_CATEGORIES:
        return f"autonomous.failure.{value}"
    return "autonomous.failure.unknown_failure"


def _enum_value(value) -> str:
    return value.value if isinstance(value, Enum) else str(value or "")


def sanitize_identifier(value) -> str:
    """Keep only bounded provider/model identifiers, never arbitrary text."""
    try:
        text = str(value or "")
    except Exception:
        return ""
    if re.match(r"^[A-Za-z]:/", text) or "://" in text:
        return ""
    return text if _SAFE_IDENTIFIER.fullmatch(text) else ""


def _known_code(value, allowed: set[str], fallback: str) -> str:
    text = _enum_value(value)
    return text if text in allowed else fallback


def _safe_reason(value, fallback: str) -> str:
    text = _enum_value(value)
    return text if text in _KNOWN_REASON_CODES else fallback


def _safe_int(value, default: int = 0) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(parsed, 0), 1_000_000)


def _run_ref(task_id) -> str:
    """Return a one-way, non-sensitive correlation id for a run."""
    if not task_id:
        return ""
    digest = hashlib.sha256(str(task_id).encode("utf-8", errors="replace")).hexdigest()
    return f"run_{digest[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def actual_provider_model(agent, fallback_provider="", fallback_model="") -> tuple[str, str]:
    """Read the provider/model currently active on an agent, safely."""
    try:
        provider_value = getattr(agent, "provider", "")
    except Exception:
        provider_value = ""
    try:
        model_value = getattr(agent, "model", "")
    except Exception:
        model_value = ""
    provider = sanitize_identifier(provider_value)
    model = sanitize_identifier(model_value)
    return (
        provider or sanitize_identifier(fallback_provider),
        model or sanitize_identifier(fallback_model),
    )


def make_diagnostic(
    st: dict | None,
    *,
    stage,
    category,
    reason_code=None,
    attempt=None,
    max_attempts=None,
    replan_count=None,
    max_replans=None,
    provider=None,
    model=None,
    resumable=False,
    checkpoint_present=False,
    transition_at=None,
    planning_output_category=None,
) -> dict:
    """Build one allow-listed diagnostic from explicit structured metadata."""
    st = st if isinstance(st, dict) else {}
    budgets = st.get("budgets") if isinstance(st.get("budgets"), dict) else {}
    cursor = st.get("cursor") if isinstance(st.get("cursor"), dict) else {}
    phases = st.get("phases") if isinstance(st.get("phases"), list) else []
    current_attempt = 0
    try:
        p = int(cursor.get("phase", 0) or 0)
        t = int(cursor.get("task", 0) or 0)
        if 0 <= p < len(phases):
            tasks = phases[p].get("tasks") or []
            if 0 <= t < len(tasks):
                current_attempt = _safe_int(tasks[t].get("attempts", 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        current_attempt = 0

    category_value = _known_code(
        category, _KNOWN_CATEGORIES, FailureCategory.UNKNOWN_FAILURE.value,
    )
    stage_value = _known_code(
        stage, _KNOWN_STAGES, AutonomousStage.ORCHESTRATOR.value,
    )
    reason_value = _safe_reason(reason_code, category_value)
    timestamp = str(transition_at or _now_iso())
    if not _SAFE_TIMESTAMP.fullmatch(timestamp):
        timestamp = _now_iso()

    diagnostic = {
        "run_ref": _run_ref(st.get("task_id")),
        "stage": stage_value,
        "error_category": category_value,
        "attempt": _safe_int(current_attempt if attempt is None else attempt),
        "max_attempts": _safe_int(
            budgets.get("max_attempts_per_task", 0)
            if max_attempts is None else max_attempts
        ),
        "replan_count": _safe_int(
            budgets.get("replan_count", 0)
            if replan_count is None else replan_count
        ),
        "max_replans": _safe_int(
            budgets.get("max_replans", 0) if max_replans is None else max_replans
        ),
        "provider": sanitize_identifier(
            st.get("provider", "") if provider is None else provider
        ),
        "model": sanitize_identifier(st.get("model", "") if model is None else model),
        "resumable": bool(resumable),
        "checkpoint_present": bool(checkpoint_present),
        "reason_code": reason_value,
        "transition_at": timestamp,
    }
    output_category = _known_code(
        planning_output_category,
        _KNOWN_PLANNING_OUTPUT_CATEGORIES,
        "",
    )
    if output_category:
        diagnostic["planning_output_category"] = output_category
    return diagnostic


def normalize_diagnostic(raw) -> dict | None:
    """Load an existing diagnostic fail-closed, dropping all unknown fields."""
    if not isinstance(raw, dict):
        return None
    stage = _known_code(
        raw.get("stage"), _KNOWN_STAGES, AutonomousStage.ORCHESTRATOR.value,
    )
    category = _known_code(
        raw.get("error_category"), _KNOWN_CATEGORIES,
        FailureCategory.UNKNOWN_FAILURE.value,
    )
    reason = _safe_reason(raw.get("reason_code"), category)
    run_ref = str(raw.get("run_ref") or "")
    if not _SAFE_RUN_REF.fullmatch(run_ref):
        run_ref = ""
    timestamp = str(raw.get("transition_at") or "")
    if not _SAFE_TIMESTAMP.fullmatch(timestamp):
        timestamp = _now_iso()
    diagnostic = {
        "run_ref": run_ref,
        "stage": stage,
        "error_category": category,
        "attempt": _safe_int(raw.get("attempt")),
        "max_attempts": _safe_int(raw.get("max_attempts")),
        "replan_count": _safe_int(raw.get("replan_count")),
        "max_replans": _safe_int(raw.get("max_replans")),
        "provider": sanitize_identifier(raw.get("provider")),
        "model": sanitize_identifier(raw.get("model")),
        "resumable": raw.get("resumable") is True,
        "checkpoint_present": raw.get("checkpoint_present") is True,
        "reason_code": reason,
        "transition_at": timestamp,
    }
    output_category = _known_code(
        raw.get("planning_output_category"),
        _KNOWN_PLANNING_OUTPUT_CATEGORIES,
        "",
    )
    if output_category:
        diagnostic["planning_output_category"] = output_category
    return diagnostic


def record_diagnostic(st: dict, diagnostic: dict, *, emit: bool = True) -> dict:
    """Set the current diagnostic, append bounded history and emit a safe log."""
    clean = normalize_diagnostic(diagnostic)
    if clean is None:
        clean = make_diagnostic(
            st,
            stage=AutonomousStage.ORCHESTRATOR,
            category=FailureCategory.UNKNOWN_FAILURE,
        )
    st["failure_diagnostic"] = clean
    history = [
        item for item in (
            normalize_diagnostic(entry)
            for entry in (st.get("diagnostic_history") or [])
        ) if item is not None
    ]
    history.append(dict(clean))
    st["diagnostic_history"] = history[-MAX_DIAGNOSTIC_HISTORY:]
    if emit:
        emit_diagnostic_log(clean)
    return clean


def clear_current(st: dict) -> None:
    """Clear stale current failure state while retaining bounded history."""
    st["failure_diagnostic"] = None


def set_checkpoint_present(st: dict, present: bool) -> None:
    """Keep the current diagnostic and its latest history entry consistent."""
    if not isinstance(st, dict):
        return
    current = st.get("failure_diagnostic")
    if not isinstance(current, dict):
        return
    current["checkpoint_present"] = bool(present)
    history = st.get("diagnostic_history")
    if not isinstance(history, list) or not history:
        return
    latest = history[-1]
    if (isinstance(latest, dict)
            and latest.get("transition_at") == current.get("transition_at")
            and latest.get("error_category") == current.get("error_category")):
        latest["checkpoint_present"] = bool(present)


def projection(st: dict | None) -> dict:
    """Return additive status-frame fields for the current diagnostic."""
    if not isinstance(st, dict):
        return {}
    clean = normalize_diagnostic(st.get("failure_diagnostic"))
    return dict(clean) if clean is not None else {}


def emit_diagnostic_log(diagnostic: dict) -> None:
    """Print one reconstructable record containing structured safe fields only."""
    clean = normalize_diagnostic(diagnostic)
    if clean is None:
        return
    print("[AUTONOMOUS_DIAGNOSTIC] " + json.dumps(clean, sort_keys=True))
