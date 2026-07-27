#!/usr/bin/env python3
"""Backend-verified scheduled-task tools for OSIA.

This module is the single write path used by chat tools for ``cron_jobs``.
It does not schedule anything itself: the existing ``osia.cron.CronManager``
continues to poll and execute the jobs stored in settings.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from osia import config
from osia.memory import MemorySecretError, reject_secret_like_entry
from osia.scope_normalization import canonical_hash, canonical_serialize


ERROR_VALIDATION = "validation_error"
ERROR_SECRET = "secret_rejected"
ERROR_NOT_FOUND = "not_found"
ERROR_UNSUPPORTED = "unsupported_schedule"
ERROR_STORAGE = "storage_error"
ERROR_CONVERSATION_NOT_FOUND = "conversation_not_found"
ERROR_CONVERSATION_AMBIGUOUS = "conversation_ambiguous"
ERROR_MODEL_NOT_FOUND = "model_not_found"
ERROR_MODEL_AMBIGUOUS = "model_ambiguous"
ERROR_AUTHORIZATION_REQUIRED = "authorization_required"
ERROR_AUTHORIZATION_STALE = "authorization_stale"
ERROR_AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
ERROR_PC_REVIEW_REQUIRED = "pc_local_review_required"

SCHEDULED_TASK_SCHEMA_VERSION = 2
AUTHORIZATION_PROTOCOL_VERSION = 1
AUTHORIZATION_REQUIRED = "authorization_required"
AUTHORIZED = "authorized"
AUTHORIZATION_EXPIRED = "authorization_expired"
AUTHORIZATION_REVOKED = "authorization_revoked"
AUTHORIZATION_EDITED = "authorization_invalidated_by_edit"
BLOCKED_OUTCOME_UNKNOWN = "blocked_outcome_unknown"
DISABLED = "disabled"
RUNNING = "running"

_AUTHORIZATION_STATES = {
    AUTHORIZATION_REQUIRED,
    AUTHORIZED,
    AUTHORIZATION_EXPIRED,
    AUTHORIZATION_REVOKED,
    AUTHORIZATION_EDITED,
    BLOCKED_OUTCOME_UNKNOWN,
    DISABLED,
    RUNNING,
}
_MOBILE_SAFE_CATEGORIES = {"safe_local_setting"}
_PUBLIC_CAPABILITY_CATEGORIES = {
    "read_only", "dynamic_scope_requires_review", "safe_local_setting",
    "local_path", "directory_tree", "local_paths", "connector_operation",
    "local_command", "tool_operation", "sensitive_scope",
}
_MAX_EXECUTION_RECORDS = 64
_DEFAULT_GRANT_TTL_SECONDS = 30 * 24 * 60 * 60

_TASK_ID_RE = re.compile(r"^cron_[A-Za-z0-9_-]{8,}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_SUPPORTED_SOURCES = {"chat", "settings", "mobile_settings"}
_CHANGE_CALLBACK: Callable[[], None] | None = None
_CHANGE_CALLBACKS: list[Callable[[], None]] = []
_JOBS_LOCK = threading.RLock()
_AUTHORIZATION_LOCK = threading.RLock()
_PENDING_AUTHORIZATIONS: dict[str, dict[str, Any]] = {}
_RESPONDED_AUTHORIZATIONS: list[str] = []

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


def normalize_task_instruction(value: Any) -> str:
    """Return the stable instruction representation used by the task hash."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


_READ_ONLY_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:remind\s+me\s+to\s+)?(?:return|reply|respond|say|"
    r"summari[sz]e|compare|report|tell(?:\s+me)?|list|show|read|get|what|who|"
    r"when|where|how|rappelle(?:-moi|\s+moi)?\s+de\s+|retourne|reponds?|"
    r"dis(?:-moi)?|resume|compare|rapporte|liste|montre|lis|quel|quelle|quand|"
    r"ou|comment)\b",
    re.IGNORECASE,
)
_SIDE_EFFECT_WORD_RE = re.compile(
    r"\b(?:send|email|message|post|publish|buy|purchase|order|book|reserve|"
    r"delete|remove|erase|move|rename|write|edit|modify|create|upload|download|"
    r"install|uninstall|run|execute|launch|open|navigate|click|fill|type|set|"
    r"change|kill|stop|start|shutdown|restart|envoyer|courriel|message|publier|"
    r"acheter|commander|reserver|supprimer|effacer|deplacer|renommer|ecrire|"
    r"modifier|creer|televerser|telecharger|installer|executer|lancer|ouvrir|"
    r"naviguer|cliquer|remplir|saisir|regler|changer|tuer|arreter|demarrer)\b",
    re.IGNORECASE,
)
_SET_VOLUME_RE = re.compile(
    r"\b(?:set|change|adjust|regle(?:r)?|mets?|mettre|change)\b[^\n]{0,40}"
    r"\b(?:volume|son)\b[^0-9]{0,16}(\d{1,3})\s*%?",
    re.IGNORECASE,
)


def _derived_exact_calls(instruction: str) -> list[dict[str, Any]]:
    """Derive only deterministic, exact calls; unknown targets stay ungranted."""

    match = _SET_VOLUME_RE.search(_fold_text(instruction))
    if match:
        level = int(match.group(1))
        if 0 <= level <= 100:
            return [{"tool": "set_volume", "arguments": {"level": level}}]
    return []


def _deterministically_read_only(instruction: str) -> bool:
    folded = _fold_text(instruction)
    if not folded or _SIDE_EFFECT_WORD_RE.search(folded):
        return False
    return bool(_READ_ONLY_PREFIX_RE.search(folded))


def _model_identity(job: dict[str, Any]) -> dict[str, str]:
    model = job.get("model") if isinstance(job.get("model"), dict) else {}
    return {
        "provider": str(model.get("provider") or "").strip(),
        "id": str(model.get("id") or model.get("model") or "").strip(),
    }


def _conversation_binding(value: Any) -> str:
    clean = str(value or "").strip()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest() if clean else ""


def canonical_task_definition(job: dict[str, Any]) -> dict[str, Any]:
    """Return the timestamp/title-free execution definition for hashing."""

    constraints = job.get("capability_constraints")
    if not isinstance(constraints, list):
        constraints = []
    safe_constraints = []
    for item in constraints:
        if not isinstance(item, dict):
            continue
        safe_constraints.append({
            "capability_id": str(item.get("capability_id") or ""),
            "tool_category": str(item.get("tool_category") or ""),
            "effect_class": str(item.get("effect_class") or ""),
            "safe_category": str(item.get("safe_category") or ""),
            "call_binding": str(item.get("call_binding") or ""),
        })
    safe_constraints.sort(key=lambda item: canonical_serialize(item))
    return {
        "instruction": normalize_task_instruction(job.get("task")),
        "schedule": str(job.get("schedule") or "").strip(),
        "timezone": str(job.get("timezone") or "").strip(),
        "model": _model_identity(job),
        "capability_constraints": safe_constraints,
        "target_binding": str(job.get("target_binding") or ""),
        "conversation_binding": _conversation_binding(job.get("conversation_id")),
        "desired_enabled": bool(job.get("desired_enabled", job.get("enabled", False))),
        "execution_mode": str(job.get("execution_mode") or "agent"),
    }


def task_definition_hash(job: dict[str, Any]) -> str:
    return canonical_hash(canonical_task_definition(job))


def is_bound_scheduled_context(context: Any) -> bool:
    """Identify WOR-195 contexts without changing legacy policy test fixtures."""

    return bool(
        _TASK_ID_RE.match(str(getattr(context, "scheduled_task_id", "") or ""))
        and re.fullmatch(r"[1-9][0-9]*", str(getattr(context, "task_revision", "") or ""))
        and re.fullmatch(
            r"[0-9a-f]{64}:[0-9]+",
            str(getattr(context, "plan_revision", "") or ""),
        )
    )


def _authorization_binding(job: dict[str, Any], authorization_revision: int | None = None) -> str:
    revision = (
        int(job.get("authorization_revision", 0))
        if authorization_revision is None else int(authorization_revision)
    )
    return f"{str(job.get('definition_hash') or '')}:{revision}"


def _permission_snapshot() -> tuple[Any | None, Any | None]:
    try:
        from osia.action_policy import get_default_permission_policy

        policy = get_default_permission_policy()
        return policy, policy.store.load_state()
    except Exception:
        return None, None


