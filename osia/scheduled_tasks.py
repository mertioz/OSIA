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
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from osia import config
from osia.memory import MemorySecretError, reject_secret_like_entry


ERROR_VALIDATION = "validation_error"
ERROR_SECRET = "secret_rejected"
ERROR_NOT_FOUND = "not_found"
ERROR_UNSUPPORTED = "unsupported_schedule"
ERROR_STORAGE = "storage_error"
ERROR_CONVERSATION_NOT_FOUND = "conversation_not_found"
ERROR_CONVERSATION_AMBIGUOUS = "conversation_ambiguous"
ERROR_MODEL_NOT_FOUND = "model_not_found"
ERROR_MODEL_AMBIGUOUS = "model_ambiguous"

_TASK_ID_RE = re.compile(r"^cron_[A-Za-z0-9_-]{8,}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_SUPPORTED_SOURCES = {"chat", "settings", "mobile_settings"}
_CHANGE_CALLBACK: Callable[[], None] | None = None
_CHANGE_CALLBACKS: list[Callable[[], None]] = []

_UNSAFE_MARKUP_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_result|tool_code|think|thinking|system|developer)\b",
    re.IGNORECASE,
)
_BARE_TOOL_JSON_RE = re.compile(r'(?is)\{\s*"tool"\s*:\s*"[^"]+"\s*,')
_SYSTEM_PROMPT_RE = re.compile(
    r"(?is)\b(?:system prompt|developer message|ignore previous instructions|"
    r"(?:regles|r\u00e8gles) absolues|absolute rules)\b"
)

_SCHEDULE_OBJECT_RE = re.compile(
    r"\b(?:scheduled tasks?|schedules?|reminders?|cron jobs?|crons?|"
    r"taches? planifiees?|taches? programmees?|rappels?)\b"
)
_SCHEDULE_CREATE_RE = re.compile(
    r"\b(?:remind me|set (?:me )?(?:a )?reminder|"
    r"create (?:a )?(?:scheduled task|reminder)|"
    r"rappelle(?:-moi| moi)|planifie(?:-moi)?|programme-moi)\b|"
    r"(?:^|\b(?:please|to|me|you)\s+)schedule\b"
)
_EXPLICIT_TEMPORAL_RE = re.compile(
    r"\b(?:every|each)\s+(?:(?:\d+|one|two|three)\s+)?"
    r"(?:minutes?|hours?|days?|weeks?|months?)\b|"
    r"\b(?:daily|hourly|weekly|monthly)\b|"
    r"\b(?:tomorrow|tonight)\s+(?:at\s+)?\d{1,2}(?::\d{2})?\b|"
    r"\bin\s+\d+\s+(?:minutes?|hours?|days?|weeks?)\b|"
    r"\b(?:tous les|toutes les|chaque)\s+"
    r"(?:(?:\d+|un|une|deux|trois)\s+)?"
    r"(?:minutes?|heures?|jours?|semaines?|mois)\b|"
    r"\bquotidiennement\b|"
    r"\b(?:demain|ce soir)\s+(?:a\s+)?\d{1,2}(?:h\d{0,2}|:\d{2})?\b|"
    r"\bdans\s+\d+\s+(?:minutes?|heures?|jours?|semaines?)\b"
)
_SCHEDULE_LIST_RE = re.compile(
    r"\b(?:list|show|display|what|which|liste|affiche|montre|quels?|quelles?)\b"
)
_SCHEDULE_UPDATE_RE = re.compile(
    r"\b(?:update|change|edit|modify|reschedule|move|postpone|"
    r"mets? a jour|change|modifie|replanifie|decale|reporte)\b"
)
_SCHEDULE_DELETE_RE = re.compile(
    r"\b(?:delete|remove|cancel|stop|supprime|annule|arrete)\b"
)
_SCHEDULE_FAILURE_WORDS = (
    "not scheduled",
    "could not schedule",
    "couldn't schedule",
    "was not scheduled",
    "wasn't scheduled",
    "pas planifie",
    "pas programme",
    "n'ai pas pu planifier",
)


