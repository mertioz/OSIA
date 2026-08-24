#!/usr/bin/env python3
"""OSIA autonomous task state — crash-safe persistent checkpoint (WOR-45 Phase 1).

The autonomous mode runs a long, multi-step objective. So a crash / restart /
power loss mid-run can be *resumed* rather than lost, the whole task lives in a
single JSON file on disk that is rewritten **atomically** after every meaningful
step (a "checkpoint").

Storage layout — one file, resolved from the shared config dir::

    <CONFIG_DIR>/task_state.json

``CONFIG_DIR`` is imported from :mod:`osia.config` (never recomputed): it is the
in-repo ``<repo>/config`` in dev/repo mode, else ``~/.osia`` on every OS — the
exact same dir the GUI and the mobile server use. ``CONFIG_DIR`` is a ``str``,
so we wrap it in ``Path`` here, mirroring ``osia/server/server.py``.

Schema (``schema_version == 2``)::

    {
      "schema_version": 1,
      "task_id":        "<hex-uuid>",
      "objective":      "<the overall goal, verbatim>",
      "status":         "in_progress" | "done" | "failed" | "aborted",
      "created_at":     "<ISO-8601 local>",
      "updated_at":     "<ISO-8601 local>",
      "provider":       "<provider id>",
      "model":          "<model id>",
      "language":       "<authoritative turn language>",
      "cursor":         {"phase": <int>, "task": <int>},
      "budgets":        {"max_attempts_per_task": 3,
                         "max_replans": 2, "replan_count": 0},
      "context_digest": "<rolling distilled context, '' at start>",
      "phases": [
        {"name": "<phase name>",
         "status": "pending" | "in_progress" | "done" | "failed",
         "tasks": [
           {"id": "<id>", "objective": "...", "success_criterion": "...",
            "status": "pending" | "in_progress" | "done" | "failed",
            "attempts": 0, "distilled_summary": "", "last_error": null}
         ]}
      ],
      "log": [ {"ts": "...", "event": "...", "ref": ..., "note": ...} ]
    }

This module is **pure state**: no GUI, no agent, no network. Constructors and
mutators only build / mutate plain dicts; the *caller* decides when to persist
by calling :func:`save_task_state`. Every disk write is atomic (temp file +
``os.replace``) and serialised by a module-level re-entrant lock, exactly like
:mod:`osia.conversations`.
"""

import json
import math
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from osia.autonomous import diagnostics
from osia import document_artifacts, document_sources, i18n
from osia.turn_context import normalize_locale
from osia.tools.browser_state import (
    BrowserReferenceError,
    BrowserStateRef,
    safe_url_display,
)

# Imported (never recomputed) so the autonomous harness, the GUI and the mobile
# server all resolve the SAME config/data dir. CONFIG_DIR is a str → wrap in
# Path, mirroring osia/server/server.py. See CLAUDE.md "VALEURS CRITIQUES".
from osia.config import CONFIG_DIR as _CONFIG_DIR

# ── Storage layout ─────────────────────────────────────────────────────────

TASK_STATE_FILE = Path(_CONFIG_DIR) / "task_state.json"
SCHEMA_VERSION = 2

# ``status`` remains the legacy engine lifecycle field so existing callers keep
# their public contract.  ``run_state`` is the finer WOR-194 state machine used
# for authorization and protocol decisions.
RUN_STATES = frozenset({
    "planning",
    "authorization_pending",
    "ready",
    "running",
    "suspended_scope_extension",
    "suspended_authorization_revoked",
    "blocked_outcome_unknown",
    "completed",
    "failed",
    "cancelled",
    "suspended_browser_intervention",
    "suspended_browser_state",
    "waiting_tool",
    "verifying_tool",
    "resuming",
    "blocked_unknown_effect",
    "cancellation_requested",
    "cancelled_proven",
})
AUTHORIZATION_STATUSES = frozenset({
    "pending", "authorized", "scope_extension_required", "revoked",
    "outcome_unknown", "not_required_full_access", "corrupt",
})
OPERATION_STATUSES = frozenset({
    "prepared", "authorized", "dispatched", "success_verified",
    "success_unverified", "cancelled_proven", "partial_effect_verified",
    "outcome_unknown", "suspended_scope_extension",
    "blocked_authorization", "failed_before_dispatch",
    "waiting_tool", "verifying_tool", "resuming",
    "cancellation_requested",
})
TASK_STATUSES = frozenset({
    "pending", "in_progress", "waiting_tool", "verifying_tool", "resuming",
    "blocked_unknown_effect", "done", "failed", "cancellation_requested",
    "cancelled_proven",
})
BROWSER_SUSPENSION_REASONS = frozenset({
    "manual_login_required", "captcha_required", "two_factor_required",
    "permission_required", "browser_state_stale",
    "download_attention_required", "unsupported_browser_behavior",
})

# Default budgets — bounded autonomy: at most N attempts per task and N replans
# overall (the planner/orchestrator in later phases enforce these).
DEFAULT_MAX_ATTEMPTS_PER_TASK = 3
DEFAULT_MAX_REPLANS = 2