def _safe_capability_summary(job: dict[str, Any]) -> list[str]:
    values = job.get("capability_constraints")
    if isinstance(values, list):
        categories = sorted({
            (
                str(item.get("safe_category") or item.get("tool_category") or "")
                if str(item.get("safe_category") or item.get("tool_category") or "")
                in _PUBLIC_CAPABILITY_CATEGORIES
                else "sensitive_scope"
            )
            for item in values if isinstance(item, dict)
            and str(item.get("safe_category") or item.get("tool_category") or "")
        })
        if categories:
            return categories
    if str(job.get("execution_mode") or "") == "read_only":
        return ["read_only"]
    return ["dynamic_scope_requires_review"]


def _constraint_metadata_for_calls(
    calls: list[dict[str, Any]],
    *,
    context=None,
    policy=None,
) -> tuple[list[dict[str, str]], list[Any]]:
    """Build exact policy requests while retaining only value-free task metadata."""

    from osia.action_policy import build_permission_request

    metadata: list[dict[str, str]] = []
    requests = []
    for call in calls:
        if not isinstance(call, dict):
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_REQUIRED,
                "capability proposal is incomplete",
            )
        tool = str(call.get("tool") or "").strip()
        arguments = call.get("arguments", {})
        if not tool or not isinstance(arguments, dict):
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_REQUIRED,
                "capability proposal is incomplete",
            )
        request = build_permission_request(
            tool,
            arguments,
            confirmation_domain="action",
            origin_id="osia",
            execution_context=context,
            force_sensitive=True,
            policy=policy,
        )
        if not request.private_scope.grantable:
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_REQUIRED,
                "dynamic or unknown targets require later authorization",
            )
        binding = request.normalized_call_hash
        metadata.append({
            "capability_id": canonical_hash({
                "tool": request.tool_identity,
                "call_binding": binding,
            })[:32],
            "tool_category": request.tool_category,
            "effect_class": request.effect_class,
            "safe_category": request.safe_category,
            "call_binding": binding,
        })
        requests.append(request)
    metadata.sort(key=lambda item: canonical_serialize(item))
    return metadata, requests


def _derived_constraint_metadata(instruction: str) -> list[dict[str, str]]:
    calls = _derived_exact_calls(instruction)
    if not calls:
        return []
    try:
        metadata, _requests = _constraint_metadata_for_calls(calls)
        return metadata
    except Exception:
        return []


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
    original_schema = clean.get("schema_version")
    task = normalize_task_instruction(clean.get("task"))
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
    desired_enabled = bool(clean.get("desired_enabled", clean.get("enabled", False)))
    if clean.get("desired_enabled") is not desired_enabled:
        clean["desired_enabled"] = desired_enabled
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
    expected_mode = "read_only" if _deterministically_read_only(task) else "agent"
    if clean.get("execution_mode") != expected_mode:
        clean["execution_mode"] = expected_mode
        changed = True

    constraints = clean.get("capability_constraints")
    constraints_valid = isinstance(constraints, list) and all(
        isinstance(item, dict)
        and all(isinstance(item.get(name, ""), str) for name in (
            "capability_id", "tool_category", "effect_class",
            "safe_category", "call_binding",
        ))
        for item in constraints
    )
    if not constraints_valid:
        constraints = _derived_constraint_metadata(task)
        clean["capability_constraints"] = constraints
        changed = True
    expected_target_binding = (
        canonical_hash(sorted(
            str(item.get("call_binding") or "")
            for item in constraints if isinstance(item, dict)
        ))
        if constraints else ""
    )
    if clean.get("target_binding") != expected_target_binding:
        clean["target_binding"] = expected_target_binding
        changed = True

    policy, permission_state = _permission_snapshot()
    current_epoch = int(getattr(permission_state, "policy_epoch", 0) or 0)
    full_access = str(getattr(permission_state, "permission_mode", "")) in {
        "PermissionMode.FULL_ACCESS", "full_access",
    }
    schema_is_current = original_schema == SCHEDULED_TASK_SCHEMA_VERSION
    legacy_schema = original_schema is None
    unknown_schema = not legacy_schema and not schema_is_current
    if unknown_schema:
        unknown_was_authorized = bool(
            clean.get("grant_refs")
            or clean.get("enabled")
            or clean.get("authorization_status") not in {
                None, AUTHORIZATION_REQUIRED,
            }
        )
        revoked_epoch = _revoke_grant_refs(clean.get("grant_refs"))
        if unknown_was_authorized and policy is not None:
            try:
                revoked_epoch = int(policy.store.invalidate_active_permits())
            except Exception:
                pass
        if clean.get("authorization_status") != AUTHORIZATION_REQUIRED:
            clean["authorization_status"] = AUTHORIZATION_REQUIRED
            changed = True
        if clean.get("enabled") is not False:
            clean["enabled"] = False
            changed = True
        clean["schema_state"] = "unknown_schema"
        clean["grant_refs"] = []
        clean["policy_epoch"] = revoked_epoch
        clean.setdefault("task_revision", 1)
        clean.setdefault("authorization_revision", 0)
        clean["definition_hash"] = task_definition_hash(clean)
        clean["capability_summary"] = _safe_capability_summary(clean)
    else:
        auth_status = clean.get("authorization_status")
        task_revision = clean.get("task_revision")
        authorization_revision = clean.get("authorization_revision")
        grant_refs = clean.get("grant_refs")
        stored_hash = str(clean.get("definition_hash") or "")
        expected_hash = task_definition_hash(clean)
        authorization_fields_valid = (
            schema_is_current
            and type(task_revision) is int and task_revision >= 1
            and type(authorization_revision) is int and authorization_revision >= 0
            and type(clean.get("policy_epoch")) is int
            and auth_status in _AUTHORIZATION_STATES
            and isinstance(grant_refs, list)
            and all(isinstance(ref, str) and bool(ref) for ref in grant_refs)
            and bool(re.fullmatch(r"[0-9a-f]{64}", stored_hash))
        )
        if legacy_schema:
            clean["schema_version"] = SCHEDULED_TASK_SCHEMA_VERSION
            clean["task_revision"] = 1
            clean["authorization_revision"] = 0
            clean["policy_epoch"] = current_epoch
            clean["grant_refs"] = []
            clean["definition_hash"] = expected_hash
            clean["authorization_status"] = (
                AUTHORIZED if desired_enabled and (expected_mode == "read_only" or full_access)
                else DISABLED if not desired_enabled
                else AUTHORIZATION_REQUIRED
            )
            clean["enabled"] = bool(
                desired_enabled and clean["authorization_status"] == AUTHORIZED
            )
            clean["migration_state"] = (
                "read_only_proven" if expected_mode == "read_only"
                else "full_access" if full_access
                else "authorization_required"
            )
            changed = True
        elif not authorization_fields_valid or stored_hash != expected_hash:
            revoked_epoch = _revoke_grant_refs(
                grant_refs if isinstance(grant_refs, list) else []
            )
            if policy is not None:
                try:
                    revoked_epoch = int(policy.store.invalidate_active_permits())
                except Exception:
                    pass
            try:
                next_task_revision = max(1, int(task_revision)) + 1
            except (TypeError, ValueError, OverflowError):
                next_task_revision = 1
            try:
                next_authorization_revision = max(0, int(authorization_revision)) + 1
            except (TypeError, ValueError, OverflowError):
                next_authorization_revision = 1
            clean.update({
                "schema_version": SCHEDULED_TASK_SCHEMA_VERSION,
                "task_revision": next_task_revision,
                "authorization_revision": next_authorization_revision,
                "definition_hash": expected_hash,
                "policy_epoch": revoked_epoch,
                "authorization_status": AUTHORIZATION_EDITED,
                "grant_refs": [],
                "enabled": False,
                "authorization_expires_at": None,
                "authorization_bounds": None,
            })
            clean.pop("authorization_request", None)
            clean["migration_state"] = "authorization_fields_failed_closed"
            changed = True
        else:
            clean["schema_version"] = SCHEDULED_TASK_SCHEMA_VERSION
            if clean.get("policy_epoch") != current_epoch:
                clean["policy_epoch"] = current_epoch
                changed = True
            if not desired_enabled:
                if clean.get("authorization_status") != DISABLED:
                    clean["authorization_status"] = DISABLED
                    changed = True
                if clean.get("enabled") is not False:
                    clean["enabled"] = False
                    changed = True
            elif clean.get("authorization_status") not in {AUTHORIZED, RUNNING}:
                if clean.get("enabled") is not False:
                    clean["enabled"] = False
                    changed = True
            elif clean.get("enabled") is not True:
                clean["enabled"] = True
                changed = True
        clean["capability_summary"] = _safe_capability_summary(clean)

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
    changed = changed or clean != job
    return clean, changed


