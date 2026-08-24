"""Canonical durable semantic-action truth and reconciliation (WOR-269).

This module does not execute mutations and does not own a second persistence
engine.  It projects the existing :mod:`osia.tool_attempts` journal into one
``ActionRecord`` per semantic effect and coordinates read-only reconciliation
through small observer adapters.  The durable store remains the sole writer of
attempt files; adapters can only report typed evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol


ACTION_TRUTH_SCHEMA_VERSION = 1
MAX_ACTION_TIMELINE_EVENTS = 128
MAX_ACTION_EVIDENCE = 64
MAX_ACTION_OBSERVERS = 64
MAX_RECONCILIATION_EVENTS = 64


class CanonicalEffectClass(str, Enum):
    """Replay semantics derived from the authoritative capability contract."""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"
    DESTRUCTIVE_OR_HIGH_RISK = "destructive_or_high_risk"


class ActionState(str, Enum):
    PREPARED = "prepared"
    PRECONDITION_FAILED = "precondition_failed"
    DISPATCH_STARTED = "dispatch_started"
    DISPATCH_RETURNED = "dispatch_returned"
    ACCEPTED = "accepted"
    STILL_RUNNING = "still_running"
    EFFECT_OBSERVED = "effect_observed"
    EFFECT_VERIFIED = "effect_verified"
    NOT_APPLIED_VERIFIED = "not_applied_verified"
    EFFECT_POSSIBLE = "effect_possible"
    OBSERVER_FAILED = "observer_failed"
    TARGET_STALE = "target_stale"
    RECONCILING = "reconciling"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    CANCELLED = "cancelled"


class EffectTruth(str, Enum):
    """Effect truth is intentionally orthogonal to observer execution state."""

    NOT_POSSIBLE = "not_possible"
    POSSIBLE = "possible"
    OBSERVED = "observed"
    VERIFIED = "verified"
    NOT_APPLIED = "not_applied"


class ObserverStatus(str, Enum):
    NOT_RUN = "not_run"
    RUNNING = "running"
    MATCH = "match"
    NON_APPLIED = "non_applied"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    TARGET_STALE = "target_stale"


class ReconciliationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    RUNNING = "running"
    VERIFIED = "verified"
    NOT_APPLIED = "not_applied"
    PROPAGATION_PENDING = "propagation_pending"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


class SettlePolicyKind(str, Enum):
    LOCAL_UI_IMMEDIATE = "local_ui_immediate"
    LOCAL_UI_PROPAGATING = "local_ui_propagating"
    NAVIGATION = "navigation"
    NETWORK_READ = "network_read"
    ASYNC_JOB = "async_job"


class ObservationClassification(str, Enum):
    MATCH = "match"
    NON_APPLIED = "non_applied"
    STILL_RUNNING = "still_running"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    TARGET_STALE = "target_stale"


@dataclass(frozen=True)
class SettlePolicy:
    kind: SettlePolicyKind
    max_probes: int
    probe_delays_seconds: tuple[float, ...]
    hard_deadline_seconds: float
    event_driven: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["probe_delays_seconds"] = list(self.probe_delays_seconds)
        return value


@dataclass(frozen=True)
class ObservationResult:
    classification: ObservationClassification
    observer: str
    reason_code: str
    evidence_ref: str = ""
    observed_at: float = 0.0
    exact: bool = False
    target_generation: str = ""
    provider_operation_ref: str = ""


@dataclass(frozen=True)
class ActionGateDecision:
    allowed: bool
    reason_code: str
    blocking_action_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetryDecision:
    allowed: bool
    reason_code: str
    reuse_idempotency_identity: bool = False
    alternate_mechanism_only: bool = False


@dataclass(frozen=True)
class ReconciliationDecision:
    action_id: str
    state: ActionState
    effect_truth: EffectTruth
    reconciliation_status: ReconciliationStatus
    reason_code: str
    replay_allowed: bool
    observer_status: ObserverStatus
    safe_avenues_exhausted: bool = False


@dataclass(frozen=True)
class ActionIntent:
    effect_class: CanonicalEffectClass
    semantic_effect_id: str = ""
    semantic_node_id: str = ""
    dependency_action_ids: tuple[str, ...] = ()
    dependency_effect_ids: tuple[str, ...] = ()
    independent_of_action_ids: tuple[str, ...] = ()
    conflict_scope: str = ""
    idempotency_identity: str = ""
    target: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionRecord:
    action_id: str
    semantic_effect_id: str
    run_id: str
    turn_id: str
    request_id: str
    goal_contract_ref: str
    effect_class: str
    operation: str
    target: dict[str, Any]
    idempotency_identity: str
    current_state: str
    effect_truth: str
    conflict_scope: str = ""
    expected_postcondition_ref: str = ""
    effect_role: str = "semantic"
    attempts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    observers: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    reconciliation_state: str = ReconciliationStatus.NOT_REQUIRED.value
    reconciliation_history: list[dict[str, Any]] = field(default_factory=list)
    legacy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Clock(Protocol):
    def time(self) -> float: ...


class SystemClock:
    def time(self) -> float:
        return time.time()


class ReconciliationAdapter(Protocol):
    name: str

    def supports(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> bool: ...

    def observe(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> ObservationResult: ...


class ActionTruthStore(Protocol):
    def list_action_attempts(self, action_id: str) -> list[dict[str, Any]]: ...

    def list_unresolved_action_attempts(
        self, *, conversation_id: str = "", turn_id: str = ""
    ) -> list[dict[str, Any]]: ...

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
    ) -> dict[str, Any]: ...


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,192}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_URL_OPERATIONS = frozenset({
    "navigate", "navigate_browser", "open_browser", "open_new_tab",
    "open_new_browser_tab", "open_search_result", "search_web",
})
_READ_OPERATIONS = frozenset({
    "get_browser_state", "inspect_browser_page", "read_page",
    "get_page_text", "web_state", "inspect_desktop_surfaces",
    "request_visual_observation",
})


def _bounded(value: Any, maximum: int = 192) -> str:
    text = str(value or "").strip()
    if not text or any(ord(char) < 32 for char in text):
        return ""
    return text[:maximum]


def _safe_id(value: Any, prefix: str = "id") -> str:
    text = _bounded(value, 192)
    if text and _SAFE_ID.fullmatch(text):
        return text
    if not text:
        return ""
    return prefix + "_" + hashlib.sha256(
        text.encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def _safe_reason(value: Any, fallback: str = "") -> str:
    text = _bounded(value, 96).lower().replace("-", "_")
    return text if _SAFE_REASON.fullmatch(text) else fallback


def evidence_reference(value: Any) -> str:
    """Return a stable reference without persisting the observed payload."""

    if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=lambda _item: "<unsupported>",
    ).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _enum_value(value: Any, enum_type, default):
    try:
        return enum_type(str(getattr(value, "value", value) or ""))
    except ValueError:
        return default


def canonical_effect_class(
    side_effect: Any,
    idempotency: Any,
    *,
    idempotency_identity: str = "",
) -> CanonicalEffectClass:
    """Map the authoritative tool contract to replay semantics.

    Names and HTTP verbs are deliberately ignored. Unknown/dynamic contracts
    fail closed as high risk.
    """

    side = str(getattr(side_effect, "value", side_effect) or "")
    idem = str(getattr(idempotency, "value", idempotency) or "")
    if side == "read_only":
        return CanonicalEffectClass.READ_ONLY
    if side in {"irreversible_external", "unknown_high_risk"}:
        return CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK
    if side in {"dynamically_classified", ""}:
        return CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK
    if idem == "idempotent" or (
        idem == "conditionally_idempotent" and bool(idempotency_identity)
    ):
        return CanonicalEffectClass.IDEMPOTENT_WRITE
    return CanonicalEffectClass.NON_IDEMPOTENT_WRITE


def safe_url_identity(value: Any) -> str:
    try:
        from osia.tools.browser_state import safe_url_display

        return _bounded(safe_url_display(value), 640)
    except Exception:
        return ""


def action_target_for_tool(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Build a privacy-bounded target identity for one semantic operation."""

    name = _bounded(tool_name, 96)
    source = arguments if isinstance(arguments, Mapping) else {}
    target: dict[str, Any] = {"kind": "generic"}
    url = source.get("url") or source.get("target_url")
    if name in _URL_OPERATIONS and url:
        target.update({"kind": "browser", "safe_url": safe_url_identity(url)})
    state_ref = source.get("state_ref")
    if isinstance(state_ref, Mapping):
        target.update({
            "kind": "browser",
            "browser_session_id": _safe_id(
                state_ref.get("browser_session_id"), "browser"
            ),
            "context_id": _safe_id(state_ref.get("context_id"), "context"),
            "context_epoch": state_ref.get("context_epoch"),
            "page_id": _safe_id(state_ref.get("page_id"), "page"),
            "page_generation": state_ref.get("page_generation"),
            "navigation_id": _safe_id(
                state_ref.get("navigation_id"), "navigation"
            ),
            "document_epoch": state_ref.get("document_epoch"),
        })
    if name in {
        "activate_desktop_surface", "inspect_desktop_surfaces",
        "request_visual_observation",
    }:
        target.update({
            "kind": "desktop",
            "surface_ref": _safe_id(source.get("surface_ref"), "surface"),
            "application": _safe_id(source.get("target"), "application"),
            "window_generation": _safe_id(
                source.get("expected_window_generation"), "generation"
            ),
        })
    return _safe_action_target(target)


