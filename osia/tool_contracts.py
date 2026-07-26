"""Authoritative OSIA tool contracts and execution-safety metadata (WOR-178).

The module is deliberately independent from :mod:`osia.agent`: static
dispatchers are bound only after all ``tool_*`` functions exist, and MCP tools
are registered dynamically.  This keeps direct function imports compatible and
avoids a circular import while making schemas and safety policy single-source.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
import shlex
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


class SideEffectClass(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_LOCAL = "reversible_local"
    PERSISTENT_LOCAL = "persistent_local"
    EXTERNAL_MUTATION = "external_mutation"
    IRREVERSIBLE_EXTERNAL = "irreversible_external"
    DYNAMICALLY_CLASSIFIED = "dynamically_classified"
    UNKNOWN_HIGH_RISK = "unknown_high_risk"


class ExecutionOutcome(str, Enum):
    NOT_STARTED = "not_started"
    REJECTED_BEFORE_EXECUTION = "rejected_before_execution"
    CONFIRMED_FAILURE_BEFORE_EFFECT = "confirmed_failure_before_effect"
    SUCCESS_VERIFIED = "success_verified"
    SUCCESS_UNVERIFIED = "success_unverified"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED_PROVEN = "cancelled_proven"


class IdempotencyClass(str, Enum):
    IDEMPOTENT = "idempotent"
    CONDITIONALLY_IDEMPOTENT = "conditionally_idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    DYNAMIC = "dynamic"
    UNKNOWN = "unknown"


class ConfirmationPolicy(str, Enum):
    NO_CONFIRMATION = "no_confirmation"
    EXPLICIT_INTENT_REQUIRED = "explicit_intent_required"
    CONFIRMATION_REQUIRED = "confirmation_required"
    FINAL_COMMIT_CONFIRMATION = "final_commit_confirmation"
    DENIED_BY_POLICY = "denied_by_policy"


class RetryPolicy(str, Enum):
    READ_ONCE_AFTER_CONFIRMED_FAILURE = "read_once_after_confirmed_failure"
    NOT_STARTED_ONLY = "not_started_only"
    NEVER_AFTER_DISPATCH = "never_after_dispatch"


class ResultPolicy(str, Enum):
    BOUNDED_MODEL_ONLY = "bounded_model_only"
    BOUNDED_MODEL_AND_SCREENSHOT = "bounded_model_and_screenshot"
    PRIVATE_MODEL_ONLY = "private_model_only"


class ObservabilityPolicy(str, Enum):
    METADATA_ONLY = "metadata_only"
    PRIVATE_METADATA_ONLY = "private_metadata_only"
    IDENTIFIERS_ONLY = "identifiers_only"


class ProgressEvidencePolicy(str, Enum):
    NONE = "none"
    NEW_STATE_FINGERPRINT = "new_state_fingerprint"
    VERIFIED_EFFECT = "verified_effect"
    DOCUMENT_ARTIFACT = "document_artifact"
    VERIFIED_FORM_STATE = "verified_form_state"


class EvidenceClassification(str, Enum):
    PROVED = "proved"
    UNAVAILABLE_IN_ENVIRONMENT = "unavailable_in_environment"
    NOT_SAFELY_TESTABLE = "not_safely_testable"
    FAILED = "failed"
    REAL_VALIDATION_REQUIRED = "real_validation_required"
    METADATA_ONLY = "metadata_only"


class RealValidationRequirement(str, Enum):
    NONE = "none"
    CONTROLLED_READ_ONLY = "controlled_read_only"
    CONTROLLED_LOCAL = "controlled_local"
    USER_VISIBLE = "user_visible"
    EXTERNAL_NOT_SAFE = "external_not_safe"


ALL_TEXT_PROVIDERS = (
    "anthropic", "chatgpt_account", "deepseek", "google", "groq",
    "ollama", "openai", "xai",
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    dispatcher_name: str
    capability: str
    input_schema: dict
    preconditions: tuple[str, ...]
    side_effect: SideEffectClass
    idempotency: IdempotencyClass
    confirmation: ConfirmationPolicy
    timeout_seconds: float
    retry: RetryPolicy
    providers: tuple[str, ...]
    result_policy: ResultPolicy
    observability: ObservabilityPolicy
    progress_evidence: ProgressEvidencePolicy
    deterministic_test_status: EvidenceClassification
    controlled_integration_status: EvidenceClassification
    real_validation_requirement: RealValidationRequirement
    verified_success: bool = False
    dispatcher: Callable[..., Any] | None = None
    dynamic: bool = False
    connector_id: str = ""

    @property
    def fallback_barrier_on_dispatch(self) -> bool:
        return self.side_effect not in {SideEffectClass.READ_ONLY}


@dataclass(frozen=True)
class EffectiveToolContract:
    spec: ToolSpec
    side_effect: SideEffectClass
    idempotency: IdempotencyClass
    confirmation: ConfirmationPolicy
    retry: RetryPolicy

    @property
    def commits_fallback_barrier(self) -> bool:
        return self.side_effect != SideEffectClass.READ_ONLY


@dataclass(frozen=True)
class ToolExecutionRecord:
    result: Any
    is_error: bool
    outcome: ExecutionOutcome
    attempts: int
    dispatched: bool
    timed_out: bool
    contract: EffectiveToolContract


class ToolRegistryError(RuntimeError):
    pass


class ToolValidationError(ValueError):
    """One stable, value-free validation failure."""

    def __init__(self, code: str = "invalid_arguments"):
        super().__init__(code)
        self.code = code


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()):
        entries: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise ToolRegistryError("invalid tool specification")
            if spec.name in entries:
                raise ToolRegistryError(f"duplicate tool name: {spec.name}")
            entries[spec.name] = spec
        self._entries = entries

    def bind(self, dispatchers: Mapping[str, Callable[..., Any]]) -> "ToolRegistry":
        bound = []
        for spec in self._entries.values():
            dispatcher = dispatchers.get(spec.dispatcher_name)
            if not callable(dispatcher):
                raise ToolRegistryError(
                    f"missing dispatcher for {spec.name}: {spec.dispatcher_name}"
                )
            bound.append(replace(spec, dispatcher=dispatcher))
        return ToolRegistry(bound)

    def get(self, name: str) -> ToolSpec | None:
        return self._entries.get(str(name or ""))

    def names(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def values(self) -> tuple[ToolSpec, ...]:
        return tuple(self._entries.values())

    def callables(self) -> dict[str, Callable[..., Any]]:
        missing = [spec.name for spec in self._entries.values()
                   if not callable(spec.dispatcher)]
        if missing:
            raise ToolRegistryError("unbound tool specification")
        return {spec.name: spec.dispatcher for spec in self._entries.values()}

    def assert_complete(self, executable: Mapping[str, Callable[..., Any]]) -> None:
        executable_names = {
            name for name in executable if not str(name).startswith("mcp__")
        }
        spec_names = set(self._entries)
        if executable_names != spec_names:
            missing = sorted(executable_names - spec_names)
            orphan = sorted(spec_names - executable_names)
            raise ToolRegistryError(
                f"tool registry mismatch (missing={missing}, orphan={orphan})"
            )
        for name in spec_names:
            if executable.get(name) is not self._entries[name].dispatcher:
                raise ToolRegistryError(f"dispatcher mismatch for {name}")


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _string(*, minimum: int = 1, maximum: int = 4096,
            pattern: str | None = None, enum: Iterable[str] | None = None,
            fmt: str | None = None) -> dict:
    schema: dict[str, Any] = {
        "type": "string", "minLength": minimum, "maxLength": maximum,
    }
    if pattern is not None:
        schema["pattern"] = pattern
    if enum is not None:
        schema["enum"] = list(enum)
    if fmt is not None:
        schema["format"] = fmt
    return schema


def _integer(minimum: int, maximum: int) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _number(minimum: float, maximum: float) -> dict:
    return {"type": "number", "minimum": minimum, "maximum": maximum}


def _boolean() -> dict:
    return {"type": "boolean"}


def _array(items: dict, *, maximum: int = 64) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum}


def _object(properties: Mapping[str, dict] | None = None, *,
            required: Iterable[str] = (), allow_extra: bool = False,
            **keywords: Any) -> dict:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": bool(allow_extra),
        "maxProperties": 128,
    }
    schema.update(keywords)
    return schema


PATH = _string(maximum=4096)
URL = _string(maximum=8192, pattern=r"^\S+$")
IDENTIFIER = _string(maximum=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
FIELD_REF = _string(maximum=28, pattern=r"^fld_[A-Za-z0-9_-]{24}$")
ARTIFACT_ID = _string(maximum=36, pattern=r"^doc_[0-9a-f]{32}$")
TEXT = _string(minimum=0, maximum=16_000)
LARGE_TEXT = _string(minimum=0, maximum=2_000_000)

SCHEDULE_SCHEMA = _object({
    "type": _string(enum=("once", "daily", "interval", "weekly", "cron"), maximum=16),
    "run_at": _nullable(_string(maximum=64)),
    "time": _nullable(_string(maximum=16, pattern=r"^\d{1,2}:\d{2}$")),
    "timezone": _nullable(_string(maximum=64)),
    "every": _nullable(_integer(1, 1_000_000)),
    "unit": _nullable(_string(enum=("minute", "minutes", "hour", "hours", "day", "days"), maximum=16)),
    "days_of_week": _nullable(_array(_string(maximum=16), maximum=7)),
    "expression": _nullable(_string(maximum=128)),
}, required=("type",), allOf=[
    {
        "if": {"properties": {"type": {"const": "once"}}, "required": ["type"]},
        "then": {"required": ["run_at"], "properties": {"run_at": _string(maximum=64)}},
    },
    {
        "if": {"properties": {"type": {"const": "daily"}}, "required": ["type"]},
        "then": {"required": ["time"], "properties": {"time": _string(maximum=16, pattern=r"^\d{1,2}:\d{2}$")}},
    },
    {
        "if": {"properties": {"type": {"const": "interval"}}, "required": ["type"]},
        "then": {
            "required": ["every", "unit"],
            "properties": {
                "every": _integer(1, 1_000_000),
                "unit": _string(enum=("minute", "minutes", "hour", "hours", "day", "days"), maximum=16),
            },
        },
    },
    {
        "if": {"properties": {"type": {"const": "weekly"}}, "required": ["type"]},
        "then": {
            "required": ["time", "days_of_week"],
            "properties": {
                "time": _string(maximum=16, pattern=r"^\d{1,2}:\d{2}$"),
                "days_of_week": _array(_string(maximum=16), maximum=7),
            },
        },
    },
    {
        "if": {"properties": {"type": {"const": "cron"}}, "required": ["type"]},
        "then": {"required": ["expression"], "properties": {"expression": _string(maximum=128)}},
    },
])

MODEL_SCHEMA = _object({
    "provider": _string(maximum=64),
    "id": _string(maximum=160),
}, required=("provider", "id"))

MEMORY_VALUE_SCHEMA = _object({
    "kind": _string(
        enum=("path", "url", "uri", "steam_appid", "text", "steps"),
        maximum=32,
    ),
    "data": {"anyOf": [LARGE_TEXT, _array(TEXT, maximum=128)]},
}, required=("kind", "data"))


def _spec(
    name: str,
    capability: str,
    schema: dict,
    side_effect: SideEffectClass,
    idempotency: IdempotencyClass,
    confirmation: ConfirmationPolicy,
    *,
    preconditions: Iterable[str] = (),
    timeout: float = 30.0,
    retry: RetryPolicy | None = None,
    result: ResultPolicy = ResultPolicy.BOUNDED_MODEL_ONLY,
    observability: ObservabilityPolicy = ObservabilityPolicy.METADATA_ONLY,
    progress: ProgressEvidencePolicy = ProgressEvidencePolicy.NONE,
    deterministic: EvidenceClassification = EvidenceClassification.METADATA_ONLY,
    integration: EvidenceClassification = EvidenceClassification.METADATA_ONLY,
    real: RealValidationRequirement = RealValidationRequirement.NONE,
    verified: bool = False,
) -> ToolSpec:
    if retry is None:
        retry = (
            RetryPolicy.READ_ONCE_AFTER_CONFIRMED_FAILURE
            if side_effect == SideEffectClass.READ_ONLY
            and idempotency == IdempotencyClass.IDEMPOTENT
            else RetryPolicy.NEVER_AFTER_DISPATCH
        )
    return ToolSpec(
        name=name,
        dispatcher_name="tool_" + name,
        capability=capability,
        input_schema=schema,
        preconditions=tuple(preconditions),
        side_effect=side_effect,
        idempotency=idempotency,
        confirmation=confirmation,
        timeout_seconds=float(timeout),
        retry=retry,
        providers=ALL_TEXT_PROVIDERS,
        result_policy=result,
        observability=observability,
        progress_evidence=progress,
        deterministic_test_status=deterministic,
        controlled_integration_status=integration,
        real_validation_requirement=real,
        verified_success=verified,
    )


def _scheduler_properties() -> dict[str, dict]:
    return {
        "title": _nullable(_string(maximum=240)),
        "instruction": _nullable(_string(maximum=16_000)),
        "schedule": _nullable(SCHEDULE_SCHEMA),
        "enabled": _nullable(_boolean()),
        "conversation_id": _nullable(IDENTIFIER),
        "conversation_title": _nullable(_string(maximum=240)),
        "conversation_query": _nullable(_string(maximum=240)),
        "source": _nullable(_string(maximum=32)),
        "model": _nullable(MODEL_SCHEMA),
        "model_id": _nullable(_string(maximum=160)),
        "model_provider": _nullable(_string(maximum=64)),
        "model_query": _nullable(_string(maximum=240)),
    }


def _build_static_specs() -> tuple[ToolSpec, ...]:
    R = SideEffectClass.READ_ONLY
    RL = SideEffectClass.REVERSIBLE_LOCAL
    PL = SideEffectClass.PERSISTENT_LOCAL
    EM = SideEffectClass.EXTERNAL_MUTATION
    IE = SideEffectClass.IRREVERSIBLE_EXTERNAL
    DY = SideEffectClass.DYNAMICALLY_CLASSIFIED
    I = IdempotencyClass.IDEMPOTENT
    C = IdempotencyClass.CONDITIONALLY_IDEMPOTENT
    N = IdempotencyClass.NON_IDEMPOTENT
    D = IdempotencyClass.DYNAMIC
    NC = ConfirmationPolicy.NO_CONFIRMATION
    EI = ConfirmationPolicy.EXPLICIT_INTENT_REQUIRED
    CR = ConfirmationPolicy.CONFIRMATION_REQUIRED
    FC = ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION
    state = ProgressEvidencePolicy.NEW_STATE_FINGERPRINT
    effect = ProgressEvidencePolicy.VERIFIED_EFFECT
    read_real = RealValidationRequirement.CONTROLLED_READ_ONLY
    local_real = RealValidationRequirement.CONTROLLED_LOCAL
    external_real = RealValidationRequirement.EXTERNAL_NOT_SAFE

    scheduler = _scheduler_properties()
    create_scheduler = dict(scheduler)
    create_scheduler.update({
        "title": _string(maximum=240),
        "instruction": _string(maximum=16_000),
        "schedule": SCHEDULE_SCHEMA,
        "enabled": _boolean(),
        "source": _string(maximum=32),
    })
    specs = [
        _spec("save_memory", "Persist one validated local memory", _object({
            "type": _string(enum=("route", "preference", "fact", "workflow", "contact", "shortcut"), maximum=32),
            "category": _string(enum=("app", "site", "deep_link", "folder", "file", "game", "user", "workflow", "contact", "other"), maximum=32),
            "key": _string(maximum=240), "title": _string(maximum=240),
            "value": MEMORY_VALUE_SCHEMA, "confidence": _number(0.0, 1.0),
            "tags": _array(_string(maximum=80), maximum=32),
        }, required=("type", "category", "key", "title", "value")), PL, C, EI,
              preconditions=("local_memory_available",), progress=effect,
              real=local_real, verified=True),
        _spec("create_scheduled_task", "Create one validated scheduled task",
              _object(create_scheduler, required=("title", "instruction", "schedule")), PL, N, EI,
              preconditions=("explicit_scheduling_intent",), progress=effect,
              real=local_real, verified=True),
        _spec("list_scheduled_tasks", "List local scheduled tasks", _object(), R, I, NC,
              preconditions=("scheduler_available",), progress=state, real=read_real),
        _spec("update_scheduled_task", "Update one scheduled task", _object({
            "task_id": IDENTIFIER, **scheduler,
        }, required=("task_id",), anyOf=[
            {"required": [field]} for field in (
                "title", "instruction", "schedule", "enabled", "conversation_id",
                "conversation_title", "conversation_query", "model", "model_id",
                "model_provider", "model_query",
            )
        ]), PL, C, EI, preconditions=("explicit_scheduling_intent",),
              progress=effect, real=local_real, verified=True),
        _spec("delete_scheduled_task", "Delete one scheduled task",
              _object({"task_id": IDENTIFIER}, required=("task_id",)), PL, I, EI,
              preconditions=("explicit_scheduling_intent",), progress=effect,
              real=local_real, verified=True),
        _spec("bash", "Execute one local shell command",
              _object({"command": _string(maximum=32_768),
                       "timeout": _nullable(_number(0.1, 300.0))}, required=("command",)),
              DY, D, CR, timeout=310.0, real=local_real),
        _spec("run_command", "Execute one local shell command",
              _object({"command": _string(maximum=32_768),
                       "timeout": _nullable(_number(0.1, 300.0))}, required=("command",)),
              DY, D, CR, timeout=310.0, real=local_real),
        _spec("open_application", "Launch a local application",
              _object({"name": _string(maximum=512)}, required=("name",)), RL, C, NC,
              preconditions=("application_resolvable",), real=RealValidationRequirement.USER_VISIBLE),
        _spec("open_uri", "Open an allowlisted URI through the OS",
              _object({"uri": URL}, required=("uri",)), RL, N, NC,
              preconditions=("allowlisted_uri_scheme",), real=RealValidationRequirement.USER_VISIBLE),
        _spec("take_screenshot", "Capture the current desktop", _object(), R, I, NC,
              preconditions=("screen_capture_available",), timeout=30.0,
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("read_file", "Read bounded local text", _object({"path": PATH}, required=("path",)),
              R, I, NC, preconditions=("readable_file",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("read_document", "Extract a bounded local document to a private artifact",
              _object({"path": PATH, "language": _nullable(_string(maximum=24)),
                       "max_file_bytes": _nullable(_integer(1, 20 * 1024 * 1024)),
                       "max_pages": _nullable(_integer(1, 100)),
                       "max_pixels": _nullable(_integer(1, 16_000_000)),
                       "max_chars": _nullable(_integer(1, 2_000_000)),
                       "timeout_seconds": _nullable(_number(1.0, 25.0))}, required=("path",)),
              PL, N, NC, preconditions=("supported_pdf_png_or_jpeg",), timeout=40.0,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              progress=ProgressEvidencePolicy.DOCUMENT_ARTIFACT, real=read_real, verified=True),
        _spec("read_document_artifact", "Read one bounded private artifact chunk",
              _object({"artifact_id": ARTIFACT_ID, "offset": _integer(0, 2_000_000),
                       "chunk_chars": _nullable(_integer(1, 8_000))}, required=("artifact_id",)),
              R, I, NC, preconditions=("authorized_live_artifact",),
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY, real=read_real),
        _spec("write_file", "Write bounded local text", _object({"path": PATH, "content": LARGE_TEXT},
              required=("path", "content")), PL, C, NC, progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("list_files", "List one local directory", _object({"path": PATH}, required=("path",)),
              R, I, NC, preconditions=("readable_directory",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("delete_file", "Delete one local file", _object({"path": PATH}, required=("path",)),
              PL, I, CR, preconditions=("existing_file",), progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("create_directory", "Create one local directory", _object({"path": PATH}, required=("path",)),
              PL, I, NC, progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("delete_directory", "Delete one local directory",
              _object({"path": PATH, "recursive": _boolean()}, required=("path",)),
              PL, I, CR, preconditions=("existing_directory",), progress=effect, real=local_real,
              verified=True, observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("rename_file", "Rename or move one local item",
              _object({"old_path": PATH, "new_path": PATH}, required=("old_path", "new_path")),
              PL, N, NC, progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("copy_file", "Copy one local file",
              _object({"src": PATH, "dst": PATH}, required=("src", "dst")),
              PL, C, NC, progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("get_file_info", "Read local file metadata", _object({"path": PATH}, required=("path",)),
              R, I, NC, progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("search_files", "Search bounded local filenames",
              _object({"directory": PATH, "pattern": _string(maximum=512)},
                      required=("directory", "pattern")), R, I, NC,
              progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("open_in_editor", "Open a local file in an editor",
              _object({"path": PATH}, required=("path",)), RL, C, NC,
              real=RealValidationRequirement.USER_VISIBLE,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("edit_file_lines", "Replace a bounded local line range",
              _object({"path": PATH, "start_line": _integer(1, 10_000_000),
                       "end_line": _integer(1, 10_000_000), "new_content": LARGE_TEXT},
                      required=("path", "start_line", "end_line", "new_content")),
              PL, C, NC, progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("get_system_info", "Read bounded host resource information", _object(), R, I, NC,
              progress=state, real=read_real),
        _spec("list_running_processes", "List bounded running-process metadata",
              _object({"limit": _integer(1, 200)}), R, I, NC, progress=state, real=read_real),
        _spec("kill_process", "Terminate one matching local process",
              _object({"name_or_pid": {"anyOf": [_integer(1, 2_147_483_647),
                                                   _string(maximum=260)]}},
                      required=("name_or_pid",)), PL, C, CR,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("set_volume", "Set local output volume",
              _object({"level": _integer(0, 100)}, required=("level",)), RL, I, NC,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("get_clipboard", "Read bounded clipboard text", _object(), R, I, NC,
              progress=state, real=RealValidationRequirement.USER_VISIBLE,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("set_clipboard", "Replace local clipboard text",
              _object({"text": _string(minimum=0, maximum=1_000_000)}, required=("text",)),
              RL, I, NC, real=RealValidationRequirement.USER_VISIBLE,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("navigate", "Navigate the managed browser", _object({"url": URL}, required=("url",)),
              RL, C, NC, preconditions=("managed_browser",),
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT, progress=state,
              real=RealValidationRequirement.USER_VISIBLE, verified=True),
        _spec("click", "Click one managed-browser element",
              _object({"selector": _string(maximum=4096)}, required=("selector",)),
              DY, D, FC, preconditions=("managed_browser",),
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("fill", "Prepare one managed-browser field",
              _object({"selector": _string(maximum=4096), "value": TEXT},
                      required=("selector", "value")), EM, I, NC,
              preconditions=("managed_browser",),
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY, real=external_real),
        _spec("inspect_form_fields", "Inspect bounded semantic form metadata",
              _object({"include_hidden": _boolean(), "include_disabled": _boolean(),
                       "max_fields": _integer(1, 100)}), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("fill_form_field", "Prepare and verify one semantic form field",
              _object({"field_ref": FIELD_REF,
                       "value": {"anyOf": [TEXT, _boolean(), _integer(-1_000_000_000, 1_000_000_000),
                                             _number(-1_000_000_000.0, 1_000_000_000.0), {"type": "null"}]}},
                      required=("field_ref", "value")), EM, I, NC,
              preconditions=("fresh_semantic_field_reference",),
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              progress=ProgressEvidencePolicy.VERIFIED_FORM_STATE,
              real=external_real, verified=True),
        _spec("read_page", "Read bounded visible browser text", _object(), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("get_current_url", "Read the managed browser URL", _object(), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real),
        _spec("scroll", "Scroll the managed browser",
              _object({"direction": _string(enum=("up", "down"), maximum=8),
                       "amount": _integer(1, 100_000)}), RL, C, NC,
              preconditions=("managed_browser",), real=RealValidationRequirement.USER_VISIBLE),
        _spec("go_back", "Navigate backward in managed-browser history", _object(), RL, C, NC,
              preconditions=("managed_browser",), progress=state,
              real=RealValidationRequirement.USER_VISIBLE, verified=True),
        _spec("go_forward", "Navigate forward in managed-browser history", _object(), RL, C, NC,
              preconditions=("managed_browser",), progress=state,
              real=RealValidationRequirement.USER_VISIBLE, verified=True),
        _spec("open_new_tab", "Open one managed-browser tab",
              _object({"url": URL}, required=("url",)), RL, N, NC,
              preconditions=("managed_browser",), progress=state,
              real=RealValidationRequirement.USER_VISIBLE, verified=True),
        _spec("find_and_click", "Click one managed-browser element by label",
              _object({"text": _string(maximum=1000)}, required=("text",)),
              DY, D, FC, preconditions=("managed_browser",),
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("wait_for_load", "Wait for managed-browser DOM readiness", _object(), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real),
        _spec("get_page_text", "Read larger bounded browser text", _object(), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("scroll_down", "Scroll the managed browser downward",
              _object({"amount": _nullable(_integer(1, 100_000))}), RL, C, NC,
              preconditions=("managed_browser",), real=RealValidationRequirement.USER_VISIBLE),
        _spec("type_text", "Type into one managed-browser element",
              _object({"selector": _string(maximum=4096), "text": TEXT},
                      required=("selector", "text")), EM, N, NC,
              preconditions=("managed_browser",),
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY, real=external_real),
        _spec("press_key", "Press one managed-browser key",
              _object({"key": _string(maximum=80)}, required=("key",)), DY, D, FC,
              preconditions=("managed_browser",), real=RealValidationRequirement.USER_VISIBLE),
        _spec("open_browser", "Open one supported managed browser",
              _object({"name": _string(enum=("firefox", "chrome", "chromium", "opera", "edge", "brave"), maximum=16),
                       "url": _nullable(URL)}, required=("name",)), RL, C, NC,
              preconditions=("supported_browser_installed",), progress=state,
              real=RealValidationRequirement.USER_VISIBLE, verified=True),
        _spec("desktop_click", "Click one described native desktop target",
              _object({"description": _string(maximum=1000)}, required=("description",)),
              DY, D, FC, timeout=120.0, result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("desktop_type", "Type text into the focused native application",
              _object({"text": TEXT}, required=("text",)), EM, N, NC,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("desktop_press", "Press one native desktop key or shortcut",
              _object({"key": _string(maximum=80)}, required=("key",)), DY, D, FC,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("desktop_scroll", "Scroll the focused native application",
              _object({"direction": _string(enum=("up", "down"), maximum=8),
                       "amount": _integer(1, 1000)}), RL, C, NC,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("is_running", "Check whether an application process is running",
              _object({"app": _string(maximum=260)}, required=("app",)), R, I, NC,
              progress=state, real=read_real),
        _spec("is_installed", "Check whether an application is installed",
              _object({"app": _string(maximum=260)}, required=("app",)), R, I, NC,
              progress=state, real=read_real),
        _spec("has_window", "Check whether an application has a visible window",
              _object({"app": _string(maximum=260)}, required=("app",)), R, I, NC,
              progress=state, real=read_real),
        _spec("steam_game_state", "Read deterministic Steam install/run state",
              _object({"name": _string(maximum=260)}, required=("name",)), R, I, NC,
              progress=state, real=read_real),
        _spec("web_state", "Read deterministic managed-browser state",
              _object({"expect_url": _nullable(_string(maximum=2048)),
                       "expect_text": _nullable(_string(maximum=4000)),
                       "expect_selector": _nullable(_string(maximum=4096))}), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_list_labels", "List Gmail labels", _object(), R, I, NC,
              preconditions=("gmail_read_connected",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_list_latest", "List bounded latest Gmail messages",
              _object({"max_results": _integer(1, 50)}), R, I, NC,
              preconditions=("gmail_read_connected",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_search", "Search bounded Gmail messages",
              _object({"query": _string(maximum=1000), "max_results": _integer(1, 50)},
                      required=("query",)), R, I, NC,
              preconditions=("gmail_read_connected",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_read_message", "Read one bounded Gmail message",
              _object({"message_id": IDENTIFIER}, required=("message_id",)), R, I, NC,
              preconditions=("gmail_read_connected",), progress=state, real=read_real,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_read_thread", "Read one bounded Gmail thread",
              _object({"thread_id": IDENTIFIER}, required=("thread_id",)), R, I, NC,
              preconditions=("gmail_read_connected",), progress=state, real=read_real,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_create_draft", "Create one Gmail draft",
              _object({"to": _string(maximum=2000), "subject": _string(minimum=0, maximum=998),
                       "body": _string(minimum=0, maximum=1_000_000),
                       "cc": _nullable(_string(minimum=0, maximum=2000)),
                       "bcc": _nullable(_string(minimum=0, maximum=2000))},
                      required=("to", "subject", "body")), EM, N, CR, timeout=150.0,
              preconditions=("gmail_compose_connected",), real=external_real, verified=True,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_send_draft", "Send one existing Gmail draft",
              _object({"draft_id": IDENTIFIER}, required=("draft_id",)), IE, N, CR, timeout=150.0,
              preconditions=("gmail_compose_connected",), real=external_real, verified=True,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_mark_read", "Mark one Gmail message read",
              _object({"message_id": IDENTIFIER}, required=("message_id",)), EM, I, CR, timeout=150.0,
              preconditions=("gmail_modify_connected",), real=external_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_mark_unread", "Mark one Gmail message unread",
              _object({"message_id": IDENTIFIER}, required=("message_id",)), EM, I, CR, timeout=150.0,
              preconditions=("gmail_modify_connected",), real=external_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_apply_label", "Apply one exact Gmail label",
              _object({"message_id": IDENTIFIER, "label": _string(maximum=225)},
                      required=("message_id", "label")), EM, I, CR, timeout=150.0,
              preconditions=("gmail_modify_connected",), real=external_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("gmail_remove_label", "Remove one exact Gmail label",
              _object({"message_id": IDENTIFIER, "label": _string(maximum=225)},
                      required=("message_id", "label")), EM, I, CR, timeout=150.0,
              preconditions=("gmail_modify_connected",), real=external_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
    ]
    return tuple(specs)


STATIC_TOOL_SPECS = _build_static_specs()
STATIC_TOOL_REGISTRY = ToolRegistry(STATIC_TOOL_SPECS)

_bound_static_registry: ToolRegistry | None = None
_dynamic_lock = threading.RLock()
_dynamic_specs: dict[str, ToolSpec] = {}


def bind_static_registry(dispatchers: Mapping[str, Callable[..., Any]]) -> ToolRegistry:
    global _bound_static_registry
    registry = STATIC_TOOL_REGISTRY.bind(dispatchers)
    _bound_static_registry = registry
    return registry


def get_static_registry() -> ToolRegistry:
    return _bound_static_registry or STATIC_TOOL_REGISTRY


def get_tool_spec(name: str) -> ToolSpec | None:
    static = get_static_registry().get(name)
    if static is not None:
        return static
    with _dynamic_lock:
        return _dynamic_specs.get(str(name or ""))


def compatibility_tool_spec(name: str, dispatcher: Callable[..., Any]) -> ToolSpec:
    """Conservative metadata for a transient in-process compatibility hook.

    Production tools are never expected to use this path; it preserves the
    long-standing mutable ``TOOL_MAP`` test seam without adding such callables
    to the authoritative static or connected-MCP inventory.
    """
    properties: dict[str, dict] = {}
    required: list[str] = []
    allow_extra = False
    try:
        for parameter in inspect.signature(dispatcher).parameters.values():
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                allow_extra = True
                continue
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
            }:
                continue
            properties[parameter.name] = {}
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)
    except (TypeError, ValueError):
        allow_extra = True
    return ToolSpec(
        name=str(name or "unknown_tool")[:128],
        dispatcher_name=getattr(dispatcher, "__name__", "compatibility_tool"),
        dispatcher=dispatcher,
        capability="Transient in-process compatibility capability",
        input_schema=_object(properties, required=required, allow_extra=allow_extra),
        preconditions=("transient_registry_binding",),
        side_effect=SideEffectClass.UNKNOWN_HIGH_RISK,
        idempotency=IdempotencyClass.UNKNOWN,
        confirmation=ConfirmationPolicy.NO_CONFIRMATION,
        timeout_seconds=30.0,
        retry=RetryPolicy.NEVER_AFTER_DISPATCH,
        providers=ALL_TEXT_PROVIDERS,
        result_policy=ResultPolicy.BOUNDED_MODEL_ONLY,
        observability=ObservabilityPolicy.METADATA_ONLY,
        progress_evidence=ProgressEvidencePolicy.NONE,
        deterministic_test_status=EvidenceClassification.METADATA_ONLY,
        controlled_integration_status=EvidenceClassification.METADATA_ONLY,
        real_validation_requirement=RealValidationRequirement.NONE,
    )


def register_dynamic_tool(
    *, name: str, dispatcher: Callable[..., Any], capability: str,
    input_schema: dict, connector_id: str, read_only: bool,
    semantics_known: bool,
) -> ToolSpec:
    if not callable(dispatcher):
        raise ToolRegistryError("dynamic dispatcher is not callable")
    hardened_schema = harden_input_schema(input_schema)
    try:
        Draft202012Validator.check_schema(hardened_schema)
    except SchemaError as exc:
        raise ToolRegistryError("invalid dynamic tool schema") from exc
    if read_only and semantics_known:
        side_effect = SideEffectClass.READ_ONLY
        idempotency = IdempotencyClass.IDEMPOTENT
        confirmation = ConfirmationPolicy.NO_CONFIRMATION
        retry = RetryPolicy.READ_ONCE_AFTER_CONFIRMED_FAILURE
    elif semantics_known:
        side_effect = SideEffectClass.EXTERNAL_MUTATION
        idempotency = IdempotencyClass.UNKNOWN
        confirmation = ConfirmationPolicy.CONFIRMATION_REQUIRED
        retry = RetryPolicy.NEVER_AFTER_DISPATCH
    else:
        side_effect = SideEffectClass.UNKNOWN_HIGH_RISK
        idempotency = IdempotencyClass.UNKNOWN
        confirmation = ConfirmationPolicy.CONFIRMATION_REQUIRED
        retry = RetryPolicy.NEVER_AFTER_DISPATCH
    spec = ToolSpec(
        name=name, dispatcher_name=getattr(dispatcher, "__name__", name),
        dispatcher=dispatcher,
        # Third-party descriptions may contain user/provider content. Keep the
        # evidence/observability registry categorical; prompt metadata remains
        # separately bounded by the established MCP registry.
        capability="Connected MCP capability",
        input_schema=hardened_schema,
        preconditions=("connector_connected",), side_effect=side_effect,
        idempotency=idempotency, confirmation=confirmation,
        timeout_seconds=150.0, retry=retry, providers=ALL_TEXT_PROVIDERS,
        result_policy=ResultPolicy.PRIVATE_MODEL_ONLY,
        observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
        progress_evidence=ProgressEvidencePolicy.NONE,
        deterministic_test_status=EvidenceClassification.METADATA_ONLY,
        controlled_integration_status=EvidenceClassification.METADATA_ONLY,
        real_validation_requirement=RealValidationRequirement.EXTERNAL_NOT_SAFE,
        verified_success=False, dynamic=True, connector_id=str(connector_id or "")[:128],
    )
    with _dynamic_lock:
        if name in _dynamic_specs:
            raise ToolRegistryError(f"duplicate dynamic tool name: {name}")
        _dynamic_specs[name] = spec
    return spec


def unregister_dynamic_tools(names: Iterable[str]) -> None:
    with _dynamic_lock:
        for name in names:
            _dynamic_specs.pop(str(name), None)


def dynamic_tool_specs() -> tuple[ToolSpec, ...]:
    with _dynamic_lock:
        return tuple(_dynamic_specs[name] for name in sorted(_dynamic_specs))


def _safe_capability(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text[:220] or "Connected MCP capability"


def harden_input_schema(schema: Any) -> dict:
    """Return a bounded copy suitable for validation, without trusting values."""
    if not isinstance(schema, dict):
        return _object()
    hardened = copy.deepcopy(schema)
    if not hardened:
        return _object()
    if hardened.get("type") in (None, "object") and isinstance(
        hardened.get("properties", {}), dict
    ):
        hardened.setdefault("type", "object")
        # Unknown top-level arguments are rejected unless the server explicitly
        # opted into them with additionalProperties=true/a schema.
        hardened.setdefault("additionalProperties", False)
        hardened.setdefault("maxProperties", 128)
    return hardened


def _reject_non_finite(value: Any, *, depth: int = 0) -> None:
    if depth > 32:
        raise ToolValidationError("structure_too_deep")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolValidationError("non_finite_number")
    if isinstance(value, dict):
        if len(value) > 128:
            raise ToolValidationError("object_too_large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ToolValidationError("invalid_property_name")
            _reject_non_finite(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 1024:
            raise ToolValidationError("array_too_large")
        for item in value:
            _reject_non_finite(item, depth=depth + 1)
    elif isinstance(value, str) and len(value) > 2_000_000:
        raise ToolValidationError("string_too_large")


def validate_arguments(schema: dict, arguments: Any) -> dict:
    """Strictly validate one call without coercion or value-bearing errors."""
    if not isinstance(arguments, dict):
        raise ToolValidationError("arguments_not_object")
    _reject_non_finite(arguments)
    effective = harden_input_schema(schema)
    try:
        Draft202012Validator.check_schema(effective)
        validator = Draft202012Validator(
            effective, format_checker=FormatChecker(),
        )
        validator.validate(arguments)
    except SchemaError as exc:
        raise ToolValidationError("invalid_tool_schema") from exc
    except ValidationError as exc:
        raise ToolValidationError("invalid_arguments") from exc
    except Exception as exc:
        # Reference-resolution and custom-vocabulary failures are third-party
        # schema failures, never dispatch errors and never value-bearing UI.
        raise ToolValidationError("invalid_tool_schema") from exc
    return dict(arguments)


def validation_error_text(lang: str = "en") -> str:
    if str(lang or "").lower().startswith("fr"):
        return "ERROR: arguments d'outil invalides ; l'outil n'a pas été exécuté"
    return "ERROR: invalid tool arguments; the tool was not executed"


_SHELL_META_RE = re.compile(r"[\r\n;&|<>`$(){}\[\]]")
_READ_ONLY_COMMANDS = frozenset({
    "cat", "date", "df", "dir", "du", "get-childitem", "get-content",
    "get-ciminstance", "get-date", "get-item", "get-location", "get-process",
    "head", "hostname", "id", "ls", "ps", "pwd", "select-string", "stat",
    "test-path", "type", "uname", "wc", "where", "where.exe", "which",
    "whoami",
})


def shell_command_is_proven_read_only(command: Any) -> bool:
    text = str(command or "").strip()
    if not text or len(text) > 32_768 or _SHELL_META_RE.search(text):
        return False
    try:
        parts = shlex.split(text, posix=False)
    except (TypeError, ValueError):
        return False
    if not parts:
        return False
    executable = parts[0].strip('"\'').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable.endswith(".exe") and executable != "where.exe":
        executable = executable[:-4]
    if executable not in _READ_ONLY_COMMANDS:
        return False
    forbidden = {
        "-delete", "--delete", "-exec", "--exec", "-command", "-encodedcommand",
        "-file", "-out-file", "-redirectstandardoutput", "-redirectstandarderror",
    }
    return not any(str(part).lower() in forbidden for part in parts[1:])


_FINAL_COMMIT_RE = re.compile(
    r"(?:^|[^a-z])(?:send|submit|apply|publish|post|confirm|finalize|purchase|pay|"
    r"save|delete|envoyer|soumettre|postuler|publier|confirmer|finaliser|payer|"
    r"enregistrer|supprimer)(?:[^a-z]|$)", re.IGNORECASE,
)


def is_final_browser_commit(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    name = str(tool_name or "")
    values = arguments if isinstance(arguments, Mapping) else {}
    if name in {"press_key", "desktop_press"}:
        return str(values.get("key") or "").strip().lower() in {"enter", "return"}
    field = {
        "click": "selector", "find_and_click": "text",
        "desktop_click": "description",
    }.get(name)
    return bool(field and _FINAL_COMMIT_RE.search(str(values.get(field) or "")))


def effective_contract(
    spec: ToolSpec, arguments: Mapping[str, Any], user_request: str = "",
) -> EffectiveToolContract:
    del user_request  # user prose never lowers the structural risk classification
    side_effect = spec.side_effect
    idempotency = spec.idempotency
    confirmation = spec.confirmation
    retry = spec.retry
    if spec.name in {"bash", "run_command"}:
        if shell_command_is_proven_read_only(arguments.get("command")):
            side_effect = SideEffectClass.READ_ONLY
            idempotency = IdempotencyClass.IDEMPOTENT
            confirmation = ConfirmationPolicy.NO_CONFIRMATION
            retry = RetryPolicy.READ_ONCE_AFTER_CONFIRMED_FAILURE
        else:
            side_effect = SideEffectClass.UNKNOWN_HIGH_RISK
            idempotency = IdempotencyClass.UNKNOWN
            confirmation = ConfirmationPolicy.CONFIRMATION_REQUIRED
            retry = RetryPolicy.NEVER_AFTER_DISPATCH
    elif spec.side_effect == SideEffectClass.DYNAMICALLY_CLASSIFIED:
        if is_final_browser_commit(spec.name, arguments):
            side_effect = SideEffectClass.IRREVERSIBLE_EXTERNAL
            idempotency = IdempotencyClass.NON_IDEMPOTENT
            confirmation = ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION
        else:
            side_effect = SideEffectClass.EXTERNAL_MUTATION
            idempotency = IdempotencyClass.UNKNOWN
            confirmation = ConfirmationPolicy.NO_CONFIRMATION
        retry = RetryPolicy.NEVER_AFTER_DISPATCH
    return EffectiveToolContract(spec, side_effect, idempotency, confirmation, retry)


def normalized_call_hash(tool_name: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"tool": str(tool_name), "arguments": dict(arguments)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ConfirmationGrant:
    token: str
    call_hash: str
    expires_at: float


class ConfirmationLedger:
    """Bounded, exact-call, expiring and single-use confirmation grants."""

    def __init__(self, *, ttl_seconds: float = 120.0, clock=None, maximum: int = 256):
        self.ttl_seconds = max(0.1, min(float(ttl_seconds), 600.0))
        self._clock = clock or time.monotonic
        self._maximum = max(1, min(int(maximum), 4096))
        self._lock = threading.Lock()
        self._counter = 0
        self._grants: dict[str, ConfirmationGrant] = {}

    def issue(self, tool_name: str, arguments: Mapping[str, Any]) -> ConfirmationGrant:
        call_hash = normalized_call_hash(tool_name, arguments)
        with self._lock:
            self._purge_locked()
            self._counter += 1
            seed = f"{self._counter}:{call_hash}:{self._clock():.9f}".encode("ascii")
            token = hashlib.sha256(seed).hexdigest()
            grant = ConfirmationGrant(
                token=token, call_hash=call_hash,
                expires_at=self._clock() + self.ttl_seconds,
            )
            self._grants[token] = grant
            while len(self._grants) > self._maximum:
                self._grants.pop(next(iter(self._grants)))
            return grant

    def consume(self, token: str, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        with self._lock:
            self._purge_locked()
            grant = self._grants.pop(str(token or ""), None)
        return bool(
            grant is not None
            and grant.expires_at >= self._clock()
            and grant.call_hash == normalized_call_hash(tool_name, arguments)
        )

    def _purge_locked(self) -> None:
        now = self._clock()
        for token, grant in tuple(self._grants.items()):
            if grant.expires_at < now:
                self._grants.pop(token, None)


CONFIRMATION_LEDGER = ConfirmationLedger()
_approved_call: ContextVar[str] = ContextVar("osia_approved_tool_call", default="")


@contextmanager
def confirmed_call_scope(tool_name: str, arguments: Mapping[str, Any]):
    token = _approved_call.set(normalized_call_hash(tool_name, arguments))
    try:
        yield
    finally:
        _approved_call.reset(token)


def current_call_is_confirmed(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    return _approved_call.get() == normalized_call_hash(tool_name, arguments)


def safe_confirmation_summary(
    spec: ToolSpec | None, effective: EffectiveToolContract | None = None,
) -> str:
    contract = effective
    side_effect = contract.side_effect if contract else (
        spec.side_effect if spec else SideEffectClass.UNKNOWN_HIGH_RISK
    )
    name = spec.name if spec else "unknown_tool"
    if name in {"delete_file", "delete_directory"}:
        return "Delete one local item"
    if name == "kill_process":
        return "Stop one local process"
    if name in {"bash", "run_command"}:
        return "Run one high-risk local command"
    if name == "gmail_send_draft":
        return "Send one existing Gmail draft"
    if name.startswith("gmail_"):
        return "Change one Gmail item"
    if side_effect == SideEffectClass.IRREVERSIBLE_EXTERNAL:
        return "Commit one final external action"
    if spec and spec.dynamic:
        return "Run one connected write capability"
    return "Perform one sensitive action"


def bounded_model_result(result: Any, *, limit: int = 64_000) -> Any:
    """Bound model reinjection without creating a log/diagnostic preview."""
    if isinstance(result, dict):
        clean = dict(result)
        text = str(clean.get("text", ""))
        if len(text) > limit:
            clean["text"] = text[:limit] + "\n... [TRUNCATED]"
        screenshot = clean.get("screenshot")
        if isinstance(screenshot, str) and len(screenshot) > 16_000_000:
            clean.pop("screenshot", None)
            clean["text"] = str(clean.get("text", "")) + (
                "\n[Oversized screenshot withheld]"
            )
        return clean
    text = "" if result is None else str(result)
    return text if len(text) <= limit else text[:limit] + "\n... [TRUNCATED]"


def static_tool_prompt_block(lang: str = "en") -> str:
    """Render provider-facing native schemas from the authoritative registry."""
    fr = str(lang or "").lower().startswith("fr")
    lines = [
        "## Outils disponibles (schemas autoritatifs)" if fr
        else "## Available Tools (authoritative schemas)",
        (
            "Les arguments doivent respecter exactement ces schemas; les champs inconnus sont refuses."
            if fr else
            "Arguments must match these schemas exactly; unknown fields are rejected."
        ),
    ]
    for spec in get_static_registry().values():
        schema = spec.input_schema
        props = schema.get("properties") if isinstance(schema, dict) else {}
        props = props if isinstance(props, dict) else {}
        required = schema.get("required") if isinstance(schema, dict) else []
        required = required if isinstance(required, list) else []
        parts = [f'"tool": "{spec.name}"']
        for key in required:
            if key in props:
                parts.append(f'"{key}": {_schema_placeholder(props[key])}')
        usage = "{" + ", ".join(parts) + "}"
        optional = [key for key in props if key not in required]
        suffix = ""
        if optional:
            label = "optionnels" if fr else "optional"
            suffix = f" ({label}: {', '.join(optional)})"
        lines.append(f"- {spec.name}: {spec.capability}. Usage: {usage}{suffix}")
    return "\n".join(lines)


def _schema_placeholder(schema: Any) -> str:
    if isinstance(schema, dict) and "anyOf" in schema:
        options = schema.get("anyOf") or []
        schema = next((item for item in options
                       if isinstance(item, dict) and item.get("type") != "null"), {})
    kind = schema.get("type") if isinstance(schema, dict) else None
    if kind == "boolean":
        return "true"
    if kind in {"integer", "number"}:
        return "0"
    if kind == "array":
        return "[]"
    if kind == "object":
        return "{}"
    return '"<value>"'


def replace_static_tool_prompt_section(body: str, lang: str = "en") -> str:
    fr = str(lang or "").lower().startswith("fr")
    start = "## Outils disponibles" if fr else "## Available Tools"
    end = "## Comment utiliser les outils" if fr else "## How to Use Tools"
    start_index = body.find(start)
    end_index = body.find(end)
    if start_index < 0 or end_index <= start_index:
        return body
    return (
        body[:start_index]
        + static_tool_prompt_block(lang)
        + "\n\n"
        + body[end_index:]
    )


def assert_prompt_tool_examples_compatible(block: str) -> None:
    """Fail startup when a hard-coded usage example drifts from the registry.

    Examples remain useful for capability-specific guidance, while tool names,
    required fields and allowed top-level fields stay mechanically checked.
    Placeholder values are intentionally not validated as real call values.
    """
    decoder = json.JSONDecoder()
    for line in str(block or "").splitlines():
        if "Usage" not in line:
            continue
        start = line.find("{", line.find("Usage"))
        if start < 0:
            continue
        try:
            example, _end = decoder.raw_decode(line[start:])
        except (TypeError, ValueError) as exc:
            raise ToolRegistryError("invalid provider tool usage example") from exc
        if not isinstance(example, dict) or not isinstance(example.get("tool"), str):
            raise ToolRegistryError("invalid provider tool usage example")
        spec = get_static_registry().get(example["tool"])
        if spec is None:
            continue
        schema = spec.input_schema
        properties = set((schema.get("properties") or {}).keys())
        supplied = set(example) - {"tool"}
        required = set(schema.get("required") or [])
        if not supplied <= properties or not required <= supplied:
            raise ToolRegistryError(
                f"provider usage example drift for {spec.name}"
            )


def matrix_rows(
    *, configured_connectors: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    rows = [_matrix_row(spec, executable=True) for spec in get_static_registry().values()]
    connected = {spec.connector_id for spec in dynamic_tool_specs() if spec.connector_id}
    rows.extend(_matrix_row(spec, executable=True) for spec in dynamic_tool_specs())
    for connector in configured_connectors:
        connector_id = str(connector.get("id") or "")[:128]
        if not connector_id or connector_id in connected:
            continue
        rows.append({
            "tool_name": f"connector::{connector_id}",
            "capability": "Configured connector inventory",
            "schema_status": "unavailable",
            "preconditions": ["connector_connected"],
            "side_effect_class": SideEffectClass.UNKNOWN_HIGH_RISK.value,
            "idempotency": IdempotencyClass.UNKNOWN.value,
            "confirmation_policy": ConfirmationPolicy.CONFIRMATION_REQUIRED.value,
            "timeout_seconds": None,
            "retry_policy": RetryPolicy.NEVER_AFTER_DISPATCH.value,
            "fallback_barrier": "on_dispatch",
            "result_redaction_policy": ResultPolicy.PRIVATE_MODEL_ONLY.value,
            "observability_policy": ObservabilityPolicy.PRIVATE_METADATA_ONLY.value,
            "supported_providers": list(ALL_TEXT_PROVIDERS),
            "deterministic_test_status": EvidenceClassification.METADATA_ONLY.value,
            "controlled_integration_status": EvidenceClassification.UNAVAILABLE_IN_ENVIRONMENT.value,
            "real_validation_requirement": RealValidationRequirement.EXTERNAL_NOT_SAFE.value,
            "current_evidence_classification": EvidenceClassification.UNAVAILABLE_IN_ENVIRONMENT.value,
            "currently_executable": False,
        })
    return sorted(rows, key=lambda row: row["tool_name"])


def _matrix_row(spec: ToolSpec, *, executable: bool) -> dict[str, Any]:
    if spec.real_validation_requirement == RealValidationRequirement.EXTERNAL_NOT_SAFE:
        evidence = EvidenceClassification.NOT_SAFELY_TESTABLE
    elif spec.real_validation_requirement != RealValidationRequirement.NONE:
        evidence = EvidenceClassification.REAL_VALIDATION_REQUIRED
    elif spec.deterministic_test_status == EvidenceClassification.PROVED:
        evidence = EvidenceClassification.PROVED
    else:
        evidence = EvidenceClassification.METADATA_ONLY
    return {
        "tool_name": spec.name,
        "capability": spec.capability,
        "schema_status": "validated",
        "preconditions": list(spec.preconditions),
        "side_effect_class": spec.side_effect.value,
        "idempotency": spec.idempotency.value,
        "confirmation_policy": spec.confirmation.value,
        "timeout_seconds": spec.timeout_seconds,
        "retry_policy": spec.retry.value,
        "fallback_barrier": "on_dispatch" if spec.fallback_barrier_on_dispatch else "on_unknown_outcome",
        "result_redaction_policy": spec.result_policy.value,
        "observability_policy": spec.observability.value,
        "supported_providers": list(spec.providers),
        "deterministic_test_status": spec.deterministic_test_status.value,
        "controlled_integration_status": spec.controlled_integration_status.value,
        "real_validation_requirement": spec.real_validation_requirement.value,
        "current_evidence_classification": evidence.value,
        "currently_executable": bool(executable),
    }


__all__ = [
    "ALL_TEXT_PROVIDERS", "CONFIRMATION_LEDGER", "ConfirmationLedger",
    "ConfirmationPolicy", "EffectiveToolContract", "EvidenceClassification",
    "ExecutionOutcome", "IdempotencyClass", "ObservabilityPolicy",
    "ProgressEvidencePolicy", "RealValidationRequirement", "ResultPolicy",
    "RetryPolicy", "STATIC_TOOL_REGISTRY", "STATIC_TOOL_SPECS",
    "SideEffectClass", "ToolRegistry", "ToolRegistryError", "ToolSpec",
    "ToolExecutionRecord", "ToolValidationError", "assert_prompt_tool_examples_compatible",
    "bind_static_registry", "bounded_model_result",
    "compatibility_tool_spec",
    "confirmed_call_scope", "current_call_is_confirmed", "dynamic_tool_specs",
    "effective_contract", "get_static_registry", "get_tool_spec",
    "harden_input_schema", "is_final_browser_commit", "matrix_rows",
    "normalized_call_hash", "register_dynamic_tool",
    "replace_static_tool_prompt_section", "safe_confirmation_summary",
    "shell_command_is_proven_read_only", "static_tool_prompt_block",
    "unregister_dynamic_tools", "validate_arguments", "validation_error_text",
]
