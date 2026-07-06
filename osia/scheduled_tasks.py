#!/usr/bin/env python3
"""Backend-verified scheduled-task tools for OSIA.

This module is the single write path used by chat tools for ``cron_jobs``.
It does not schedule anything itself: the existing ``osia.cron.CronManager``
continues to poll and execute the jobs stored in settings.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from osia import config
from osia.memory import MemorySecretError, reject_secret_like_entry


ERROR_VALIDATION = "validation_error"
ERROR_SECRET = "secret_rejected"
ERROR_NOT_FOUND = "not_found"
ERROR_UNSUPPORTED = "unsupported_schedule"
ERROR_STORAGE = "storage_error"

_TASK_ID_RE = re.compile(r"^cron_[A-Za-z0-9_-]{8,}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_SUPPORTED_SOURCES = {"chat", "settings", "mobile_settings"}
_CHANGE_CALLBACK: Callable[[], None] | None = None

_UNSAFE_MARKUP_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_result|tool_code|think|thinking|system|developer)\b",
    re.IGNORECASE,
)
_BARE_TOOL_JSON_RE = re.compile(r'(?is)\{\s*"tool"\s*:\s*"[^"]+"\s*,')
_SYSTEM_PROMPT_RE = re.compile(
    r"(?is)\b(?:system prompt|developer message|ignore previous instructions|"
    r"(?:regles|r\u00e8gles) absolues|absolute rules)\b"
)


class ScheduledTaskError(Exception):
    """Structured error surfaced as ``ok:false`` to the agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def set_change_callback(callback: Callable[[], None] | None) -> None:
    """Register a best-effort notification hook for config refresh broadcasts."""

    global _CHANGE_CALLBACK
    _CHANGE_CALLBACK = callback


def _notify_changed() -> None:
    callback = _CHANGE_CALLBACK
    if callback is None:
        return
    try:
        callback()
    except Exception as exc:
        print(f"[SCHEDULED_TASKS] change callback failed: {exc}")


def _now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).astimezone().isoformat(timespec="seconds")


def _new_task_id() -> str:
    return "cron_" + secrets.token_urlsafe(12).rstrip("=")


def _local_timezone_name() -> str:
    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, "key", None)
    if key:
        return str(key)
    name = datetime.now().astimezone().tzname()
    return str(name or "local")


def _time_parts(value: Any) -> tuple[int, int]:
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        raise ScheduledTaskError(
            ERROR_VALIDATION,
            "time must be a valid 24-hour HH:MM value",
        )
    return int(match.group(1)), int(match.group(2))


def _format_time(value: Any) -> str:
    hour, minute = _time_parts(value)
    return f"{hour:02d}:{minute:02d}"