def _safe_action_target(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    kind = str(source.get("kind") or "generic")
    if kind not in {"generic", "browser", "desktop", "api_mcp", "system", "file"}:
        kind = "generic"
    target: dict[str, Any] = {"kind": kind}
    safe_url = safe_url_identity(source.get("safe_url"))
    if safe_url:
        target["safe_url"] = safe_url
    for key in (
        "browser_session_id", "context_id", "page_id", "frame_id",
        "navigation_id",
        "surface_ref", "application", "window_generation",
        "native_window_id", "provider_resource_ref", "conflict_scope",
    ):
        safe = _safe_id(source.get(key), key)
        if safe:
            target[key] = safe
    for key in ("page_generation", "document_epoch", "context_epoch"):
        try:
            number = max(0, int(source.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            number = 0
        if number:
            target[key] = number
    return target


def normalize_action_target(value: Any) -> dict[str, Any]:
    """Public privacy-bounded normalization for dependency-gate inputs."""

    return _safe_action_target(value)


def semantic_effect_identity(
    *,
    turn_id: str,
    semantic_node_id: str,
    attempted_effect: str,
    operation: str,
    target: Mapping[str, Any],
    equivalence_fingerprint: str = "",
    argument_fingerprint: str = "",
) -> str:
    strong_semantic_binding = bool(semantic_node_id and attempted_effect)
    semantic_material = {
        "turn_id": _safe_id(turn_id, "turn"),
        "semantic_node_id": _safe_id(semantic_node_id, "node"),
        "attempted_effect": _safe_id(attempted_effect, "effect"),
        # Tool/mechanism names must not split one semantic effect across
        # equivalent strategies once the strategy has supplied a canonical
        # node + attempted effect binding.
        "operation": (
            "" if strong_semantic_binding else _safe_id(operation, "operation")
        ),
        "target": _safe_action_target(target),
        "equivalence": _bounded(equivalence_fingerprint, 80),
        "argument": (
            "" if (strong_semantic_binding or equivalence_fingerprint) else
            _bounded(argument_fingerprint, 80)
        ),
    }
    encoded = json.dumps(
        semantic_material, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return "effect_" + hashlib.sha256(encoded).hexdigest()[:32]


def action_identity(*, run_id: str, turn_id: str, semantic_effect_id: str) -> str:
    encoded = "|".join((
        _safe_id(run_id, "run") or _safe_id(turn_id, "turn"),
        _safe_id(semantic_effect_id, "effect"),
    )).encode("utf-8")
    return "action_" + hashlib.sha256(encoded).hexdigest()[:32]


def settle_policy_for(
    *,
    operation: str,
    family: str,
    effect_class: CanonicalEffectClass,
    effect_role: str = "semantic",
) -> SettlePolicy:
    operation = str(operation or "").lower()
    family = str(family or "generic").lower()
    if operation in _URL_OPERATIONS or "navigation" in operation:
        return SettlePolicy(
            SettlePolicyKind.NAVIGATION,
            max_probes=8,
            probe_delays_seconds=(0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 12.0),
            hard_deadline_seconds=45.0,
            event_driven=True,
        )
    if family == "browser" and effect_class is CanonicalEffectClass.READ_ONLY:
        return SettlePolicy(
            SettlePolicyKind.NETWORK_READ,
            max_probes=3,
            probe_delays_seconds=(0.0, 0.5, 2.0),
            hard_deadline_seconds=30.0,
            event_driven=True,
        )
    if family == "api_mcp":
        return SettlePolicy(
            SettlePolicyKind.ASYNC_JOB,
            max_probes=8,
            probe_delays_seconds=(0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 45.0),
            hard_deadline_seconds=120.0,
            event_driven=True,
        )
    if effect_role == "preparatory":
        return SettlePolicy(
            SettlePolicyKind.LOCAL_UI_IMMEDIATE,
            max_probes=1,
            probe_delays_seconds=(0.0,),
            hard_deadline_seconds=2.0,
        )
    return SettlePolicy(
        SettlePolicyKind.LOCAL_UI_PROPAGATING,
        max_probes=3,
        probe_delays_seconds=(0.0, 1.0, 1.0),
        hard_deadline_seconds=4.0,
    )


def _truth_from_state(state: ActionState, dispatch_started: bool) -> EffectTruth:
    if state is ActionState.EFFECT_VERIFIED:
        return EffectTruth.VERIFIED
    if state is ActionState.NOT_APPLIED_VERIFIED:
        return EffectTruth.NOT_APPLIED
    if state is ActionState.EFFECT_OBSERVED:
        return EffectTruth.OBSERVED
    if dispatch_started or state in {
        ActionState.DISPATCH_STARTED,
        ActionState.DISPATCH_RETURNED,
        ActionState.ACCEPTED,
        ActionState.STILL_RUNNING,
        ActionState.EFFECT_POSSIBLE,
        ActionState.RECONCILING,
        ActionState.TARGET_STALE,
    }:
        return EffectTruth.POSSIBLE
    return EffectTruth.NOT_POSSIBLE


def new_action_truth(
    *,
    action_id: str,
    semantic_effect_id: str,
    effect_class: CanonicalEffectClass,
    operation: str,
    target: Mapping[str, Any] | None,
    goal_contract_ref: str = "",
    semantic_node_id: str = "",
    attempted_effect: str = "",
    idempotency_identity: str = "",
    dependencies: Iterable[str] = (),
    conflict_scope: str = "",
    expected_postcondition_ref: str = "",
    effect_role: str = "semantic",
    settle_policy: SettlePolicy | None = None,
    occurred_at: float | None = None,
) -> dict[str, Any]:
    now = time.time() if occurred_at is None else float(occurred_at)
    state = ActionState.PREPARED
    return {
        "schema_version": ACTION_TRUTH_SCHEMA_VERSION,
        "action_id": _safe_id(action_id, "action"),
        "semantic_effect_id": _safe_id(semantic_effect_id, "effect"),
        "semantic_node_id": _safe_id(semantic_node_id, "node"),
        "goal_contract_ref": _safe_id(goal_contract_ref, "goal"),
        "effect_class": effect_class.value,
        "operation": _safe_id(operation, "operation"),
        "attempted_effect": _safe_id(attempted_effect, "effect"),
        "target": _safe_action_target(target),
        "idempotency_identity": _safe_id(idempotency_identity, "idempotency"),
        "dependencies": [
            item for value in dependencies
            if (item := _safe_id(value, "dependency"))
        ][:32],
        "conflict_scope": _safe_id(conflict_scope, "scope"),
        "expected_postcondition_ref": _safe_id(
            expected_postcondition_ref, "postcondition"
        ),
        "effect_role": (
            "preparatory" if effect_role == "preparatory" else "semantic"
        ),
        "current_state": state.value,
        "effect_truth": EffectTruth.NOT_POSSIBLE.value,
        "dispatch_started_at": 0.0,
        "dispatch_returned_at": 0.0,
        "accepted_at": 0.0,
        "provider_operation_ref": "",
        "observer_status": ObserverStatus.NOT_RUN.value,
        "reconciliation_status": ReconciliationStatus.NOT_REQUIRED.value,
        "current_reason": "action_prepared",
        "evidence": [],
        "observers": [],
        "reconciliation_history": [],
        "timeline": [{
            "state": state.value,
            "reason_code": "action_prepared",
            "occurred_at": now,
        }],
        "settle_policy": (
            settle_policy or settle_policy_for(
                operation=operation,
                family="generic",
                effect_class=effect_class,
                effect_role=effect_role,
            )
        ).to_dict(),
        "legacy": False,
    }


def legacy_action_truth(record: Mapping[str, Any]) -> dict[str, Any]:
    """Interpret a legacy attempt without upgrading ambiguity into success."""

    def _legacy_time(value: Any) -> float:
        try:
            return max(0.0, float(value or 0.0))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    side = record.get("side_effect")
    idem = record.get("idempotence")
    identity = str(record.get("remote_operation_key") or "")
    effect_class = canonical_effect_class(
        side, idem, idempotency_identity=identity
    )
    dispatch_state = str(record.get("dispatch_state") or "not_dispatched")
    terminal = str(record.get("terminal_outcome") or "")
    status = str(record.get("status") or "not_started")
    dispatch_started = dispatch_state not in {
        "not_dispatched", "owner_started", "cancelled_before_effect",
    }
    primitive_journal = record.get("primitive_journal")
    if (
        str(record.get("tool_name") or "") == "activate_desktop_surface"
        and isinstance(primitive_journal, list)
    ):
        dispatch_started = any(
            isinstance(item, Mapping)
            and item.get("effect_role") == "semantic"
            and item.get("lifecycle") in {
                "dispatch_started", "dispatch_returned", "receipt_bound",
                "verified", "unknown",
            }
            for item in primitive_journal
        )
    if terminal == "success_verified":
        state = ActionState.EFFECT_VERIFIED
    elif terminal in {
        "confirmed_failure_before_effect", "rejected_before_execution",
    } or status == "failed_before_effect":
        state = ActionState.FAILED_BEFORE_EFFECT
    elif terminal == "cancelled_proven" or status == "cancelled_proven":
        state = ActionState.CANCELLED
    elif terminal in {"outcome_unknown", "success_unverified"}:
        state = ActionState.EFFECT_POSSIBLE
    elif status in {"pending", "waiting_verification"} and dispatch_started:
        state = ActionState.RECONCILING
    elif terminal == "confirmed_success":
        state = (
            ActionState.EFFECT_VERIFIED
            if effect_class is CanonicalEffectClass.READ_ONLY
            else ActionState.EFFECT_OBSERVED
        )
    elif dispatch_started:
        state = ActionState.DISPATCH_STARTED
    else:
        state = ActionState.PREPARED
    semantic_effect_id = "effect_legacy_" + hashlib.sha256(
        str(record.get("tool_attempt_id") or "legacy").encode("utf-8")
    ).hexdigest()[:24]
    action_id = "action_legacy_" + hashlib.sha256(
        semantic_effect_id.encode("utf-8")
    ).hexdigest()[:24]
    truth = new_action_truth(
        action_id=action_id,
        semantic_effect_id=semantic_effect_id,
        effect_class=effect_class,
        operation=str(record.get("tool_name") or "legacy_action"),
        target={},
        idempotency_identity=identity,
        occurred_at=_legacy_time(record.get("created_at")),
    )
    truth.update({
        "current_state": state.value,
        "effect_truth": _truth_from_state(state, dispatch_started).value,
        "dispatch_started_at": (
            _legacy_time(record.get("owner_dispatch_claimed_at"))
            if dispatch_started else 0.0
        ),
        "reconciliation_status": (
            ReconciliationStatus.PENDING.value
            if state in {ActionState.EFFECT_POSSIBLE, ActionState.RECONCILING}
            else ReconciliationStatus.NOT_REQUIRED.value
        ),
        "current_reason": _safe_reason(
            record.get("reason_code"), "legacy_attempt_interpreted"
        ),
        "legacy": True,
    })
    truth["timeline"] = [{
        "state": state.value,
        "reason_code": truth["current_reason"],
        "occurred_at": _legacy_time(record.get("updated_at")),
        "legacy": True,
    }]
    return truth


def normalize_action_truth(
    value: Any, *, legacy_record: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value.get("action_id"):
        return legacy_action_truth(legacy_record or {})
    source = dict(value)
    state = _enum_value(
        source.get("current_state"), ActionState, ActionState.EFFECT_POSSIBLE
    )
    truth = _enum_value(
        source.get("effect_truth"), EffectTruth,
        _truth_from_state(state, bool(source.get("dispatch_started_at"))),
    )
    effect_class = _enum_value(
        source.get("effect_class"), CanonicalEffectClass,
        CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK,
    )
    observer_status = _enum_value(
        source.get("observer_status"), ObserverStatus, ObserverStatus.NOT_RUN
    )
    reconciliation = _enum_value(
        source.get("reconciliation_status"), ReconciliationStatus,
        ReconciliationStatus.PENDING,
    )

    def _events(name: str, maximum: int) -> list[dict[str, Any]]:
        result = []
        for raw in source.get(name) or []:
            if not isinstance(raw, Mapping):
                continue
            try:
                event_time = max(
                    0.0, float(raw.get("occurred_at") or 0.0)
                )
            except (TypeError, ValueError, OverflowError):
                event_time = 0.0
            item = {
                "state": _safe_reason(raw.get("state"), "event"),
                "reason_code": _safe_reason(
                    raw.get("reason_code"), "private_reason_withheld"
                ),
                "occurred_at": event_time,
            }
            for key in (
                "evidence_ref", "observer", "classification",
                "target_generation", "provider_operation_ref",
            ):
                text = _safe_id(raw.get(key), key)
                if text:
                    item[key] = text
            if raw.get("exact") is True:
                item["exact"] = True
            if raw.get("legacy") is True:
                item["legacy"] = True
            result.append(item)
        return result[-maximum:]

    target = _safe_action_target(source.get("target"))
    policy = source.get("settle_policy")
    policy = dict(policy) if isinstance(policy, Mapping) else {}
    def _number(raw: Any) -> float:
        try:
            return max(0.0, float(raw or 0.0))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    return {
        "schema_version": ACTION_TRUTH_SCHEMA_VERSION,
        "action_id": _safe_id(source.get("action_id"), "action"),
        "semantic_effect_id": _safe_id(
            source.get("semantic_effect_id"), "effect"
        ),
        "semantic_node_id": _safe_id(source.get("semantic_node_id"), "node"),
        "goal_contract_ref": _safe_id(source.get("goal_contract_ref"), "goal"),
        "effect_class": effect_class.value,
        "operation": _safe_id(source.get("operation"), "operation"),
        "attempted_effect": _safe_id(
            source.get("attempted_effect"), "effect"
        ),
        "target": target,
        "idempotency_identity": _safe_id(
            source.get("idempotency_identity"), "idempotency"
        ),
        "dependencies": [
            item for raw in (source.get("dependencies") or [])[:32]
            if (item := _safe_id(raw, "dependency"))
        ],
        "conflict_scope": _safe_id(source.get("conflict_scope"), "scope"),
        "expected_postcondition_ref": _safe_id(
            source.get("expected_postcondition_ref"), "postcondition"
        ),
        "effect_role": (
            "preparatory"
            if source.get("effect_role") == "preparatory" else "semantic"
        ),
        "current_state": state.value,
        "effect_truth": truth.value,
        "dispatch_started_at": _number(source.get("dispatch_started_at")),
        "dispatch_returned_at": _number(source.get("dispatch_returned_at")),
        "accepted_at": _number(source.get("accepted_at")),
        "provider_operation_ref": _safe_id(
            source.get("provider_operation_ref"), "provider_ref"
        ),
        "observer_status": observer_status.value,
        "reconciliation_status": reconciliation.value,
        "current_reason": _safe_reason(
            source.get("current_reason"), "action_state_recorded"
        ),
        "evidence": _events("evidence", MAX_ACTION_EVIDENCE),
        "observers": _events("observers", MAX_ACTION_OBSERVERS),
        "reconciliation_history": _events(
            "reconciliation_history", MAX_RECONCILIATION_EVENTS
        ),
        "timeline": _events("timeline", MAX_ACTION_TIMELINE_EVENTS),
        "settle_policy": policy,
        "legacy": source.get("legacy") is True,
    }


def transition_action_truth(
    value: Mapping[str, Any],
    *,
    state: ActionState | str,
    reason_code: str,
    evidence: Mapping[str, Any] | None = None,
    observer: Mapping[str, Any] | None = None,
    reconciliation_status: ReconciliationStatus | str | None = None,
    exact: bool = False,
    occurred_at: float | None = None,
) -> dict[str, Any]:
    """Apply one invariant-checked lifecycle transition to an attempt truth."""

    result = normalize_action_truth(value)
    target_state = _enum_value(state, ActionState, ActionState.EFFECT_POSSIBLE)
    now = time.time() if occurred_at is None else float(occurred_at)
    dispatch_started = bool(result.get("dispatch_started_at"))
    legacy_ambiguity = bool(
        result.get("legacy") is True
        and result.get("effect_truth") in {
            EffectTruth.POSSIBLE.value, EffectTruth.OBSERVED.value,
        }
    )
    if target_state in {
        ActionState.DISPATCH_RETURNED,
        ActionState.ACCEPTED,
        ActionState.STILL_RUNNING,
        ActionState.EFFECT_OBSERVED,
        ActionState.EFFECT_VERIFIED,
        ActionState.EFFECT_POSSIBLE,
        ActionState.RECONCILING,
    } and (
        target_state is not ActionState.DISPATCH_STARTED
        and not dispatch_started
        and not legacy_ambiguity
    ):
        raise ValueError("semantic_dispatch_not_started")
    if target_state in {
        ActionState.PRECONDITION_FAILED, ActionState.FAILED_BEFORE_EFFECT,
    } and dispatch_started:
        raise ValueError("failed_before_effect_after_dispatch")
    if target_state is ActionState.NOT_APPLIED_VERIFIED and not exact:
        raise ValueError("exact_non_application_evidence_required")
    if target_state is ActionState.EFFECT_VERIFIED and not evidence:
        raise ValueError("verified_effect_evidence_required")

    reason = _safe_reason(reason_code, "action_state_recorded")
    event = {
        "state": target_state.value,
        "reason_code": reason,
        "occurred_at": now,
    }
    if target_state is ActionState.DISPATCH_STARTED:
        result["dispatch_started_at"] = result.get("dispatch_started_at") or now
        dispatch_started = True
    elif target_state is ActionState.DISPATCH_RETURNED:
        result["dispatch_returned_at"] = now
    elif target_state is ActionState.ACCEPTED:
        result["accepted_at"] = now

    if evidence:
        evidence_item = {
            "state": target_state.value,
            "reason_code": reason,
            "occurred_at": now,
            "evidence_ref": str(
                evidence.get("evidence_ref") or evidence_reference(evidence)
            ),
            "exact": exact is True,
        }
        provider_ref = _safe_id(
            evidence.get("provider_operation_ref"), "provider_ref"
        )
        if provider_ref:
            evidence_item["provider_operation_ref"] = provider_ref
            result["provider_operation_ref"] = provider_ref
        result["evidence"] = (
            list(result.get("evidence") or []) + [evidence_item]
        )[-MAX_ACTION_EVIDENCE:]
        event["evidence_ref"] = evidence_item["evidence_ref"]
        event["exact"] = exact is True

    if observer:
        status = _enum_value(
            observer.get("status"), ObserverStatus, ObserverStatus.INCONCLUSIVE
        )
        try:
            observer_time = max(
                0.0, float(observer.get("observed_at") or now)
            )
        except (TypeError, ValueError, OverflowError):
            observer_time = now
        observer_item = {
            "state": ActionState.OBSERVER_FAILED.value
            if status is ObserverStatus.FAILED else target_state.value,
            "reason_code": _safe_reason(
                observer.get("reason_code"), reason
            ),
            "occurred_at": observer_time,
            "observer": _safe_id(observer.get("observer"), "observer"),
            "classification": status.value,
            "evidence_ref": str(
                observer.get("evidence_ref")
                or evidence_reference({
                    "observer": observer.get("observer"),
                    "status": status.value,
                    "reason": observer.get("reason_code"),
                })
            ),
            "exact": observer.get("exact") is True,
        }
        generation = _safe_id(
            observer.get("target_generation"), "generation"
        )
        if generation:
            observer_item["target_generation"] = generation
        result["observers"] = (
            list(result.get("observers") or []) + [observer_item]
        )[-MAX_ACTION_OBSERVERS:]
        result["observer_status"] = status.value

    if target_state is ActionState.OBSERVER_FAILED:
        # A failed observer is a diagnostic event, not a rewrite of effect
        # truth. Preserve the last action state and record the failure only.
        result["observer_status"] = ObserverStatus.FAILED.value
    else:
        previous_truth = result.get("effect_truth")
        result["current_state"] = target_state.value
        result["effect_truth"] = _truth_from_state(
            target_state, dispatch_started
        ).value
        if (
            target_state is ActionState.CANCELLED
            and previous_truth in {
                EffectTruth.VERIFIED.value,
                EffectTruth.NOT_APPLIED.value,
                EffectTruth.OBSERVED.value,
            }
        ):
            result["effect_truth"] = previous_truth
    result["current_reason"] = reason
    result["timeline"] = (
        list(result.get("timeline") or []) + [event]
    )[-MAX_ACTION_TIMELINE_EVENTS:]
    if reconciliation_status is not None:
        reconciled = _enum_value(
            reconciliation_status,
            ReconciliationStatus,
            ReconciliationStatus.PENDING,
        )
        result["reconciliation_status"] = reconciled.value
        result["reconciliation_history"] = (
            list(result.get("reconciliation_history") or []) + [{
                "state": reconciled.value,
                "reason_code": reason,
                "occurred_at": now,
                **({"evidence_ref": event["evidence_ref"]}
                   if event.get("evidence_ref") else {}),
            }]
        )[-MAX_RECONCILIATION_EVENTS:]
    return normalize_action_truth(result)


def retry_decision(
    *,
    effect_class: CanonicalEffectClass | str,
    state: ActionState | str,
    effect_truth: EffectTruth | str | None = None,
    dispatch_started: bool,
    idempotency_identity: str = "",
) -> RetryDecision:
    effect = _enum_value(
        effect_class, CanonicalEffectClass,
        CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK,
    )
    current = _enum_value(state, ActionState, ActionState.EFFECT_POSSIBLE)
    truth = _enum_value(
        effect_truth,
        EffectTruth,
        _truth_from_state(current, dispatch_started),
    )
    if current is ActionState.CANCELLED:
        return RetryDecision(False, "authoritative_user_cancellation")
    if effect is CanonicalEffectClass.READ_ONLY:
        return RetryDecision(True, "read_only_reexecution_safe")
    if truth is EffectTruth.VERIFIED or current is ActionState.EFFECT_VERIFIED:
        return RetryDecision(False, "effect_verified_replay_closed")
    if current in {
        ActionState.FAILED_BEFORE_EFFECT,
        ActionState.PRECONDITION_FAILED,
    } or (
        not dispatch_started and truth is EffectTruth.NOT_POSSIBLE
    ):
        return RetryDecision(
            True, "failed_before_effect_frontier_open",
            alternate_mechanism_only=True,
        )
    if truth is EffectTruth.NOT_APPLIED or current is ActionState.NOT_APPLIED_VERIFIED:
        return RetryDecision(
            True, "not_applied_verified_frontier_open",
            alternate_mechanism_only=True,
        )
    if (
        effect is CanonicalEffectClass.IDEMPOTENT_WRITE
        and bool(_safe_id(idempotency_identity, "idempotency"))
    ):
        return RetryDecision(
            True,
            "canonical_idempotency_identity_reusable",
            reuse_idempotency_identity=True,
        )
    return RetryDecision(False, "blocked_due_to_possible_duplicate_effect")


def gate_action(
    proposed: ActionIntent,
    unresolved_actions: Iterable[ActionRecord | Mapping[str, Any]],
) -> ActionGateDecision:
    """Block only duplicative/dependent mutations; reads are never blocked."""

    if proposed.effect_class is CanonicalEffectClass.READ_ONLY:
        return ActionGateDecision(True, "read_only_allowed_during_reconciliation")
    blockers: list[str] = []
    unresolved = []
    for raw in unresolved_actions:
        if isinstance(raw, ActionRecord):
            item = raw
        elif isinstance(raw, Mapping):
            try:
                item = ActionRecord(**{
                    key: raw.get(key)
                    for key in ActionRecord.__dataclass_fields__
                })
            except TypeError:
                continue
        else:
            continue
        if item.effect_class == CanonicalEffectClass.READ_ONLY.value:
            continue
        if item.effect_truth not in {
            EffectTruth.POSSIBLE.value, EffectTruth.OBSERVED.value,
        } and item.current_state not in {
            ActionState.RECONCILING.value,
            ActionState.EFFECT_POSSIBLE.value,
            ActionState.STILL_RUNNING.value,
            ActionState.TARGET_STALE.value,
        }:
            continue
        unresolved.append(item)
    for action in unresolved:
        same_effect = bool(
            proposed.semantic_effect_id
            and proposed.semantic_effect_id == action.semantic_effect_id
        )
        dependent = bool(
            action.action_id in proposed.dependency_action_ids
            or action.semantic_effect_id in proposed.dependency_effect_ids
            or (
                proposed.semantic_node_id
                and proposed.semantic_node_id in action.dependencies
            )
        )
        conflicts = bool(
            proposed.conflict_scope
            and proposed.conflict_scope == str(
                action.conflict_scope
                or (action.target or {}).get("conflict_scope")
                or (action.target or {}).get("surface_ref")
                or (action.target or {}).get("page_id")
                or ""
            )
        )
        existing_target = action.target or {}
        proposed_target = proposed.target or {}
        provably_disjoint = bool(
            existing_target.get("kind") == proposed_target.get("kind") == "desktop"
            and existing_target.get("application")
            and proposed_target.get("application")
            and existing_target.get("application")
            != proposed_target.get("application")
        ) or bool(
            existing_target.get("kind") == proposed_target.get("kind") == "browser"
            and existing_target.get("page_id")
            and proposed_target.get("page_id")
            and existing_target.get("page_id") != proposed_target.get("page_id")
        )
        explicitly_independent = bool(
            action.action_id in proposed.independent_of_action_ids
            or provably_disjoint
        )
        canonical_idempotent_replay = bool(
            same_effect
            and proposed.effect_class is CanonicalEffectClass.IDEMPOTENT_WRITE
            and action.effect_class == CanonicalEffectClass.IDEMPOTENT_WRITE.value
            and proposed.idempotency_identity
            and proposed.idempotency_identity == action.idempotency_identity
        )
        if canonical_idempotent_replay:
            continue
        if same_effect or dependent or conflicts or not explicitly_independent:
            blockers.append(action.action_id)
    if blockers:
        return ActionGateDecision(
            False,
            "blocked_due_to_possible_duplicate_effect",
            tuple(dict.fromkeys(blockers)),
        )
    return ActionGateDecision(True, "independent_branch_allowed")


class ProvidedObservationAdapter:
    name = "provided_observation"

    def supports(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> bool:
        return bool(current_world_state.get("observer_results"))

    def observe(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> ObservationResult:
        queue = current_world_state.get("observer_results")
        raw = queue.pop(0) if isinstance(queue, list) and queue else None
        if isinstance(raw, ObservationResult):
            return raw
        if not isinstance(raw, Mapping):
            return ObservationResult(
                ObservationClassification.FAILED,
                self.name,
                "observer_result_missing",
            )
        classification = _enum_value(
            raw.get("classification"),
            ObservationClassification,
            ObservationClassification.INCONCLUSIVE,
        )
        return ObservationResult(
            classification=classification,
            observer=_safe_id(raw.get("observer"), "observer") or self.name,
            reason_code=_safe_reason(
                raw.get("reason_code"), "observer_inconclusive"
            ),
            evidence_ref=str(
                raw.get("evidence_ref") or evidence_reference(raw)
            ),
            observed_at=float(raw.get("observed_at") or 0.0),
            exact=raw.get("exact") is True,
            target_generation=_safe_id(
                raw.get("target_generation"), "generation"
            ),
            provider_operation_ref=_safe_id(
                raw.get("provider_operation_ref"), "provider_ref"
            ),
        )


class BrowserReconciliationAdapter:
    name = "browser_state_observer"

    def __init__(self, reader: Callable[[], Any] | None = None):
        self.reader = reader

    def supports(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> bool:
        return bool(
            (action.target or {}).get("kind") == "browser"
            or action.operation in _URL_OPERATIONS
            or action.operation in _READ_OPERATIONS
        )

    def observe(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> ObservationResult:
        reader = current_world_state.get("browser_reader") or self.reader
        try:
            payload = (
                current_world_state.get("browser_snapshot")
                if current_world_state.get("browser_snapshot") is not None
                else reader() if callable(reader) else None
            )
        except Exception:
            return ObservationResult(
                ObservationClassification.FAILED,
                self.name,
                "browser_state_observer_failed",
            )
        if not isinstance(payload, Mapping):
            return ObservationResult(
                ObservationClassification.FAILED,
                self.name,
                "browser_state_observer_missing",
            )
        receipt = payload.get("receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        error = payload.get("error")
        error = error if isinstance(error, Mapping) else {}
        reason = _safe_reason(
            error.get("code") or receipt.get("reason_code"),
            "browser_state_observed",
        )
        if any(token in reason for token in ("stale", "closed", "lost")):
            return ObservationResult(
                ObservationClassification.TARGET_STALE,
                self.name,
                "browser_page_target_stale",
                evidence_reference({"reason": reason}),
                exact=True,
            )
        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        current_url = safe_url_identity(
            data.get("safe_url") or payload.get("safe_url")
        )
        expected_url = str((action.target or {}).get("safe_url") or "")
        state = data.get("state")
        state = state if isinstance(state, Mapping) else {}
        generation = _safe_id(state.get("page_generation"), "generation")
        evidence = evidence_reference({
            "safe_url": current_url,
            "state": {
                key: state.get(key) for key in (
                    "browser_session_id", "context_id", "page_id",
                    "context_epoch", "page_generation", "navigation_id",
                    "document_epoch",
                )
            },
        })
        if action.effect_class == CanonicalEffectClass.READ_ONLY.value:
            # Fresh browser state can verify a state-read itself, but it cannot
            # recreate a lost page extraction/site-audit result.  Those reads
            # remain eligible for safe re-execution instead of false success.
            state_read = action.operation in {
                "get_browser_state", "web_state", "get_current_url",
            }
            if state_read and (
                payload.get("ok") is True
                or receipt.get("outcome") == "confirmed_success"
            ):
                return ObservationResult(
                    ObservationClassification.MATCH,
                    self.name,
                    "read_only_result_observed",
                    evidence,
                    exact=True,
                    target_generation=generation,
                )
            return ObservationResult(
                ObservationClassification.INCONCLUSIVE,
                self.name,
                "read_only_result_not_retained",
                evidence,
                target_generation=generation,
            )
        if expected_url and current_url == expected_url:
            return ObservationResult(
                ObservationClassification.MATCH,
                self.name,
                "browser_url_postcondition_verified",
                evidence,
                exact=True,
                target_generation=generation,
            )
        if (
            action.operation in {"open_browser", "open_new_browser_tab"}
            and not expected_url
            and (payload.get("ok") is True or receipt.get("outcome") == "confirmed_success")
            and state.get("page_id")
        ):
            return ObservationResult(
                ObservationClassification.MATCH,
                self.name,
                "browser_page_postcondition_verified",
                evidence,
                exact=True,
                target_generation=generation,
            )
        expected_page = str((action.target or {}).get("page_id") or "")
        observed_page = str(state.get("page_id") or "")
        if expected_page and observed_page and expected_page != observed_page:
            return ObservationResult(
                ObservationClassification.TARGET_STALE,
                self.name,
                "browser_page_target_stale",
                evidence,
                exact=True,
                target_generation=generation,
            )
        if current_world_state.get("browser_exact_non_application") is True:
            return ObservationResult(
                ObservationClassification.NON_APPLIED,
                self.name,
                "browser_navigation_not_applied_verified",
                evidence,
                exact=True,
                target_generation=generation,
            )
        return ObservationResult(
            ObservationClassification.INCONCLUSIVE,
            self.name,
            "browser_postcondition_inconclusive",
            evidence,
            target_generation=generation,
        )


class DesktopReconciliationAdapter:
    name = "desktop_surface_observer"

    def __init__(
        self,
        reader: Callable[[str], Any] | None = None,
        vision_reader: Callable[[ActionRecord], Any] | None = None,
    ):
        self.reader = reader
        self.vision_reader = vision_reader

    def supports(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> bool:
        return bool(
            (action.target or {}).get("kind") == "desktop"
            or action.operation in {
                "activate_desktop_surface", "open_application",
                "inspect_desktop_surfaces", "request_visual_observation",
            }
        )

    def observe(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> ObservationResult:
        reader = current_world_state.get("desktop_reader") or self.reader
        target = str((action.target or {}).get("application") or "")
        try:
            payload = (
                current_world_state.get("desktop_snapshot")
                if current_world_state.get("desktop_snapshot") is not None
                else reader(target) if callable(reader) else None
            )
            if not isinstance(payload, Mapping):
                vision_reader = (
                    current_world_state.get("desktop_vision_reader")
                    or self.vision_reader
                )
                payload = (
                    vision_reader(action)
                    if callable(vision_reader) else payload
                )
        except Exception:
            return ObservationResult(
                ObservationClassification.FAILED,
                self.name,
                "desktop_observer_failed",
            )
        if not isinstance(payload, Mapping):
            return ObservationResult(
                ObservationClassification.FAILED,
                self.name,
                "desktop_observer_missing",
            )
        receipt = payload.get("receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        reason = _safe_reason(
            receipt.get("reason_code"), "desktop_state_observed"
        )
        evidence = evidence_reference({
            "outcome": receipt.get("outcome"),
            "reason": reason,
            "surface": receipt.get("observed_window_surface_ref")
            or receipt.get("window_surface_ref"),
            "generation": receipt.get("window_generation"),
        })
        generation = _safe_id(
            receipt.get("window_generation"), "generation"
        )
        visual = payload.get("visual")
        visual = visual if isinstance(visual, Mapping) else {}
        verdict = visual.get("verdict")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        expected_ref = str(action.expected_postcondition_ref or "")
        verdict_expected_ref = (
            evidence_reference(verdict.get("expected_postcondition"))
            if verdict.get("expected_postcondition") else ""
        )
        expected_surface = str((action.target or {}).get("surface_ref") or "")
        observed_surface = str(
            receipt.get("surface_ref")
            or receipt.get("window_surface_ref")
            or ""
        )
        expected_generation = str(
            (action.target or {}).get("window_generation") or ""
        )
        exact_visual_binding = bool(
            expected_ref
            and verdict_expected_ref == expected_ref
            and (not expected_surface or observed_surface == expected_surface)
            and (not expected_generation or generation == expected_generation)
            and receipt.get("exact_surface_pixels") is True
        )
        if verdict.get("status") == "match" and exact_visual_binding:
            return ObservationResult(
                ObservationClassification.MATCH,
                "desktop_vision_observer",
                "vision_postcondition_verified",
                evidence,
                observed_at=float(receipt.get("observed_at") or 0.0),
                exact=True,
                target_generation=generation,
            )
        if receipt.get("outcome") == "confirmed_success" and reason in {
            "exact_final_state_observation",
            "exact_desktop_target_focus_observed",
            "exact_desktop_primitive_state_change",
            "exact_desktop_observable_effect_change",
            "vision_postcondition_verified",
        }:
            return ObservationResult(
                ObservationClassification.MATCH,
                self.name,
                "desktop_postcondition_verified",
                evidence,
                observed_at=float(receipt.get("observed_at") or 0.0),
                exact=True,
                target_generation=generation,
            )
        if (
            receipt.get("outcome") == "confirmed_absence"
            and receipt.get("no_partial_effect") is True
        ):
            return ObservationResult(
                ObservationClassification.NON_APPLIED,
                self.name,
                "desktop_effect_not_applied_verified",
                evidence,
                observed_at=float(receipt.get("observed_at") or 0.0),
                exact=True,
                target_generation=generation,
            )
        if any(token in reason for token in ("stale", "not_found", "unavailable")):
            return ObservationResult(
                ObservationClassification.TARGET_STALE,
                self.name,
                "desktop_target_stale",
                evidence,
                exact=True,
                target_generation=generation,
            )
        return ObservationResult(
            ObservationClassification.INCONCLUSIVE,
            self.name,
            "desktop_postcondition_inconclusive",
            evidence,
            observed_at=float(receipt.get("observed_at") or 0.0),
            target_generation=generation,
        )


class ActionTruthCoordinator:
    """Canonical action-level coordinator over the existing attempt store."""

    def __init__(
        self,
        store: ActionTruthStore | None = None,
        *,
        adapters: Iterable[ReconciliationAdapter] = (),
        clock: Clock | None = None,
    ):
        if store is None:
            from osia.tool_attempts import get_tool_attempt_store

            store = get_tool_attempt_store()
        self.store = store
        self.clock = clock or SystemClock()
        supplied = list(adapters)
        self.adapters: list[ReconciliationAdapter] = supplied or [
            ProvidedObservationAdapter(),
            BrowserReconciliationAdapter(),
            DesktopReconciliationAdapter(),
        ]

    @staticmethod
    def _attempt_truth(attempt: Mapping[str, Any]) -> dict[str, Any]:
        return normalize_action_truth(
            attempt.get("action_truth"), legacy_record=attempt
        )

    def action_record(self, action_id: str) -> ActionRecord | None:
        attempts = self.store.list_action_attempts(action_id)
        if not attempts:
            return None

        def _created_at(item: Mapping[str, Any]) -> float:
            try:
                return max(0.0, float(item.get("created_at") or 0.0))
            except (TypeError, ValueError, OverflowError):
                return 0.0

        attempts = sorted(
            attempts, key=_created_at,
        )
        truths = [self._attempt_truth(item) for item in attempts]
        terminal_verified = next((
            item for item in reversed(truths)
            if item["effect_truth"] == EffectTruth.VERIFIED.value
        ), None)
        # Verified effect closes the semantic action permanently.  Exact
        # non-application only reopens the frontier for a later attempt; it
        # must not hide that later attempt's possible/observed state.
        current = terminal_verified or truths[-1]
        first = attempts[0]
        evidence = [
            item for truth in truths for item in truth.get("evidence") or []
        ][-MAX_ACTION_EVIDENCE:]
        observers = [
            item for truth in truths for item in truth.get("observers") or []
        ][-MAX_ACTION_OBSERVERS:]
        history = [
            item for truth in truths
            for item in truth.get("reconciliation_history") or []
        ][-MAX_RECONCILIATION_EVENTS:]
        return ActionRecord(
            action_id=current["action_id"],
            semantic_effect_id=current["semantic_effect_id"],
            run_id=str(first.get("autonomous_run_id") or first.get("turn_id") or ""),
            turn_id=str(first.get("turn_id") or ""),
            request_id=str(first.get("request_id") or ""),
            goal_contract_ref=current["goal_contract_ref"],
            effect_class=current["effect_class"],
            operation=current["operation"],
            target=dict(current.get("target") or {}),
            idempotency_identity=current["idempotency_identity"],
            current_state=current["current_state"],
            effect_truth=current["effect_truth"],
            attempts=[dict(item) for item in attempts],
            evidence=evidence,
            observers=observers,
            dependencies=list(current.get("dependencies") or []),
            conflict_scope=current.get("conflict_scope", ""),
            expected_postcondition_ref=current.get(
                "expected_postcondition_ref", ""
            ),
            effect_role=current.get("effect_role", "semantic"),
            reconciliation_state=current["reconciliation_status"],
            reconciliation_history=history,
            legacy=all(item.get("legacy") is True for item in truths),
        )

    def unresolved_actions(
        self, *, conversation_id: str = "", turn_id: str = ""
    ) -> list[ActionRecord]:
        records: dict[str, ActionRecord] = {}
        for attempt in self.store.list_unresolved_action_attempts(
            conversation_id=conversation_id, turn_id=turn_id
        ):
            truth = self._attempt_truth(attempt)
            action_id = truth["action_id"]
            if action_id in records:
                continue
            action = self.action_record(action_id)
            if action is not None and (
                action.effect_truth in {
                    EffectTruth.POSSIBLE.value, EffectTruth.OBSERVED.value,
                }
                or action.current_state in {
                    ActionState.RECONCILING.value,
                    ActionState.STILL_RUNNING.value,
                    ActionState.TARGET_STALE.value,
                }
            ):
                records[action_id] = action
        return list(records.values())

    @staticmethod
    def _latest_attempt_id(action: ActionRecord) -> str:
        return str(action.attempts[-1].get("tool_attempt_id") or "")

    def _update(
        self,
        action: ActionRecord,
        *,
        state: ActionState,
        reason_code: str,
        evidence: Mapping[str, Any] | None = None,
        observer: Mapping[str, Any] | None = None,
        reconciliation_status: ReconciliationStatus | None = None,
        exact: bool = False,
    ) -> ActionRecord:
        self.store.update_action_truth(
            self._latest_attempt_id(action),
            state=state.value,
            reason_code=reason_code,
            evidence=evidence,
            observer=observer,
            reconciliation_status=(
                reconciliation_status.value if reconciliation_status else ""
            ),
            exact=exact,
            occurred_at=self.clock.time(),
        )
        return self.action_record(action.action_id) or action

    def reconcile(
        self,
        action_id: str,
        current_world_state: Mapping[str, Any] | None = None,
    ) -> ReconciliationDecision:
        """Reconstruct one action truth using receipts then safe observers."""

        world = dict(current_world_state or {})
        action = self.action_record(action_id)
        if action is None:
            return ReconciliationDecision(
                action_id,
                ActionState.TARGET_STALE,
                EffectTruth.NOT_POSSIBLE,
                ReconciliationStatus.EXHAUSTED,
                "action_record_missing",
                False,
                ObserverStatus.NOT_RUN,
                True,
            )
        dispatch_started = any(
            bool(self._attempt_truth(item).get("dispatch_started_at"))
            for item in action.attempts
        )
        if action.current_state == ActionState.CANCELLED.value:
            return ReconciliationDecision(
                action.action_id,
                ActionState.CANCELLED,
                _enum_value(
                    action.effect_truth,
                    EffectTruth,
                    EffectTruth.POSSIBLE if dispatch_started else EffectTruth.NOT_POSSIBLE,
                ),
                ReconciliationStatus.CANCELLED,
                "authoritative_user_cancellation",
                False,
                ObserverStatus.NOT_RUN,
            )
        if action.effect_truth == EffectTruth.VERIFIED.value:
            return ReconciliationDecision(
                action.action_id,
                ActionState.EFFECT_VERIFIED,
                EffectTruth.VERIFIED,
                ReconciliationStatus.VERIFIED,
                "effect_already_verified",
                False,
                ObserverStatus.MATCH,
            )
        if (
            not dispatch_started
            and action.effect_truth not in {
                EffectTruth.POSSIBLE.value, EffectTruth.OBSERVED.value,
            }
        ):
            action = self._update(
                action,
                state=ActionState.NOT_APPLIED_VERIFIED,
                reason_code="dispatch_not_started",
                evidence={"evidence_ref": evidence_reference("dispatch_not_started")},
                reconciliation_status=ReconciliationStatus.NOT_APPLIED,
                exact=True,
            )
            return ReconciliationDecision(
                action.action_id,
                ActionState.NOT_APPLIED_VERIFIED,
                EffectTruth.NOT_APPLIED,
                ReconciliationStatus.NOT_APPLIED,
                "dispatch_not_started",
                True,
                ObserverStatus.NOT_RUN,
            )
        action = self._update(
            action,
            state=ActionState.RECONCILING,
            reason_code="reconciliation_pending",
            reconciliation_status=ReconciliationStatus.RUNNING,
        )
        applicable = [
            adapter for adapter in self.adapters if adapter.supports(action, world)
        ]
        last_status = ObserverStatus.NOT_RUN
        any_nonfailure = False
        for adapter in applicable:
            try:
                observed = adapter.observe(action, world)
            except Exception:
                observed = ObservationResult(
                    ObservationClassification.FAILED,
                    getattr(adapter, "name", "observer"),
                    "observer_failed",
                )
            status = {
                ObservationClassification.MATCH: ObserverStatus.MATCH,
                ObservationClassification.NON_APPLIED: ObserverStatus.NON_APPLIED,
                ObservationClassification.STILL_RUNNING: ObserverStatus.RUNNING,
                ObservationClassification.INCONCLUSIVE: ObserverStatus.INCONCLUSIVE,
                ObservationClassification.FAILED: ObserverStatus.FAILED,
                ObservationClassification.TARGET_STALE: ObserverStatus.TARGET_STALE,
            }[observed.classification]
            last_status = status
            any_nonfailure = any_nonfailure or status not in {
                ObserverStatus.FAILED, ObserverStatus.NOT_RUN,
            }
            observer = {
                "status": status.value,
                "observer": observed.observer,
                "reason_code": observed.reason_code,
                "evidence_ref": observed.evidence_ref,
                "observed_at": observed.observed_at or self.clock.time(),
                "exact": observed.exact,
                "target_generation": observed.target_generation,
            }
            latest_attempt = (
                action.attempts[-1] if action.attempts else {}
            )
            observer_material = "\0".join((
                action.action_id,
                str(observed.observer or ""),
                observed.classification.value,
                str(observed.evidence_ref or ""),
                str(observed.target_generation or ""),
            ))
            observer["observer_attempt_id"] = (
                "observer_" + hashlib.sha256(
                    observer_material.encode("utf-8", errors="replace")
                ).hexdigest()[:40]
            )
            observer["execution_epoch"] = str(
                latest_attempt.get("execution_epoch") or ""
            )
            evidence = {
                "evidence_ref": observed.evidence_ref
                or evidence_reference(observer),
                "provider_operation_ref": observed.provider_operation_ref,
            }
            if observed.classification is ObservationClassification.MATCH:
                action = self._update(
                    action,
                    state=ActionState.EFFECT_VERIFIED,
                    reason_code="effect_verified",
                    evidence=evidence,
                    observer=observer,
                    reconciliation_status=ReconciliationStatus.VERIFIED,
                    exact=observed.exact,
                )
                return ReconciliationDecision(
                    action.action_id,
                    ActionState.EFFECT_VERIFIED,
                    EffectTruth.VERIFIED,
                    ReconciliationStatus.VERIFIED,
                    "effect_verified",
                    False,
                    ObserverStatus.MATCH,
                )
            if (
                observed.classification is ObservationClassification.NON_APPLIED
                and observed.exact
            ):
                action = self._update(
                    action,
                    state=ActionState.NOT_APPLIED_VERIFIED,
                    reason_code="not_applied_verified",
                    evidence=evidence,
                    observer=observer,
                    reconciliation_status=ReconciliationStatus.NOT_APPLIED,
                    exact=True,
                )
                return ReconciliationDecision(
                    action.action_id,
                    ActionState.NOT_APPLIED_VERIFIED,
                    EffectTruth.NOT_APPLIED,
                    ReconciliationStatus.NOT_APPLIED,
                    "not_applied_verified",
                    True,
                    ObserverStatus.NON_APPLIED,
                )
            if observed.classification is ObservationClassification.STILL_RUNNING:
                action = self._update(
                    action,
                    state=ActionState.STILL_RUNNING,
                    reason_code="propagation_pending",
                    evidence=evidence,
                    observer=observer,
                    reconciliation_status=ReconciliationStatus.PROPAGATION_PENDING,
                )
                return ReconciliationDecision(
                    action.action_id,
                    ActionState.STILL_RUNNING,
                    EffectTruth.POSSIBLE,
                    ReconciliationStatus.PROPAGATION_PENDING,
                    "propagation_pending",
                    False,
                    ObserverStatus.RUNNING,
                )
            if observed.classification is ObservationClassification.TARGET_STALE:
                action = self._update(
                    action,
                    state=ActionState.TARGET_STALE,
                    reason_code="target_stale",
                    evidence=evidence,
                    observer=observer,
                    reconciliation_status=ReconciliationStatus.PENDING,
                    exact=observed.exact,
                )
                return ReconciliationDecision(
                    action.action_id,
                    ActionState.TARGET_STALE,
                    EffectTruth.POSSIBLE,
                    ReconciliationStatus.PENDING,
                    "target_stale",
                    False,
                    ObserverStatus.TARGET_STALE,
                )
            action = self._update(
                action,
                state=(
                    ActionState.OBSERVER_FAILED
                    if observed.classification is ObservationClassification.FAILED
                    else ActionState.EFFECT_POSSIBLE
                ),
                reason_code=(
                    "observer_failed_after_dispatch"
                    if observed.classification is ObservationClassification.FAILED
                    else "observer_inconclusive"
                ),
                evidence=evidence if observed.evidence_ref else None,
                observer=observer,
                reconciliation_status=ReconciliationStatus.PENDING,
            )
        exhausted = bool(world.get("safe_avenues_exhausted"))
        status = (
            ReconciliationStatus.EXHAUSTED
            if exhausted else ReconciliationStatus.PENDING
        )
        reason = (
            "reconciliation_exhausted"
            if exhausted else
            "observer_failed_after_dispatch"
            if applicable and not any_nonfailure else
            "reconciliation_pending"
        )
        action = self._update(
            action,
            state=ActionState.EFFECT_POSSIBLE,
            reason_code=reason,
            reconciliation_status=status,
        )
        return ReconciliationDecision(
            action.action_id,
            ActionState.EFFECT_POSSIBLE,
            EffectTruth.POSSIBLE,
            status,
            reason,
            False,
            last_status,
            exhausted,
        )


__all__ = [
    "ACTION_TRUTH_SCHEMA_VERSION", "ActionGateDecision", "ActionIntent",
    "ActionRecord", "ActionState", "ActionTruthCoordinator",
    "BrowserReconciliationAdapter", "CanonicalEffectClass",
    "DesktopReconciliationAdapter", "EffectTruth", "ObservationClassification",
    "ObservationResult", "ObserverStatus", "ProvidedObservationAdapter",
    "ReconciliationDecision", "ReconciliationStatus", "RetryDecision",
    "SettlePolicy", "SettlePolicyKind", "SystemClock", "action_identity",
    "action_target_for_tool", "canonical_effect_class", "evidence_reference",
    "gate_action", "legacy_action_truth", "new_action_truth",
    "normalize_action_target", "normalize_action_truth", "retry_decision",
    "safe_url_identity",
    "semantic_effect_identity", "settle_policy_for", "transition_action_truth",
]