# Re-entrant so a future caller can save() while already holding the lock.
_lock = threading.RLock()
_USER_CANCELLED_TERMINAL_KIND = "user_cancelled"
_DOCUMENT_PATH_RE = re.compile(
    r"(?i)(?:(?P<quote>[\"'`])(?:[A-Z]:[\\/]|/)[^\"'`\r\n]+?"
    r"\.(?:pdf|png|jpe?g)(?P=quote)|(?:[A-Z]:[\\/]|/)"
    r"[^\"'`\r\n]+?\.(?:pdf|png|jpe?g))"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/])[^\s\"'`<>]+"
)
_UNIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?m)(?:(?<=^)|(?<=\s)|(?<=[\"'`]))/(?!/)[^\s\"'`<>]+"
)
_WEB_URL_RE = re.compile(r"(?i)https?://[^\s\"'`<>]+")
_ENUMERATED_REQUIREMENT_RE = re.compile(
    r"(?m)^\s*(?P<number>\d{1,4})[.)]\s+(?P<text>\S.*)$"
)
_HANDOFF_REF_RE = re.compile(
    r"^(?:el|field)_[A-Za-z0-9_-]{16,64}$"
)


# ── Internals ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Current local time as an ISO-8601 string (seconds precision).

    Matches :func:`osia.conversations._now_iso` so all OSIA timestamps look the
    same on disk.
    """
    return datetime.now().isoformat(timespec="seconds")


def _write_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically — temp file + ``os.replace`` so a crashed/partial
    write never leaves a half-finished ``task_state.json`` behind.

    ``os.replace`` is atomic on both POSIX and Windows. Mirrors
    :func:`osia.conversations._write_atomic` byte-for-byte.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(_checkpoint_projection(data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _user_cancelled_terminal_authority(value, subject: dict) -> dict | None:
    """Validate an immutable cancellation terminal for this exact durable run."""

    if not isinstance(value, dict):
        return None
    try:
        revision = int(value.get("revision", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    identity = {
        "run_id": str(subject.get("run_id") or ""),
        "task_id": str(subject.get("task_id") or ""),
        "source_turn_id": str(subject.get("source_turn_id") or ""),
    }
    if (
        value.get("schema_version") != 1
        or value.get("kind") != _USER_CANCELLED_TERMINAL_KIND
        or revision < 1
        or not identity["run_id"]
        or any(str(value.get(key) or "") != expected
               for key, expected in identity.items())
    ):
        return None
    diagnostic = diagnostics.normalize_diagnostic(value.get("failure_diagnostic"))
    return {
        "schema_version": 1,
        "kind": _USER_CANCELLED_TERMINAL_KIND,
        "revision": revision,
        **identity,
        "established_at": str(value.get("established_at") or "")[:64],
        "failure_diagnostic": diagnostic,
    }


def _persisted_terminal_authority(subject: dict) -> dict | None:
    try:
        raw = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _user_cancelled_terminal_authority(
        raw.get("terminal_authority"), subject,
    )


def _apply_user_cancelled_terminal_authority(
    subject: dict,
    authority: dict,
) -> None:
    subject["terminal_authority"] = dict(authority)
    subject["status"] = "aborted"
    subject["run_state"] = "cancelled"
    subject["abandon_state"] = "abandoned_proven"
    if authority.get("failure_diagnostic") is not None:
        subject["failure_diagnostic"] = dict(authority["failure_diagnostic"])


def establish_user_cancelled_terminal(state: dict) -> None:
    """Make exact user cancellation authoritative over later stale callbacks."""

    if not isinstance(state, dict) or not str(state.get("run_id") or ""):
        raise ValueError("invalid_user_cancelled_terminal")
    existing = _user_cancelled_terminal_authority(
        state.get("terminal_authority"), state,
    )
    revision = int((existing or {}).get("revision", 0) or 0) + 1
    authority = {
        "schema_version": 1,
        "kind": _USER_CANCELLED_TERMINAL_KIND,
        "revision": revision,
        "run_id": str(state.get("run_id") or ""),
        "task_id": str(state.get("task_id") or ""),
        "source_turn_id": str(state.get("source_turn_id") or ""),
        "established_at": _now_iso(),
        "failure_diagnostic": diagnostics.normalize_diagnostic(
            state.get("failure_diagnostic")
        ),
    }
    _apply_user_cancelled_terminal_authority(state, authority)


def redact_document_paths(value) -> str:
    """Remove absolute local document paths from persisted/status text."""

    text = str(value or "")
    return _DOCUMENT_PATH_RE.sub("[local document]", text)


def _absolute_path_matches(value: str) -> list[str]:
    text = str(value or "")
    return [
        *(match.group(0) for match in _WINDOWS_ABSOLUTE_PATH_RE.finditer(text)),
        *(match.group(0) for match in _UNIX_ABSOLUTE_PATH_RE.finditer(text)),
    ]


def _private_path_basenames(value: str) -> tuple[str, ...]:
    names = []
    for raw in _absolute_path_matches(value):
        name = re.split(r"[\\/]", raw.rstrip(".,;:!?)]}"))[-1]
        if name and name not in names:
            names.append(name)
    return tuple(names[:64])


def sanitize_autonomous_text(value, *, private_filenames=()) -> str:
    """Remove private paths and URL query/fragment data from checkpoints."""

    text = str(value or "")

    def _safe_url(match):
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;:!?)]:":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        return safe_url_display(raw) + trailing

    text = _WEB_URL_RE.sub(_safe_url, text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("[private local path]", text)
    text = _UNIX_ABSOLUTE_PATH_RE.sub("[private local path]", text)
    text = redact_document_paths(text)
    for name in sorted(
        {str(item) for item in private_filenames if str(item)},
        key=len,
        reverse=True,
    ):
        text = re.sub(re.escape(name), "[private file]", text, flags=re.IGNORECASE)
    return text


def extract_enumerated_requirements(value: str) -> list[dict]:
    """Extract explicit numbered requirements without merging adjacent items."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_ENUMERATED_REQUIREMENT_RE.finditer(text))
    if len(matches) < 2:
        return []
    requirements = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start("text"):end].strip()
        requirements.append({
            "requirement_id": f"req_{index + 1:03d}",
            "source_number": int(match.group("number")),
            "text": body,
        })
    return requirements


def _checkpoint_projection(value, *, private_filenames=()):
    """Drop runtime-only source strings before any Autonomous disk write."""

    if isinstance(value, dict):
        filenames = tuple(private_filenames)
        if not filenames:
            filenames = _private_path_basenames(
                value.get("_runtime_objective", "")
            )
        return {
            key: _checkpoint_projection(
                item, private_filenames=filenames,
            )
            for key, item in value.items()
            if not str(key).startswith("_runtime_")
        }
    if isinstance(value, list):
        return [
            _checkpoint_projection(item, private_filenames=private_filenames)
            for item in value
        ]
    if isinstance(value, str):
        return sanitize_autonomous_text(
            value, private_filenames=private_filenames,
        )
    return value


def _safe_browser_state_ref(value) -> dict | None:
    try:
        return BrowserStateRef.parse(value).to_dict()
    except BrowserReferenceError:
        return None


def _safe_nonnegative_number(value, *, integer=False):
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if isinstance(number, float) and not math.isfinite(number):
        return 0
    return max(0, number)


def _safe_action_receipt(value) -> dict | None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    dispatch = str(value.get("dispatch_state") or "")
    outcome = str(value.get("outcome") or "")
    if dispatch not in {
        "not_dispatched", "dispatched", "effect_confirmed", "effect_unknown",
    } or outcome not in {
        "confirmed_failure_before_effect", "confirmed_success", "outcome_unknown",
    }:
        return None
    evidence = []
    for item in (value.get("evidence") or [])[:16]:
        if not isinstance(item, dict):
            continue
        clean = {}
        for key, raw in list(item.items())[:12]:
            name = re.sub(r"[^a-z0-9_]", "", str(key).lower())[:48]
            if not name:
                continue
            if isinstance(raw, str):
                clean[name] = raw[:512]
            elif raw is None or isinstance(raw, (bool, int, float)):
                clean[name] = raw
        if clean:
            evidence.append(clean)
    return {
        "schema_version": 1,
        "operation_id": str(value.get("operation_id") or "")[:96],
        "tool_call_id": str(value.get("tool_call_id") or "")[:96],
        "before_state": _safe_browser_state_ref(value.get("before_state")),
        "after_state": _safe_browser_state_ref(value.get("after_state")),
        "dispatch_state": dispatch,
        "outcome": outcome,
        "evidence": evidence,
        "started_at": _safe_nonnegative_number(value.get("started_at")),
        "completed_at": _safe_nonnegative_number(value.get("completed_at")),
    }


def safe_browser_operation_evidence(result) -> dict | None:
    """Return value-free typed evidence suitable for durable verification."""

    if not isinstance(result, dict):
        return None
    receipt = _safe_action_receipt(result.get("receipt"))
    if receipt is None:
        return None
    safe_receipt = {
        "before_state": receipt.get("before_state"),
        "after_state": receipt.get("after_state"),
        "dispatch_state": receipt.get("dispatch_state"),
        "outcome": receipt.get("outcome"),
        "evidence": receipt.get("evidence") or [],
    }
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    safe_data = {}
    for key in (
        "status", "pages_advanced", "steps", "byte_size", "field_count",
        "selected", "file_selected", "mime_type", "classification",
    ):
        value = data.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            if key in data:
                safe_data[key] = value
        elif isinstance(value, str):
            clean = value[:160]
            if re.fullmatch(r"[A-Za-z0-9_+./-]{1,160}", clean):
                safe_data[key] = clean
    return {"receipt": safe_receipt, "data": safe_data}


def _safe_reference_handoffs(values, current_state=None) -> list[dict]:
    current = _safe_browser_state_ref(current_state)
    handoffs = []
    for item in values if isinstance(values, list) else ():
        if not isinstance(item, dict):
            continue
        bound_state = _safe_browser_state_ref(item.get("state"))
        if bound_state is None or (current is not None and bound_state != current):
            continue
        operation = re.sub(
            r"[^a-z0-9_]", "", str(item.get("operation") or "").lower()
        )[:80]
        references = []
        for reference in item.get("references") or []:
            if not isinstance(reference, dict):
                continue
            field = re.sub(
                r"[^a-z0-9_]", "", str(reference.get("field") or "").lower()
            )[:48]
            opaque = str(reference.get("ref") or "")
            if not field.endswith("_ref") or not _HANDOFF_REF_RE.fullmatch(opaque):
                continue
            candidate = {"field": field, "ref": opaque}
            if candidate not in references:
                references.append(candidate)
            if len(references) >= 48:
                break
        if operation and references:
            handoffs.append({
                "operation": operation,
                "state": bound_state,
                "references": references,
            })
        if len(handoffs) >= 8:
            break
    return handoffs


def _collect_safe_handoff_refs(value) -> list[dict]:
    references = []

    def visit(item):
        if len(references) >= 48:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                field = re.sub(r"[^a-z0-9_]", "", str(key).lower())[:48]
                if (
                    field.endswith("_ref")
                    and isinstance(child, str)
                    and _HANDOFF_REF_RE.fullmatch(child)
                ):
                    candidate = {"field": field, "ref": child}
                    if candidate not in references:
                        references.append(candidate)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item[:100]:
                visit(child)

    visit(value)
    return references


def normalize_browser_checkpoint(value) -> dict:
    source = value if isinstance(value, dict) else {}
    pending = source.get("pending_operation")
    pending = pending if isinstance(pending, dict) else {}
    reason = str(source.get("manual_intervention") or "")
    browser_state = _safe_browser_state_ref(source.get("browser_state"))
    return {
        "browser_state": browser_state,
        "last_verified_state": _safe_browser_state_ref(
            source.get("last_verified_state")
        ),
        "active_popup_state": _safe_browser_state_ref(
            source.get("active_popup_state")
        ),
        "pending_operation": {
            "operation_id": str(pending.get("operation_id") or "")[:96],
            "kind": (
                str(pending.get("kind") or "")
                if str(pending.get("kind") or "") in {"upload", "download"}
                else ""
            ),
        } if pending else None,
        "last_action_receipt": _safe_action_receipt(
            source.get("last_action_receipt")
        ),
        "last_artifact_summary": (
            {
                "artifact_id": str(
                    (source.get("last_artifact_summary") or {}).get("artifact_id") or ""
                )[:64],
                "size": _safe_nonnegative_number(
                    (source.get("last_artifact_summary") or {}).get("size"),
                    integer=True,
                ),
                "mime_type": str(
                    (source.get("last_artifact_summary") or {}).get("mime_type") or ""
                )[:160],
                "classification": str(
                    (source.get("last_artifact_summary") or {}).get("classification") or ""
                )[:64],
                "private": True,
            }
            if isinstance(source.get("last_artifact_summary"), dict)
            and re.fullmatch(
                r"web_[0-9a-f]{32}",
                str(source["last_artifact_summary"].get("artifact_id") or ""),
            )
            else None
        ),
        "reference_handoffs": _safe_reference_handoffs(
            source.get("reference_handoffs"), browser_state,
        ),
        "manual_intervention": (
            reason if reason in BROWSER_SUSPENSION_REASONS else ""
        ),
    }


def record_browser_execution(st: dict, tool_identity: str, result) -> None:
    browser_tools = {
        "navigate", "click", "go_back", "go_forward", "open_new_tab",
        "find_and_click", "wait_for_load", "open_browser",
        "get_browser_state", "navigate_browser", "inspect_browser_page",
        "wait_for_browser_condition", "activate_browser_element",
        "list_browser_tabs", "select_browser_tab", "close_browser_tab",
        "reload_browser_page", "browser_back", "browser_forward",
        "open_new_browser_tab", "return_to_browser_opener", "search_web",
        "open_search_result", "paginate_browser", "scroll_infinite",
        "list_browser_dialogs", "handle_browser_dialog",
        "upload_browser_file", "download_browser_file",
    }
    if tool_identity not in browser_tools or not isinstance(result, dict):
        return
    checkpoint = normalize_browser_checkpoint(st.get("browser_checkpoint"))
    receipt = _safe_action_receipt(result.get("receipt"))
    if receipt is not None:
        checkpoint["last_action_receipt"] = receipt
        after = receipt.get("after_state")
        if after is not None:
            checkpoint["browser_state"] = after
        if (
            receipt.get("outcome") == "confirmed_success"
            and receipt.get("dispatch_state") == "effect_confirmed"
            and after is not None
        ):
            checkpoint["last_verified_state"] = after
        if tool_identity in {"upload_browser_file", "download_browser_file"}:
            checkpoint["pending_operation"] = None
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    checkpoint["reference_handoffs"] = _safe_reference_handoffs(
        checkpoint.get("reference_handoffs"), checkpoint.get("browser_state"),
    )
    references = _collect_safe_handoff_refs(data)
    if references and checkpoint.get("browser_state") is not None:
        operation = re.sub(
            r"[^a-z0-9_]", "", str(tool_identity or "").lower()
        )[:80]
        checkpoint["reference_handoffs"] = [
            item for item in checkpoint.get("reference_handoffs") or []
            if item.get("operation") != operation
        ]
        checkpoint["reference_handoffs"].append({
            "operation": operation,
            "state": checkpoint["browser_state"],
            "references": references,
        })
        checkpoint["reference_handoffs"] = _safe_reference_handoffs(
            checkpoint["reference_handoffs"], checkpoint["browser_state"],
        )
    intervention = str(data.get("manual_intervention") or "")
    artifact = data.get("artifact")
    if isinstance(artifact, dict) and re.fullmatch(
        r"web_[0-9a-f]{32}", str(artifact.get("artifact_id") or ""),
    ):
        checkpoint["last_artifact_summary"] = {
            "artifact_id": str(artifact["artifact_id"]),
            "size": _safe_nonnegative_number(artifact.get("size"), integer=True),
            "mime_type": str(artifact.get("mime_type") or "")[:160],
            "classification": str(artifact.get("classification") or "")[:64],
            "private": True,
        }
    if intervention in BROWSER_SUSPENSION_REASONS:
        checkpoint["manual_intervention"] = intervention
        st["run_state"] = "suspended_browser_intervention"
    st["browser_checkpoint"] = checkpoint


def mark_pending_browser_operation(st: dict, operation_id: str, kind: str) -> None:
    if kind not in {"upload", "download"}:
        return
    checkpoint = normalize_browser_checkpoint(st.get("browser_checkpoint"))
    checkpoint["pending_operation"] = {
        "operation_id": str(operation_id or "")[:96], "kind": kind,
    }
    st["browser_checkpoint"] = checkpoint


def suspend_browser(st: dict, reason: str) -> None:
    if reason not in BROWSER_SUSPENSION_REASONS:
        raise ValueError("invalid_browser_suspension_reason")
    checkpoint = normalize_browser_checkpoint(st.get("browser_checkpoint"))
    checkpoint["manual_intervention"] = reason
    st["browser_checkpoint"] = checkpoint
    st["run_state"] = (
        "suspended_browser_state"
        if reason == "browser_state_stale" else "suspended_browser_intervention"
    )


def _normalize_task(index: int, task, *, private_filenames=()) -> dict:
    """Coerce a loose task spec into the strict on-disk task schema.

    Accepts either a bare ``str`` (treated as the task objective) or a dict with
    any subset of the task fields; missing fields get their schema defaults so
    callers never hand-build the full dict.
    """
    if isinstance(task, str):
        task = {"objective": task}
    elif not isinstance(task, dict):
        task = {}
    raw_objective = task.get("objective", "") or ""
    raw_criterion = task.get("success_criterion", "") or ""
    requirement_id = str(task.get("requirement_id") or "")
    if not re.fullmatch(r"req_\d{3,4}", requirement_id):
        requirement_id = ""
    raw_status = str(task.get("status") or "pending")
    status = raw_status if raw_status in TASK_STATUSES else "pending"
    tool_attempt_id = str(task.get("tool_attempt_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{0,160}", tool_attempt_id):
        tool_attempt_id = ""
    tool_operation_id = str(task.get("tool_operation_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{0,160}", tool_operation_id):
        tool_operation_id = ""
    tool_reason = str(task.get("tool_terminal_reason") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", tool_reason):
        tool_reason = ""
    next_reconciliation = task.get("tool_next_reconciliation_at")
    if (
        isinstance(next_reconciliation, bool)
        or not isinstance(next_reconciliation, (int, float))
        or not math.isfinite(float(next_reconciliation))
        or next_reconciliation < 0
    ):
        next_reconciliation = None
    try:
        tool_revision = max(
            0, int(task.get("tool_operation_revision", 0) or 0),
        )
    except (TypeError, ValueError, OverflowError):
        tool_revision = 0
    semantic_attempts = []
    for item in (task.get("semantic_attempt_tuples") or [])[:128]:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            continue
        postcondition, strategy_id, provider_route, revision = item
        try:
            revision = max(0, int(revision))
        except (TypeError, ValueError, OverflowError):
            continue
        values = [
            str(postcondition or "")[:96],
            str(strategy_id or "")[:160],
            str(provider_route or "")[:160],
            revision,
        ]
        if all(values[:3]) and values not in semantic_attempts:
            semantic_attempts.append(values)
    semantic_tokens = []
    for token in (task.get("semantic_evidence_tokens") or [])[:128]:
        value = str(token or "")
        if re.fullmatch(r"[0-9a-f]{24}", value) and value not in semantic_tokens:
            semantic_tokens.append(value)
    try:
        semantic_revision = max(
            0, int(task.get("semantic_evidence_revision", 0) or 0),
        )
    except (TypeError, ValueError, OverflowError):
        semantic_revision = 0
    try:
        from osia.execution_strategy import normalize_execution_memory

        execution_memory = normalize_execution_memory(
            task.get("execution_memory"),
        )
    except Exception:
        execution_memory = {
            "schema_version": 1,
            "effects": [],
            "desktop_observation": {},
            "evidence_revision": 0,
        }
    normalized = {
        "id": str(task.get("id") or f"t{index}"),
        "objective": sanitize_autonomous_text(
            raw_objective, private_filenames=private_filenames,
        ),
        "success_criterion": sanitize_autonomous_text(
            raw_criterion, private_filenames=private_filenames,
        ),
        "requirement_id": requirement_id,
        "status": status,
        "attempts": int(task.get("attempts", 0) or 0),
        "distilled_summary": task.get("distilled_summary", "") or "",
        "last_error": task.get("last_error", None),
        "artifact_ids": _normalize_artifact_ids(task.get("artifact_ids")),
        "tool_attempt_id": tool_attempt_id,
        "tool_operation_id": tool_operation_id,
        "tool_operation_revision": tool_revision,
        "tool_durable_state": (
            str(task.get("tool_durable_state") or "")
            if str(task.get("tool_durable_state") or "") in TASK_STATUSES
            else ""
        ),
        "tool_next_reconciliation_at": next_reconciliation,
        "tool_terminal_reason": tool_reason,
        "tool_continuation_claimed": bool(
            task.get("tool_continuation_claimed", False)
        ),
        "semantic_attempt_tuples": semantic_attempts,
        "semantic_evidence_tokens": semantic_tokens,
        "semantic_evidence_revision": semantic_revision,
        "semantic_frontier_remaining": [
            str(item or "")[:160]
            for item in (task.get("semantic_frontier_remaining") or [])[:64]
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", str(item or ""))
        ],
        "semantic_next_strategy": (
            str(task.get("semantic_next_strategy") or "")[:160]
            if re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,160}",
                str(task.get("semantic_next_strategy") or ""),
            ) else ""
        ),
        "execution_memory": execution_memory,
    }
    if normalized["objective"] != raw_objective:
        normalized["_runtime_objective"] = raw_objective
    if normalized["success_criterion"] != raw_criterion:
        normalized["_runtime_success_criterion"] = raw_criterion
    return normalized


def _normalize_artifact_ids(values) -> list[str]:
    """Keep only bounded opaque IDs; no paths or payloads enter checkpoints."""

    clean: list[str] = []
    for value in values if isinstance(values, list) else ():
        try:
            artifact_id = document_artifacts.valid_artifact_id(value)
        except document_artifacts.ArtifactStoreError:
            continue
        if artifact_id not in clean:
            clean.append(artifact_id)
        if len(clean) >= document_artifacts.MAX_ARTIFACT_REFS:
            break
    return clean


def normalize_document_sources(values) -> list[dict]:
    """Keep only opaque run inputs and bounded safe inventory metadata."""

    clean = []
    seen = set()
    for item in values if isinstance(values, list) else ():
        if not isinstance(item, dict):
            continue
        try:
            source_ref = document_sources.valid_source_ref(item.get("source_ref"))
        except document_sources.DocumentSourceError:
            continue
        if source_ref in seen:
            continue
        type_label = str(item.get("type_label") or "document").lower()
        family = str(item.get("document_family") or "unknown").lower()
        source_state = str(item.get("state") or "ready").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,31}", type_label):
            type_label = "document"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", family):
            family = "unknown"
        if source_state not in {"ready", "failed"}:
            source_state = "failed"
        warnings = []
        for warning in item.get("warnings") or []:
            candidate = str(warning or "")
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
                warnings.append(candidate)
            if len(warnings) >= 8:
                break
        clean.append({
            "source_ref": source_ref,
            "type_label": type_label,
            "document_family": family,
            "state": source_state,
            "warnings": warnings,
        })
        seen.add(source_ref)
        if len(clean) >= 10:
            break
    return clean


def _normalize_phase(phase, *, private_filenames=()) -> dict:
    """Coerce a loose phase spec into the strict on-disk phase schema.

    Accepts either a bare ``str`` (the phase name, no tasks) or a dict with
    ``name``/``status``/``tasks``. Each task is normalised via
    :func:`_normalize_task`.
    """
    if isinstance(phase, str):
        name, status, raw_tasks = phase, None, []
    elif isinstance(phase, dict):
        name = phase.get("name", "") or ""
        status = phase.get("status")
        raw_tasks = phase.get("tasks") or []
    else:
        name, status, raw_tasks = "", None, []
    return {
        "name": sanitize_autonomous_text(
            name, private_filenames=private_filenames,
        ),
        "status": status or "pending",
        "tasks": [
            _normalize_task(i, t, private_filenames=private_filenames)
            for i, t in enumerate(raw_tasks)
        ],
    }


# ── Constructors ───────────────────────────────────────────────────────────

def new_task_state(objective: str, provider: str, model: str, phases, *,
                   language: str | None = None,
                   language_mode: str = "single-language",
                   allowed_languages=(),
                   source_language: str | None = None,
                   target_language: str | None = None,
                   run_id: str | None = None) -> dict:
    """Build a fresh ``task_state`` dict (NOT persisted — caller saves).

    ``phases`` is a loose list of phase specs (each a name string or a
    ``{name, status, tasks}`` dict); it is normalised into the strict schema so
    callers never hand-build nested dicts. The new task starts ``in_progress``
    with the cursor at ``(phase=0, task=0)``, default budgets, an empty
    ``context_digest`` and an empty ``log``.
    """
    now = _now_iso()
    private_filenames = _private_path_basenames(objective)
    safe_objective = sanitize_autonomous_text(
        objective, private_filenames=private_filenames,
    )
    raw_requirements = extract_enumerated_requirements(objective)
    checklist = []
    for requirement in raw_requirements:
        raw_text = requirement["text"]
        safe_text = sanitize_autonomous_text(
            raw_text, private_filenames=private_filenames,
        )
        item = {
            "requirement_id": requirement["requirement_id"],
            "source_number": requirement["source_number"],
            "criterion": safe_text,
            "status": "pending",
            "evidence_ref": "",
            "failure_reason": "",
            "retry_eligible": True,
        }
        if safe_text != raw_text:
            item["_runtime_criterion"] = raw_text
        checklist.append(item)
    normalized_language = normalize_locale(language) or i18n.DEFAULT_LANG
    normalized_mode = (
        language_mode
        if language_mode in {"single-language", "translation", "multilingual"}
        else "single-language"
    )
    normalized_allowed = list(dict.fromkeys(
        code for code in (normalize_locale(item) for item in allowed_languages)
        if code
    )) or [normalized_language]
    result = {
        "schema_version": SCHEMA_VERSION,
        "task_id": uuid.uuid4().hex,
        "run_id": str(run_id or uuid.uuid4().hex),
        "objective": safe_objective,
        "status": "in_progress",
        "created_at": now,
        "updated_at": now,
        "provider": provider or "",
        "model": model or "",
        "provider_id": provider or "",
        "model_id": model or "",
        "plan_revision": 1,
        "authorization_revision": 0,
        "policy_epoch": 0,
        "run_state": "planning",
        "authorization_status": "pending",
        "authorization_source": "none",
        "grant_refs": [],
        "capability_summary": [],
        "authorization_request": None,
        "pending_scope_extension": None,
        "handled_authorization_requests": [],
        "revocation": {
            "revoked": False,
            "revoked_at": None,
            "reason_code": "",
        },
        "operation_records": [],
        "ambiguity_barriers": [],
        "browser_checkpoint": normalize_browser_checkpoint({}),
        "source_conversation_id": "",
        "source_turn_id": "",
        "source_request_id": "",
        "source_assistant_message_id": "",
        "source_permission_epoch": 0,
        "attached_tool_attempt_id": "",
        "terminal_response": "",
        "language": normalized_language,
        "language_mode": normalized_mode,
        "allowed_languages": normalized_allowed,
        "source_language": normalize_locale(source_language) or "",
        "target_language": normalize_locale(target_language) or "",
        "cursor": {"phase": 0, "task": 0},
        "budgets": {
            "max_attempts_per_task": DEFAULT_MAX_ATTEMPTS_PER_TASK,
            "max_replans": DEFAULT_MAX_REPLANS,
            "replan_count": 0,
        },
        "context_digest": "",
        "checklist": checklist,
        "document_sources": [],
        "artifacts": [],
        "phases": [
            _normalize_phase(p, private_filenames=private_filenames)
            for p in (phases or [])
        ],
        "log": [],
        "failure_diagnostic": None,
        "diagnostic_history": [],
    }
    if safe_objective != (objective or ""):
        result["_runtime_objective"] = objective or ""
    return result


def initialize_authorization_pending(
    st: dict,
    capability_summary,
    *,
    policy_epoch: int,
    request: dict | None,
) -> dict:
    """Finish planning without granting authority or starting execution."""

    if not isinstance(st, dict):
        raise ValueError("invalid_autonomous_state")
    st["schema_version"] = SCHEMA_VERSION
    st["run_state"] = "authorization_pending"
    st["authorization_status"] = "pending"
    st["authorization_source"] = "none"
    st["policy_epoch"] = max(0, int(policy_epoch))
    st["grant_refs"] = []
    st["capability_summary"] = [
        dict(item) for item in (capability_summary or [])
        if isinstance(item, dict)
    ]
    st["authorization_request"] = dict(request) if isinstance(request, dict) else None
    st["pending_scope_extension"] = None
    return st


def operation_record(st: dict, operation_id: str) -> dict | None:
    """Return one durable operation record by opaque id."""

    for record in st.get("operation_records") or []:
        if isinstance(record, dict) and record.get("operation_id") == operation_id:
            return record
    return None


def equivalent_operation_record(
    st: dict,
    *,
    tool_identity: str,
    normalized_call_hash: str,
    private_scope_hash: str,
    autonomous_equivalence_hash: str = "",
    statuses=None,
) -> dict | None:
    """Find a durable equivalent operation without relying on plan revision."""

    allowed = set(statuses or OPERATION_STATUSES)
    for record in st.get("operation_records") or []:
        if not isinstance(record, dict) or record.get("status") not in allowed:
            continue
        exact_match = (
            record.get("normalized_call_hash") == normalized_call_hash
            and record.get("private_scope_hash") == private_scope_hash
        )
        semantic_match = bool(
            autonomous_equivalence_hash
            and record.get("autonomous_equivalence_hash")
            == autonomous_equivalence_hash
        )
        if record.get("tool_identity") == tool_identity and (
            exact_match or semantic_match
        ):
            return record
    return None


def upsert_operation_record(st: dict, record: dict) -> dict:
    """Insert/update one value-free operation record in the checkpoint."""

    if not isinstance(record, dict):
        raise ValueError("invalid_operation_record")
    operation_id = str(record.get("operation_id") or "")
    status = str(record.get("status") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", operation_id):
        raise ValueError("invalid_operation_id")
    if status not in OPERATION_STATUSES:
        raise ValueError("invalid_operation_status")
    records = st.setdefault("operation_records", [])
    existing = operation_record(st, operation_id)
    if existing is None:
        records.append(dict(record))
    else:
        existing.update(record)
    # A bounded history is sufficient for one bounded plan and avoids an
    # externally-corrupted state growing without limit.
    if len(records) > 512:
        del records[:-512]
    return operation_record(st, operation_id) or dict(record)


def equivalent_ambiguity_barrier(
    st: dict,
    *,
    tool_identity: str,
    normalized_call_hash: str,
    private_scope_hash: str,
    autonomous_equivalence_hash: str = "",
) -> dict | None:
    """Find a durable no-replay barrier for an equivalent private operation."""

    for barrier in st.get("ambiguity_barriers") or []:
        if not isinstance(barrier, dict):
            continue
        exact_match = (
            barrier.get("normalized_call_hash") == normalized_call_hash
            and barrier.get("private_scope_hash") == private_scope_hash
        )
        semantic_match = bool(
            autonomous_equivalence_hash
            and barrier.get("autonomous_equivalence_hash")
            == autonomous_equivalence_hash
        )
        if barrier.get("tool_identity") == tool_identity and (
            exact_match or semantic_match
        ):
            return barrier
    return None


def add_ambiguity_barrier(st: dict, record: dict) -> dict:
    """Persist one OUTCOME_UNKNOWN equivalence barrier idempotently."""

    existing = equivalent_ambiguity_barrier(
        st,
        tool_identity=str(record.get("tool_identity") or ""),
        normalized_call_hash=str(record.get("normalized_call_hash") or ""),
        private_scope_hash=str(record.get("private_scope_hash") or ""),
        autonomous_equivalence_hash=str(
            record.get("autonomous_equivalence_hash") or ""
        ),
    )
    if existing is not None:
        return existing
    barrier = {
        "operation_id": str(record.get("operation_id") or ""),
        "tool_identity": str(record.get("tool_identity") or ""),
        "normalized_call_hash": str(record.get("normalized_call_hash") or ""),
        "private_scope_hash": str(record.get("private_scope_hash") or ""),
        "autonomous_equivalence_hash": str(
            record.get("autonomous_equivalence_hash") or ""
        ),
        "reason_code": "outcome_unknown",
        "created_at": _now_iso(),
    }
    st.setdefault("ambiguity_barriers", []).append(barrier)
    return barrier


def new_failed_task_state(provider: str, model: str, diagnostic: dict, *,
                          language: str | None = None) -> dict:
    """Build a terminal, objective-free state for a failure before planning.

    Planning and invalid-resume failures have no executable plan to checkpoint.
    Keeping their state objective-free makes the diagnostic durable without
    copying a user prompt or other arbitrary content into the failure record.
    """
    st = new_task_state("", provider, model, [], language=language)
    st["status"] = "failed"
    st["run_state"] = "failed"
    st["authorization_status"] = "corrupt"
    clean = diagnostics.normalize_diagnostic(diagnostic) or {}
    refreshed = diagnostics.make_diagnostic(
        st,
        stage=clean.get("stage"),
        category=clean.get("error_category"),
        reason_code=clean.get("reason_code"),
        attempt=clean.get("attempt"),
        max_attempts=clean.get("max_attempts"),
        replan_count=clean.get("replan_count"),
        max_replans=clean.get("max_replans"),
        provider=clean.get("provider") or provider,
        model=clean.get("model") or model,
        resumable=clean.get("resumable", False),
        checkpoint_present=clean.get("checkpoint_present", True),
        transition_at=clean.get("transition_at"),
    )
    diagnostics.record_diagnostic(st, refreshed, emit=False)
    return st


# ── Mutators (pure dict manipulation — caller persists via save_task_state) ──

def advance_cursor(state: dict) -> dict:
    """Move the cursor to the next task, rolling over to the next phase.

    If the current phase has another task, advance ``cursor.task``; otherwise
    move to the next phase (``cursor.phase += 1``, ``cursor.task = 0``). Pure
    dict manipulation — returns ``state`` for chaining; the caller saves.
    """
    cursor = state.setdefault("cursor", {"phase": 0, "task": 0})
    phases = state.get("phases") or []
    p = cursor.get("phase", 0)
    t = cursor.get("task", 0)
    if 0 <= p < len(phases):
        tasks = phases[p].get("tasks") or []
        if t + 1 < len(tasks):
            cursor["task"] = t + 1
            return state
    cursor["phase"] = p + 1
    cursor["task"] = 0
    return state


def set_task_status(state: dict, phase_idx: int, task_idx: int, status: str,
                    **fields) -> dict:
    """Set a single task's ``status`` (and any extra fields, e.g. ``attempts``,
    ``distilled_summary``, ``last_error``).

    Out-of-range indices are ignored (no-op) so a stale cursor never raises.
    Pure dict manipulation — returns ``state``; the caller saves.
    """
    phases = state.get("phases") or []
    if 0 <= phase_idx < len(phases):
        tasks = phases[phase_idx].get("tasks") or []
        if 0 <= task_idx < len(tasks):
            task = tasks[task_idx]
            if status not in TASK_STATUSES:
                raise ValueError("invalid_autonomous_task_status")
            task["status"] = status
            for key, value in fields.items():
                task[key] = value
            requirement_id = str(task.get("requirement_id") or "")
            checklist_item = next((
                item for item in state.get("checklist") or []
                if isinstance(item, dict)
                and item.get("requirement_id") == requirement_id
            ), None)
            if checklist_item is not None:
                attempts = max(0, int(task.get("attempts", 0) or 0))
                max_attempts = max(1, int(
                    (state.get("budgets") or {}).get(
                        "max_attempts_per_task", DEFAULT_MAX_ATTEMPTS_PER_TASK,
                    ) or DEFAULT_MAX_ATTEMPTS_PER_TASK
                ))
                semantic_retry = bool(
                    task.get("semantic_next_strategy")
                    or task.get("semantic_frontier_remaining")
                )
                if status == "done":
                    checklist_item.update({
                        "status": "verified",
                        "evidence_ref": f"task:{str(task.get('id') or '')[:64]}",
                        "failure_reason": "",
                        "retry_eligible": False,
                    })
                elif status == "failed":
                    checklist_item.update({
                        "status": "blocked",
                        "evidence_ref": "",
                        "failure_reason": str(
                            task.get("last_error") or "requirement_not_verified"
                        )[:96],
                        "retry_eligible": attempts < max_attempts or semantic_retry,
                    })
                elif status in {
                    "in_progress", "waiting_tool", "verifying_tool",
                    "resuming", "cancellation_requested",
                }:
                    checklist_item.update({
                        "status": "active",
                        "evidence_ref": "",
                        "failure_reason": str(task.get("last_error") or "")[:96],
                        "retry_eligible": (
                            status == "in_progress"
                            and (attempts < max_attempts or semantic_retry)
                        ),
                    })
                elif status == "blocked_unknown_effect":
                    checklist_item.update({
                        "status": "blocked",
                        "evidence_ref": "",
                        "failure_reason": "blocked_unknown_effect",
                        "retry_eligible": False,
                    })
                elif status == "cancelled_proven":
                    checklist_item.update({
                        "status": "blocked",
                        "evidence_ref": "",
                        "failure_reason": "cancelled_proven",
                        "retry_eligible": False,
                    })
    return state


def current_task(state: dict) -> tuple[int, int, dict | None]:
    """Return the exact cursor task without advancing or repairing the plan."""

    cursor = state.get("cursor") or {}
    try:
        phase_index = int(cursor.get("phase", 0) or 0)
        task_index = int(cursor.get("task", 0) or 0)
        task = (state.get("phases") or [])[phase_index]["tasks"][task_index]
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return 0, 0, None
    return phase_index, task_index, task if isinstance(task, dict) else None


def bind_tool_attempt(
    state: dict,
    *,
    attempt_id: str,
    operation_id: str,
    operation_revision: int,
    next_reconciliation_at: float | None,
) -> dict:
    """Persist only safe common-attempt correlation on the current task."""

    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", str(attempt_id or "")):
        raise ValueError("invalid_tool_attempt_id")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", str(operation_id or "")):
        raise ValueError("invalid_tool_operation_id")
    if (
        isinstance(operation_revision, bool)
        or not isinstance(operation_revision, int)
        or operation_revision < 0
    ):
        raise ValueError("invalid_tool_operation_revision")
    if next_reconciliation_at is not None and (
        isinstance(next_reconciliation_at, bool)
        or not isinstance(next_reconciliation_at, (int, float))
        or not math.isfinite(float(next_reconciliation_at))
        or next_reconciliation_at < 0
    ):
        raise ValueError("invalid_tool_reconciliation_time")
    phase_index, task_index, task = current_task(state)
    if task is None:
        raise ValueError("autonomous_task_unavailable")
    existing = str(task.get("tool_attempt_id") or "")
    if existing and existing != attempt_id:
        raise ValueError("autonomous_task_attempt_conflict")
    return set_task_status(
        state,
        phase_index,
        task_index,
        "waiting_tool",
        tool_attempt_id=attempt_id,
        tool_operation_id=operation_id,
        tool_operation_revision=operation_revision,
        tool_durable_state="waiting_tool",
        tool_next_reconciliation_at=(
            float(next_reconciliation_at)
            if next_reconciliation_at is not None else None
        ),
        tool_terminal_reason="",
        tool_continuation_claimed=False,
        last_error=None,
    )


def checklist_remaining(state: dict) -> list[dict]:
    """Return sanitized required criteria that are not yet verified."""

    return [
        item for item in state.get("checklist") or []
        if isinstance(item, dict) and item.get("status") != "verified"
    ]


def append_log(state: dict, event: str, ref=None, note=None) -> dict:
    """Append a timestamped entry to ``state['log']``.

    Pure dict manipulation — returns ``state``; the caller saves.
    """
    state.setdefault("log", []).append({
        "ts": _now_iso(),
        "event": event,
        "ref": ref,
        "note": note,
    })
    return state


def resume_state_is_valid(st) -> bool:
    """Whether ``st`` is a structurally safe, resumable checkpoint.

    A cursor exactly one phase past the plan is valid: it represents a crash
    after the final cursor advance but before the terminal ``done`` checkpoint.
    """
    if not isinstance(st, dict) or st.get("status") != "in_progress":
        return False
    if st.get("schema_version") != SCHEMA_VERSION:
        return False
    if st.get("run_state") not in {
        "ready", "running", "suspended_scope_extension",
        "suspended_authorization_revoked", "blocked_outcome_unknown",
        "suspended_browser_intervention", "suspended_browser_state",
        "waiting_tool", "verifying_tool", "resuming",
        "blocked_unknown_effect", "cancellation_requested",
        "cancelled_proven",
    }:
        return False
    if st.get("authorization_status") not in AUTHORIZATION_STATUSES:
        return False
    phases = st.get("phases")
    cursor = st.get("cursor")
    if not isinstance(phases, list) or not phases or not isinstance(cursor, dict):
        return False
    try:
        p = int(cursor.get("phase", 0))
        t = int(cursor.get("task", 0))
    except (TypeError, ValueError, OverflowError):
        return False
    if p < 0 or p > len(phases) or t < 0:
        return False
    for phase in phases:
        if not isinstance(phase, dict) or not isinstance(phase.get("tasks"), list):
            return False
        if any(not isinstance(task, dict) for task in phase["tasks"]):
            return False
    if p == len(phases) and t != 0:
        return False
    if p < len(phases) and t > len(phases[p]["tasks"]):
        return False
    return True


def execution_state_is_valid(st) -> bool:
    """Whether authorization fields are structurally safe for dispatch."""

    if not isinstance(st, dict) or st.get("schema_version") != SCHEMA_VERSION:
        return False
    if st.get("run_state") not in RUN_STATES:
        return False
    if st.get("authorization_status") not in AUTHORIZATION_STATUSES:
        return False
    if not isinstance(st.get("run_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", st.get("run_id", "")
    ):
        return False
    for name in ("plan_revision", "authorization_revision", "policy_epoch"):
        if type(st.get(name)) is not int or st[name] < 0:
            return False
    for name in (
        "source_conversation_id", "source_turn_id", "source_request_id",
        "source_assistant_message_id", "attached_tool_attempt_id",
    ):
        if not isinstance(st.get(name, ""), str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{0,160}", st.get(name, ""),
        ):
            return False
    if (
        not isinstance(st.get("terminal_response", ""), str)
        or len(st.get("terminal_response", "")) > 32_000
    ):
        return False
    if (
        type(st.get("source_permission_epoch", 0)) is not int
        or st.get("source_permission_epoch", 0) < 0
    ):
        return False
    refs = st.get("grant_refs")
    if not isinstance(refs, list) or any(
        not isinstance(ref, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", ref)
        for ref in refs
    ):
        return False
    if not isinstance(st.get("operation_records"), list):
        return False
    if normalize_document_sources(st.get("document_sources")) != st.get(
        "document_sources"
    ):
        return False
    normalized_browser = normalize_browser_checkpoint(st.get("browser_checkpoint"))
    if normalized_browser != st.get("browser_checkpoint"):
        return False
    for record in st["operation_records"]:
        if not isinstance(record, dict) or record.get("status") not in OPERATION_STATUSES:
            return False
        if not isinstance(record.get("operation_id"), str):
            return False
        attempt_id = str(record.get("tool_attempt_id") or "")
        if attempt_id and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,160}", attempt_id,
        ):
            return False
    for _phase in st.get("phases") or []:
        for task in _phase.get("tasks") or []:
            if task.get("status") not in TASK_STATUSES:
                return False
            attempt_id = str(task.get("tool_attempt_id") or "")
            if attempt_id and not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,160}", attempt_id,
            ):
                return False
    return True