def _load_normalized_jobs(*, persist: bool = True) -> list[dict[str, Any]]:
    with _JOBS_LOCK:
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


def _revoke_grant_refs(grant_refs: Any) -> int:
    """Idempotently revoke opaque refs and return the resulting policy epoch."""

    policy, state = _permission_snapshot()
    epoch = int(getattr(state, "policy_epoch", 0) or 0)
    if policy is None:
        return epoch
    for grant_ref in tuple(str(ref or "") for ref in (grant_refs or ())):
        if not grant_ref:
            continue
        try:
            result = policy.store.revoke_grant(grant_ref)
            epoch = int(result.policy_epoch)
        except Exception:
            # Clearing ordinary refs is still fail-closed; the protected store
            # remains the authority and a surviving mismatched grant cannot bind
            # to the incremented task/authorization revision.
            pass
    try:
        epoch = int(policy.store.load_state().policy_epoch)
    except Exception:
        pass
    return epoch


def _invalidate_authorization(
    job: dict[str, Any],
    *,
    status: str,
    increment_task_revision: bool,
) -> dict[str, Any]:
    clean = dict(job)
    refs = list(clean.get("grant_refs") or [])
    epoch = _revoke_grant_refs(refs)
    policy, _state = _permission_snapshot()
    if policy is not None:
        try:
            epoch = int(policy.store.invalidate_active_permits())
        except Exception:
            pass
    if increment_task_revision:
        clean["task_revision"] = max(1, int(clean.get("task_revision", 1))) + 1
    clean["authorization_revision"] = (
        max(0, int(clean.get("authorization_revision", 0))) + 1
    )
    clean.update({
        "grant_refs": [],
        "policy_epoch": epoch,
        "authorization_status": status,
        "enabled": False,
        "authorization_expires_at": None,
        "authorization_bounds": None,
        "updated_at": _iso(),
    })
    clean.pop("authorization_request", None)
    return clean


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
        execution_mode = (
            "read_only" if _deterministically_read_only(task_text) else "agent"
        )
        capability_constraints = _derived_constraint_metadata(task_text)
        _policy, permission_state = _permission_snapshot()
        policy_epoch = int(getattr(permission_state, "policy_epoch", 0) or 0)
        full_access = str(getattr(permission_state, "permission_mode", "")) in {
            "PermissionMode.FULL_ACCESS", "full_access",
        }
        desired_enabled = bool(enabled)
        may_enable = desired_enabled and (
            execution_mode == "read_only" or full_access
        )
        job = {
            "schema_version": SCHEDULED_TASK_SCHEMA_VERSION,
            "id": _new_task_id(),
            "title": clean_title,
            "task": task_text,
            "schedule": canonical,
            "desired_enabled": desired_enabled,
            "enabled": may_enable,
            "conversation_id": clean_conversation_id,
            "source": clean_source,
            "timezone": timezone_name,
            "execution_mode": execution_mode,
            "capability_constraints": capability_constraints,
            "target_binding": (
                canonical_hash(sorted(
                    str(item.get("call_binding") or "")
                    for item in capability_constraints
                ))
                if capability_constraints else ""
            ),
            "task_revision": 1,
            "authorization_revision": 0,
            "policy_epoch": policy_epoch,
            "authorization_status": (
                AUTHORIZED if may_enable else DISABLED if not desired_enabled
                else AUTHORIZATION_REQUIRED
            ),
            "grant_refs": [],
            "authorization_expires_at": None,
            "authorization_bounds": None,
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
        job["definition_hash"] = task_definition_hash(job)
        job["capability_summary"] = _safe_capability_summary(job)
        with _JOBS_LOCK:
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


def scheduler_jobs_snapshot() -> list[dict[str, Any]]:
    """The existing scheduler's normalized, migrated storage snapshot."""

    return _load_normalized_jobs()


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
        with _JOBS_LOCK:
            jobs = _load_normalized_jobs()
            idx = _find_index(jobs, clean_id)
            if idx < 0:
                raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
            job = dict(jobs[idx])
        previous_hash = str(job.get("definition_hash") or task_definition_hash(job))
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
        if instruction is not None:
            job["execution_mode"] = (
                "read_only" if _deterministically_read_only(next_task) else "agent"
            )
            job["capability_constraints"] = _derived_constraint_metadata(next_task)
            job["target_binding"] = (
                canonical_hash(sorted(
                    str(item.get("call_binding") or "")
                    for item in job["capability_constraints"]
                ))
                if job["capability_constraints"] else ""
            )
        if schedule is not None:
            canonical, timezone_name = normalize_schedule(schedule, now=now)
            job["schedule"] = canonical
            job["timezone"] = timezone_name
            job["next_run_at"] = next_run_at_for_schedule(canonical, now=now)
            job["next_run_schedule"] = canonical
        if enabled is not None:
            job["desired_enabled"] = bool(enabled)
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
        next_hash = task_definition_hash(job)
        if next_hash != previous_hash:
            job = _invalidate_authorization(
                job,
                status=(
                    AUTHORIZATION_EDITED
                    if bool(job.get("desired_enabled", False)) else DISABLED
                ),
                increment_task_revision=True,
            )
            job["definition_hash"] = next_hash
            _forget_pending_for_task(clean_id)
        else:
            job["definition_hash"] = previous_hash
            job["updated_at"] = _iso()
        job["capability_summary"] = _safe_capability_summary(job)
        with _JOBS_LOCK:
            jobs = _load_normalized_jobs()
            idx = _find_index(jobs, clean_id)
            if idx < 0:
                raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
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
        with _JOBS_LOCK:
            jobs = _load_normalized_jobs()
            idx = _find_index(jobs, clean_id)
            if idx < 0:
                raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
            removed = jobs.pop(idx)
            _revoke_grant_refs(removed.get("grant_refs"))
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
        with _JOBS_LOCK:
            existing = _load_normalized_jobs()
            existing_by_id = {
                str(item.get("id") or ""): item for item in existing
                if str(item.get("id") or "")
            }
            normalized = []
            retained_ids: set[str] = set()
            for incoming in jobs:
                if not isinstance(incoming, dict):
                    continue
                incoming_id = str(incoming.get("id") or "").strip()
                previous = existing_by_id.get(incoming_id)
                if previous is None:
                    raw = {
                        key: copy.deepcopy(incoming.get(key))
                        for key in (
                            "title", "task", "schedule", "enabled",
                            "conversation_id", "model", "timezone",
                        ) if key in incoming
                    }
                    raw["source"] = source_default
                    clean, _changed = _normalize_existing_job(
                        raw,
                        source_default=source_default,
                        touch=True,
                    )
                    normalized.append(clean)
                    retained_ids.add(str(clean.get("id") or ""))
                    continue

                retained_ids.add(incoming_id)
                candidate = dict(previous)
                redacted_round_trip = incoming.get("task_redacted") is True
                for key in ("title", "task", "schedule", "conversation_id", "timezone"):
                    if redacted_round_trip and key in {"title", "task"}:
                        continue
                    if key in incoming:
                        candidate[key] = copy.deepcopy(incoming.get(key))
                if "model" in incoming:
                    if incoming.get("model") in (None, ""):
                        candidate.pop("model", None)
                    else:
                        candidate["model"] = copy.deepcopy(incoming.get("model"))
                if "enabled" in incoming:
                    candidate["desired_enabled"] = bool(incoming.get("enabled"))
                candidate["source"] = (
                    source_default if source_default in _SUPPORTED_SOURCES
                    else str(previous.get("source") or "settings")
                )
                candidate["task"] = normalize_task_instruction(candidate.get("task"))
                task_changed = candidate["task"] != str(previous.get("task") or "")
                if task_changed:
                    candidate["execution_mode"] = (
                        "read_only"
                        if _deterministically_read_only(candidate["task"]) else "agent"
                    )
                    candidate["capability_constraints"] = (
                        _derived_constraint_metadata(candidate["task"])
                    )
                    candidate["target_binding"] = (
                        canonical_hash(sorted(
                            str(item.get("call_binding") or "")
                            for item in candidate["capability_constraints"]
                        ))
                        if candidate["capability_constraints"] else ""
                    )
                model_meta, model_changed = _normalize_model_field(candidate)
                if model_changed:
                    if model_meta:
                        candidate["model"] = model_meta
                    else:
                        candidate.pop("model", None)
                previous_hash = str(
                    previous.get("definition_hash") or task_definition_hash(previous)
                )
                next_hash = task_definition_hash(candidate)
                if next_hash != previous_hash or (
                    previous.get("schema_version") != SCHEDULED_TASK_SCHEMA_VERSION
                ):
                    candidate = _invalidate_authorization(
                        candidate,
                        status=(
                            AUTHORIZATION_EDITED
                            if bool(candidate.get("desired_enabled", False)) else DISABLED
                        ),
                        increment_task_revision=True,
                    )
                    candidate["schema_version"] = SCHEDULED_TASK_SCHEMA_VERSION
                    candidate["definition_hash"] = next_hash
                    candidate["next_run_at"] = next_run_at_for_schedule(
                        str(candidate.get("schedule") or "")
                    )
                    candidate["next_run_schedule"] = str(
                        candidate.get("schedule") or ""
                    )
                    _forget_pending_for_task(incoming_id)
                else:
                    candidate["definition_hash"] = previous_hash
                    candidate["updated_at"] = _iso()
                candidate["capability_summary"] = _safe_capability_summary(candidate)
                normalized.append(candidate)

            for removed in existing:
                removed_id = str(removed.get("id") or "")
                if removed_id and removed_id not in retained_ids:
                    _revoke_grant_refs(removed.get("grant_refs"))
            _save_jobs(normalized)
        _notify_changed()
        return _result_ok(jobs=normalized, count=len(normalized))
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)
    except Exception as exc:
        return _result_error(ERROR_STORAGE, str(exc))