def _parse_run_at(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ScheduledTaskError(ERROR_VALIDATION, "run_at is required")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduledTaskError(
            ERROR_VALIDATION,
            "run_at must be an ISO datetime like 2026-07-15T09:00:00",
        ) from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _timezone_is_local(timezone_name: str, reference: datetime) -> bool:
    name = str(timezone_name or "").strip()
    if not name or name.lower() in {"local", "localtime"}:
        return True
    local_offset = reference.astimezone().utcoffset()
    try:
        zone_offset = reference.replace(tzinfo=ZoneInfo(name)).utcoffset()
    except ZoneInfoNotFoundError:
        return name in {str(tz) for tz in time.tzname if tz}
    except Exception:
        return False
    return zone_offset == local_offset


def _normalize_timezone(value: Any, reference: datetime) -> str:
    if value in (None, ""):
        return _local_timezone_name()
    timezone_name = str(value or "").strip()
    if not _timezone_is_local(timezone_name, reference):
        raise ScheduledTaskError(
            ERROR_UNSUPPORTED,
            "Only the local PC timezone is supported by the current scheduler",
        )
    return timezone_name


def _normalize_unit(value: Any) -> str:
    unit = str(value or "").strip().lower()
    if unit.endswith("s"):
        unit = unit[:-1]
    if unit not in {"minute", "hour", "day"}:
        raise ScheduledTaskError(
            ERROR_VALIDATION,
            "unit must be one of minutes, hours, or days",
        )
    return unit


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduledTaskError(ERROR_VALIDATION, f"{field} must be an integer") from exc
    if number <= 0:
        raise ScheduledTaskError(ERROR_VALIDATION, f"{field} must be greater than zero")
    return number


def normalize_schedule(schedule: Any, *, now: datetime | None = None) -> tuple[str, str]:
    """Return ``(canonical_schedule, timezone)`` after backend validation."""

    if not isinstance(schedule, dict):
        raise ScheduledTaskError(ERROR_VALIDATION, "schedule must be an object")
    current = (now or _now()).replace(second=0, microsecond=0)
    schedule_type = str(schedule.get("type") or "").strip().lower()
    if not schedule_type:
        raise ScheduledTaskError(ERROR_VALIDATION, "schedule.type is required")

    if schedule_type == "once":
        run_at = _parse_run_at(schedule.get("run_at"))
        timezone_name = _normalize_timezone(schedule.get("timezone"), run_at)
        if run_at <= current:
            raise ScheduledTaskError(
                ERROR_VALIDATION,
                "run_at must be in the future",
            )
        return f"once:{run_at.strftime('%Y-%m-%dT%H:%M')}", timezone_name

    timezone_name = _normalize_timezone(schedule.get("timezone"), current)

    if schedule_type == "daily":
        return f"at {_format_time(schedule.get('time'))}", timezone_name

    if schedule_type == "interval":
        every = _positive_int(schedule.get("every"), "every")
        unit = _normalize_unit(schedule.get("unit"))
        time_value = schedule.get("time")
        if time_value not in (None, ""):
            if unit != "day":
                raise ScheduledTaskError(
                    ERROR_UNSUPPORTED,
                    "wall-clock times are only supported for day intervals",
                )
            at_time = _format_time(time_value)
            if every == 1:
                return f"at {at_time}", timezone_name
            return f"every {every} days at {at_time}", timezone_name
        plural = unit + "s"
        return f"every {every} {plural}", timezone_name

    if schedule_type == "weekly":
        days = schedule.get("days_of_week") or []
        if isinstance(days, str):
            days = [days]
        clean_days = [
            str(day or "").strip().lower()[:3]
            for day in days
            if str(day or "").strip()
        ]
        if not clean_days or any(day not in _DAY_NAMES for day in clean_days):
            raise ScheduledTaskError(
                ERROR_VALIDATION,
                "days_of_week must contain values like mon, tue, wed",
            )
        raise ScheduledTaskError(
            ERROR_UNSUPPORTED,
            "weekly schedules are not supported by the current cron runtime",
        )

    if schedule_type == "cron":
        raise ScheduledTaskError(
            ERROR_UNSUPPORTED,
            "cron expressions are not supported by the current cron runtime",
        )

    raise ScheduledTaskError(
        ERROR_UNSUPPORTED,
        "schedule.type must be once, daily, interval, weekly, or cron",
    )


def _reject_unsafe_text(title: str, instruction: str) -> None:
    combined = f"{title}\n{instruction}"
    if _UNSAFE_MARKUP_RE.search(combined) or _BARE_TOOL_JSON_RE.search(combined):
        raise ScheduledTaskError(
            ERROR_VALIDATION,
            "scheduled task content cannot contain tool or reasoning markup",
        )
    if _SYSTEM_PROMPT_RE.search(combined):
        raise ScheduledTaskError(
            ERROR_VALIDATION,
            "scheduled task content cannot contain hidden/system instructions",
        )
    try:
        reject_secret_like_entry({"title": title, "task": instruction})
    except MemorySecretError as exc:
        raise ScheduledTaskError(ERROR_SECRET, str(exc)) from exc


def _result_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def _result_ok(**payload: Any) -> dict[str, Any]:
    result = {"ok": True}
    result.update(payload)
    return result


def _load_jobs() -> list[dict[str, Any]]:
    raw = config.get_setting("cron_jobs", []) or []
    if not isinstance(raw, list):
        return []
    return [copy.deepcopy(job) for job in raw if isinstance(job, dict)]


def _save_jobs(jobs: list[dict[str, Any]]) -> None:
    config.set_setting("cron_jobs", copy.deepcopy(jobs))


def _normalize_existing_job(
    job: dict[str, Any],
    *,
    source_default: str = "settings",
    touch: bool = False,
) -> tuple[dict[str, Any], bool]:
    changed = False
    clean = dict(job)
    task = str(clean.get("task") or "").strip()
    schedule = str(clean.get("schedule") or "").strip()

    if not _TASK_ID_RE.match(str(clean.get("id") or "")):
        clean["id"] = _new_task_id()
        changed = True
    if not str(clean.get("title") or "").strip():
        clean["title"] = task[:80] or "Scheduled task"
        changed = True
    if clean.get("task") != task:
        clean["task"] = task
        changed = True
    if clean.get("schedule") != schedule:
        clean["schedule"] = schedule
        changed = True
    if "enabled" not in clean:
        clean["enabled"] = False
        changed = True
    else:
        enabled = bool(clean.get("enabled"))
        if clean.get("enabled") is not enabled:
            clean["enabled"] = enabled
            changed = True
    if clean.get("conversation_id") in ("", "null"):
        clean["conversation_id"] = None
        changed = True
    if clean.get("source") not in _SUPPORTED_SOURCES:
        clean["source"] = source_default
        changed = True
    if not str(clean.get("timezone") or "").strip():
        clean["timezone"] = _local_timezone_name()
        changed = True
    if not clean.get("created_at"):
        clean["created_at"] = _iso()
        changed = True
    if touch or not clean.get("updated_at"):
        clean["updated_at"] = _iso()
        changed = True
    if "last_run_at" not in clean:
        clean["last_run_at"] = None
        changed = True
    if "last_status" not in clean:
        clean["last_status"] = None
        changed = True
    return clean, changed


def _load_normalized_jobs(*, persist: bool = True) -> list[dict[str, Any]]:
    jobs = _load_jobs()
    normalized: list[dict[str, Any]] = []
    changed = False
    for job in jobs:
        clean, job_changed = _normalize_existing_job(job)
        normalized.append(clean)
        changed = changed or job_changed
    if changed and persist:
        _save_jobs(normalized)
        _notify_changed()
    return normalized


def _find_index(jobs: list[dict[str, Any]], task_id: str) -> int:
    for idx, job in enumerate(jobs):
        if str(job.get("id") or "") == task_id:
            return idx
    return -1


def _job_summary(job: dict[str, Any]) -> str:
    title = str(job.get("title") or "Scheduled task").strip()
    schedule = str(job.get("schedule") or "").strip()
    status = "enabled" if job.get("enabled") else "disabled"
    return f"{title} ({schedule}, {status})"


def create_scheduled_task(
    *,
    title: str | None = None,
    instruction: str | None = None,
    schedule: dict[str, Any] | None = None,
    enabled: bool = True,
    conversation_id: str | None = None,
    source: str = "chat",
    now: datetime | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    try:
        task_text = str(instruction or "").strip()
        clean_title = str(title or "").strip() or task_text[:80] or "Scheduled task"
        if not task_text:
            raise ScheduledTaskError(ERROR_VALIDATION, "instruction is required")
        _reject_unsafe_text(clean_title, task_text)
        canonical, timezone_name = normalize_schedule(schedule, now=now)
        clean_source = source if source in _SUPPORTED_SOURCES else "chat"
        created_at = _iso()
        job = {
            "id": _new_task_id(),
            "title": clean_title,
            "task": task_text,
            "schedule": canonical,
            "enabled": bool(enabled),
            "conversation_id": conversation_id or None,
            "source": clean_source,
            "timezone": timezone_name,
            "created_at": created_at,
            "updated_at": created_at,
            "last_run_at": None,
            "last_status": None,
        }
        jobs = _load_normalized_jobs()
        jobs.append(job)
        _save_jobs(jobs)
        verified = _load_jobs()
        if _find_index(verified, job["id"]) < 0:
            raise ScheduledTaskError(
                ERROR_STORAGE,
                "scheduled task was not readable after write",
            )
        _notify_changed()
        return _result_ok(task_id=job["id"], summary=_job_summary(job), job=job)
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)
    except Exception as exc:
        return _result_error(ERROR_STORAGE, str(exc))


def list_scheduled_tasks(**_extra: Any) -> dict[str, Any]:
    try:
        jobs = _load_normalized_jobs()
        return _result_ok(tasks=jobs, count=len(jobs))
    except Exception as exc:
        return _result_error(ERROR_STORAGE, str(exc))


def update_scheduled_task(
    *,
    task_id: str | None = None,
    title: str | None = None,
    instruction: str | None = None,
    schedule: dict[str, Any] | None = None,
    enabled: bool | None = None,
    conversation_id: str | None = None,
    source: str | None = None,
    now: datetime | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    try:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            raise ScheduledTaskError(ERROR_VALIDATION, "task_id is required")
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, clean_id)
        if idx < 0:
            raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
        job = dict(jobs[idx])
        next_title = str(title).strip() if title is not None else str(job.get("title") or "")
        next_task = (
            str(instruction).strip()
            if instruction is not None else str(job.get("task") or "").strip()
        )
        if not next_task:
            raise ScheduledTaskError(ERROR_VALIDATION, "instruction cannot be empty")
        _reject_unsafe_text(next_title, next_task)
        job["title"] = next_title or next_task[:80] or "Scheduled task"
        job["task"] = next_task
        if schedule is not None:
            canonical, timezone_name = normalize_schedule(schedule, now=now)
            job["schedule"] = canonical
            job["timezone"] = timezone_name
        if enabled is not None:
            job["enabled"] = bool(enabled)
        if conversation_id is not None:
            job["conversation_id"] = conversation_id or None
        if source is not None and source in _SUPPORTED_SOURCES:
            job["source"] = source
        job["updated_at"] = _iso()
        jobs[idx] = job
        _save_jobs(jobs)
        verified = _load_jobs()
        if _find_index(verified, clean_id) < 0:
            raise ScheduledTaskError(
                ERROR_STORAGE,
                "scheduled task was not readable after update",
            )
        _notify_changed()
        return _result_ok(task_id=clean_id, summary=_job_summary(job), job=job)
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)
    except Exception as exc:
        return _result_error(ERROR_STORAGE, str(exc))