def _migrate_loaded_state(loaded: dict) -> dict:
    """Migrate old checkpoints without ever creating authority."""

    version = loaded.get("schema_version", 1)
    if type(version) is not int or version > SCHEMA_VERSION or version < 1:
        # Unknown formats remain inspectable but can never execute or be
        # converted into authority by a later authorization response.
        loaded["schema_version"] = SCHEMA_VERSION
        loaded["status"] = "failed"
        loaded["run_state"] = "failed"
        loaded["authorization_status"] = "corrupt"
        loaded["authorization_source"] = "none"
        loaded["grant_refs"] = []
        loaded["authorization_request"] = None
        loaded["pending_scope_extension"] = None
        loaded["operation_records"] = []
        loaded["ambiguity_barriers"] = []
        loaded["browser_checkpoint"] = normalize_browser_checkpoint({})
        return loaded

    if version < SCHEMA_VERSION:
        loaded["schema_version"] = SCHEMA_VERSION
        loaded["run_id"] = str(
            loaded.get("run_id") or loaded.get("task_id") or uuid.uuid4().hex
        )
        loaded["provider_id"] = str(loaded.get("provider") or "")
        loaded["model_id"] = str(loaded.get("model") or "")
        loaded["plan_revision"] = 1
        loaded["authorization_revision"] = 0
        loaded["policy_epoch"] = 0
        loaded["grant_refs"] = []
        loaded["capability_summary"] = []
        loaded["authorization_request"] = None
        loaded["pending_scope_extension"] = None
        loaded["handled_authorization_requests"] = []
        loaded["revocation"] = {
            "revoked": False, "revoked_at": None, "reason_code": "",
        }
        loaded["operation_records"] = []
        loaded["ambiguity_barriers"] = []
        loaded["browser_checkpoint"] = normalize_browser_checkpoint({})
        legacy_status = loaded.get("status")
        loaded["run_state"] = {
            "done": "completed",
            "failed": "failed",
            "aborted": "cancelled",
        }.get(legacy_status, "authorization_pending")
        loaded["authorization_status"] = (
            "pending" if legacy_status == "in_progress" else "corrupt"
        )
        loaded["authorization_source"] = "none"
        if legacy_status == "in_progress":
            append_log(
                loaded, "authorization_migration",
                note="legacy_active_run_requires_authorization",
            )
        return loaded

    defaults = {
        "run_id": str(loaded.get("task_id") or uuid.uuid4().hex),
        "provider_id": str(loaded.get("provider") or ""),
        "model_id": str(loaded.get("model") or ""),
        "plan_revision": 1,
        "authorization_revision": 0,
        "policy_epoch": 0,
        "run_state": "authorization_pending",
        "authorization_status": "corrupt",
        "authorization_source": "none",
        "grant_refs": [],
        "capability_summary": [],
        "authorization_request": None,
        "pending_scope_extension": None,
        "handled_authorization_requests": [],
        "revocation": {"revoked": False, "revoked_at": None, "reason_code": ""},
        "operation_records": [],
        "ambiguity_barriers": [],
        "browser_checkpoint": normalize_browser_checkpoint({}),
        "source_conversation_id": "",
        "source_turn_id": "",
        "source_request_id": "",
        "source_assistant_message_id": "",
        "source_permission_epoch": 0,
        "attached_tool_attempt_id": "",
        "terminal_response": "",
    }
    for key, value in defaults.items():
        loaded.setdefault(key, value)
    loaded["document_sources"] = normalize_document_sources(
        loaded.get("document_sources")
    )
    if not execution_state_is_valid(loaded):
        loaded["run_state"] = "authorization_pending"
        loaded["authorization_status"] = "corrupt"
        loaded["grant_refs"] = []
    return loaded