def _current_job(task_id: str) -> dict[str, Any]:
    clean_id = str(task_id or "").strip()
    jobs = _load_normalized_jobs()
    idx = _find_index(jobs, clean_id)
    if idx < 0:
        raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
    return dict(jobs[idx])


def _save_current_job(job: dict[str, Any]) -> None:
    task_id = str(job.get("id") or "")
    with _JOBS_LOCK:
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, task_id)
        if idx < 0:
            raise ScheduledTaskError(ERROR_NOT_FOUND, "scheduled task not found")
        jobs[idx] = copy.deepcopy(job)
        _save_jobs(jobs)
    _notify_changed()


def _commit_authorization_fields(
    candidate: dict[str, Any], pending: dict[str, Any],
) -> dict[str, Any]:
    """Atomically reject an approval raced by an edit, then merge auth only."""

    task_id = str(pending.get("task_id") or "")
    with _JOBS_LOCK:
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, task_id)
        if idx < 0:
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "task changed")
        current = dict(jobs[idx])
        if (
            int(current.get("task_revision", 0)) != pending["task_revision"]
            or str(current.get("definition_hash") or "")
            != pending["definition_hash"]
            or int(current.get("authorization_revision", 0))
            != pending["authorization_revision"] - 1
        ):
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "task changed")
        for name in (
            "authorization_revision", "authorization_status", "enabled",
            "grant_refs", "policy_epoch", "authorization_expires_at",
            "authorization_bounds", "updated_at",
        ):
            if name in candidate:
                current[name] = copy.deepcopy(candidate[name])
        jobs[idx] = current
        _save_jobs(jobs)
    _notify_changed()
    return current


def _public_authorization_task(job: dict[str, Any]) -> dict[str, Any]:
    raw_expiration = job.get("authorization_expires_at")
    expiration = (
        float(raw_expiration)
        if isinstance(raw_expiration, (int, float))
        and not isinstance(raw_expiration, bool)
        and math.isfinite(float(raw_expiration))
        and float(raw_expiration) > 0
        else None
    )
    raw_bounds = job.get("authorization_bounds")
    bounds = None
    if isinstance(raw_bounds, dict) and raw_bounds.get("mode") in {
        "full_access", "read_only", "bounded", "recurring",
    }:
        maximum = raw_bounds.get("max_executions")
        bounds = {
            "mode": str(raw_bounds["mode"]),
            "max_executions": (
                maximum if type(maximum) is int and maximum > 0 else None
            ),
            "storage": (
                str(raw_bounds.get("storage"))
                if raw_bounds.get("storage") in {"protected", "session_only"}
                else None
            ),
        }
    raw_status = str(job.get("authorization_status") or "")
    return {
        "task_id": (
            str(job.get("id") or "")
            if _TASK_ID_RE.match(str(job.get("id") or "")) else ""
        ),
        "task_revision": (
            job["task_revision"] if type(job.get("task_revision")) is int else 0
        ),
        "definition_hash": (
            str(job.get("definition_hash") or "")
            if re.fullmatch(r"[0-9a-f]{64}", str(job.get("definition_hash") or ""))
            else ""
        ),
        "authorization_revision": (
            job["authorization_revision"]
            if type(job.get("authorization_revision")) is int else 0
        ),
        "policy_epoch": (
            job["policy_epoch"] if type(job.get("policy_epoch")) is int else 0
        ),
        "authorization_status": (
            raw_status if raw_status in _AUTHORIZATION_STATES
            else AUTHORIZATION_REQUIRED
        ),
        "capability_summary": list(_safe_capability_summary(job)),
        "authorization_expires_at": expiration,
        "authorization_bounds": bounds,
        "enabled": bool(job.get("enabled", False)),
    }


def _forget_pending_for_task(task_id: str) -> None:
    with _AUTHORIZATION_LOCK:
        stale = [
            request_id for request_id, item in _PENDING_AUTHORIZATIONS.items()
            if item.get("task_id") == task_id
        ]
        for request_id in stale:
            _PENDING_AUTHORIZATIONS.pop(request_id, None)


def _remember_responded(request_id: str) -> None:
    if request_id in _RESPONDED_AUTHORIZATIONS:
        return
    _RESPONDED_AUTHORIZATIONS.append(request_id)
    del _RESPONDED_AUTHORIZATIONS[:-256]


