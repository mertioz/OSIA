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

Schema (``schema_version == 1``)::

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
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from osia.autonomous import diagnostics
from osia import document_artifacts, i18n
from osia.turn_context import normalize_locale

# Imported (never recomputed) so the autonomous harness, the GUI and the mobile
# server all resolve the SAME config/data dir. CONFIG_DIR is a str → wrap in
# Path, mirroring osia/server/server.py. See CLAUDE.md "VALEURS CRITIQUES".
from osia.config import CONFIG_DIR as _CONFIG_DIR

# ── Storage layout ─────────────────────────────────────────────────────────

TASK_STATE_FILE = Path(_CONFIG_DIR) / "task_state.json"
SCHEMA_VERSION = 1

# Default budgets — bounded autonomy: at most N attempts per task and N replans
# overall (the planner/orchestrator in later phases enforce these).
DEFAULT_MAX_ATTEMPTS_PER_TASK = 3
DEFAULT_MAX_REPLANS = 2

# Re-entrant so a future caller can save() while already holding the lock.
_lock = threading.RLock()
_DOCUMENT_PATH_RE = re.compile(
    r"(?i)(?:(?P<quote>[\"'`])(?:[A-Z]:[\\/]|/)[^\"'`\r\n]+?"
    r"\.(?:pdf|png|jpe?g)(?P=quote)|(?:[A-Z]:[\\/]|/)"
    r"[^\"'`\r\n]+?\.(?:pdf|png|jpe?g))"
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


def redact_document_paths(value) -> str:
    """Remove absolute local document paths from persisted/status text."""

    text = str(value or "")
    return _DOCUMENT_PATH_RE.sub("[local document]", text)


def _checkpoint_projection(value):
    """Drop runtime-only source strings before any Autonomous disk write."""

    if isinstance(value, dict):
        return {
            key: _checkpoint_projection(item)
            for key, item in value.items()
            if not str(key).startswith("_runtime_")
        }
    if isinstance(value, list):
        return [_checkpoint_projection(item) for item in value]
    if isinstance(value, str):
        return redact_document_paths(value)
    return value


def _normalize_task(index: int, task) -> dict:
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
    normalized = {
        "id": str(task.get("id") or f"t{index}"),
        "objective": redact_document_paths(raw_objective),
        "success_criterion": redact_document_paths(raw_criterion),
        "status": task.get("status") or "pending",
        "attempts": int(task.get("attempts", 0) or 0),
        "distilled_summary": task.get("distilled_summary", "") or "",
        "last_error": task.get("last_error", None),
        "artifact_ids": _normalize_artifact_ids(task.get("artifact_ids")),
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


def _normalize_phase(phase) -> dict:
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
        "name": redact_document_paths(name),
        "status": status or "pending",
        "tasks": [_normalize_task(i, t) for i, t in enumerate(raw_tasks)],
    }


# ── Constructors ───────────────────────────────────────────────────────────

def new_task_state(objective: str, provider: str, model: str, phases, *,
                   language: str | None = None) -> dict:
    """Build a fresh ``task_state`` dict (NOT persisted — caller saves).

    ``phases`` is a loose list of phase specs (each a name string or a
    ``{name, status, tasks}`` dict); it is normalised into the strict schema so
    callers never hand-build nested dicts. The new task starts ``in_progress``
    with the cursor at ``(phase=0, task=0)``, default budgets, an empty
    ``context_digest`` and an empty ``log``.
    """
    now = _now_iso()
    safe_objective = redact_document_paths(objective)
    result = {
        "schema_version": SCHEMA_VERSION,
        "task_id": uuid.uuid4().hex,
        "objective": safe_objective,
        "status": "in_progress",
        "created_at": now,
        "updated_at": now,
        "provider": provider or "",
        "model": model or "",
        "language": normalize_locale(language) or i18n.DEFAULT_LANG,
        "cursor": {"phase": 0, "task": 0},
        "budgets": {
            "max_attempts_per_task": DEFAULT_MAX_ATTEMPTS_PER_TASK,
            "max_replans": DEFAULT_MAX_REPLANS,
            "replan_count": 0,
        },
        "context_digest": "",
        "artifacts": [],
        "phases": [_normalize_phase(p) for p in (phases or [])],
        "log": [],
        "failure_diagnostic": None,
        "diagnostic_history": [],
    }
    if safe_objective != (objective or ""):
        result["_runtime_objective"] = objective or ""
    return result


def new_failed_task_state(provider: str, model: str, diagnostic: dict, *,
                          language: str | None = None) -> dict:
    """Build a terminal, objective-free state for a failure before planning.

    Planning and invalid-resume failures have no executable plan to checkpoint.
    Keeping their state objective-free makes the diagnostic durable without
    copying a user prompt or other arbitrary content into the failure record.
    """
    st = new_task_state("", provider, model, [], language=language)
    st["status"] = "failed"
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
            task["status"] = status
            for key, value in fields.items():
                task[key] = value
    return state


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
        state["updated_at"] = _now_iso()
        _write_atomic(TASK_STATE_FILE, state)
        if state.get("status") in {"done", "aborted"}:
            document_artifacts.cleanup_state_artifacts(state)
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
            return True
        except OSError:
            return False