# ── Projections (read-only — pure derived views, never mutate) ───────────────

def count_progress(st: dict) -> "tuple[int, int, str]":
    """Progress counters for a task_state: ``(done, total, phase_name)``.

    The single source of truth shared by the GUI panel (``main.py``) and the
    mobile WS snapshot (``server.py``) so the two can never drift apart.

    ``done`` counts every task whose ``status == "done"``. ``total`` counts a
    task only when it is **done** *or* sits in a phase **at/after the cursor**
    (the live tail). Replan-superseded phases — the failed / never-run phases
    ``_replan`` leaves behind when it APPENDS a fresh tail without pruning — sit
    *before* the cursor and hold no done tasks, so they are excluded and the
    counter no longer inflates (WOR-45: ``5 / 5`` instead of ``5 / 12``).
    ``phase_name`` is the name of the phase under the cursor (``""`` when the
    cursor is out of range). Pure reads — safe on any thread.
    """
    phases = st.get("phases") or []
    cursor = st.get("cursor") or {}
    cur_p = int(cursor.get("phase", 0) or 0)
    done = 0
    total = 0
    for i, ph in enumerate(phases):
        for tk in (ph.get("tasks") or []):
            is_done = tk.get("status") == "done"
            if is_done:
                done += 1
            if is_done or i >= cur_p:  # live tail + all completed; excludes superseded
                total += 1
    phase_name = ""
    if 0 <= cur_p < len(phases):
        phase_name = phases[cur_p].get("name", "") or ""
    return done, total, phase_name