def begin_scheduled_task_authorization(
    task_id: str,
    *,
    calls: list[dict[str, Any]] | None = None,
    origin_ref: str = "desktop",
    mobile: bool = False,
    ttl_seconds: float = _DEFAULT_GRANT_TTL_SECONDS,
    max_executions: int | None = None,
    request_ttl_seconds: float = 300.0,
) -> dict[str, Any]:
    """Create one revision/hash/epoch-bound sanitized authorization request."""

    try:
        job = _current_job(task_id)
        if job.get("schema_version") != SCHEDULED_TASK_SCHEMA_VERSION:
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_REQUIRED,
                "unknown scheduled-task schema requires a local edit",
            )
        if job.get("authorization_status") == BLOCKED_OUTCOME_UNKNOWN:
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_REQUIRED,
                "ambiguous outcome requires explicit local reconciliation",
            )
        policy, state = _permission_snapshot()
        if policy is None or state is None:
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_UNAVAILABLE,
                "permission authority is unavailable",
            )
        full_access = str(getattr(state, "permission_mode", "")) in {
            "PermissionMode.FULL_ACCESS", "full_access",
        }
        if full_access or (
            str(job.get("execution_mode") or "") == "read_only" and not calls
        ):
            job.update({
                "authorization_status": AUTHORIZED,
                "enabled": bool(job.get("desired_enabled", False)),
                "policy_epoch": int(state.policy_epoch),
                "grant_refs": [],
                "authorization_expires_at": None,
                "authorization_bounds": {
                    "mode": "full_access" if full_access else "read_only",
                },
                "updated_at": _iso(),
            })
            _save_current_job(job)
            return _result_ok(
                already_authorized=True,
                task=_public_authorization_task(job),
            )

        exact_calls = list(calls) if calls is not None else _derived_exact_calls(
            str(job.get("task") or "")
        )
        if not exact_calls:
            raise ScheduledTaskError(
                ERROR_PC_REVIEW_REQUIRED if mobile else ERROR_AUTHORIZATION_REQUIRED,
                "unknown or dynamic targets require later PC-local authorization",
            )

        if job.get("grant_refs"):
            job = _invalidate_authorization(
                job,
                status=AUTHORIZATION_REQUIRED,
                increment_task_revision=False,
            )
            _save_current_job(job)
            state = policy.store.load_state()

        provisional_context = None
        metadata, _unused = _constraint_metadata_for_calls(
            exact_calls, context=provisional_context, policy=policy,
        )
        if metadata != job.get("capability_constraints"):
            job["capability_constraints"] = metadata
            job["target_binding"] = canonical_hash(sorted(
                item["call_binding"] for item in metadata
            ))
            job = _invalidate_authorization(
                job,
                status=AUTHORIZATION_REQUIRED,
                increment_task_revision=True,
            )
            job["definition_hash"] = task_definition_hash(job)
            job["capability_summary"] = _safe_capability_summary(job)
            _save_current_job(job)

        state = policy.store.load_state()
        next_authorization_revision = int(job.get("authorization_revision", 0)) + 1
        from osia.action_policy import ExecutionType, make_execution_context

        context = make_execution_context(
            execution_type=ExecutionType.SCHEDULED,
            interactive=False,
            scheduled_task_id=str(job["id"]),
            task_revision=str(job["task_revision"]),
            plan_revision=_authorization_binding(job),
            policy_epoch=int(state.policy_epoch),
            run_id="",
            store=policy.store,
        )
        metadata, requests = _constraint_metadata_for_calls(
            exact_calls, context=context, policy=policy,
        )
        promoted_binding = _authorization_binding(
            job, next_authorization_revision,
        )
        requests = [
            replace(
                request,
                execution_context=replace(
                    request.execution_context,
                    plan_revision=promoted_binding,
                ),
            )
            for request in requests
        ]
        if mobile and any(
            request.safe_category not in _MOBILE_SAFE_CATEGORIES
            for request in requests
        ):
            raise ScheduledTaskError(
                ERROR_PC_REVIEW_REQUIRED,
                "private scope details require PC-local review",
            )
        ttl = max(60.0, min(float(ttl_seconds), 366 * 24 * 60 * 60))
        now_ts = time.time()
        grant_expires_at = now_ts + ttl
        request_expires_at = now_ts + max(
            30.0, min(float(request_ttl_seconds), 900.0)
        )
        if max_executions is not None:
            if type(max_executions) is not int or max_executions < 1:
                raise ScheduledTaskError(
                    ERROR_VALIDATION,
                    "max_executions must be a positive integer",
                )
        request_id = "sched_auth_" + secrets.token_urlsafe(18).rstrip("=")
        pending = {
            "request_id": request_id,
            "task_id": str(job["id"]),
            "task_revision": int(job["task_revision"]),
            "definition_hash": str(job["definition_hash"]),
            "authorization_revision": next_authorization_revision,
            "policy_epoch": int(state.policy_epoch),
            "expiration": grant_expires_at,
            "request_expires_at": request_expires_at,
            "origin_ref": str(origin_ref or ""),
            "requests": requests,
            "metadata": metadata,
            "max_executions": max_executions,
        }
        _forget_pending_for_task(str(job["id"]))
        with _AUTHORIZATION_LOCK:
            _PENDING_AUTHORIZATIONS[request_id] = pending
        return _result_ok(
            request={
                "type": "scheduled_task_authorization_request",
                "protocol_version": AUTHORIZATION_PROTOCOL_VERSION,
                "request_id": request_id,
                "task_id": pending["task_id"],
                "task_revision": pending["task_revision"],
                "definition_hash": pending["definition_hash"],
                "authorization_revision": pending["authorization_revision"],
                "policy_epoch": pending["policy_epoch"],
                "expiration": pending["expiration"],
                "request_expires_at": pending["request_expires_at"],
                "origin_ref": pending["origin_ref"],
                "capabilities": [
                    {
                        "id": item["capability_id"],
                        "tool_category": item["tool_category"],
                        "effect_class": item["effect_class"],
                        "safe_category": item["safe_category"],
                    }
                    for item in metadata
                ],
                "max_executions": max_executions,
            },
        )
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message, **exc.details)
    except Exception:
        return _result_error(
            ERROR_AUTHORIZATION_UNAVAILABLE,
            "scheduled-task authorization is unavailable",
        )


def respond_scheduled_task_authorization(
    response: dict[str, Any],
    *,
    authenticated_origin: str,
) -> dict[str, Any]:
    """Validate an exact, single-response authorization envelope."""

    request_id = str(response.get("request_id") or "")
    with _AUTHORIZATION_LOCK:
        if request_id in _RESPONDED_AUTHORIZATIONS:
            return _result_error(ERROR_AUTHORIZATION_STALE, "duplicate response")
        pending = _PENDING_AUTHORIZATIONS.get(request_id)
        if pending is not None and pending.get("responding"):
            return _result_error(ERROR_AUTHORIZATION_STALE, "duplicate response")
        if pending is not None:
            pending["responding"] = True
    if pending is None:
        return _result_error(ERROR_AUTHORIZATION_STALE, "authorization request is stale")
    created_grant_refs: list[str] = []
    try:
        if response.get("protocol_version") != AUTHORIZATION_PROTOCOL_VERSION:
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "wrong protocol version")
        exact_fields = (
            "task_id", "task_revision", "definition_hash",
            "authorization_revision", "policy_epoch", "expiration", "origin_ref",
        )
        if any(response.get(name) != pending.get(name) for name in exact_fields):
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_STALE,
                "authorization response binding mismatch",
            )
        if pending.get("origin_ref") != str(authenticated_origin or ""):
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "wrong authenticated origin")
        if time.time() >= float(pending["request_expires_at"]):
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "authorization request expired")
        job = _current_job(str(pending["task_id"]))
        if (
            int(job.get("task_revision", 0)) != pending["task_revision"]
            or str(job.get("definition_hash") or "") != pending["definition_hash"]
            or int(job.get("authorization_revision", 0))
            != pending["authorization_revision"] - 1
        ):
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "task changed")
        policy, state = _permission_snapshot()
        if policy is None or state is None or int(state.policy_epoch) != pending["policy_epoch"]:
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "policy epoch changed")
        action = str(response.get("action") or "").strip().lower()
        if action not in {"approve", "deny"}:
            raise ScheduledTaskError(ERROR_VALIDATION, "action must be approve or deny")
        if action == "deny":
            job.update({
                "authorization_revision": pending["authorization_revision"],
                "authorization_status": AUTHORIZATION_REQUIRED,
                "enabled": False,
                "policy_epoch": int(state.policy_epoch),
                "updated_at": _iso(),
            })
            job = _commit_authorization_fields(job, pending)
        else:
            from osia.action_policy import UsePolicy, create_grant

            full_access = str(getattr(state, "permission_mode", "")) in {
                "PermissionMode.FULL_ACCESS", "full_access",
            }
            try:
                if not full_access:
                    use_policy = (
                        UsePolicy.BOUNDED
                        if pending["max_executions"] is not None
                        else UsePolicy.RECURRING
                    )
                    for permission_request in pending["requests"]:
                        grant = create_grant(
                            permission_request,
                            use_policy=use_policy,
                            max_uses=pending["max_executions"],
                            expires_at=float(pending["expiration"]),
                            persistent=bool(policy.store.secure_available),
                        )
                        policy.store.add_grant(grant)
                        created_grant_refs.append(grant.grant_id)
            except Exception as exc:
                _revoke_grant_refs(created_grant_refs)
                created_grant_refs.clear()
                raise ScheduledTaskError(
                    ERROR_AUTHORIZATION_UNAVAILABLE,
                    "protected grant creation failed",
                ) from exc
            state = policy.store.load_state()
            job.update({
                "authorization_revision": pending["authorization_revision"],
                "authorization_status": AUTHORIZED,
                "enabled": bool(job.get("desired_enabled", False)),
                "grant_refs": created_grant_refs,
                "policy_epoch": int(state.policy_epoch),
                "authorization_expires_at": (
                    None if full_access else float(pending["expiration"])
                ),
                "authorization_bounds": {
                    "mode": "full_access" if full_access else (
                        "bounded" if pending["max_executions"] is not None
                        else "recurring"
                    ),
                    "max_executions": pending["max_executions"],
                    "storage": (
                        "protected" if policy.store.secure_available else "session_only"
                    ),
                },
                "updated_at": _iso(),
            })
            job = _commit_authorization_fields(job, pending)
        with _AUTHORIZATION_LOCK:
            _PENDING_AUTHORIZATIONS.pop(request_id, None)
            _remember_responded(request_id)
        return _result_ok(task=_public_authorization_task(job))
    except ScheduledTaskError as exc:
        if created_grant_refs:
            _revoke_grant_refs(created_grant_refs)
        with _AUTHORIZATION_LOCK:
            current = _PENDING_AUTHORIZATIONS.get(request_id)
            if current is pending:
                current.pop("responding", None)
        return _result_error(exc.code, exc.message, **exc.details)
    except Exception:
        if created_grant_refs:
            _revoke_grant_refs(created_grant_refs)
        with _AUTHORIZATION_LOCK:
            current = _PENDING_AUTHORIZATIONS.get(request_id)
            if current is pending:
                current.pop("responding", None)
        return _result_error(
            ERROR_AUTHORIZATION_UNAVAILABLE,
            "scheduled-task authorization is unavailable",
        )