def delete_scheduled_task(
    *,
    task_id: str | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    try:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            raise ScheduledTaskError(ERROR_VALIDATION, "task_id is required")
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, clean_id)
        if idx < 0:
            raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
        removed = jobs.pop(idx)
        _save_jobs(jobs)
        _notify_changed()
        return _result_ok(
            task_id=clean_id,
            summary=f"Deleted {_job_summary(removed)}",
            deleted=True,
        )
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)
    except Exception as exc:
        return _result_error(ERROR_STORAGE, str(exc))


def replace_scheduled_tasks_from_settings(
    jobs: Any,
    *,
    source_default: str = "mobile_settings",
) -> dict[str, Any]:
    """Normalize a whole settings-list write while preserving metadata."""

    try:
        if not isinstance(jobs, list):
            raise ScheduledTaskError(ERROR_VALIDATION, "cron_jobs must be a list")
        normalized = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            clean, _changed = _normalize_existing_job(
                job,
                source_default=source_default,
                touch=True,
            )
            normalized.append(clean)
        _save_jobs(normalized)
        _notify_changed()
        return _result_ok(jobs=normalized, count=len(normalized))
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)
    except Exception as exc:
        return _result_error(ERROR_STORAGE, str(exc))


def parse_scheduled_task_tool_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        data = result
    else:
        text = str(result or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict) and isinstance(data.get("ok"), bool):
        return data
    return None