# ── Persistence ────────────────────────────────────────────────────────────

def load_task_state() -> "dict | None":
    """Load the persisted task state, or ``None`` when there is none.

    Graceful by design — returns ``None`` (never raises) when the file is
    missing, unreadable, or corrupt (e.g. a half-written/interrupted save). The
    atomic write below guarantees that an in-flight save is never observed as a
    partial file, so this read needs no lock.
    """
    try:
        if not TASK_STATE_FILE.exists():
            return None
        loaded = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return None
        loaded = _migrate_loaded_state(loaded)
        current = diagnostics.normalize_diagnostic(
            loaded.get("failure_diagnostic")
        )
        history = [
            item for item in (
                diagnostics.normalize_diagnostic(entry)
                for entry in (loaded.get("diagnostic_history") or [])
            ) if item is not None
        ][-diagnostics.MAX_DIAGNOSTIC_HISTORY:]
        loaded["failure_diagnostic"] = current
        loaded["diagnostic_history"] = history
        # Checkpoints predating WOR-177 get a deterministic compatibility
        # language exactly once; process LANG never influences resume.
        loaded["language"] = (
            normalize_locale(loaded.get("language")) or i18n.DEFAULT_LANG
        )
        loaded["language_mode"] = (
            loaded.get("language_mode")
            if loaded.get("language_mode") in {
                "single-language", "translation", "multilingual",
            }
            else "single-language"
        )
        loaded["allowed_languages"] = list(dict.fromkeys(
            code for code in (
                normalize_locale(item)
                for item in (loaded.get("allowed_languages") or ())
            ) if code
        )) or [loaded["language"]]
        loaded["source_language"] = (
            normalize_locale(loaded.get("source_language")) or ""
        )
        loaded["target_language"] = (
            normalize_locale(loaded.get("target_language")) or ""
        )
        loaded["document_sources"] = normalize_document_sources(
            loaded.get("document_sources")
        )
        loaded["artifacts"] = document_artifacts.normalize_references(
            loaded.get("artifacts")
        )
        known_artifact_ids = {
            ref["artifact_id"] for ref in loaded["artifacts"]
        }
        for phase in loaded.get("phases") or []:
            if not isinstance(phase, dict):
                continue
            for task in phase.get("tasks") or []:
                if isinstance(task, dict):
                    task.setdefault("tool_attempt_id", "")
                    task.setdefault("tool_operation_id", "")
                    task.setdefault("tool_operation_revision", 0)
                    task.setdefault("tool_durable_state", "")
                    task.setdefault("tool_next_reconciliation_at", None)
                    task.setdefault("tool_terminal_reason", "")
                    task.setdefault("tool_continuation_claimed", False)
                    task["artifact_ids"] = [
                        artifact_id
                        for artifact_id in _normalize_artifact_ids(
                            task.get("artifact_ids")
                        )
                        if artifact_id in known_artifact_ids
                    ]
        # Old terminal checkpoints predate WOR-173. Preserve compatibility and
        # make their unknown leaf cause explicit instead of inventing one.
        if loaded.get("status") == "failed" and current is None:
            current = diagnostics.make_diagnostic(
                loaded,
                stage=diagnostics.AutonomousStage.ORCHESTRATOR,
                category=diagnostics.FailureCategory.UNKNOWN_FAILURE,
                reason_code="legacy_failure",
                resumable=False,
                checkpoint_present=True,
            )
            loaded["failure_diagnostic"] = current
            loaded["diagnostic_history"] = (history + [dict(current)])[
                -diagnostics.MAX_DIAGNOSTIC_HISTORY:
            ]
        return loaded
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # UnicodeDecodeError (a ValueError subclass, NOT an OSError) is raised by
        # read_text() when the file holds non-UTF-8 bytes (binary corruption /
        # power-loss). Catch it too so load stays truly graceful — the atomic
        # write only guarantees clean UTF-8 for OUR writes, not the reader's
        # robustness to a pre-existing/externally-corrupted file.
        return None