def authorize_scheduled_task(
    task_id: str,
    *,
    calls: list[dict[str, Any]] | None = None,
    origin_ref: str = "desktop",
    mobile: bool = False,
    ttl_seconds: float = _DEFAULT_GRANT_TTL_SECONDS,
    max_executions: int | None = None,
) -> dict[str, Any]:
    """PC/local convenience facade: begin then approve the exact request."""

    begun = begin_scheduled_task_authorization(
        task_id,
        calls=calls,
        origin_ref=origin_ref,
        mobile=mobile,
        ttl_seconds=ttl_seconds,
        max_executions=max_executions,
    )
    if not begun.get("ok") or begun.get("already_authorized"):
        return begun
    request = dict(begun["request"])
    request["action"] = "approve"
    return respond_scheduled_task_authorization(
        request,
        authenticated_origin=origin_ref,
    )


def revoke_scheduled_task_grant(task_id: str, grant_ref: str) -> dict[str, Any]:
    """Revoke one exact grant and suspend the task without touching other refs."""

    try:
        job = _current_job(task_id)
        refs = list(job.get("grant_refs") or [])
        target = str(grant_ref or "")
        if target not in refs:
            return _result_ok(changed=False, task=_public_authorization_task(job))
        policy, _state = _permission_snapshot()
        if policy is not None:
            policy.store.revoke_grant(target)
        refs.remove(target)
        _policy, state = _permission_snapshot()
        job.update({
            "grant_refs": refs,
            "authorization_revision": int(job.get("authorization_revision", 0)) + 1,
            "authorization_status": AUTHORIZATION_REVOKED,
            "enabled": False,
            "policy_epoch": int(getattr(state, "policy_epoch", 0) or 0),
            "updated_at": _iso(),
        })
        _forget_pending_for_task(str(job["id"]))
        _save_current_job(job)
        return _result_ok(changed=True, task=_public_authorization_task(job))
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)


def revoke_scheduled_task_authorization(task_id: str) -> dict[str, Any]:
    """Idempotently revoke every grant for one task and block its next run."""

    try:
        job = _current_job(task_id)
        refs = list(job.get("grant_refs") or [])
        already = (
            not refs
            and job.get("authorization_status") == AUTHORIZATION_REVOKED
            and not job.get("enabled")
        )
        if already:
            return _result_ok(changed=False, task=_public_authorization_task(job))
        job = _invalidate_authorization(
            job,
            status=AUTHORIZATION_REVOKED,
            increment_task_revision=False,
        )
        _forget_pending_for_task(str(job["id"]))
        _save_current_job(job)
        return _result_ok(changed=True, task=_public_authorization_task(job))
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)


def _validate_control_envelope(
    envelope: dict[str, Any], *, authenticated_origin: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        if envelope.get("protocol_version") != AUTHORIZATION_PROTOCOL_VERSION:
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "wrong protocol version")
        if envelope.get("origin_ref") != str(authenticated_origin or ""):
            raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "wrong authenticated origin")
        job = _current_job(str(envelope.get("task_id") or ""))
        expected = {
            "task_revision": int(job.get("task_revision", 0)),
            "definition_hash": str(job.get("definition_hash") or ""),
            "authorization_revision": int(job.get("authorization_revision", 0)),
            "policy_epoch": int(job.get("policy_epoch", 0)),
            "expiration": job.get("authorization_expires_at"),
        }
        if any(envelope.get(name) != value for name, value in expected.items()):
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_STALE,
                "scheduled-task control binding mismatch",
            )
        return job, None
    except ScheduledTaskError as exc:
        return None, _result_error(exc.code, exc.message)


def revoke_scheduled_task_authorization_bound(
    envelope: dict[str, Any], *, authenticated_origin: str,
) -> dict[str, Any]:
    with _JOBS_LOCK:
        job, error = _validate_control_envelope(
            envelope, authenticated_origin=authenticated_origin,
        )
        if error is not None:
            return error
        return revoke_scheduled_task_authorization(str(job["id"]))


def disable_scheduled_task_bound(
    envelope: dict[str, Any], *, authenticated_origin: str,
) -> dict[str, Any]:
    with _JOBS_LOCK:
        job, error = _validate_control_envelope(
            envelope, authenticated_origin=authenticated_origin,
        )
        if error is not None:
            return error
        return disable_scheduled_task(str(job["id"]))


def disable_scheduled_task(task_id: str) -> dict[str, Any]:
    """Disable a task as an execution-relevant edit and revoke its authority."""

    try:
        job = _current_job(task_id)
        if not job.get("desired_enabled") and job.get("authorization_status") == DISABLED:
            return _result_ok(changed=False, task=_public_authorization_task(job))
        job["desired_enabled"] = False
        job = _invalidate_authorization(
            job,
            status=DISABLED,
            increment_task_revision=True,
        )
        job["definition_hash"] = task_definition_hash(job)
        _forget_pending_for_task(str(job["id"]))
        _save_current_job(job)
        return _result_ok(changed=True, task=_public_authorization_task(job))
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)


def preflight_scheduled_task(task_id: str) -> dict[str, Any]:
    """Reload and validate current authority before a scheduler launch."""

    try:
        job = _current_job(task_id)
        if job.get("schema_version") != SCHEDULED_TASK_SCHEMA_VERSION:
            return _result_error(
                ERROR_AUTHORIZATION_REQUIRED,
                "unknown scheduled-task schema",
                authorization_status=AUTHORIZATION_REQUIRED,
            )
        expected_hash = task_definition_hash(job)
        if expected_hash != str(job.get("definition_hash") or ""):
            job = _invalidate_authorization(
                job,
                status=AUTHORIZATION_EDITED,
                increment_task_revision=True,
            )
            job["definition_hash"] = expected_hash
            _save_current_job(job)
            return _result_error(
                ERROR_AUTHORIZATION_REQUIRED,
                "task definition changed",
                authorization_status=AUTHORIZATION_EDITED,
            )
        if not job.get("enabled") or not job.get("desired_enabled"):
            return _result_error(
                ERROR_AUTHORIZATION_REQUIRED,
                "scheduled task is disabled",
                authorization_status=str(job.get("authorization_status") or DISABLED),
            )
        if job.get("authorization_status") == BLOCKED_OUTCOME_UNKNOWN:
            return _result_error(
                ERROR_AUTHORIZATION_REQUIRED,
                "ambiguous outcome blocks replay",
                authorization_status=BLOCKED_OUTCOME_UNKNOWN,
            )
        policy, state = _permission_snapshot()
        if policy is None or state is None:
            raise ScheduledTaskError(
                ERROR_AUTHORIZATION_UNAVAILABLE,
                "permission authority is unavailable",
            )
        full_access = str(getattr(state, "permission_mode", "")) in {
            "PermissionMode.FULL_ACCESS", "full_access",
        }
        if full_access or str(job.get("execution_mode") or "") == "read_only":
            job["policy_epoch"] = int(state.policy_epoch)
            return _result_ok(job=job, authorization_source=(
                "full_access" if full_access else "structural_read_only"
            ))
        refs = list(job.get("grant_refs") or [])
        if not refs:
            job.update({
                "authorization_status": AUTHORIZATION_REQUIRED,
                "enabled": False,
                "updated_at": _iso(),
            })
            _save_current_job(job)
            return _result_error(
                ERROR_AUTHORIZATION_REQUIRED,
                "scheduled task has no recurring grant",
                authorization_status=AUTHORIZATION_REQUIRED,
            )
        status = policy.store.scheduled_grant_refs_status(
            refs,
            task_id=str(job["id"]),
            task_revision=int(job["task_revision"]),
            authorization_binding=_authorization_binding(job),
            now=time.time(),
        )
        if status != "valid":
            next_status = (
                AUTHORIZATION_EXPIRED if status == "expired_grant"
                else AUTHORIZATION_REVOKED if status in {
                    "missing_grant", "revoked_grant", "corrupt_grant",
                }
                else AUTHORIZATION_REQUIRED
            )
            job.update({
                "authorization_status": next_status,
                "enabled": False,
                "policy_epoch": int(state.policy_epoch),
                "updated_at": _iso(),
            })
            _save_current_job(job)
            return _result_error(
                ERROR_AUTHORIZATION_REQUIRED,
                status,
                authorization_status=next_status,
            )
        job["policy_epoch"] = int(state.policy_epoch)
        return _result_ok(job=job, authorization_source="grant")
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message, **exc.details)
    except Exception:
        return _result_error(
            ERROR_AUTHORIZATION_UNAVAILABLE,
            "scheduled-task authorization preflight failed",
            authorization_status=AUTHORIZATION_REQUIRED,
        )