def claims_scheduled_task_saved(text: str) -> bool:
    folded = str(text or "").casefold()
    negative_words = (
        "not scheduled",
        "could not schedule",
        "couldn't schedule",
        "was not scheduled",
        "wasn't scheduled",
        "pas planifie",
        "pas planifi\u00e9",
        "pas programm\u00e9",
        "pas programme",
        "n'ai pas pu planifier",
    )
    if any(word in folded for word in negative_words):
        return False
    success_words = (
        "scheduled",
        "planifie",
        "planifi\u00e9",
        "programm\u00e9",
        "programme",
        "cron created",
        "task created",
        "reminder set",
        "rappel est",
        "rappel a ete",
        "rappel a \u00e9t\u00e9",
    )
    return any(word in folded for word in success_words)


def looks_like_schedule_request(text: str) -> bool:
    folded = str(text or "").casefold()
    hints = (
        "every day",
        "every ",
        "daily",
        "schedule",
        "scheduled task",
        "remind me",
        "rappelle-moi",
        "rappelle moi",
        "tous les jours",
        "toutes les",
        "chaque jour",
        "planifie",
        "planifie-moi",
        "programme",
    )
    return any(hint in folded for hint in hints)


def not_scheduled_response(lang: str = "en") -> str:
    if lang == "fr":
        return "Je n'ai pas pu planifier cette tache."
    return "I could not schedule that task."