class ScheduledTaskError(Exception):
    """Structured error surfaced as ``ok:false`` to the agent."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def set_change_callback(callback: Callable[[], None] | None) -> None:
    """Replace change hooks.

    Kept for older tests/call sites. Runtime code should prefer
    add_change_callback() so the scheduler wake hook and mobile config
    broadcast hook can coexist.
    """

    global _CHANGE_CALLBACK
    _CHANGE_CALLBACKS.clear()
    _CHANGE_CALLBACK = callback
    if callback is not None:
        _CHANGE_CALLBACKS.append(callback)


def add_change_callback(callback: Callable[[], None]) -> Callable[[], None]:
    """Register one best-effort change hook and return an unsubscribe function."""

    if callback not in _CHANGE_CALLBACKS:
        _CHANGE_CALLBACKS.append(callback)

    def _unsubscribe() -> None:
        try:
            _CHANGE_CALLBACKS.remove(callback)
        except ValueError:
            pass

    return _unsubscribe


def _notify_changed() -> None:
    for callback in list(_CHANGE_CALLBACKS):
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


def _fold_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip()


def _time_from_phrase(hour: str, minute: str | None = None) -> str:
    h = int(hour)
    m = int(minute or 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduledTaskError(
            ERROR_VALIDATION,
            "time must be a valid 24-hour HH:MM value",
        )
    return f"{h:02d}:{m:02d}"


def _schedule_object_from_text(text: Any, *, now: datetime) -> dict[str, Any]:
    raw = str(text or "").strip()
    folded = _fold_text(raw)
    if not folded:
        raise ScheduledTaskError(ERROR_VALIDATION, "schedule is required")

    # Canonical strings emitted by the desktop/mobile settings editors.
    m = re.match(r"^once:(\d{4}-\d{2}-\d{2}t\d{1,2}:\d{2})$", folded)
    if m:
        return {"type": "once", "run_at": m.group(1).replace("t", "T")}
    m = re.match(r"^(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)$", folded)
    if m:
        return {"type": "daily", "time": _time_from_phrase(m.group(1), m.group(2))}
    m = re.match(
        r"^every\s+(\d+)\s*days?\s+at\s+([01]?\d|2[0-3]):([0-5]\d)$",
        folded,
    )
    if m:
        return {
            "type": "interval",
            "every": int(m.group(1)),
            "unit": "days",
            "time": _time_from_phrase(m.group(2), m.group(3)),
        }
    m = re.match(r"^every\s+(\d+)\s*(minute|hour|day)s?$", folded)
    if m:
        return {"type": "interval", "every": int(m.group(1)), "unit": m.group(2)}

    # French/English natural reminders used by chat-created tasks.
    m = re.search(
        r"\b(?:dans|in)\s+(\d+)\s+"
        r"(minutes?|mins?|heures?|hours?|jours?|days?)\b",
        folded,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith(("minute", "min")):
            delta = timedelta(minutes=n)
        elif unit.startswith(("heure", "hour")):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        run_at = (now + delta).replace(second=0, microsecond=0)
        return {"type": "once", "run_at": run_at.isoformat(timespec="minutes")}

    time_pattern = r"([01]?\d|2[0-3])\s*(?:h|:)\s*([0-5]\d)?"
    m = re.search(
        r"\b(?:tous les|chaque|every)\s+(\d+)\s+"
        r"(?:jours?|days?)\s+(?:a|at)?\s*" + time_pattern,
        folded,
    )
    if m:
        return {
            "type": "interval",
            "every": int(m.group(1)),
            "unit": "days",
            "time": _time_from_phrase(m.group(2), m.group(3)),
        }

    m = re.search(
        r"\b(?:tous les jours|chaque jour|every day|daily)\s+"
        r"(?:a|at)?\s*" + time_pattern,
        folded,
    )
    if m:
        return {"type": "daily", "time": _time_from_phrase(m.group(1), m.group(2))}

    raise ScheduledTaskError(
        ERROR_VALIDATION,
        "schedule could not be parsed; use once, daily, or every N minutes/hours/days",
    )


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

    current = (now or _now()).replace(second=0, microsecond=0)
    if isinstance(schedule, str):
        schedule = _schedule_object_from_text(schedule, now=current)
    if not isinstance(schedule, dict):
        raise ScheduledTaskError(ERROR_VALIDATION, "schedule must be an object")
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


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def next_run_at_for_schedule(
    canonical_schedule: str,
    *,
    now: datetime | None = None,
    after: datetime | None = None,
) -> str | None:
    """Return the next local run time for a canonical cron schedule."""

    from osia.cron import parse_schedule

    current = (after or now or _now()).replace(second=0, microsecond=0)
    spec = parse_schedule(canonical_schedule)
    if spec is None:
        return None
    if spec["kind"] == "once":
        return spec["dt"].isoformat(timespec="minutes")
    if spec["kind"] == "interval":
        return (current + timedelta(seconds=spec["seconds"])).isoformat(
            timespec="minutes"
        )
    if spec["kind"] == "daily":
        candidate = current.replace(
            hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate.isoformat(timespec="minutes")
    if spec["kind"] == "day_interval_at":
        candidate = current.replace(
            hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0
        )
        if candidate <= current:
            candidate += timedelta(days=spec["days"])
        return candidate.isoformat(timespec="minutes")
    return None


def _model_match_text(*parts: Any) -> str:
    return " ".join(_fold_text(part) for part in parts if str(part or "").strip())


def _model_metadata_from_match(match: dict[str, Any]) -> dict[str, str]:
    return {
        "provider": str(match.get("provider") or "").strip(),
        "id": str(match.get("id") or "").strip(),
        "label": str(match.get("label") or match.get("id") or "").strip(),
    }


def _model_registry_snapshot() -> tuple[list[dict[str, Any]], Callable[[str], str]]:
    try:
        from osia.models import get_all_models, provider_for_model

        return get_all_models(), provider_for_model
    except Exception:
        fallback_models = [
            {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic"},
            {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "anthropic"},
            {"id": "gpt-5.5", "label": "GPT-5.5", "provider": "openai"},
            {"id": "gpt-5.4", "label": "GPT-5.4", "provider": "openai"},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 Mini", "provider": "openai"},
            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "deepseek"},
            {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "google"},
        ]

        def _provider_for_model(model_id: str) -> str:
            lower = str(model_id or "").lower()
            for item in fallback_models:
                if item["id"] == model_id:
                    return item["provider"]
            if "claude" in lower:
                return "anthropic"
            if "gpt" in lower:
                return "openai"
            if "deepseek" in lower:
                return "deepseek"
            if "gemini" in lower:
                return "google"
            return "ollama"

        return fallback_models, _provider_for_model


def _resolve_model_metadata(
    *,
    model: Any = None,
    model_id: Any = None,
    model_provider: Any = None,
    model_query: Any = None,
    default_model: Any = None,
    explicit: bool = False,
) -> dict[str, str] | None:
    raw_id = str(model_id or "").strip()
    raw_provider = str(model_provider or "").strip()
    raw_query = str(model_query or "").strip()

    if isinstance(model, dict):
        raw_id = raw_id or str(
            model.get("id") or model.get("model") or model.get("model_id") or ""
        ).strip()
        raw_provider = raw_provider or str(
            model.get("provider") or model.get("model_provider") or ""
        ).strip()
        raw_query = raw_query or str(model.get("query") or model.get("label") or "").strip()
    elif model not in (None, ""):
        raw_query = raw_query or str(model).strip()

    if not explicit and not (raw_id or raw_provider or raw_query):
        if isinstance(default_model, dict):
            raw_id = str(default_model.get("id") or default_model.get("model") or "").strip()
            raw_provider = str(default_model.get("provider") or "").strip()
        elif default_model not in (None, ""):
            raw_id = str(default_model).strip()
    if not (raw_id or raw_provider or raw_query):
        return None

    models, provider_for_model = _model_registry_snapshot()
    candidates = [
        m for m in models
        if isinstance(m, dict)
        and str(m.get("id") or "").strip()
        and (not raw_provider or str(m.get("provider") or "").strip() == raw_provider)
    ]

    if raw_id:
        exact = [m for m in candidates if str(m.get("id") or "") == raw_id]
        if len(exact) == 1:
            return _model_metadata_from_match(exact[0])
        if len(exact) > 1:
            raise ScheduledTaskError(
                ERROR_MODEL_AMBIGUOUS,
                "model id matches more than one provider",
                candidates=[_model_metadata_from_match(m) for m in exact[:8]],
            )
        if not raw_provider and not explicit:
            provider = provider_for_model(raw_id)
            return {"provider": provider, "id": raw_id, "label": raw_id}

    query = raw_query or raw_id
    folded_query = _fold_text(query)
    if folded_query:
        query_tokens = [t for t in re.split(r"\W+", folded_query) if t]
        matches = []
        for m in candidates:
            haystack = _model_match_text(
                m.get("provider"), m.get("id"), m.get("label"), m.get("name")
            )
            if all(token in haystack for token in query_tokens):
                matches.append(m)
        exact_label = [
            m for m in matches
            if folded_query in {
                _fold_text(m.get("id")),
                _fold_text(m.get("label")),
                _fold_text(m.get("name")),
            }
        ]
        if len(exact_label) == 1:
            return _model_metadata_from_match(exact_label[0])
        if len(matches) == 1:
            return _model_metadata_from_match(matches[0])
        if len(matches) > 1:
            raise ScheduledTaskError(
                ERROR_MODEL_AMBIGUOUS,
                "model request is ambiguous; specify the provider or full model name",
                candidates=[_model_metadata_from_match(m) for m in matches[:8]],
            )

    if explicit:
        raise ScheduledTaskError(
            ERROR_MODEL_NOT_FOUND,
            "requested model was not found",
        )
    if raw_id:
        provider = raw_provider or provider_for_model(raw_id)
        return {"provider": provider, "id": raw_id, "label": raw_id}
    return None


def _normalize_model_field(job: dict[str, Any]) -> tuple[dict[str, str] | None, bool]:
    if "model" not in job and not (job.get("model_id") or job.get("model_provider")):
        return None, False
    try:
        meta = _resolve_model_metadata(
            model=job.get("model"),
            model_id=job.get("model_id"),
            model_provider=job.get("model_provider"),
            explicit=False,
        )
    except ScheduledTaskError:
        return None, True
    return meta, job.get("model") != meta


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


def _result_error(code: str, message: str, **details: Any) -> dict[str, Any]:
    result = {"ok": False, "code": code, "message": message}
    result.update(details)
    return result


def _result_ok(**payload: Any) -> dict[str, Any]:
    result = {"ok": True}
    result.update(payload)
    return result


def _has_conversation_ref(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text and text.casefold() != "null")


def _resolve_conversation_ref(
    *,
    conversation_id: str | None = None,
    conversation_title: str | None = None,
    conversation_query: str | None = None,
) -> tuple[str | None, str | None]:
    if not (
        _has_conversation_ref(conversation_id)
        or _has_conversation_ref(conversation_title)
        or _has_conversation_ref(conversation_query)
    ):
        return None, None
    from osia import conversations

    resolved = conversations.resolve_conversation_reference(
        conversation_id=conversation_id,
        title=conversation_title,
        query=conversation_query,
    )
    if resolved.get("ok") is True:
        return (
            str(resolved.get("conversation_id") or "").strip() or None,
            str(resolved.get("title") or "").strip() or None,
        )
    code = str(resolved.get("code") or ERROR_CONVERSATION_NOT_FOUND)
    if code not in {ERROR_CONVERSATION_NOT_FOUND, ERROR_CONVERSATION_AMBIGUOUS}:
        code = ERROR_CONVERSATION_NOT_FOUND
    details = {}
    candidates = resolved.get("candidates")
    if isinstance(candidates, list):
        details["candidates"] = candidates
    raise ScheduledTaskError(
        code,
        str(resolved.get("message") or "conversation could not be resolved"),
        **details,
    )


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
    model_meta, model_changed = _normalize_model_field(clean)
    if model_changed:
        if model_meta:
            clean["model"] = model_meta
        else:
            clean.pop("model", None)
        changed = True
    next_run_dt = _parse_dt(clean.get("next_run_at"))
    schedule_marker = str(clean.get("next_run_schedule") or "")
    should_recompute_next = (
        not next_run_dt
        or schedule_marker != schedule
        or (touch and bool(clean.get("enabled")) and next_run_dt < _now())
    )
    if should_recompute_next:
        next_run = next_run_at_for_schedule(schedule)
        if clean.get("next_run_at") != next_run:
            clean["next_run_at"] = next_run
            changed = True
        if clean.get("next_run_schedule") != schedule:
            clean["next_run_schedule"] = schedule
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
    conversation_title: str | None = None,
    conversation_query: str | None = None,
    source: str = "chat",
    model: Any = None,
    model_id: str | None = None,
    model_provider: str | None = None,
    model_query: str | None = None,
    active_model: Any = None,
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
        explicit_model = any(
            str(value or "").strip()
            for value in (model, model_id, model_provider, model_query)
        )
        model_meta = _resolve_model_metadata(
            model=model,
            model_id=model_id,
            model_provider=model_provider,
            model_query=model_query,
            default_model=active_model,
            explicit=explicit_model,
        )
        clean_source = source if source in _SUPPORTED_SOURCES else "chat"
        clean_conversation_id = conversation_id or None
        resolved_conversation_title = None
        if clean_source == "chat":
            clean_conversation_id, resolved_conversation_title = _resolve_conversation_ref(
                conversation_id=conversation_id,
                conversation_title=conversation_title,
                conversation_query=conversation_query,
            )
        created_at = _iso()
        job = {
            "id": _new_task_id(),
            "title": clean_title,
            "task": task_text,
            "schedule": canonical,
            "enabled": bool(enabled),
            "conversation_id": clean_conversation_id,
            "source": clean_source,
            "timezone": timezone_name,
            "created_at": created_at,
            "updated_at": created_at,
            "last_run_at": None,
            "last_status": None,
            "next_run_at": next_run_at_for_schedule(canonical, now=now),
            "next_run_schedule": canonical,
        }
        if model_meta:
            job["model"] = model_meta
        if resolved_conversation_title:
            job["conversation_title"] = resolved_conversation_title
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
        return _result_error(exc.code, exc.message, **exc.details)
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
    conversation_title: str | None = None,
    conversation_query: str | None = None,
    source: str | None = None,
    model: Any = None,
    model_id: str | None = None,
    model_provider: str | None = None,
    model_query: str | None = None,
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
        clean_source = source if source in _SUPPORTED_SOURCES else str(job.get("source") or "chat")
        job["title"] = next_title or next_task[:80] or "Scheduled task"
        job["task"] = next_task
        if schedule is not None:
            canonical, timezone_name = normalize_schedule(schedule, now=now)
            job["schedule"] = canonical
            job["timezone"] = timezone_name
            job["next_run_at"] = next_run_at_for_schedule(canonical, now=now)
            job["next_run_schedule"] = canonical
        if enabled is not None:
            job["enabled"] = bool(enabled)
            if bool(enabled) and _parse_dt(job.get("next_run_at")) is None:
                job["next_run_at"] = next_run_at_for_schedule(
                    str(job.get("schedule") or ""), now=now
                )
                job["next_run_schedule"] = str(job.get("schedule") or "")
        explicit_model = any(
            value is not None
            for value in (model, model_id, model_provider, model_query)
        )
        if explicit_model:
            model_meta = _resolve_model_metadata(
                model=model,
                model_id=model_id,
                model_provider=model_provider,
                model_query=model_query,
                explicit=True,
            )
            if model_meta:
                job["model"] = model_meta
        if (
            conversation_id is not None
            or _has_conversation_ref(conversation_title)
            or _has_conversation_ref(conversation_query)
        ):
            if clean_source == "chat":
                resolved_id, resolved_title = _resolve_conversation_ref(
                    conversation_id=conversation_id,
                    conversation_title=conversation_title,
                    conversation_query=conversation_query,
                )
                job["conversation_id"] = resolved_id
                if resolved_title:
                    job["conversation_title"] = resolved_title
            else:
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
        return _result_error(exc.code, exc.message, **exc.details)
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
    """Return whether a reply claims that OSIA changed a scheduled task.

    Scheduling vocabulary also appears in ordinary informational prose
    (``programme``, ``planifié``, ``scheduled``).  Match an acknowledgement or
    a concrete task/reminder claim instead of treating any occurrence of those
    words as proof of a scheduler action.
    """

    folded = _fold_text(text)
    if any(word in folded for word in _SCHEDULE_FAILURE_WORDS):
        return False
    success_patterns = (
        r"^\s*(?:scheduled|planifie|programme)[.!]?\s*$",
        r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?) (?:successfully )?"
        r"(?:scheduled|programmed|created|updated|deleted|set)\b",
        r"\b(?:the|this|that|your|my) "
        r"(?:task|reminder|scheduled job|event) "
        r"(?:(?:has|have) been|was|is now) "
        r"(?:scheduled|programmed|created|updated|deleted|set)\b",
        r"\b(?:cron|task) created\b|\breminder set\b",
        r"\b(?:j['’ ]ai|nous avons) (?:bien )?"
        r"(?:planifie|programme|cree|mis a jour|supprime)\b",
        r"\bc['’ ]est (?:bien )?(?:planifie|programme)\b",
        r"\b(?:le|la|ce|cette|votre|vos|ton|ta|tes|mon|ma|mes) "
        r"(?:rappel|tache|taches|evenement) "
        r"(?:a ete|ont ete|est maintenant|sont maintenant) "
        r"(?:planifie|programme|cree|mis a jour|supprime)\b",
    )
    return any(re.search(pattern, folded) for pattern in success_patterns)


def is_schedule_failure_response(text: str) -> bool:
    """Return whether text reports a scheduling failure."""

    folded = _fold_text(text)
    return any(word in folded for word in _SCHEDULE_FAILURE_WORDS)


def allows_scheduled_task_tool(text: str, tool_name: str) -> bool:
    """Authorize a scheduler tool only for explicit scheduler intent.

    Immediate sequencing words such as ``then``, ``after that`` and ``puis``
    carry no scheduling meaning by themselves.  Creation is authorized by an
    explicit reminder/scheduling command or a concrete future/recurring time;
    management tools additionally require both the matching operation and a
    scheduled-task/reminder noun.
    """

    folded = _fold_text(text)
    if not folded:
        return False
    if tool_name == "create_scheduled_task":
        return bool(
            _SCHEDULE_CREATE_RE.search(folded)
            or _EXPLICIT_TEMPORAL_RE.search(folded)
        )
    if not _SCHEDULE_OBJECT_RE.search(folded):
        return False
    if tool_name == "list_scheduled_tasks":
        return bool(_SCHEDULE_LIST_RE.search(folded))
    if tool_name == "update_scheduled_task":
        return bool(_SCHEDULE_UPDATE_RE.search(folded))
    if tool_name == "delete_scheduled_task":
        return bool(_SCHEDULE_DELETE_RE.search(folded))
    return False


def looks_like_schedule_request(text: str) -> bool:
    return any(
        allows_scheduled_task_tool(text, tool_name)
        for tool_name in (
            "create_scheduled_task",
            "list_scheduled_tasks",
            "update_scheduled_task",
            "delete_scheduled_task",
        )
    )


def not_scheduled_response(lang: str = "en") -> str:
    if lang == "fr":
        return "Je n'ai pas pu planifier cette tache."
    return "I could not schedule that task."