def execution_context_for_scheduled_occurrence(execution: dict[str, Any]):
    """Create the non-interactive policy context for one validated occurrence."""

    task_id = str(execution.get("task_id") or "")
    job = _current_job(task_id)
    exact = (
        int(execution.get("task_revision", -1)) == int(job.get("task_revision", 0))
        and str(execution.get("definition_hash") or "")
        == str(job.get("definition_hash") or "")
        and int(execution.get("authorization_revision", -1))
        == int(job.get("authorization_revision", 0))
    )
    if not exact:
        raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "scheduled context is stale")
    preflight = preflight_scheduled_task(task_id)
    if not preflight.get("ok"):
        raise ScheduledTaskError(
            ERROR_AUTHORIZATION_REQUIRED,
            str(preflight.get("message") or "scheduled authorization blocked"),
        )
    policy, state = _permission_snapshot()
    if policy is None or state is None:
        raise ScheduledTaskError(ERROR_AUTHORIZATION_UNAVAILABLE, "permission unavailable")
    from osia.action_policy import ExecutionType, make_execution_context

    occurrence_id = str(execution.get("occurrence_id") or "")
    base_operation = "schedbase:" + canonical_hash({
        "task_id": task_id,
        "task_revision": int(job["task_revision"]),
        "definition_hash": str(job["definition_hash"]),
        "occurrence_id": occurrence_id,
    })[:40]
    return make_execution_context(
        execution_type=ExecutionType.SCHEDULED,
        interactive=False,
        run_id="",
        scheduled_task_id=task_id,
        task_revision=str(job["task_revision"]),
        plan_revision=_authorization_binding(job),
        policy_epoch=int(state.policy_epoch),
        execution_id=occurrence_id,
        operation_id=base_operation,
        store=policy.store,
    )


def _equivalent_operation_id(
    job: dict[str, Any], tool_name: str, normalized_call_hash: str,
) -> str:
    return "schedeq:" + canonical_hash({
        "task_id": str(job.get("id") or ""),
        "task_revision": int(job.get("task_revision", 0)),
        "definition_hash": str(job.get("definition_hash") or ""),
        "tool": str(tool_name or ""),
        "call": str(normalized_call_hash or ""),
    })[:48]


def bind_request_execution_context(context, tool_name: str, call_hash: str):
    """Bind an actual scheduled call to the current task and occurrence."""

    from osia.action_policy import ExecutionType

    if context.execution_type != ExecutionType.SCHEDULED:
        return context
    job = _current_job(context.scheduled_task_id)
    if (
        context.task_revision != str(job.get("task_revision") or "")
        or context.plan_revision != _authorization_binding(job)
        or task_definition_hash(job) != str(job.get("definition_hash") or "")
    ):
        raise ScheduledTaskError(ERROR_AUTHORIZATION_STALE, "scheduled task binding changed")
    equivalent = _equivalent_operation_id(job, tool_name, call_hash)
    if equivalent in list(job.get("ambiguity_barriers") or []):
        raise ScheduledTaskError(
            ERROR_AUTHORIZATION_REQUIRED,
            "equivalent operation has an unresolved outcome",
        )
    operation_id = "schedop:" + canonical_hash({
        "equivalent": equivalent,
        "occurrence": context.execution_id,
    })[:48]
    return replace(context, operation_id=operation_id)


def validate_scheduled_permission_request(request) -> str | None:
    """Re-read task/grant state immediately before policy evaluation."""

    context = request.execution_context
    job = _current_job(context.scheduled_task_id)
    preflight = preflight_scheduled_task(str(job["id"]))
    if not preflight.get("ok"):
        return str(preflight.get("authorization_status") or AUTHORIZATION_REQUIRED)
    if (
        context.task_revision != str(job.get("task_revision") or "")
        or context.plan_revision != _authorization_binding(job)
    ):
        return AUTHORIZATION_EDITED
    equivalent = _equivalent_operation_id(
        job, request.tool_identity, request.normalized_call_hash,
    )
    if equivalent in list(job.get("ambiguity_barriers") or []):
        return BLOCKED_OUTCOME_UNKNOWN
    return None


def mark_scheduled_authorization_failure(task_id: str, reason: str) -> None:
    try:
        code = str(reason or "")
        status = (
            AUTHORIZATION_EXPIRED if "expired" in code
            else AUTHORIZATION_EDITED if "revision" in code or "changed" in code
            else BLOCKED_OUTCOME_UNKNOWN if "outcome" in code or "ambiguous" in code
            else AUTHORIZATION_REVOKED if "revoked" in code
            else AUTHORIZATION_REQUIRED
        )
        with _JOBS_LOCK:
            jobs = _load_normalized_jobs()
            idx = _find_index(jobs, str(task_id or ""))
            if idx < 0:
                return
            job = dict(jobs[idx])
            job.update({
                "authorization_status": status,
                "enabled": False,
                "updated_at": _iso(),
            })
            jobs[idx] = job
            _save_jobs(jobs)
        _notify_changed()
    except Exception:
        pass


def scheduled_execution_block_state(execution: dict[str, Any] | None) -> str:
    if not isinstance(execution, dict) or not execution.get("task_id"):
        return ""
    try:
        status = str(_current_job(str(execution["task_id"])).get(
            "authorization_status"
        ) or "")
    except Exception:
        return AUTHORIZATION_REQUIRED
    return status if status in {
        AUTHORIZATION_REQUIRED, AUTHORIZATION_EXPIRED,
        AUTHORIZATION_REVOKED, AUTHORIZATION_EDITED,
        BLOCKED_OUTCOME_UNKNOWN, DISABLED,
    } else ""


def mark_scheduled_task_running(execution: dict[str, Any]) -> bool:
    """Atomically enter RUNNING only for the still-current authorized task."""

    task_id = str(execution.get("task_id") or "")
    with _JOBS_LOCK:
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, task_id)
        if idx < 0:
            return False
        job = dict(jobs[idx])
        exact = (
            int(execution.get("task_revision", -1))
            == int(job.get("task_revision", 0))
            and str(execution.get("definition_hash") or "")
            == str(job.get("definition_hash") or "")
            and int(execution.get("authorization_revision", -1))
            == int(job.get("authorization_revision", 0))
        )
        if (
            not exact
            or job.get("authorization_status") != AUTHORIZED
            or not job.get("enabled")
        ):
            return False
        job["authorization_status"] = RUNNING
        job["updated_at"] = _iso()
        jobs[idx] = job
        _save_jobs(jobs)
    _notify_changed()
    return True