def save_task_state(state: dict) -> None:
    """Persist the task state atomically, stamping ``updated_at`` to now.

    Ensures the config dir exists, refreshes ``updated_at``, then writes under
    the module lock via :func:`_write_atomic` (temp file + ``os.replace``).
    """
    with _lock:
        TASK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        candidates = [
            candidate for candidate in (
                _user_cancelled_terminal_authority(
                    state.get("terminal_authority"), state,
                ),
                _persisted_terminal_authority(state),
            ) if candidate is not None
        ]
        if candidates:
            _apply_user_cancelled_terminal_authority(
                state,
                max(candidates, key=lambda item: item["revision"]),
            )
        state["updated_at"] = _now_iso()
        _write_atomic(TASK_STATE_FILE, state)
        if state.get("status") in {"done", "aborted"} or state.get(
            "run_state"
        ) in {"completed", "cancelled"}:
            document_artifacts.cleanup_state_artifacts(state)
            document_sources.get_source_store().release_bound_run(
                [
                    item["source_ref"]
                    for item in normalize_document_sources(
                        state.get("document_sources")
                    )
                ],
                run_owner_id=str(state.get("task_id") or ""),
                run_id=str(state.get("run_id") or ""),
            )
            state["document_sources"] = []
            _write_atomic(TASK_STATE_FILE, state)


def clear_task_state() -> bool:
    """Delete the persisted task-state file. Returns True if it existed and was
    removed, False otherwise. Best-effort — never raises.

    Not used by Phase 1's wiring, but kept here so the state layer owns its own
    lifecycle (a later phase deletes the file once a task is fully ``done``).
    """
    with _lock:
        try:
            if not TASK_STATE_FILE.exists():
                return False
            existing = load_task_state()
            TASK_STATE_FILE.unlink()
            if existing is not None:
                document_artifacts.cleanup_state_artifacts(existing)
                document_sources.get_source_store().release_bound_run(
                    [
                        item["source_ref"]
                        for item in normalize_document_sources(
                            existing.get("document_sources")
                        )
                    ],
                    run_owner_id=str(existing.get("task_id") or ""),
                    run_id=str(existing.get("run_id") or ""),
                )
            return True
        except OSError:
            return False
