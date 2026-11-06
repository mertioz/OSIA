"""Authoritative OSIA tool contracts and execution-safety metadata (WOR-178).

The module is deliberately independent from :mod:`osia.agent`: static
dispatchers are bound only after all ``tool_*`` functions exist, and MCP tools
are registered dynamically.  This keeps direct function imports compatible and
avoids a circular import while making schemas and safety policy single-source.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
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
    PENDING = "pending"
    SUCCESS_VERIFIED = "success_verified"
    SUCCESS_UNVERIFIED = "success_unverified"
    FAILED_AFTER_EFFECT = "failed_after_effect"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLATION_REQUESTED = "cancellation_requested"
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


class ToolPersistenceFamily(str, Enum):
    """Bounded waiting profile shared by related tool owners."""

    BROWSER = "browser"
    FILE = "file"
    API_MCP = "api_mcp"
    SYSTEM = "system"
    GENERIC = "generic"


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
    "lmstudio", "mistral", "ollama", "openai", "openrouter", "xai",
)



# Persistence family for managed-browser work (WOR-186/WOR-300): these tools
# either return opaque browser state/receipts or are the established semantic
# form operations.  This classification gates the durable attempt release
# policy only; the model-visible inventory is always the complete
# runtime-available registry.
BROWSER_FAMILY_TOOL_NAMES = frozenset({
    "get_browser_state", "navigate_browser", "inspect_browser_page",
    "wait_for_browser_condition", "activate_browser_element",
    "list_browser_tabs", "select_browser_tab", "close_browser_tab",
    "reload_browser_page", "browser_back", "browser_forward",
    "open_new_browser_tab", "return_to_browser_opener", "search_web",
    "open_search_result", "paginate_browser", "scroll_infinite",
    "list_browser_dialogs", "handle_browser_dialog",
    "upload_browser_file", "download_browser_file",
    "inspect_form_fields", "fill_form_field",
})
BROWSER_VERIFY_TOOL_NAMES = frozenset({
    "inspect_form_fields", "get_browser_state", "inspect_browser_page",
    "wait_for_browser_condition", "list_browser_tabs",
    "list_browser_dialogs",
})

# Legacy Vision/Mouse V3 capabilities are dormant: registered for
# compatibility, but dispatch rejects them with a typed error.  They are
# genuinely unavailable on every surface and therefore never model-visible.
DORMANT_VISUAL_V3_TOOL_NAMES = frozenset({
    "request_visual_observation", "desktop_click",
})

_FILE_PERSISTENCE_TOOLS = frozenset({
    "read_file", "read_document", "read_document_artifact", "query_document",
    "write_file", "render_pdf", "list_files", "delete_file",
    "create_directory", "delete_directory", "rename_file", "copy_file",
    "get_file_info", "search_files", "open_file", "edit_file_lines",
})
_SYSTEM_PERSISTENCE_TOOLS = frozenset({
    "bash", "run_command", "open_application", "open_uri",
    "get_system_info", "list_running_processes", "kill_process", "set_volume",
    "get_clipboard", "set_clipboard", "is_running", "is_installed",
    "has_window", "steam_game_state",
})

_ACTIVE_TOOL_SURFACE: ContextVar[str] = ContextVar(
    "osia_active_tool_surface", default="",
)


@contextmanager
def effective_tool_surface_scope(surface: str):
    """Select the engine surface for one execution turn.

    WOR-300: the surface gates DISPATCH eligibility
    (``tool_is_eligible_in_current_surface``); it never reduces the
    model-visible inventory.
    """

    normalized = str(surface or "")
    # WOR-303: one canonical Chat execution surface (the empty token, with
    # "chat" accepted as its explicit alias) plus the Scheduled temporal
    # trigger.  No other engine surface exists any more.
    if normalized not in {"", "chat", "scheduled"}:
        raise ValueError("invalid_tool_surface")
    token = _ACTIVE_TOOL_SURFACE.set(normalized)
    try:
        yield normalized
    finally:
        _ACTIVE_TOOL_SURFACE.reset(token)


def current_tool_surface() -> str:
    return _ACTIVE_TOOL_SURFACE.get()


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
    permission_category: str = "none"
    postcondition_required: bool = False
    redaction_policy: str = "bounded_model_result"
    dispatcher: Callable[..., Any] | None = None
    dynamic: bool = False
    connector_id: str = ""
    attempted_effect: str = ""
    safe_reconciliation_descriptors: tuple[str, ...] = ()

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
    tool_attempt_id: str = ""
    worker_may_be_active: bool = False
    reason_code: str = ""
    process_facts: dict[str, Any] | None = None

    def __post_init__(self):
        facts = self.process_facts
        if facts is None and isinstance(self.result, ProcessOutput):
            facts = self.result.process_facts
        if not (
            getattr(getattr(self.contract, "spec", None), "name", "") in {"bash", "run_command"}
            and self.dispatched
            and not self.timed_out
            and not self.worker_may_be_active
            and str(getattr(self.outcome, "value", self.outcome)) in {
                "success_verified", "success_unverified", "failed_after_effect", "command_completed",
            }
        ):
            facts = None
        object.__setattr__(self, "process_facts", normalize_process_facts(facts))


def normalize_process_facts(value: object) -> dict[str, Any] | None:
    """Normalize a trusted completed-process receipt, never model claims.

    Native combined output is not relabelled as stdout: callers must retain
    that distinction when verifying an exact stdout requirement.
    """
    if not isinstance(value, Mapping) or value.get("kind") != "process":
        return None
    if not isinstance(value.get("source"), str) or value["source"] not in {"local_subprocess", "provider_native"}:
        return None
    if not isinstance(value.get("exit_code"), int) or isinstance(value.get("exit_code"), bool):
        return None
    if any(not isinstance(value.get(key), str) or not value[key] for key in ("command", "cwd")):
        return None
    if len(value["command"]) > 32_768 or len(value["cwd"]) > 32_768:
        return None
    stdout, stderr, combined = (value.get(key) for key in ("stdout", "stderr", "combined_output"))
    separate = isinstance(stdout, str) and isinstance(stderr, str) and combined is None
    aggregate = stdout is None and stderr is None and isinstance(combined, str)
    if not (separate or aggregate):
        return None
    if value.get("source") == "local_subprocess" and not separate:
        return None
    if not isinstance(value.get("output_truncated", False), bool):
        return None
    fields = {
        "kind": "process", "source": value["source"],
        "command": value["command"], "cwd": value["cwd"], "exit_code": value["exit_code"],
        "stdout": stdout[:64_000] if separate else None,
        "stderr": stderr[:64_000] if separate else None,
        "combined_output": combined[:64_000] if aggregate else None,
        "output_truncated": bool(value.get("output_truncated")) or any(
            isinstance(item, str) and len(item) > 64_000 for item in (stdout, stderr, combined)
        ),
    }
    return fields


class ProcessOutput(str):
    """Backward-compatible shell display text carrying actual subprocess facts."""

    def __new__(cls, text: str, *, command: str, cwd: str, stdout: str,
                stderr: str, exit_code: int):
        value = super().__new__(cls, text)
        facts = normalize_process_facts({
            "kind": "process", "source": "local_subprocess", "command": command,
            "cwd": cwd, "stdout": stdout, "stderr": stderr,
            "combined_output": None, "exit_code": exit_code,
        })
        if facts is None:
            raise ValueError("invalid_completed_process_facts")
        value.process_facts = facts
        return value


@dataclass(frozen=True)
class ToolPersistenceContract:
    """One value-free, tool-family-specific durable waiting policy.

    ``release_after_seconds`` bounds how long a Chat/provider turn waits for a
    local owner.  The owner may continue after that boundary, while the common
    attempt store performs read-only reconciliation.  Inactivity and hard
    limits remain distinct so new verified progress can extend only the former.
    """

    family: ToolPersistenceFamily
    release_after_seconds: float
    inactivity_timeout_seconds: float
    hard_timeout_seconds: float
    check_delays_seconds: tuple[float, ...]
    safe_alternatives: tuple[str, ...] = ()
    restart_reconciliation: bool = True


@dataclass(frozen=True)
class ToolValidationIssue:
    """One schema-derived, value-free argument diagnostic."""

    path: str
    code: str
    expected: Any = None


class ToolRegistryError(RuntimeError):
    pass


class ToolValidationError(ValueError):
    """One stable, value-free validation failure."""

    def __init__(
        self,
        code: str = "invalid_arguments",
        issues: Iterable[ToolValidationIssue] = (),
    ):
        super().__init__(code)
        self.code = code
        self.issues = tuple(issues)[:16]


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
ABSOLUTE_DOCUMENT_PATH = _string(
    maximum=4096, pattern=r"^(?:/|[A-Za-z]:[\\/]|\\\\)",
)
URL = _string(maximum=8192, pattern=r"^\S+$")
IDENTIFIER = _string(maximum=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
SEMANTIC_APPLICATION_LABEL = _string(
    maximum=512,
    pattern=r"^[^\x00-\x1f\x7f]+$",
)
FIELD_REF = _string(maximum=28, pattern=r"^fld_[A-Za-z0-9_-]{24}$")
ARTIFACT_ID = _string(maximum=36, pattern=r"^doc_[0-9a-f]{32}$")
WEB_ARTIFACT_ID = _string(maximum=36, pattern=r"^web_[0-9a-f]{32}$")
DOCUMENT_SOURCE_REF = _string(
    maximum=36, pattern=r"^(?:src|doc|web)_[0-9a-f]{32}$",
)
ELEMENT_REF = _string(maximum=80, pattern=r"^el_[A-Za-z0-9_-]{16,64}$")
BROWSER_STATE_REF = _object({
    "schema_version": {"const": 1},
    "browser_session_id": _string(maximum=80, pattern=r"^browser_[A-Za-z0-9_-]{16,64}$"),
    "context_id": _string(maximum=80, pattern=r"^context_[A-Za-z0-9_-]{16,64}$"),
    "context_epoch": _integer(1, 2_147_483_647),
    "page_id": _string(maximum=80, pattern=r"^page_[A-Za-z0-9_-]{16,64}$"),
    "page_generation": _integer(1, 2_147_483_647),
    "navigation_id": _string(maximum=80, pattern=r"^nav_[A-Za-z0-9_-]{16,64}$"),
    "document_epoch": _integer(0, 2_147_483_647),
    "created_at": _number(0.0, 99_999_999_999.0),
}, required=(
    "schema_version", "browser_session_id", "context_id", "context_epoch",
    "page_id", "page_generation", "navigation_id", "document_epoch",
    "created_at",
))
def _wait_spec_schema(maximum_timeout: float) -> dict:
    return _object({
        "condition_type": _string(enum=(
            "navigation_commit", "document_epoch_change", "url_change", "url_match",
            "dom_selector_state", "element_state", "text_appearance",
            "text_disappearance", "popup_creation", "dialog_appearance",
            "dialog_closure", "download_start", "download_completion",
            "network_quiet", "stable_content_fingerprint", "custom_postcondition",
        ), maximum=64),
        "expected": {},
        "timeout_seconds": _number(0.05, maximum_timeout),
        "quiet_period_seconds": _number(0.05, 10.0),
        "failure_policy": _string(enum=(
            "confirmed_failure", "outcome_unknown_if_dispatched",
        ), maximum=48),
    }, required=("condition_type", "timeout_seconds"))


WAIT_SPEC = _wait_spec_schema(60.0)
ACTIVATION_WAIT_SPEC = _wait_spec_schema(120.0)
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

PDF_TEXT = _string(maximum=64_000)
PDF_LIST = _array(_string(maximum=4_000), maximum=100)
PDF_LIST["minItems"] = 1
PDF_TABLE_ROW = _array(_string(maximum=4_000), maximum=12)
PDF_TABLE_ROW["minItems"] = 1
PDF_TABLE_ROWS = _array(PDF_TABLE_ROW, maximum=100)
PDF_NODE = _object({
    "id": _string(maximum=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"),
    "text": _string(maximum=500),
}, required=("id", "text"))
PDF_EDGE = _object({
    "from": _string(maximum=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"),
    "to": _string(maximum=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"),
    "label": _nullable(_string(maximum=80)),
}, required=("from", "to"))
PDF_BLOCK_SCHEMA = {
    "oneOf": [
        _object({
            "type": {"const": "heading"},
            "text": PDF_TEXT,
            "level": _integer(1, 3),
        }, required=("type", "text")),
        _object({
            "type": {"const": "paragraph"},
            "text": PDF_TEXT,
        }, required=("type", "text")),
        _object({
            "type": {"const": "bullet_list"},
            "items": PDF_LIST,
        }, required=("type", "items")),
        _object({
            "type": {"const": "numbered_list"},
            "items": PDF_LIST,
        }, required=("type", "items")),
        _object({
            "type": {"const": "table"},
            "headers": PDF_TABLE_ROW,
            "rows": PDF_TABLE_ROWS,
        }, required=("type", "headers", "rows")),
        _object({"type": {"const": "page_break"}}, required=("type",)),
        _object({
            "type": {"const": "flow_diagram"},
            "nodes": _array(PDF_NODE, maximum=12),
            "edges": _array(PDF_EDGE, maximum=20),
        }, required=("type", "nodes", "edges")),
    ],
}
PDF_RENDER_SCHEMA = _object({
    "title": _string(maximum=500),
    "filename": _string(maximum=120, pattern=r'^[^<>:"/\\|?*\x00-\x1f]+$'),
    "destination": _string(enum=("desktop",), maximum=16),
    "page_size": _string(enum=("A4", "Letter"), maximum=16),
    "blocks": _array(PDF_BLOCK_SCHEMA, maximum=200),
}, required=("title", "filename", "destination", "page_size", "blocks"))
PDF_RENDER_SCHEMA["properties"]["blocks"]["minItems"] = 1

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
    permission_category: str = "none",
    postcondition_required: bool = False,
    redaction_policy: str = "bounded_model_result",
    attempted_effect: str = "",
    safe_reconciliation_descriptors: Iterable[str] = (),
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
        permission_category=permission_category,
        postcondition_required=postcondition_required,
        redaction_policy=redaction_policy,
        attempted_effect=str(attempted_effect or ""),
        safe_reconciliation_descriptors=tuple(
            str(item or "")
            for item in safe_reconciliation_descriptors
            if str(item or "")
        ),
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
        _spec("declare_outcomes", "Optionally record high-level result notes ({id,result}); never required for tools or completion",
              _object({"outcomes": {"type": "array", "items": {"type": "object"}}}, required=("outcomes",)),
              R, I, NC, timeout=10.0, retry=RetryPolicy.NEVER_AFTER_DISPATCH),
        _spec("create_chat_skill", "Save a new reusable OSIA skill requested through /skillcreator",
              _object({
                  "name": _string(maximum=64, pattern=r"^[a-z][a-z0-9-]*$"),
                  "description": _string(maximum=300),
                  "instructions": _string(maximum=12_000),
                  "required_tools": _array(_string(maximum=128), maximum=24),
              }, required=("name", "description", "instructions")), PL, N, NC,
              preconditions=("user_selected_skillcreator",), progress=effect,
              real=local_real, verified=True, observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
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
              preconditions=("application_resolvable",),
              attempted_effect="application_open",
              postcondition_required=True,
              safe_reconciliation_descriptors=(
                  "local_desktop_application_observation",
              ),
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("open_uri", "Open an allowlisted URI through the OS",
              _object({"uri": URL}, required=("uri",)), RL, N, NC,
              preconditions=("allowlisted_uri_scheme",), real=RealValidationRequirement.USER_VISIBLE),
        _spec("take_screenshot", "Read current global desktop pixels for an explicitly requested whole-desktop or overlay context; returns bounded visual evidence and never a document artifact", _object(), R, I, NC,
              preconditions=("screen_capture_available",), timeout=30.0,
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec("save_screenshot", "Attach an exact window screenshot, including a background window, to the current reply",
              _object({"window": _string(maximum=240), "surface_ref": IDENTIFIER}), R, I, NC,
              preconditions=("screen_capture_available",), timeout=30.0,
              result=ResultPolicy.BOUNDED_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "inspect_desktop_surfaces",
              "Read structured native desktop state and resolve an exact foreground or background app into opaque window/view/tab/pane/control references; returns accessibility evidence and actuation preconditions, not pixels",
              _object({"target": _string(maximum=1000)}), R, I, NC,
              preconditions=("foreground_desktop_surface_available",), timeout=12.0,
              result=ResultPolicy.BOUNDED_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              progress=state, real=read_real),
        _spec(
              "request_visual_observation",
              "Observe the exact target visually using image input available to the current model. Returns one fresh frame in that model's normal reasoning loop; optionally refine a prior frame with visual_frame_id plus one normalized visual_bbox; read-only, privacy/capture restricted, maximum two refinements, and never action authority",
              _object({
                  "surface_ref": IDENTIFIER,
                  "reason": _string(enum=(
                      "ambiguity", "incomplete_accessibility", "custom_rendered",
                      "popup_or_overlay", "small_target", "state_conflict",
                      "verification",
                  ), maximum=32),
                  "target": _string(maximum=1000),
                  "visual_frame_id": IDENTIFIER,
                  "visual_bbox": _object({
                      "left": _number(0.0, 1.0),
                      "top": _number(0.0, 1.0),
                      "right": _number(0.0, 1.0),
                      "bottom": _number(0.0, 1.0),
                  }, required=("left", "top", "right", "bottom")),
                  "expected_application": SEMANTIC_APPLICATION_LABEL,
                  "expected_window_generation": IDENTIFIER,
                  "expected_native_window_id": IDENTIFIER,
              }, required=("surface_ref", "reason")), R, I, NC,
              preconditions=("effective_model_native_vision", "fresh_desktop_surface_ref"),
              timeout=30.0, result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              progress=state, real=RealValidationRequirement.USER_VISIBLE),
        _spec("read_file", "Read bounded local text", _object({"path": PATH}, required=("path",)),
              R, I, NC, preconditions=("readable_file",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec(
              "read_document",
              "Read an existing PDF or document with this dedicated tool, never shell, read_file, or render_pdf. Pass exactly one canonical source: path for an exact absolute local path, for example "
              '{"path":"C:\\\\Users\\\\Example\\\\Desktop\\\\report.pdf"}, '
              "or source_ref for an opaque attachment/browser artifact reference. "
              "max_pages is only a whole-document safety ceiling: omit it for "
              "selected-page questions and query the resulting document_id instead",
              _object({"path": ABSOLUTE_DOCUMENT_PATH,
                       "source_ref": DOCUMENT_SOURCE_REF,
                       "language": _nullable(_string(maximum=24)),
                       "max_file_bytes": _nullable(_integer(1, 20 * 1024 * 1024)),
                       "max_pages": _nullable(_integer(1, 100)),
                       "max_pixels": _nullable(_integer(1, 16_000_000)),
                       "max_chars": _nullable(_integer(1, 2_000_000)),
                       "timeout_seconds": _nullable(_number(1.0, 25.0))}, oneOf=[
                           {"required": ["path"]},
                           {"required": ["source_ref"]},
                       ]),
              PL, C, NC, preconditions=("authorized_document_source",), timeout=195.0,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              progress=ProgressEvidencePolicy.DOCUMENT_ARTIFACT, real=read_real,
              verified=True, redaction_policy="document_private_bounded"),
        _spec("read_document_artifact", "Read one bounded private artifact chunk",
              _object({"artifact_id": ARTIFACT_ID, "offset": _integer(0, 2_000_000),
                       "chunk_chars": _nullable(_integer(1, 8_000))}, required=("artifact_id",)),
              R, I, NC, preconditions=("authorized_live_artifact",),
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY, real=read_real),
        _spec("query_document", "Query one private structured document by opaque reference. For text on a requested PDF page, use search with the user's semantic target or chunks with provenance; pages lists rendered page-frame metadata and can be empty for native PDF text. Omit max_chars for the safe 8000-character response default; larger requested budgets up to 32000 are accepted but safely capped to 8000 per response",
              _object({
                  "document_id": ARTIFACT_ID,
                  "operation": _string(enum=(
                      "summary", "search", "chunks", "sections", "headings",
                      "table", "sheets", "range", "formula", "slides",
                      "notes", "embedded", "provenance",
                      "metadata", "ocr", "frames", "pages", "transcript",
                      "time_range", "timeline", "tracks", "chapters",
                  ), maximum=32),
                  "query": _nullable(_string(maximum=1_000)),
                  "cursor": _nullable(_string(maximum=64, pattern=r"^cursor_[a-z_]+_[0-9a-f]{8}$")),
                  "max_items": _integer(1, 100),
                  "max_chars": _integer(1, 32_000),
                  "section_id": _nullable(_string(maximum=32, pattern=r"^section_[0-9]{6}$")),
                  "table_id": _nullable(_string(maximum=32, pattern=r"^table_[0-9]{6}$")),
                  "sheet_id": _nullable(_string(maximum=32, pattern=r"^sheet_[0-9]{6}$")),
                  "cell_range_ref": _nullable(_string(maximum=32, pattern=r"^\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})?$")),
                  "slide_id": _nullable(_string(maximum=32, pattern=r"^slide_[0-9]{6}$")),
                  "embedded_artifact_id": _nullable(_string(maximum=40, pattern=r"^embedded_[0-9]{6}$")),
                  "frame_id": _nullable(_string(maximum=32, pattern=r"^frame_[0-9]{6}$")),
                  "track_id": _nullable(_string(maximum=32, pattern=r"^track_[0-9]{6}$")),
                  "start_seconds": _nullable(_number(0.0, 3_600.0)),
                  "end_seconds": _nullable(_number(0.001, 3_600.0)),
              }, required=("document_id", "operation"), allOf=[
                  {
                      "if": {"properties": {"operation": {"const": "search"}}, "required": ["operation"]},
                      "then": {"required": ["query"]},
                  },
                  {
                      "if": {"properties": {"operation": {"const": "range"}}, "required": ["operation"]},
                      "then": {"required": ["sheet_id", "cell_range_ref"]},
                  },
                  {
                      "if": {"properties": {"operation": {"enum": ["time_range", "timeline"]}}, "required": ["operation"]},
                      "then": {"required": ["start_seconds", "end_seconds"]},
                  },
              ]),
              R, I, NC, preconditions=("authorized_live_artifact",), timeout=30.0,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              progress=state, real=read_real,
              redaction_policy="document_private_bounded"),
        _spec("write_file", "Write bounded local text", _object({"path": PATH, "content": LARGE_TEXT},
              required=("path", "content")), PL, C, NC, progress=effect, real=local_real, verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec(
            "render_pdf",
            "Authoritative operation to create/export a PDF, place it on the Desktop, or produce an illustrated PDF. Use destination exactly 'desktop' (never a path) and page_size 'A4' or 'Letter'. Every block is one flat object with a type field: {'type':'heading','text':'...','level':1}, {'type':'paragraph','text':'...'}, {'type':'bullet_list','items':['...']}, {'type':'numbered_list','items':['...']}, {'type':'table','headers':['...'],'rows':[['...']]}, {'type':'page_break'}, or {'type':'flow_diagram','nodes':[{'id':'n1','text':'...'}],'edges':[{'from':'n1','to':'n2','label':'...'}]}",
            PDF_RENDER_SCHEMA,
            PL,
            N,
            CR,
            preconditions=("exact_authorized_desktop_destination",),
            progress=effect,
            deterministic=EvidenceClassification.PROVED,
            integration=EvidenceClassification.PROVED,
            real=RealValidationRequirement.USER_VISIBLE,
            verified=True,
            permission_category="files_modify",
            postcondition_required=True,
            observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
        ),
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
              real=RealValidationRequirement.USER_VISIBLE, verified=True),
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
        _spec("scroll", "Scroll the managed browser up or down and report the actual movement",
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
        _spec("desktop_click", "Click one described native desktop target; when the current model selected a target from request_visual_observation, pass that exact visual_frame_id and one normalized visual_bbox",
              _object({
                  "description": _string(maximum=1000),
                  "visual_frame_id": IDENTIFIER,
                  "visual_bbox": _object({
                      "left": _number(0.0, 1.0),
                      "top": _number(0.0, 1.0),
                      "right": _number(0.0, 1.0),
                      "bottom": _number(0.0, 1.0),
                  }, required=("left", "top", "right", "bottom")),
              }, required=("description",)),
              DY, D, FC, timeout=120.0, result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "activate_desktop_surface",
              "Activate one fresh opaque native desktop candidate/pattern pair through background-safe UIA Invoke/SelectionItem/Toggle/ExpandCollapse when that exact pattern is currently actionable, or use action_pattern=Window for an exact window when foreground is a proven downstream precondition; objective must exactly match its current safe accessible label. action_pattern must come from the candidate's fresh structured actuation patterns, never from name guessing. For a stateless Invoke, observable_effect must identify a different observed surface and the exact independently observable text expected after this activation; invocation alone is never proof. Optionally provide semantic_batch as the complete 2-8 primitive sequence (including this primary first primitive) only when every item is branch-independent, reversible local work with no permission or transition boundary. Each item needs a unique exact post-primitive observable effect that is not already true before the batch; different items may use different observed witness surfaces. When a control leaves the primary display unchanged, use a fresh expression/status witness instead of claiming the unchanged display as its effect. The top-level observable_effect must equal the last item. OSIA journals and freshness-checks every primitive, stops at the first uncertainty/stale/transition, and performs one final semantic checkpoint",
              _object({
                  "surface_ref": IDENTIFIER,
                  "objective": _string(maximum=240),
                  "action_pattern": _string(
                      enum=(
                          "Invoke", "SelectionItem", "Toggle",
                          "ExpandCollapse", "Window",
                      ),
                      maximum=32,
                  ),
                  "observable_effect": _object({
                      "kind": _string(
                          enum=("exact_surface_text",), maximum=48,
                      ),
                      "surface_ref": IDENTIFIER,
                      "expected_text": _string(maximum=240),
                  }, required=("kind", "surface_ref", "expected_text")),
                  "semantic_batch": {
                      "type": "array",
                      "minItems": 2,
                      "maxItems": 8,
                      "items": _object({
                          "surface_ref": IDENTIFIER,
                          "objective": _string(maximum=240),
                          "action_pattern": _string(
                              enum=("Invoke", "SelectionItem", "Toggle"),
                              maximum=32,
                          ),
                          "observable_effect": _object({
                              "kind": _string(
                                  enum=("exact_surface_text",), maximum=48,
                              ),
                              "surface_ref": IDENTIFIER,
                              "expected_text": _string(maximum=240),
                          }, required=(
                              "kind", "surface_ref", "expected_text",
                          )),
                          "branch_independent": {"type": "boolean", "const": True},
                          "transition_boundary": _string(
                              enum=("none",), maximum=16,
                          ),
                          "permission_boundary": {"type": "boolean", "const": False},
                          "safety_class": _string(
                              enum=("reversible_local",), maximum=32,
                          ),
                      }, required=(
                          "surface_ref", "objective", "action_pattern",
                          "observable_effect", "branch_independent",
                          "transition_boundary", "permission_boundary",
                          "safety_class",
                      )),
                  },
              }, required=("surface_ref", "objective", "action_pattern")),
              DY, D, FC, timeout=30.0,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE,
              postcondition_required=True,
              safe_reconciliation_descriptors=(
                  "local_desktop_application_observation",
              )),
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
        _spec(
              "computer_screenshot",
              "Capture one exact native window without changing focus, including background windows",
              _object({"window": _string(maximum=240), "surface_ref": IDENTIFIER}),
              R, I, NC,
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.CONTROLLED_READ_ONLY),
        _spec(
              "computer_zoom",
              "Create one read-only crop of the current Computer Use frame and attach those exact pixels to the same active model. Coordinates are pixels in the referenced image",
              _object({
                  "frame_id": IDENTIFIER,
                  "left": _number(0, 1_000_000),
                  "top": _number(0, 1_000_000),
                  "right": _number(0, 1_000_000),
                  "bottom": _number(0, 1_000_000),
              }, required=("frame_id", "left", "top", "right", "bottom")),
              R, I, NC,
              result=ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.CONTROLLED_READ_ONLY),
        _spec(
              "computer_observation",
              "After inspecting a fresh Computer Use V4 image, optionally report the selected model's interpretation as satisfied, not satisfied, or inconclusive. Reuse frame_id exactly. This is a read-only frame-bound model report, not independent verification or proof that a requested effect occurred. Use the current pixels or an authoritative structured source to support a factual visual claim",
              _object({
                  "frame_id": IDENTIFIER,
                  "status": _string(enum=(
                      "satisfied", "not_satisfied", "inconclusive",
                  ), maximum=32),
                  "observed_state": _string(maximum=512),
              }, required=("frame_id", "status", "observed_state")),
              R, I, NC,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.CONTROLLED_READ_ONLY),
        _spec(
              "computer_click",
              "Click once at x/y pixels in the exact image identified by frame_id. Coordinates are never repaired. V4 checks bounds, target identity, foreground, geometry, stale client pixels, permission, and ActionTruth immediately before dispatch. Call computer_screenshot after the result",
              _object({
                  "frame_id": IDENTIFIER,
                  "x": _number(0, 1_000_000),
                  "y": _number(0, 1_000_000),
                  "intent": _string(maximum=160),
              }, required=("frame_id", "x", "y")),
              DY, D, FC,
              timeout=120.0,
              verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "computer_double_click",
              "Double-click once at x/y pixels in the exact image identified by frame_id through the same V4 stale, permission, and exactly-once boundary. Call computer_screenshot after the result",
              _object({
                  "frame_id": IDENTIFIER,
                  "x": _number(0, 1_000_000),
                  "y": _number(0, 1_000_000),
                  "intent": _string(maximum=160),
              }, required=("frame_id", "x", "y")),
              DY, D, FC,
              timeout=120.0,
              verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "computer_type",
              "Type text into the focused element of the exact window bound to frame_id. V4 revalidates the frame and ActionTruth before the existing native keyboard primitive. Call computer_screenshot after an unbatched action",
              _object({
                  "frame_id": IDENTIFIER,
                  "text": TEXT,
                  "intent": _string(maximum=160),
              }, required=("frame_id", "text")),
              EM, N, NC,
              timeout=120.0,
              verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "computer_keypress",
              "Press one key or shortcut in the exact window bound to frame_id. Enter/Return and other commit-like keys retain the existing final-action permission classification. Call computer_screenshot after the result",
              _object({
                  "frame_id": IDENTIFIER,
                  "key": _string(maximum=80),
                  "intent": _string(maximum=160),
                  "observe_after": _boolean(),
              }, required=("frame_id", "key")),
              DY, D, FC,
              timeout=120.0,
              verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "computer_scroll",
              "Scroll the exact foreground window bound to frame_id through the V4 stale and ActionTruth boundary. Call computer_screenshot when the new pixels are needed",
              _object({
                  "frame_id": IDENTIFIER,
                  "direction": _string(
                      enum=("up", "down", "left", "right"), maximum=8,
                  ),
                  "amount": _integer(1, 50),
                  "intent": _string(maximum=160),
                  "observe_after": _boolean(),
              }, required=("frame_id",)),
              RL, C, NC,
              timeout=120.0,
              verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=RealValidationRequirement.USER_VISIBLE),
        _spec(
              "computer_actions",
              "Execute a bounded list of 1-3 branch-independent Computer Use actions from one current frame when no intermediate screenshot is needed. Stop on stale, failure, uncertainty, or observe_after. Use separate screenshot/action calls when the next action depends on seeing a result",
              _object({
                  "actions": {**_array(_object({
                      "kind": _string(enum=(
                          "click", "double_click", "type", "keypress", "scroll",
                      ), maximum=24),
                      "frame_id": IDENTIFIER,
                      "x": _nullable(_number(0, 1_000_000)),
                      "y": _nullable(_number(0, 1_000_000)),
                      "text": _string(maximum=1_000_000),
                      "key": _string(maximum=80),
                      "direction": _string(
                          enum=("up", "down", "left", "right"), maximum=8,
                      ),
                      "amount": _integer(1, 50),
                      "observe_after": _boolean(),
                      "effect_id": _string(maximum=160),
                  }, required=("kind", "frame_id")), maximum=3), "minItems": 1},
              }, required=("actions",)),
              DY, D, FC,
              timeout=120.0,
              verified=True,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
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
        _spec("steam_game_state",
              "Read a Steam game's install status, running status and download progress by title or app ID",
              _object({"name": _string(maximum=260)}, required=("name",)), R, I, NC,
              progress=state, real=read_real),
        _spec("web_state", "Read deterministic managed-browser state",
              _object({"expect_url": _nullable(_string(maximum=2048)),
                       "expect_text": _nullable(_string(maximum=4000)),
                       "expect_selector": _nullable(_string(maximum=4096))}), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY),
        _spec("get_browser_state", "Read the authoritative opaque browser state",
              _object(), R, I, NC, preconditions=("managed_browser",),
              progress=state, real=read_real,
              observability=ObservabilityPolicy.IDENTIFIERS_ONLY,
              redaction_policy="browser_private_bounded"),
        _spec("navigate_browser", "Navigate from an optional authoritative browser state",
              _object({"url": URL, "state_ref": _nullable(BROWSER_STATE_REF),
                       "wait_spec": _nullable(WAIT_SPEC)}, required=("url",)),
              RL, C, NC, preconditions=("managed_browser",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("inspect_browser_page", "Inspect bounded structured rendered page content",
              _object({
                  "state_ref": _nullable(BROWSER_STATE_REF),
                  "mode": _string(enum=(
                      "snapshot", "main_content", "article", "headings", "links",
                      "metadata", "table", "list", "page_text_chunk",
                  ), maximum=32),
                  "target_ref": _nullable(ELEMENT_REF),
                  "cursor": _nullable(ELEMENT_REF),
                  "frame_ref": _nullable(ELEMENT_REF),
                  "max_items": _integer(1, 100),
              }), R, I, NC, preconditions=("managed_browser",), progress=state,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              real=read_real, redaction_policy="browser_private_bounded"),
        _spec("wait_for_browser_condition", "Wait for one bounded browser condition",
              _object({"state_ref": BROWSER_STATE_REF, "wait_spec": WAIT_SPEC},
                      required=("state_ref", "wait_spec")), R, I, NC,
              preconditions=("fresh_browser_reference",), timeout=65.0,
              progress=state, real=read_real,
              observability=ObservabilityPolicy.IDENTIFIERS_ONLY,
              postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("activate_browser_element", "Activate one opaque browser element exactly once",
              _object({
                  "state_ref": BROWSER_STATE_REF, "element_ref": ELEMENT_REF,
                  "wait_spec": ACTIVATION_WAIT_SPEC,
                  "trusted_final_action": _nullable(_boolean()),
                  "destination_origin": _nullable(_string(maximum=512)),
                  "trusted_target_binding": _nullable(_string(maximum=64)),
              }, required=("state_ref", "element_ref", "wait_spec")), DY, D, FC,
              preconditions=("fresh_browser_and_element_references",), timeout=125.0,
              progress=effect, real=RealValidationRequirement.USER_VISIBLE,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("list_browser_tabs", "List authoritative managed-browser tabs",
              _object({"state_ref": _nullable(BROWSER_STATE_REF)}), R, I, NC,
              preconditions=("managed_browser",), progress=state, real=read_real,
              observability=ObservabilityPolicy.IDENTIFIERS_ONLY,
              redaction_policy="browser_private_bounded"),
        _spec("select_browser_tab", "Select one authoritative managed-browser tab",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              RL, I, NC, preconditions=("fresh_browser_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("close_browser_tab", "Close one authoritative managed-browser tab",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              RL, I, NC, preconditions=("fresh_browser_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("reload_browser_page", "Reload one authoritative browser page",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              RL, C, NC, preconditions=("fresh_browser_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("browser_back", "Navigate backward from an authoritative browser state",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              RL, C, NC, preconditions=("fresh_browser_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("browser_forward", "Navigate forward from an authoritative browser state",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              RL, C, NC, preconditions=("fresh_browser_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("open_new_browser_tab", "Open and verify one new managed-browser tab",
              _object({"url": URL, "state_ref": _nullable(BROWSER_STATE_REF)},
                      required=("url",)), RL, N, NC,
              preconditions=("managed_browser",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("return_to_browser_opener", "Return from a popup to its opener",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              RL, I, NC, preconditions=("popup_with_live_opener",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("search_public_web", "Search public information and return source URLs without opening a browser",
              _object({"query": _string(minimum=1, maximum=300)}, required=("query",)),
              R, I, NC, timeout=30.0, retry=RetryPolicy.NEVER_AFTER_DISPATCH,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY, progress=state, real=read_real),
        _spec("read_public_web_page", "Read a public HTTP(S) page as text without opening a browser. Use next_offset to continue",
              _object({"url": URL, "offset": _integer(0, 2 * 1024 * 1024)}, required=("url",)),
              R, I, NC, timeout=30.0, retry=RetryPolicy.NEVER_AFTER_DISPATCH,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY, progress=state, real=read_real),
        _spec("search_web", "Search in the managed browser and return opaque result references; inspecting existing links is not a search",
              _object({"query": _string(minimum=1, maximum=300),
                       "state_ref": _nullable(BROWSER_STATE_REF)}, required=("query",)),
              RL, C, NC, preconditions=("configured_search_engine",), progress=effect,
              real=RealValidationRequirement.CONTROLLED_READ_ONLY,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("open_search_result", "Open one opaque result_ref returned by search_web; use this instead of generic element activation for a structured search result",
              _object({"state_ref": BROWSER_STATE_REF, "result_ref": ELEMENT_REF},
                      required=("state_ref", "result_ref")), RL, N, NC,
              preconditions=("fresh_search_result_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("paginate_browser", "Activate one fresh opaque next-page control per bounded page and return the verified current page state",
              _object({"state_ref": BROWSER_STATE_REF, "next_ref": ELEMENT_REF,
                       "max_pages": _integer(1, 20)},
                      required=("state_ref", "next_ref")), RL, N, NC,
              preconditions=("fresh_pagination_reference",), timeout=450.0,
              progress=effect, real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("scroll_infinite", "Scroll a bound browser page down to load more items",
              _object({"state_ref": BROWSER_STATE_REF, "max_steps": _integer(1, 20)},
                      required=("state_ref",)), RL, C, NC,
              preconditions=("fresh_browser_reference",), timeout=150.0,
              progress=effect, real=RealValidationRequirement.USER_VISIBLE,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("list_browser_dialogs", "List pending browser dialogs without message content",
              _object({"state_ref": BROWSER_STATE_REF}, required=("state_ref",)),
              R, I, NC, preconditions=("fresh_browser_reference",), progress=state,
              real=read_real, observability=ObservabilityPolicy.IDENTIFIERS_ONLY,
              redaction_policy="browser_private_bounded"),
        _spec("handle_browser_dialog", "Accept or dismiss one exact browser dialog",
              _object({"state_ref": BROWSER_STATE_REF, "dialog_ref": ELEMENT_REF,
                       "action": _string(enum=("accept", "dismiss"), maximum=16),
                       "prompt_text": _nullable(_string(maximum=4_000))},
                      required=("state_ref", "dialog_ref", "action")),
              EM, N, NC, preconditions=("fresh_dialog_reference",), progress=effect,
              real=RealValidationRequirement.USER_VISIBLE,
              observability=ObservabilityPolicy.PRIVATE_METADATA_ONLY,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_private_bounded"),
        _spec("upload_browser_file", "Select one exact private file for one exact browser target",
              _object({
                  "state_ref": BROWSER_STATE_REF, "element_ref": ELEMENT_REF,
                  "artifact_id": _nullable(WEB_ARTIFACT_ID),
                  "local_path": _nullable(PATH),
                  "prepared_upload_ref": _nullable(_string(maximum=80)),
                  "destination_origin": _nullable(_string(maximum=512)),
                  "file_size": _nullable(_integer(0, 512 * 1024 * 1024)),
                  "detected_mime": _nullable(_string(maximum=160)),
                  "operation_expires_at": _nullable(_number(0.0, 99_999_999_999.0)),
              }, required=("state_ref", "element_ref"), oneOf=[
                  {"required": ["artifact_id"], "properties": {"artifact_id": WEB_ARTIFACT_ID}},
                  {"required": ["local_path"], "properties": {"local_path": PATH}},
              ]), IE, N, CR, preconditions=(
                  "fresh_browser_reference", "exact_file_identity", "exact_origin",
                  "exact_upload_target", "unexpired_upload_operation",
              ), timeout=90.0, progress=effect, real=local_real,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.IDENTIFIERS_ONLY,
              permission_category="send_publish_submit", postcondition_required=True,
              redaction_policy="browser_upload_private"),
        _spec("download_browser_file", "Download one exact target into a private opaque artifact",
              _object({"state_ref": BROWSER_STATE_REF, "element_ref": ELEMENT_REF},
                      required=("state_ref", "element_ref")), EM, N, NC,
              preconditions=("fresh_browser_and_element_references",), timeout=180.0,
              progress=ProgressEvidencePolicy.DOCUMENT_ARTIFACT, real=local_real,
              result=ResultPolicy.PRIVATE_MODEL_ONLY,
              observability=ObservabilityPolicy.IDENTIFIERS_ONLY,
              permission_category="browser_account", postcondition_required=True,
              redaction_policy="browser_download_private"),
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
    browser_tools = {
        "navigate", "click", "fill", "inspect_form_fields", "fill_form_field",
        "read_page", "get_current_url", "scroll", "go_back", "go_forward",
        "open_new_tab", "find_and_click", "wait_for_load", "get_page_text",
        "scroll_down", "type_text", "press_key", "open_browser", "web_state",
        "get_browser_state", "navigate_browser", "inspect_browser_page",
        "wait_for_browser_condition", "activate_browser_element",
        "list_browser_tabs", "select_browser_tab", "close_browser_tab",
        "reload_browser_page", "browser_back", "browser_forward",
        "open_new_browser_tab", "return_to_browser_opener", "search_web",
        "open_search_result", "paginate_browser", "scroll_infinite",
        "list_browser_dialogs", "handle_browser_dialog",
        "upload_browser_file", "download_browser_file",
        "computer_screenshot", "computer_zoom", "computer_observation",
        "computer_click",
        "computer_double_click", "computer_type", "computer_keypress",
        "computer_scroll", "computer_actions",
    }
    legacy_browser_tools = {
        "navigate", "click", "fill", "read_page", "get_current_url",
        "scroll", "go_back", "go_forward", "open_new_tab",
        "find_and_click", "wait_for_load", "get_page_text", "scroll_down",
        "type_text", "press_key", "open_browser", "web_state",
    }
    normalized = []
    for spec in specs:
        if spec.name not in browser_tools:
            normalized.append(spec)
            continue
        normalized.append(replace(
            spec,
            permission_category=(
                spec.permission_category
                if spec.permission_category != "none"
                else "browser_account"
            ),
            postcondition_required=(
                spec.postcondition_required
                or spec.side_effect != SideEffectClass.READ_ONLY
            ),
            redaction_policy=(
                spec.redaction_policy
                if spec.redaction_policy != "bounded_model_result"
                else "browser_private_bounded"
            ),
        ))
    return tuple(normalized)


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


def _validation_issue(error: ValidationError) -> list[ToolValidationIssue]:
    """Project jsonschema errors without copying rejected argument values."""

    path_parts = [str(item)[:80] for item in error.absolute_path]
    base_path = "$" + "".join(
        f"[{item}]" if item.isdigit() else f".{item}"
        for item in path_parts
    )
    validator = str(error.validator or "invalid")[:48]
    if validator in {"anyOf", "oneOf"} and error.context:
        # Report constraints for branches matching the value's JSON type.
        # Nullable numeric failures otherwise say only "anyOf", hiding the
        # actual bound. Never copy error.message or the rejected instance.
        matching = [item for item in error.context if item.validator != "type"]
        if matching:
            return [
                issue for item in matching[:8] for issue in _validation_issue(item)
            ][:16]
    if validator == "required" and isinstance(error.validator_value, list):
        instance_keys = set(error.instance) if isinstance(error.instance, dict) else set()
        properties = (
            error.schema.get("properties")
            if isinstance(error.schema, dict)
            and isinstance(error.schema.get("properties"), dict)
            else {}
        )
        return [
            ToolValidationIssue(
                path=f"{base_path}.{str(name)[:80]}",
                code="missing_required_field",
                expected=_safe_schema_expectation(properties.get(name)),
            )
            for name in error.validator_value
            if isinstance(name, str) and name not in instance_keys
        ][:16]
    if validator == "additionalProperties" and isinstance(error.instance, dict):
        allowed = set(
            (error.schema.get("properties") or {}).keys()
            if isinstance(error.schema, dict) else ()
        )
        unexpected = [
            str(name)[:80]
            for name in error.instance
            if isinstance(name, str) and name not in allowed
        ][:16]
        if unexpected:
            return [
                ToolValidationIssue(
                    path=f"{base_path}.{name}",
                    code="unexpected_field",
                    expected="declared_schema_field",
                )
                for name in unexpected
            ]
    expected: Any = None
    if validator == "type":
        raw = error.validator_value
        if isinstance(raw, str):
            expected = raw[:48]
        elif isinstance(raw, list):
            expected = [str(item)[:48] for item in raw[:8]]
    elif validator == "enum" and isinstance(error.validator_value, list):
        allowed = []
        for item in error.validator_value[:16]:
            if item is None or isinstance(item, (bool, int, float)):
                allowed.append(item)
            elif isinstance(item, str) and len(item) <= 80:
                allowed.append(item)
        expected = {"allowed": allowed}
    elif validator in {
        "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
    } and isinstance(error.validator_value, (int, float)):
        expected = {validator: error.validator_value}
    elif validator in {
        "format", "pattern", "oneOf", "anyOf",
        "additionalProperties",
    }:
        expected = validator
    return [ToolValidationIssue(base_path, validator, expected)]


def _safe_schema_expectation(schema: Any) -> Any:
    """Project only field types/enums from a schema, never instance values."""

    if not isinstance(schema, dict):
        return "required"
    enum = schema.get("enum")
    if isinstance(enum, list):
        allowed = [
            item for item in enum[:16]
            if item is None or isinstance(item, (bool, int, float))
            or isinstance(item, str) and len(item) <= 80
        ]
        return {"allowed": allowed}
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        return raw_type[:48]
    if isinstance(raw_type, list):
        return [str(item)[:48] for item in raw_type[:8]]
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        types = []
        for candidate in any_of[:8]:
            if isinstance(candidate, dict) and isinstance(candidate.get("type"), str):
                types.append(candidate["type"][:48])
        if types:
            return sorted(set(types))
    return "required"


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
        errors = sorted(
            validator.iter_errors(arguments),
            key=lambda item: (
                tuple(str(part) for part in item.absolute_path),
                str(item.validator or ""),
            ),
        )
        if errors:
            issues: list[ToolValidationIssue] = []
            for error in errors[:16]:
                issues.extend(_validation_issue(error))
            raise ToolValidationError("invalid_arguments", issues[:16])
    except SchemaError as exc:
        raise ToolValidationError("invalid_tool_schema") from exc
    except ToolValidationError:
        raise
    except ValidationError as exc:
        raise ToolValidationError("invalid_arguments") from exc
    except Exception as exc:
        # Reference-resolution and custom-vocabulary failures are third-party
        # schema failures, never dispatch errors and never value-bearing UI.
        raise ToolValidationError("invalid_tool_schema") from exc
    return dict(arguments)


def validate_tool_arguments(spec: ToolSpec, arguments: Any) -> dict:
    """Validate a call plus browser wait semantics before any dispatch gate."""

    normalized = arguments
    if spec.name == "request_visual_observation" and isinstance(arguments, dict):
        reason = arguments.get("reason")
        reason_schema = (
            spec.input_schema.get("properties", {}).get("reason", {})
        )
        canonical_reasons = set(reason_schema.get("enum", ()))
        if (
            isinstance(reason, str)
            and reason.strip()
            and len(reason) <= 1000
            and reason not in canonical_reasons
        ):
            # Provider-generated prose is harmless metadata, but rejecting it
            # costs the freshness of the exact opaque surface reference.  Keep
            # the public enum strict and conservatively canonicalize prose to
            # the read-only verification reason before schema validation.
            normalized = dict(arguments)
            normalized["reason"] = "verification"
    clean = validate_arguments(spec.input_schema, normalized)
    if (
        spec.name == "request_visual_observation"
        and clean.get("surface_ref")
        and clean.get("expected_native_window_id") == clean.get("surface_ref")
    ):
        # An opaque surface ref is already the authority needed to recover the
        # private native identity.  Treat an exact copy in the optional native
        # assertion as an unambiguous category error, never as contradictory
        # ActionTruth identity.  Any distinct mismatch remains fail-closed.
        clean.pop("expected_native_window_id", None)
    wait_spec = clean.get("wait_spec")
    if wait_spec is None:
        return clean
    try:
        # Pure value validation: browser_state intentionally has no Playwright
        # dependency, so this remains a pre-policy/pre-I/O contract check.
        from osia.tools.browser_state import WaitSpec

        WaitSpec.parse(wait_spec)
    except ValueError as exc:
        code = str(exc or "invalid_wait_spec")[:64]
        path = "$.wait_spec"
        expected: Any = "object"
        if code == "missing_wait_timeout" or "timeout" in code:
            path = "$.wait_spec.timeout_seconds"
            expected = {"type": "number", "minimum": 0.05, "maximum": 120.0}
        elif code in {
            "invalid_dom_primitive", "invalid_dom_state",
            "invalid_element_state", "invalid_element_reference",
            "invalid_custom_postcondition", "nested_custom_postcondition",
            "invalid_wait_expected", "wait_expected_empty",
            "wait_expected_too_large",
        }:
            path = "$.wait_spec.expected"
            expected = "supported_condition_shape"
        raise ToolValidationError(
            "invalid_arguments",
            [ToolValidationIssue(path=path, code=code, expected=expected)],
        ) from exc
    return clean


def validation_error_details(error: ToolValidationError, repair_count: int = 0) -> dict:
    """Return bounded schema diagnostics containing names/types, never values."""

    count = 1 if int(repair_count or 0) >= 1 else 0
    return {
        "status": "failed_before_effect",
        "reason": str(getattr(error, "code", "invalid_arguments") or "invalid_arguments")[:64],
        "dispatch_state": "not_dispatched",
        "repair_count": count,
        "issues": [
            {
                "path": issue.path,
                "code": issue.code,
                **({"expected": issue.expected} if issue.expected is not None else {}),
            }
            for issue in getattr(error, "issues", ())[:16]
        ],
    }


def computer_use_argument_shape(arguments: Any, schema: Mapping | None = None) -> dict:
    """Return a value-free structural shape for Computer Use diagnostics.

    String contents and numeric values are omitted. A string is represented by
    its length, except that a value matching an enum from the supplied registry
    schema is retained so action-kind mismatches can be diagnosed safely.
    """

    safe_key = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
    safe_enum = re.compile(r"[A-Za-z0-9_.:-]{1,32}\Z")

    def _enum_values(current: Any) -> set[str]:
        if not isinstance(current, Mapping):
            return set()
        values = {
            value for value in current.get("enum", ())
            if isinstance(value, str) and safe_enum.fullmatch(value)
        }
        for key in ("anyOf", "oneOf", "allOf"):
            for branch in current.get(key, ()) if isinstance(current.get(key), list) else ():
                values.update(_enum_values(branch))
        return values

    def _shape(value: Any, current_schema: Any, depth: int) -> dict:
        if depth >= 7:
            return {"type": "truncated"}
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "boolean"}
        if isinstance(value, str):
            result = {"type": "string", "length": len(value)}
            if value in _enum_values(current_schema):
                result["schema_enum"] = value
            return result
        if isinstance(value, (int, float)):
            return {"type": "number"}
        if isinstance(value, Mapping):
            properties = (
                current_schema.get("properties", {})
                if isinstance(current_schema, Mapping) else {}
            )
            properties = properties if isinstance(properties, Mapping) else {}
            fields: dict[str, dict] = {}
            entries = sorted(value.items(), key=lambda item: str(item[0]))
            for key, item in entries[:24]:
                name = str(key)
                safe_name = name if safe_key.fullmatch(name) else "<other>"
                if safe_name in fields:
                    safe_name = f"<other_{len(fields)}>"
                fields[safe_name] = _shape(
                    item, properties.get(name), depth + 1,
                )
            result = {"type": "object", "fields": fields}
            if len(entries) > 24:
                result["omitted_fields"] = len(entries) - 24
            return result
        if isinstance(value, (list, tuple)):
            item_schema = (
                current_schema.get("items")
                if isinstance(current_schema, Mapping) else None
            )
            result = {
                "type": "array",
                "length": len(value),
                "items": [
                    _shape(item, item_schema, depth + 1)
                    for item in value[:8]
                ],
            }
            if len(value) > 8:
                result["omitted_items"] = len(value) - 8
            return result
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"type": "bytes", "length": len(value)}
        return {"type": "other"}

    return _shape(arguments, schema, 0)


def tool_schema_fingerprint(schema: Mapping) -> str:
    """Return a stable SHA-256 identity for one JSON-compatible tool schema."""

    canonical = json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persisted_computer_use_argument_diagnostic(value: Any) -> dict | None:
    """Project one bounded value-free Computer Use validation diagnostic.

    The final projection is intentionally limited to correlation IDs, provider
    and model labels, tool/schema identity, structural types/lengths, enum
    labels, validation paths/codes, and dispatch state.
    """

    if (
        not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or type(value.get("dispatch_started")) is not bool
    ):
        return None
    safe_id_pattern = re.compile(r"[A-Za-z0-9._:/-]{1,192}\Z")
    safe_name_pattern = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
    safe_model_pattern = re.compile(r"[A-Za-z][A-Za-z0-9_./:-]{0,159}\Z")
    safe_code_pattern = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
    safe_path_pattern = re.compile(r"\$[A-Za-z0-9_.$\[\]-]{0,180}\Z")
    safe_enum_pattern = re.compile(r"[A-Za-z0-9_.:-]{1,32}\Z")

    def _label(raw: Any, pattern, *, maximum: int = 192) -> str:
        if not isinstance(raw, str) or len(raw) > maximum:
            return ""
        return raw if pattern.fullmatch(raw) else ""

    def _shape(raw: Any, depth: int = 0) -> dict | None:
        if not isinstance(raw, Mapping):
            return None
        kind = raw.get("type")
        if kind not in {
            "object", "array", "string", "boolean", "number", "null",
            "bytes", "other", "truncated",
        }:
            return None
        if kind == "truncated":
            return {"type": "truncated"}
        if depth >= 7:
            return None
        result = {"type": kind}
        if kind == "string" or kind in {"array", "bytes"}:
            length = raw.get("length")
            if type(length) is not int or not 0 <= length <= 1_000_000:
                return None
            result["length"] = length
        if kind == "string" and raw.get("schema_enum") is not None:
            enum_value = _label(raw.get("schema_enum"), safe_enum_pattern, maximum=32)
            if not enum_value:
                return None
            result["schema_enum"] = enum_value
        if kind == "object":
            raw_fields = raw.get("fields")
            if not isinstance(raw_fields, Mapping) or len(raw_fields) > 24:
                return None
            fields = {}
            for raw_name, raw_shape in raw_fields.items():
                name = str(raw_name)
                if name != "<other>" and not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,63}", name,
                ) and not re.fullmatch(r"<other_[0-9]{1,2}>", name):
                    name = "<other>"
                child = _shape(raw_shape, depth + 1)
                if child is None:
                    return None
                fields[name] = child
            result["fields"] = fields
        if kind == "array":
            raw_items = raw.get("items")
            if not isinstance(raw_items, list) or len(raw_items) > 8:
                return None
            items = [_shape(item, depth + 1) for item in raw_items]
            if any(item is None for item in items):
                return None
            result["items"] = items
        for key in ("omitted_fields", "omitted_items"):
            if key in raw:
                count = raw[key]
                if type(count) is not int or not 0 <= count <= 1_000_000:
                    return None
                result[key] = count
        return result

    def _issues(raw: Any) -> list[dict] | None:
        if not isinstance(raw, list) or len(raw) > 16:
            return None
        output = []
        for issue in raw:
            if not isinstance(issue, Mapping):
                return None
            path = _label(issue.get("path"), safe_path_pattern, maximum=181)
            code = _label(issue.get("code"), safe_code_pattern, maximum=80)
            if not path or not code:
                return None
            output.append({"path": path, "code": code})
        return output

    correlation = {}
    for field in ("conversation_id", "request_id", "turn_id"):
        label = _label(value.get(field), safe_id_pattern)
        if not label:
            return None
        correlation[field] = label
    provider = _label(value.get("provider_id"), safe_name_pattern)
    model = _label(value.get("model_id"), safe_model_pattern, maximum=160)
    first_call = value.get("first_call")
    if not provider or not model or not isinstance(first_call, Mapping):
        return None

    def _call(raw: Mapping, *, repair: bool) -> dict | None:
        tool = _label(raw.get("tool_name"), safe_name_pattern)
        schema_hash = str(raw.get("schema_sha256") or "").lower()
        shape = _shape(raw.get("argument_shape"))
        validation = raw.get("validation")
        if (
            not tool
            or not tool.startswith("computer_")
            or not re.fullmatch(r"[0-9a-f]{64}", schema_hash)
            or shape is None
            or not isinstance(validation, Mapping)
            or type(validation.get("passed")) is not bool
        ):
            return None
        issues = _issues(validation.get("issues"))
        if issues is None:
            return None
        output = {
            "tool_name": tool,
            "schema_sha256": schema_hash,
            "argument_shape": shape,
            "validation": {
                "passed": validation["passed"],
                "issues": issues,
            },
        }
        if repair:
            repair_provider = _label(raw.get("provider_id"), safe_name_pattern)
            repair_model = _label(raw.get("model_id"), safe_model_pattern, maximum=160)
            if not repair_provider or not repair_model:
                return None
            output["provider_id"] = repair_provider
            output["model_id"] = repair_model
        return output

    safe_first = _call(first_call, repair=False)
    if safe_first is None:
        return None
    result = {
        "schema_version": 1,
        **correlation,
        "provider_id": provider,
        "model_id": model,
        "first_call": safe_first,
        "repair": None,
        "dispatch_started": bool(value.get("dispatch_started")),
    }
    repair_data = value.get("repair")
    if repair_data is not None:
        if (
            not isinstance(repair_data, Mapping)
            or type(repair_data.get("attempt")) is not int
            or repair_data.get("attempt") != 1
        ):
            return None
        safe_repair = _call(repair_data, repair=True)
        if safe_repair is None:
            return None
        result["repair"] = {"attempt": 1, **safe_repair}
    return result


def validation_error_text(lang: str = "en") -> str:
    code = str(lang or "").lower().split("-", 1)[0].split("_", 1)[0]
    messages = {
        "en": "ERROR: invalid tool arguments; the tool was not executed",
        "fr": "ERROR: arguments d'outil invalides ; l'outil n'a pas été exécuté",
        "zh": "ERROR: 工具参数无效；工具未执行",
        "ja": "ERROR: ツールの引数が無効です。ツールは実行されませんでした",
        "de": "ERROR: ungültige Werkzeugargumente; das Werkzeug wurde nicht ausgeführt",
        "es": "ERROR: argumentos de herramienta no válidos; la herramienta no se ejecutó",
    }
    return messages.get(code, messages["en"])


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
    if name == "activate_browser_element":
        return values.get("trusted_final_action") is True
    if name in {"press_key", "desktop_press", "computer_keypress"}:
        return str(values.get("key") or "").strip().lower() in {"enter", "return"}
    if name == "computer_actions":
        actions = values.get("actions")
        return bool(
            isinstance(actions, list)
            and any(
                isinstance(item, Mapping)
                and (
                    (
                        str(item.get("kind") or "") == "keypress"
                        and str(item.get("key") or "").strip().lower()
                        in {"enter", "return"}
                    )
                    or _FINAL_COMMIT_RE.search(str(item.get("effect_id") or ""))
                )
                for item in actions
            )
        )
    field = {
        "click": "selector", "find_and_click": "text",
        "desktop_click": "description",
        "computer_click": "intent", "computer_double_click": "intent",
        "activate_desktop_surface": "objective",
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


def persistence_contract(
    spec: ToolSpec,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    owner_timeout_seconds: float,
) -> ToolPersistenceContract:
    """Resolve one bounded durable-waiting policy from the tool contract.

    The values are deliberately family-specific.  They do not authorize a
    replay: alternatives are read-only reconciliation strategies, while retry
    permission remains exclusively governed by :class:`RetryPolicy`.
    """

    name = str(tool_name or spec.name or "")
    owner_timeout = max(0.05, min(float(owner_timeout_seconds), 86_400.0))
    wait_timeout = 0.0
    wait_spec = arguments.get("wait_spec") if isinstance(arguments, Mapping) else None
    if isinstance(wait_spec, Mapping):
        try:
            wait_timeout = max(0.0, min(float(wait_spec.get("timeout_seconds") or 0), 86_400.0))
        except (TypeError, ValueError, OverflowError):
            wait_timeout = 0.0

    if name in BROWSER_FAMILY_TOOL_NAMES or any(
        "browser" in str(item) for item in spec.preconditions
    ):
        family = ToolPersistenceFamily.BROWSER
        release_after = min(owner_timeout, 3.0)
        inactivity = max(owner_timeout + 5.0, wait_timeout + 10.0)
        hard = max(inactivity + 15.0, owner_timeout * 1.5)
        delays = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0)
        alternatives = (
            ("fresh_browser_postcondition",)
            if spec.postcondition_required else ()
        )
    elif name in _FILE_PERSISTENCE_TOOLS:
        family = ToolPersistenceFamily.FILE
        release_after = min(owner_timeout, 1.5)
        inactivity = owner_timeout + 2.0
        hard = max(inactivity + 10.0, owner_timeout * 1.5)
        delays = (0.5, 1.0, 2.0, 4.0, 8.0)
        alternatives = ()
    elif spec.dynamic or name.startswith("mcp__") or name.startswith("gmail_"):
        family = ToolPersistenceFamily.API_MCP
        release_after = min(owner_timeout, 3.0)
        inactivity = owner_timeout + 5.0
        hard = max(inactivity + 20.0, owner_timeout * 1.25)
        delays = (1.0, 2.0, 5.0, 10.0, 15.0)
        alternatives = ()
    elif name.startswith("computer_"):
        family = ToolPersistenceFamily.GENERIC
        release_after = min(owner_timeout, 10.0)
        inactivity = owner_timeout + 5.0
        hard = max(inactivity + 15.0, owner_timeout * 1.25)
        delays = (1.0, 2.0, 5.0, 10.0)
        alternatives = ()
    elif name in _SYSTEM_PERSISTENCE_TOOLS:
        family = ToolPersistenceFamily.SYSTEM
        release_after = min(owner_timeout, 2.0)
        inactivity = owner_timeout + 2.0
        hard = max(inactivity + 15.0, owner_timeout * 1.1)
        delays = (1.0, 2.0, 5.0, 10.0, 20.0)
        alternatives = spec.safe_reconciliation_descriptors
    else:
        family = ToolPersistenceFamily.GENERIC
        release_after = min(owner_timeout, 2.0)
        inactivity = owner_timeout + 2.0
        hard = max(inactivity + 10.0, owner_timeout * 1.5)
        delays = (1.0, 2.0, 5.0)
        alternatives = spec.safe_reconciliation_descriptors

    return ToolPersistenceContract(
        family=family,
        release_after_seconds=max(0.05, release_after),
        inactivity_timeout_seconds=max(0.1, inactivity),
        hard_timeout_seconds=max(inactivity, hard),
        check_delays_seconds=delays,
        safe_alternatives=alternatives,
    )


def normalized_call_hash(tool_name: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"tool": str(tool_name), "arguments": dict(arguments)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
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
            and hmac.compare_digest(
                grant.call_hash, normalized_call_hash(tool_name, arguments)
            )
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
    return hmac.compare_digest(
        _approved_call.get(), normalized_call_hash(tool_name, arguments)
    )


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
    if name == "upload_browser_file":
        return "Select one exact private file for one exact site target"
    if name == "activate_browser_element":
        return "Commit one exact final browser action"
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
    display = text if len(text) <= limit else text[:limit] + "\n... [TRUNCATED]"
    if isinstance(result, ProcessOutput):
        preserved = str.__new__(ProcessOutput, display)
        preserved.process_facts = dict(result.process_facts)
        return preserved
    return display


def static_tool_inventory_specs() -> tuple[ToolSpec, ...]:
    """Return the complete runtime-available static inventory.

    WOR-300: the model-visible inventory is composed from genuine runtime
    availability alone and never depends on the request, the execution
    strategy, the semantic domain or the engine surface.  The engine surface
    still gates dispatch through ``tool_is_eligible_in_current_surface``; an
    internal engine restriction never reduces the visible inventory.
    """

    return tuple(
        spec for spec in get_static_registry().values()
        if _static_tool_runtime_available(spec)
    )


def tool_disabled_by_chat_command(name: str) -> bool:
    from osia.slash_commands import current_chat_commands

    special_mode, directives = current_chat_commands()
    if name == "create_chat_skill":
        return not directives.skill_creator
    return special_mode and (
        name.startswith("computer_")
        or name in DORMANT_VISUAL_V3_TOOL_NAMES
        or name in {"take_screenshot", "save_screenshot", "desktop_scroll"}
    )


def _static_tool_runtime_available(spec: ToolSpec) -> bool:
    """Return only genuine runtime availability for one static capability.

    WOR-300 exposure invariant: the model-visible surface is composed from
    runtime availability alone.  Request wording, semantic classification,
    GoalContract/ExecutionStrategy state, semantic domain, estimated relevance
    and execution-frontier progress are never consulted here.  At dispatch, only technical availability/argument validation,
    BLOCK_SYSTEM_DESTRUCTION, current target freshness, exact request/attempt
    ownership, and correlated cancellation may constrain execution. Semantic
    relevance, historical ActionTruth, and OUTCOME_UNKNOWN do not veto a fresh
    model-requested call; seeing a capability does not override real technical
    availability or integrity checks.
    """

    if tool_disabled_by_chat_command(spec.name):
        return False
    if spec.name in DORMANT_VISUAL_V3_TOOL_NAMES:
        # Retired/dormant capability: dispatch fails with a typed error.
        return False
    if spec.name.startswith("computer_"):
        import sys

        if sys.platform != "win32":
            # Computer Use V4 exact-window capture/actuation is win32-only.
            return False
        try:
            from osia.computer_use_v4_integration import (
                COMPUTER_USE_V4_TOOL_NAMES,
                computer_use_v4_tools_available,
            )
        except Exception:
            return False
        if spec.name not in COMPUTER_USE_V4_TOOL_NAMES:
            return False
        # Genuine runtime capability: the active turn must exist and the
        # selected provider/model must accept image input.
        return bool(computer_use_v4_tools_available())
    return True


def tool_is_runtime_available(spec_or_name: ToolSpec | str) -> bool:
    """Return genuine runtime availability for one capability (WOR-300).

    Registered dynamic (MCP) tools are available by their registration alone;
    static tools follow platform/provider/turn availability.  Surface state,
    request wording and execution strategy are never consulted.
    """

    spec = (
        get_tool_spec(spec_or_name)
        if isinstance(spec_or_name, str) else spec_or_name
    )
    if spec is None:
        return False
    if spec.dynamic:
        return True
    return _static_tool_runtime_available(spec)


def effective_static_tool_specs() -> tuple[ToolSpec, ...]:
    """Return the exact ordered static specs exposed in the current turn.

    Every registered capability available in the current runtime is exposed
    on every turn, identically for a trivial question and a broad coding
    request, with or without an active GoalContract or ExecutionStrategy, and
    identically across engine surfaces.
    """

    return static_tool_inventory_specs()


def static_tool_prompt_block(lang: str = "en") -> str:
    """Render compact canonical signatures from the authoritative registry."""

    del lang
    specs = effective_static_tool_specs()
    schemas, definitions = _prompt_schema_bundle(specs)
    lines = [
        "## Available OSIA capabilities",
        "Only these turn capabilities exist. Arguments are top-level; unknown fields are rejected.",
    ]
    for spec in specs:
        schema = spec.input_schema
        props = schema.get("properties") if isinstance(schema, dict) else {}
        props = props if isinstance(props, dict) else {}
        required = schema.get("required") if isinstance(schema, dict) else []
        required = required if isinstance(required, list) else []
        if not required and isinstance(schema, dict):
            # A mutually-exclusive source contract (currently read_document)
            # has no unconditional required field.  Show the first declared
            # alternative so the generated Usage remains directly callable.
            for branch in schema.get("oneOf") or []:
                branch_required = (
                    branch.get("required") if isinstance(branch, dict) else []
                )
                if isinstance(branch_required, list) and branch_required:
                    required = branch_required
                    break
        required = [key for key in required if key in props]
        signature = ", ".join(
            f"{key}:{_prompt_schema_type(props[key])}" for key in required
        )
        optional = [key for key in props if key not in required]
        if optional:
            shown = optional[:3]
            signature += ("; " if signature else "") + "optional:" + ",".join(
                shown
            )
            if len(optional) > len(shown):
                signature += ",..."
        capability = " ".join(str(spec.capability or "").split())[:75]
        if spec.name == "render_pdf":
            # ``blocks:list`` alone is not callable: retain the bounded nested
            # discriminators while omitting the former prose examples.
            capability = (
                "Create/export PDF; blocks="
                "heading{text,level?}|paragraph{text}|"
                "bullet_list{items}|numbered_list{items}|"
                "table{headers,rows}|page_break|"
                "flow_diagram{nodes,edges}"
            )
        output = {
            ResultPolicy.BOUNDED_MODEL_ONLY: "model",
            ResultPolicy.BOUNDED_MODEL_AND_SCREENSHOT: "model+image",
            ResultPolicy.PRIVATE_MODEL_ONLY: "private-model",
        }[spec.result_policy]
        contract = [
            f"out={output}",
            f"effect={spec.side_effect.name}",
        ]
        if spec.preconditions:
            contract.append("pre=" + spec.preconditions[0])
        # WOR-301: confirmation policies no longer gate dispatch, so the
        # catalogue must not advertise one to the model.
        lines.append(
            f"- {spec.name}({signature}): {capability}; " + "; ".join(contract)
            + "; input_schema=" + json.dumps(
                schemas[spec.name], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
        )
    if definitions:
        lines.append(
            "Shared input schema definitions: resolve every #/$defs/ reference "
            "against this block. " + json.dumps(
                {"$defs": definitions}, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines)


def _prompt_schema_bundle(specs: Iterable[ToolSpec]) -> tuple[dict, dict]:
    """Losslessly factor repeated schemas without hiding any tool/constraint."""
    specs = tuple(specs)
    counts: dict[str, int] = {}
    values: dict[str, dict] = {}
    def key(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    def collect(value):
        if isinstance(value, dict):
            encoded = key(value)
            # Only schema nodes, not property-name maps, may become $refs.
            is_schema = (
                isinstance(value.get("type"), str)
                or isinstance(value.get("anyOf"), list)
                or isinstance(value.get("oneOf"), list)
            )
            if is_schema and len(encoded) > 35:
                counts[encoded] = counts.get(encoded, 0) + 1
                values[encoded] = value
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for spec in specs:
        collect(spec.input_schema)
    names = {encoded: f"s{index}" for index, encoded in enumerate(
        sorted(encoded for encoded, count in counts.items()
               if len(encoded) * (count - 1) > 28 * count + 16)
    )}
    def project(value, *, definition_root=False):
        if isinstance(value, dict):
            encoded = key(value)
            if not definition_root and encoded in names:
                return {"$ref": "#/$defs/" + names[encoded]}
            return {k: project(v) for k, v in value.items()}
        if isinstance(value, list):
            return [project(item) for item in value]
        return value
    return (
        {spec.name: project(spec.input_schema) for spec in specs},
        {name: project(values[encoded], definition_root=True) for encoded, name in names.items()},
    )


def _prompt_schema_type(schema: Any) -> str:
    if isinstance(schema, dict):
        options = schema.get("anyOf") or schema.get("oneOf") or []
        if isinstance(options, list) and options:
            variants = []
            for item in options:
                if not isinstance(item, dict) or item.get("type") == "null":
                    continue
                rendered = _prompt_schema_type(item)
                if rendered not in variants:
                    variants.append(rendered)
            if variants:
                return "|".join(variants[:4])
    if isinstance(schema, dict) and isinstance(schema.get("enum"), list):
        values = [str(value) for value in schema["enum"][:8]]
        rendered = "|".join(values)
        if rendered and len(rendered) <= 80:
            return "enum[" + rendered + "]"
    kind = schema.get("type") if isinstance(schema, dict) else None
    return {
        "array": "list",
        "boolean": "bool",
        "integer": "int",
        "number": "number",
        "object": "object",
        "string": "string",
    }.get(kind, "value")


def replace_static_tool_prompt_section(body: str, lang: str = "en") -> str:
    del lang
    start = (
        "## Available Capabilities"
        if "## Available Capabilities" in body else
        "## Available Tools"
    )
    end = (
        "## Tool call format"
        if "## Tool call format" in body else
        "## How to Use Tools"
    )
    start_index = body.find(start)
    end_index = body.find(end)
    if start_index < 0 or end_index <= start_index:
        return body
    return (
        body[:start_index]
        + static_tool_prompt_block()
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
            "permission_category": "connectors",
            "postcondition_required": True,
            "redaction_policy": "private_connector_metadata",
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
        "permission_category": spec.permission_category,
        "postcondition_required": spec.postcondition_required,
        "redaction_policy": spec.redaction_policy,
        "supported_providers": list(spec.providers),
        "deterministic_test_status": spec.deterministic_test_status.value,
        "controlled_integration_status": spec.controlled_integration_status.value,
        "real_validation_requirement": spec.real_validation_requirement.value,
        "current_evidence_classification": evidence.value,
        "currently_executable": bool(executable),
    }


__all__ = [
    "ALL_TEXT_PROVIDERS", "BROWSER_FAMILY_TOOL_NAMES",
    "BROWSER_VERIFY_TOOL_NAMES", "CONFIRMATION_LEDGER",
    "ConfirmationLedger",
    "ConfirmationPolicy", "EffectiveToolContract", "EvidenceClassification",
    "ExecutionOutcome", "IdempotencyClass", "ObservabilityPolicy",
    "ProgressEvidencePolicy", "RealValidationRequirement", "ResultPolicy",
    "RetryPolicy", "STATIC_TOOL_REGISTRY", "STATIC_TOOL_SPECS",
    "SideEffectClass", "ToolRegistry", "ToolRegistryError", "ToolSpec",
    "ToolExecutionRecord", "ToolPersistenceContract", "ToolPersistenceFamily",
    "ProcessOutput", "normalize_process_facts",
    "ToolValidationError", "ToolValidationIssue",
    "assert_prompt_tool_examples_compatible",
    "bind_static_registry", "bounded_model_result",
    "compatibility_tool_spec",
    "confirmed_call_scope", "current_call_is_confirmed", "dynamic_tool_specs",
    "current_tool_surface", "effective_contract", "effective_static_tool_specs",
    "effective_tool_surface_scope", "get_static_registry", "get_tool_spec",
    "harden_input_schema", "is_final_browser_commit", "matrix_rows",
    "normalized_call_hash", "persistence_contract", "register_dynamic_tool",
    "replace_static_tool_prompt_section", "safe_confirmation_summary",
    "shell_command_is_proven_read_only", "static_tool_inventory_specs",
    "static_tool_prompt_block",
    "tool_is_eligible_in_current_surface", "tool_is_runtime_available",
    "unregister_dynamic_tools", "validate_arguments", "validation_error_details",
    "validation_error_text", "computer_use_argument_shape",
    "tool_schema_fingerprint", "persisted_computer_use_argument_diagnostic",
]