def mark_scheduled_task_finished(execution: dict[str, Any]) -> None:
    """Restore AUTHORIZED without overwriting a concurrent revoke or edit."""

    task_id = str(execution.get("task_id") or "")
    with _JOBS_LOCK:
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, task_id)
        if idx < 0:
            return
        job = dict(jobs[idx])
        exact = (
            int(execution.get("task_revision", -1))
            == int(job.get("task_revision", 0))
            and str(execution.get("definition_hash") or "")
            == str(job.get("definition_hash") or "")
            and int(execution.get("authorization_revision", -1))
            == int(job.get("authorization_revision", 0))
        )
        if not exact or job.get("authorization_status") != RUNNING:
            return
        job["authorization_status"] = AUTHORIZED
        job["updated_at"] = _iso()
        jobs[idx] = job
        _save_jobs(jobs)
    _notify_changed()


def record_scheduled_tool_execution(
    execution: dict[str, Any] | None,
    tool_name: str,
    arguments: dict[str, Any],
    execution_record,
) -> None:
    """Persist only a value-free outcome and durable ambiguity barrier."""

    if not isinstance(execution, dict) or not execution.get("task_id"):
        return
    from osia.tool_contracts import ExecutionOutcome, normalized_call_hash

    call_hash = normalized_call_hash(tool_name, arguments)
    operation_binding = {
        "id": str(execution.get("task_id") or ""),
        "task_revision": int(execution.get("task_revision", 0) or 0),
        "definition_hash": str(execution.get("definition_hash") or ""),
    }
    equivalent = _equivalent_operation_id(
        operation_binding, tool_name, call_hash,
    )
    outcome = execution_record.outcome
    if outcome is ExecutionOutcome.SUCCESS_VERIFIED:
        safe_outcome = "success_verified"
    elif outcome is ExecutionOutcome.CANCELLED_PROVEN:
        safe_outcome = "cancelled_proven"
    elif outcome in {
        ExecutionOutcome.NOT_STARTED,
        ExecutionOutcome.REJECTED_BEFORE_EXECUTION,
        ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
    }:
        safe_outcome = "failed_before_dispatch"
    else:
        safe_outcome = "outcome_unknown"
    task_id = str(execution.get("task_id") or "")
    with _JOBS_LOCK:
        jobs = _load_normalized_jobs()
        idx = _find_index(jobs, task_id)
        if idx < 0:
            return
        job = dict(jobs[idx])
        if (
            safe_outcome == "failed_before_dispatch"
            and job.get("authorization_status") in {
                AUTHORIZATION_REQUIRED, AUTHORIZATION_EXPIRED,
                AUTHORIZATION_REVOKED, AUTHORIZATION_EDITED,
                BLOCKED_OUTCOME_UNKNOWN,
            }
        ):
            safe_outcome = "blocked_authorization"
        records = list(job.get("execution_records") or [])
        records.append({
            "occurrence_id": str(execution.get("occurrence_id") or ""),
            "equivalent_operation_id": equivalent,
            "outcome": safe_outcome,
            "safe_category": str(
                execution_record.contract.spec.capability or "tool"
            ),
            "dispatched": bool(execution_record.dispatched),
            "attempts": int(execution_record.attempts),
            "recorded_at": _iso(),
        })
        job["execution_records"] = records[-_MAX_EXECUTION_RECORDS:]
        job["last_execution_outcome"] = safe_outcome
        if safe_outcome == "outcome_unknown":
            barriers = list(job.get("ambiguity_barriers") or [])
            if equivalent not in barriers:
                barriers.append(equivalent)
            job["ambiguity_barriers"] = barriers[-_MAX_EXECUTION_RECORDS:]
            job["authorization_status"] = BLOCKED_OUTCOME_UNKNOWN
            job["enabled"] = False
        job["updated_at"] = _iso()
        jobs[idx] = job
        _save_jobs(jobs)
    _notify_changed()


def record_scheduled_occurrence(
    execution: dict[str, Any],
    *,
    outcome: str,
) -> None:
    """Persist one sanitized terminal record for every scheduler occurrence."""

    allowed = {
        "success_verified", "cancelled_proven", "failed_before_dispatch",
        "partial_effect_verified", "outcome_unknown", "blocked_authorization",
    }
    safe_outcome = outcome if outcome in allowed else "outcome_unknown"
    try:
        task_id = str(execution.get("task_id") or "")
        with _JOBS_LOCK:
            jobs = _load_normalized_jobs()
            idx = _find_index(jobs, task_id)
            if idx < 0:
                return
            job = dict(jobs[idx])
            records = list(job.get("occurrence_records") or [])
            records.append({
                "occurrence_id": str(execution.get("occurrence_id") or ""),
                "outcome": safe_outcome,
                "recorded_at": _iso(),
            })
            job["occurrence_records"] = records[-_MAX_EXECUTION_RECORDS:]
            if safe_outcome in {"outcome_unknown", "partial_effect_verified"}:
                barriers = list(job.get("ambiguity_barriers") or [])
                if not barriers:
                    barriers.append("schedocc:" + canonical_hash({
                        "task_id": str(execution.get("task_id") or ""),
                        "task_revision": int(
                            execution.get("task_revision", 0) or 0
                        ),
                        "definition_hash": str(
                            execution.get("definition_hash") or ""
                        ),
                        "occurrence_id": str(
                            execution.get("occurrence_id") or ""
                        ),
                    })[:48])
                    job["ambiguity_barriers"] = barriers
                job["authorization_status"] = BLOCKED_OUTCOME_UNKNOWN
                job["enabled"] = False
            job["updated_at"] = _iso()
            jobs[idx] = job
            _save_jobs(jobs)
        _notify_changed()
    except Exception:
        pass


def reconcile_scheduled_outcome(
    task_id: str,
    equivalent_operation_id: str,
    outcome: str,
) -> dict[str, Any]:
    """Explicit local reconciliation; execution remains disabled for reauth."""

    if outcome not in {"success_verified", "cancelled_proven", "partial_effect_verified"}:
        return _result_error(ERROR_VALIDATION, "invalid reconciliation outcome")
    try:
        job = _current_job(task_id)
        barriers = list(job.get("ambiguity_barriers") or [])
        target = str(equivalent_operation_id or "")
        if target not in barriers:
            return _result_ok(changed=False, task=_public_authorization_task(job))
        barriers.remove(target)
        job = _invalidate_authorization(
            job,
            status=AUTHORIZATION_REQUIRED,
            increment_task_revision=False,
        )
        job["ambiguity_barriers"] = barriers
        job["reconciliation_outcome"] = outcome
        _save_current_job(job)
        return _result_ok(changed=True, task=_public_authorization_task(job))
    except ScheduledTaskError as exc:
        return _result_error(exc.code, exc.message)


def sanitized_mobile_jobs(jobs: Any = None) -> list[dict[str, Any]]:
    """Return task-management metadata without instructions, scopes or grants."""

    source = _load_normalized_jobs() if jobs is None else [
        dict(item) for item in jobs if isinstance(item, dict)
    ]
    sanitized = []
    for job in source:
        public = _public_authorization_task(job)
        schedule = str(job.get("schedule") or "")
        try:
            from osia.cron import parse_schedule

            if parse_schedule(schedule) is None:
                schedule = ""
        except Exception:
            schedule = ""
        timezone_name = str(job.get("timezone") or "")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            timezone_name = ""
        stored_model = _model_identity(job)
        model = {"provider": "", "id": ""}
        try:
            models, _provider_for_model = _model_registry_snapshot()
            exact_models = [
                item for item in models if isinstance(item, dict)
                and str(item.get("id") or "") == stored_model["id"]
                and (
                    not stored_model["provider"]
                    or str(item.get("provider") or "")
                    == stored_model["provider"]
                )
            ]
            if len(exact_models) == 1:
                model = _model_identity({
                    "model": _model_metadata_from_match(exact_models[0]),
                })
        except Exception:
            pass
        for name in ("provider", "id"):
            value = str(model.get(name) or "")
            if (
                len(value) > 160
                or "://" in value
                or "\\" in value
                or not re.fullmatch(r"[A-Za-z0-9_.:/+-]*", value)
            ):
                model[name] = ""
        sanitized.append({
            "id": public["task_id"],
            "title": "Scheduled task",
            "task": "Scheduled task",
            "task_redacted": True,
            "schedule": schedule,
            "timezone": timezone_name,
            "enabled": public["enabled"],
            "desired_enabled": bool(job.get("desired_enabled", False)),
            "model": model,
            **public,
        })
    return sanitized


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
    from osia.turn_context import localized_text

    return localized_text("not_scheduled", lang)
