"""WOR-260 Phase 4A adaptive-reasoning policy (shadow only).

This module is a pure sidecar.  It consumes already-authoritative reasoning
metadata and Phase-3 state snapshots, then returns an immutable recommendation.
It never changes provider payloads, permissions, identity, outcome handling,
terminality, persisted settings, or runtime dispatch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Iterable, Mapping


REASONING_CONTEXT_SCHEMA_VERSION = "1.0"


class ReasoningPhase(str, Enum):
    INTENT_COMPILATION = "intent_compilation"
    CAPABILITY_SEARCH = "capability_search"
    INITIAL_PLANNING = "initial_planning"
    PRE_ACTION_VALIDATION = "pre_action_validation"
    POST_ACTION_VERIFICATION = "post_action_verification"
    REPLANNING = "replanning"
    RECONCILIATION = "reconciliation"
    TERMINAL_BLOCK_REVIEW = "terminal_block_review"


class DifficultyClass(str, Enum):
    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL_RECONCILIATION = "critical_reconciliation"


class GoalComplexity(str, Enum):
    SIMPLE = "simple"
    MULTI_STEP = "multi_step"
    DEPENDENCY_HEAVY = "dependency_heavy"


class AmbiguityLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OutcomeUnknownComplexity(str, Enum):
    NONE = "none"
    SIMPLE = "simple"
    COMPLEX = "complex"


class SideEffectRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class Irreversibility(str, Enum):
    NONE = "none"
    REVERSIBLE = "reversible"
    DIFFICULT = "difficult"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class ProgressSignal(str, Enum):
    CLEAR = "clear"
    PARTIAL = "partial"
    STALLED = "stalled"
    UNKNOWN = "unknown"


class ProviderParameterBehavior(str, Enum):
    OMIT_PROVIDER_DEFAULT = "omit_provider_default"
    EMIT_NATIVE_VALUE = "emit_native_value"
    OVERRIDE_BLOCKED = "override_blocked"


class DecisionConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ReasoningCapabilityValue:
    """One value copied from the exact authoritative capability mapping."""

    osia_level: str
    native_value: str | bool | int | float | None
    provider_parameter_name: str | None
    ordinal: int
    disables_reasoning: bool


@dataclass(frozen=True)
class AuthoritativeReasoningCapability:
    provider_id: str
    model_id: str
    configurable: bool
    supports_auto: bool
    ordered_values: tuple[ReasoningCapabilityValue, ...]
    provider_default_value: str
    provider_default_behavior: str
    source_authority: str

    @property
    def supported_osia_values(self) -> tuple[str, ...]:
        prefix = ("auto",) if self.supports_auto else ()
        return prefix + tuple(item.osia_level for item in self.ordered_values)

    @property
    def adaptive_values(self) -> tuple[ReasoningCapabilityValue, ...]:
        """Declared values that represent positive reasoning effort."""

        return tuple(item for item in self.ordered_values if not item.disables_reasoning)

    def value_for(self, osia_level: str) -> ReasoningCapabilityValue | None:
        wanted = _normalize_requested_level(osia_level)
        return next(
            (item for item in self.ordered_values if item.osia_level == wanted),
            None,
        )


@dataclass(frozen=True)
class ReasoningDecision:
    provider_id: str
    model_id: str
    phase: ReasoningPhase
    difficulty_class: DifficultyClass
    requested_mode: str
    adaptive_allowed: bool
    selected_osia_level: str | None
    selected_native_value: str | bool | int | float | None
    provider_parameter_behavior: ProviderParameterBehavior
    escalation_reason_codes: tuple[str, ...]
    deescalation_reason_codes: tuple[str, ...]
    unsupported_reason: str
    confidence_class: DecisionConfidence


@dataclass(frozen=True)
class ReasoningDecisionContext:
    schema_version: str
    provider_id: str
    model_id: str
    user_reasoning_mode: str
    explicit_turn_reasoning: str | None
    supported_reasoning_values: tuple[str, ...]
    provider_default_behavior: str
    phase: ReasoningPhase
    goal_complexity: GoalComplexity
    candidate_count: int
    close_candidate_count: int
    ambiguity_level: AmbiguityLevel
    contradiction_count: int
    unresolved_constraint_count: int
    remaining_goal_node_count: int
    replan_count: int
    repeated_replan_count: int
    stale_ref_count: int
    outcome_unknown_count: int
    outcome_unknown_complexity: OutcomeUnknownComplexity
    verifier_status: str
    required_observation_count: int
    evidence_strength: str
    evidence_conflict: bool
    side_effect_risk: SideEffectRisk
    irreversibility: Irreversibility
    cross_application_count: int
    previous_reasoning_decision: ReasoningDecision | None
    progress_signal: ProgressSignal
    exact_candidate_count: int = 0
    exact_direct_capability: bool = False
    semantic_effect_exact: bool = False
    surface_account_exact: bool = False
    unresolved_permission_count: int = 0
    unresolved_account_binding_count: int = 0
    dependency_count: int = 0
    incomplete_world_state_count: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != REASONING_CONTEXT_SCHEMA_VERSION:
            raise ValueError("unsupported_reasoning_context_schema")
        non_negative = (
            "candidate_count",
            "close_candidate_count",
            "contradiction_count",
            "unresolved_constraint_count",
            "remaining_goal_node_count",
            "replan_count",
            "repeated_replan_count",
            "stale_ref_count",
            "outcome_unknown_count",
            "required_observation_count",
            "cross_application_count",
            "exact_candidate_count",
            "unresolved_permission_count",
            "unresolved_account_binding_count",
            "dependency_count",
            "incomplete_world_state_count",
        )
        if any(int(getattr(self, name)) < 0 for name in non_negative):
            raise ValueError("reasoning_context_counts_must_be_non_negative")


_PRIMARY_PARAMETER_NAMES = (
    "reasoning_effort",
    "output_config.effort",
    "effort",
    "thinking.type",
)
_UNKNOWN_VALUES = frozenset({"", "unknown", "none", "null", "unavailable"})
_STRONG_EVIDENCE = frozenset({"observable_state", "exact_postcondition"})
_RISK_ORDER = {
    SideEffectRisk.NONE: 0,
    SideEffectRisk.LOW: 1,
    SideEffectRisk.MODERATE: 2,
    SideEffectRisk.HIGH: 3,
    SideEffectRisk.CRITICAL: 4,
    SideEffectRisk.UNKNOWN: 5,
}
_IRREVERSIBILITY_ORDER = {
    Irreversibility.NONE: 0,
    Irreversibility.REVERSIBLE: 1,
    Irreversibility.DIFFICULT: 2,
    Irreversibility.IRREVERSIBLE: 3,
    Irreversibility.UNKNOWN: 4,
}


def _normalize_requested_level(value: object) -> str:
    if not isinstance(value, str):
        return ""
    level = value.strip().lower()
    if level == "default":
        return "auto"
    if level == "none":
        return "off"
    if (
        1 <= len(level) <= 32
        and level[0].isalpha()
        and all(char.isalnum() or char in {"_", "-"} for char in level)
    ):
        return level
    return ""


def _provider_parameters(kwargs: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    parameters: list[tuple[str, Any]] = []

    def visit(value: Any, prefix: str = "") -> None:
        if not isinstance(value, Mapping):
            if prefix and isinstance(value, (str, bool, int, float)):
                parameters.append((prefix, value))
            return
        for raw_key, child in value.items():
            key = str(raw_key)
            child_prefix = (
                prefix
                if not prefix and key == "extra_body"
                else f"{prefix}.{key}" if prefix else key
            )
            visit(child, child_prefix)

    visit(kwargs)
    return tuple(parameters)


def _primary_parameter(
    kwargs: Mapping[str, Any],
) -> tuple[str | None, str | bool | int | float | None]:
    parameters = _provider_parameters(kwargs)
    for name in _PRIMARY_PARAMETER_NAMES:
        match = next((item for item in parameters if item[0] == name), None)
        if match:
            return match
    return parameters[0] if parameters else (None, None)


def adapt_authoritative_reasoning_capability(
    provider_id: str,
    model_id: str,
    raw_capability: Mapping[str, Any] | None,
) -> AuthoritativeReasoningCapability:
    """Normalize one exact WOR-209 capability without inventing values.

    Declaration order is preserved as the provider/model-specific effort order.
    The policy never interprets a model name or manufactures a universal label.
    """

    provider = str(provider_id or "").strip().lower()
    model = str(model_id or "").strip()
    options = (
        tuple(raw_capability.get("options") or ())
        if isinstance(raw_capability, Mapping)
        else ()
    )
    seen: set[str] = set()
    supports_auto = False
    values: list[ReasoningCapabilityValue] = []
    for option in options:
        if not isinstance(option, Mapping):
            continue
        option_id = _normalize_requested_level(option.get("id"))
        if not option_id or option_id in seen:
            continue
        seen.add(option_id)
        if option_id == "auto":
            supports_auto = True
            continue
        kwargs = option.get("kwargs")
        if not isinstance(kwargs, Mapping):
            continue
        parameter_name, native_value = _primary_parameter(kwargs)
        if parameter_name is None:
            continue
        values.append(
            ReasoningCapabilityValue(
                osia_level=option_id,
                native_value=native_value,
                provider_parameter_name=parameter_name,
                ordinal=len(values),
                disables_reasoning=(
                    option_id == "off"
                    or str(native_value).strip().lower() in {"none", "off", "disabled"}
                ),
            )
        )
    return AuthoritativeReasoningCapability(
        provider_id=provider,
        model_id=model,
        configurable=bool(values),
        supports_auto=supports_auto,
        ordered_values=tuple(values),
        provider_default_value=str(
            (raw_capability or {}).get("provider_default") or ""
        ),
        provider_default_behavior="omit_provider_parameter",
        source_authority=str((raw_capability or {}).get("evidence") or "unknown"),
    )


def capability_from_current_registry(
    provider_id: str,
    model_id: str,
) -> AuthoritativeReasoningCapability:
    """Read the current exact-pair registry; no discovery or network is used."""

    from osia.reasoning import get_reasoning_capability

    raw = get_reasoning_capability(provider_id, model_id)
    return adapt_authoritative_reasoning_capability(provider_id, model_id, raw)


def _difficulty_assessment(
    context: ReasoningDecisionContext,
) -> tuple[
    DifficultyClass,
    tuple[str, ...],
    tuple[str, ...],
    DecisionConfidence,
]:
    escalation: list[str] = []
    deescalation: list[str] = []
    score = 0

    if context.close_candidate_count >= 2:
        score += 2
        escalation.append("multiple_close_candidates")
    if context.cross_application_count >= 2:
        score += 2
        escalation.append("multi_application_plan")
    if context.ambiguity_level == AmbiguityLevel.HIGH:
        score += 2
        escalation.append("high_ambiguity")
    elif context.ambiguity_level in {AmbiguityLevel.LOW, AmbiguityLevel.MEDIUM}:
        score += 1
        escalation.append("structured_ambiguity")
    if context.contradiction_count or context.evidence_conflict:
        score += 2
        escalation.append("contradictory_evidence")
    if context.stale_ref_count:
        score += 1
        escalation.append("stale_state_replan")
    if context.repeated_replan_count:
        score += 3
        escalation.append("repeated_replanning")
    elif context.replan_count:
        score += 1
        escalation.append("state_conditioned_replan")
    if context.unresolved_constraint_count or context.required_observation_count >= 2:
        score += 2
        escalation.append("unresolved_postconditions")
    if context.verifier_status == "contradicted":
        score += 2
        escalation.append("verifier_contradiction")
    if context.dependency_count >= 2 or context.goal_complexity == GoalComplexity.DEPENDENCY_HEAVY:
        score += 2
        escalation.append("dependency_complexity")
    elif context.goal_complexity == GoalComplexity.MULTI_STEP:
        score += 1
        escalation.append("multi_step_plan")
    if context.unresolved_account_binding_count:
        score += 2
        escalation.append("account_surface_ambiguity")
    if context.unresolved_permission_count:
        score += 1
        escalation.append("unresolved_permission_state")
    if context.incomplete_world_state_count:
        score += 1
        escalation.append("incomplete_world_state")
    if context.side_effect_risk in {SideEffectRisk.HIGH, SideEffectRisk.CRITICAL}:
        score += 1
        escalation.append("high_side_effect_risk")
    if context.irreversibility in {Irreversibility.DIFFICULT, Irreversibility.IRREVERSIBLE}:
        score += 2
        escalation.append("irreversible_action_review")
    if context.outcome_unknown_count:
        score += 2
        escalation.append("outcome_unknown_reconciliation")
        if context.outcome_unknown_complexity == OutcomeUnknownComplexity.COMPLEX:
            score += 2
            escalation.append("complex_reconciliation")

    critical = bool(
        context.outcome_unknown_count
        and context.outcome_unknown_complexity == OutcomeUnknownComplexity.COMPLEX
        and (
            context.phase == ReasoningPhase.RECONCILIATION
            or context.evidence_conflict
            or context.contradiction_count
        )
    ) or bool(
        context.phase == ReasoningPhase.TERMINAL_BLOCK_REVIEW
        and context.verifier_status == "contradicted"
        and context.irreversibility == Irreversibility.IRREVERSIBLE
    )

    if context.exact_candidate_count == 1 and context.close_candidate_count <= 1:
        deescalation.append("single_exact_candidate")
    if context.exact_direct_capability and context.semantic_effect_exact:
        deescalation.append("exact_direct_capability")
    if (
        context.evidence_strength in _STRONG_EVIDENCE
        and not context.evidence_conflict
        and not context.contradiction_count
    ):
        deescalation.append("strong_verified_state")
    if context.remaining_goal_node_count <= 1 and context.dependency_count == 0:
        deescalation.append("simple_remaining_goal")
    if (
        context.previous_reasoning_decision is not None
        and context.previous_reasoning_decision.difficulty_class
        in {DifficultyClass.HIGH, DifficultyClass.CRITICAL_RECONCILIATION}
        and context.ambiguity_level in {AmbiguityLevel.NONE, AmbiguityLevel.LOW}
        and not context.evidence_conflict
    ):
        deescalation.append("ambiguity_resolved")
    if context.progress_signal == ProgressSignal.CLEAR:
        deescalation.append("progress_reduced_complexity")

    if critical:
        difficulty = DifficultyClass.CRITICAL_RECONCILIATION
    elif score >= 4:
        difficulty = DifficultyClass.HIGH
    elif score >= 2:
        difficulty = DifficultyClass.MEDIUM
    elif score >= 1:
        difficulty = DifficultyClass.LOW
    else:
        difficulty = DifficultyClass.TRIVIAL

    if context.incomplete_world_state_count or context.evidence_conflict:
        confidence = DecisionConfidence.LOW
    elif difficulty in {DifficultyClass.TRIVIAL, DifficultyClass.LOW} and deescalation:
        confidence = DecisionConfidence.HIGH
    else:
        confidence = DecisionConfidence.MEDIUM
    return (
        difficulty,
        tuple(dict.fromkeys(escalation)),
        tuple(dict.fromkeys(deescalation)),
        confidence,
    )


def select_level_for_difficulty(
    capability: AuthoritativeReasoningCapability,
    difficulty: DifficultyClass,
) -> str:
    """Map a normalized class to one declared exact-pair value."""

    values = capability.adaptive_values
    if not capability.configurable or not values:
        return "auto"
    if difficulty == DifficultyClass.TRIVIAL and capability.supports_auto:
        return "auto"
    if difficulty == DifficultyClass.LOW:
        index = 0
    elif difficulty == DifficultyClass.MEDIUM:
        index = (len(values) - 1) // 2
    elif difficulty == DifficultyClass.HIGH:
        index = min(
            len(values) - 1,
            int(math.ceil((len(values) - 1) * 0.75)),
        )
    else:
        index = len(values) - 1
    return values[index].osia_level


def determine_reasoning_decision(
    context: ReasoningDecisionContext,
    authoritative_reasoning_capability: AuthoritativeReasoningCapability,
) -> ReasoningDecision:
    """Return a deterministic shadow recommendation with strict precedence."""

    capability = authoritative_reasoning_capability
    difficulty, escalation, deescalation, confidence = _difficulty_assessment(context)
    requested_source = (
        context.explicit_turn_reasoning
        if context.explicit_turn_reasoning is not None
        else context.user_reasoning_mode
    )
    requested = _normalize_requested_level(requested_source)
    explicit_non_auto = requested != "auto"
    supported_snapshot = tuple(context.supported_reasoning_values)

    if capability.provider_id != context.provider_id or capability.model_id != context.model_id:
        return _safe_default_decision(
            context,
            difficulty,
            requested or str(requested_source or ""),
            escalation,
            deescalation,
            "capability_exact_pair_mismatch",
        )
    if supported_snapshot and supported_snapshot != capability.supported_osia_values:
        return _safe_default_decision(
            context,
            difficulty,
            requested or str(requested_source or ""),
            escalation,
            deescalation,
            "capability_snapshot_mismatch",
        )
    if context.provider_default_behavior != capability.provider_default_behavior:
        return _safe_default_decision(
            context,
            difficulty,
            requested or str(requested_source or ""),
            escalation,
            deescalation,
            "provider_default_snapshot_mismatch",
        )
    if not requested:
        return _safe_default_decision(
            context,
            difficulty,
            str(requested_source or ""),
            escalation,
            deescalation,
            "unsupported_explicit_reasoning",
        )

    if explicit_non_auto:
        value = capability.value_for(requested)
        if value is None:
            return _safe_default_decision(
                context,
                difficulty,
                requested,
                escalation,
                deescalation,
                "unsupported_explicit_reasoning",
            )
        return ReasoningDecision(
            provider_id=context.provider_id,
            model_id=context.model_id,
            phase=context.phase,
            difficulty_class=difficulty,
            requested_mode=requested,
            adaptive_allowed=False,
            selected_osia_level=requested,
            selected_native_value=value.native_value,
            provider_parameter_behavior=ProviderParameterBehavior.EMIT_NATIVE_VALUE,
            escalation_reason_codes=tuple(
                dict.fromkeys((*escalation, "explicit_user_override"))
            ),
            deescalation_reason_codes=deescalation,
            unsupported_reason="",
            confidence_class=confidence,
        )

    if not capability.configurable:
        return ReasoningDecision(
            provider_id=context.provider_id,
            model_id=context.model_id,
            phase=context.phase,
            difficulty_class=difficulty,
            requested_mode="auto",
            adaptive_allowed=False,
            selected_osia_level="auto",
            selected_native_value=None,
            provider_parameter_behavior=ProviderParameterBehavior.OMIT_PROVIDER_DEFAULT,
            escalation_reason_codes=tuple(
                dict.fromkeys((*escalation, "reasoning_not_configurable"))
            ),
            deescalation_reason_codes=deescalation,
            unsupported_reason="reasoning_not_configurable",
            confidence_class=DecisionConfidence.UNRESOLVED,
        )

    selected = select_level_for_difficulty(capability, difficulty)
    if selected == "auto":
        native_value = None
        behavior = ProviderParameterBehavior.OMIT_PROVIDER_DEFAULT
    else:
        value = capability.value_for(selected)
        if value is None:
            return _safe_default_decision(
                context,
                difficulty,
                requested,
                escalation,
                deescalation,
                "supported_value_mapping_missing",
            )
        native_value = value.native_value
        behavior = ProviderParameterBehavior.EMIT_NATIVE_VALUE
    return ReasoningDecision(
        provider_id=context.provider_id,
        model_id=context.model_id,
        phase=context.phase,
        difficulty_class=difficulty,
        requested_mode="auto",
        adaptive_allowed=True,
        selected_osia_level=selected,
        selected_native_value=native_value,
        provider_parameter_behavior=behavior,
        escalation_reason_codes=escalation,
        deescalation_reason_codes=deescalation,
        unsupported_reason="",
        confidence_class=confidence,
    )


def _safe_default_decision(
    context: ReasoningDecisionContext,
    difficulty: DifficultyClass,
    requested: str,
    escalation: tuple[str, ...],
    deescalation: tuple[str, ...],
    reason: str,
) -> ReasoningDecision:
    return ReasoningDecision(
        provider_id=context.provider_id,
        model_id=context.model_id,
        phase=context.phase,
        difficulty_class=difficulty,
        requested_mode=requested,
        adaptive_allowed=False,
        selected_osia_level=None,
        selected_native_value=None,
        provider_parameter_behavior=ProviderParameterBehavior.OVERRIDE_BLOCKED,
        escalation_reason_codes=tuple(dict.fromkeys((*escalation, reason))),
        deescalation_reason_codes=deescalation,
        unsupported_reason=reason,
        confidence_class=DecisionConfidence.UNRESOLVED,
    )


def _known(value: object) -> bool:
    return str(value or "").strip().lower() not in _UNKNOWN_VALUES


def _max_risk(values: Iterable[SideEffectRisk]) -> SideEffectRisk:
    return max(tuple(values) or (SideEffectRisk.NONE,), key=_RISK_ORDER.__getitem__)


def _max_irreversibility(values: Iterable[Irreversibility]) -> Irreversibility:
    return max(
        tuple(values) or (Irreversibility.NONE,),
        key=_IRREVERSIBILITY_ORDER.__getitem__,
    )


def _step_risk(side_effect_class: object) -> tuple[SideEffectRisk, Irreversibility]:
    value = str(side_effect_class or "").strip().lower()
    if value in {"none", "read_only", "no_side_effect"}:
        return SideEffectRisk.NONE, Irreversibility.NONE
    if value == "reversible_local":
        return SideEffectRisk.MODERATE, Irreversibility.REVERSIBLE
    if value in {"persistent_local", "external_mutation"}:
        return SideEffectRisk.HIGH, Irreversibility.DIFFICULT
    if value == "irreversible_external":
        return SideEffectRisk.CRITICAL, Irreversibility.IRREVERSIBLE
    if value in {"dynamically_classified", "unknown_high_risk", "unknown"}:
        return SideEffectRisk.UNKNOWN, Irreversibility.UNKNOWN
    return SideEffectRisk.LOW, Irreversibility.REVERSIBLE


def build_reasoning_decision_context(
    *,
    provider_id: str,
    model_id: str,
    user_reasoning_mode: str,
    phase: ReasoningPhase,
    capability: AuthoritativeReasoningCapability,
    world_state: Any,
    verification_result: Any,
    shadow_plan_result: Any,
    explicit_turn_reasoning: str | None = None,
    previous_reasoning_decision: ReasoningDecision | None = None,
    replan_count: int = 0,
    repeated_replan_count: int = 0,
) -> ReasoningDecisionContext:
    """Project Phase-3 objects into policy-only structured difficulty signals."""

    plans = tuple(getattr(shadow_plan_result, "candidate_plans", ()) or ())
    selected = getattr(shadow_plan_result, "selected_plan", None)
    selected_steps = tuple(getattr(selected, "ordered_steps", ()) or ())
    selected_confidence = str(getattr(selected, "confidence_class", ""))
    close_count = sum(
        1
        for plan in plans
        if str(getattr(plan, "confidence_class", "")) == selected_confidence
        and abs(
            len(tuple(getattr(plan, "ordered_steps", ()) or ()))
            - len(selected_steps)
        ) <= 1
    )
    unresolved = set(getattr(shadow_plan_result, "unresolved_state", ()) or ())
    unresolved.update(getattr(selected, "unresolved_constraints", ()) or ())
    remaining_nodes = tuple(getattr(world_state, "remaining_goal_node_ids", ()) or ())
    dependency_count = sum(
        len(tuple(getattr(step, "dependency_ids", ()) or ())) for step in selected_steps
    )
    if dependency_count >= 2 or len(remaining_nodes) >= 3:
        goal_complexity = GoalComplexity.DEPENDENCY_HEAVY
    elif len(selected_steps) >= 2 or len(remaining_nodes) >= 2:
        goal_complexity = GoalComplexity.MULTI_STEP
    else:
        goal_complexity = GoalComplexity.SIMPLE

    contradictions = set(
        getattr(world_state, "contradicted_postconditions", ()) or ()
    )
    contradictions.update(
        getattr(verification_result, "contradicted_postcondition_ids", ()) or ()
    )
    outcome_unknown = tuple(
        getattr(world_state, "outcome_unknown_equivalence_classes", ()) or ()
    )
    if not outcome_unknown:
        unknown_complexity = OutcomeUnknownComplexity.NONE
    elif (
        len(outcome_unknown) > 1
        or contradictions
        or phase == ReasoningPhase.RECONCILIATION
        and len(getattr(verification_result, "required_observation_types", ()) or ()) > 1
    ):
        unknown_complexity = OutcomeUnknownComplexity.COMPLEX
    else:
        unknown_complexity = OutcomeUnknownComplexity.SIMPLE

    if close_count >= 3 or contradictions:
        ambiguity = AmbiguityLevel.HIGH
    elif close_count == 2 or len(unresolved) >= 2:
        ambiguity = AmbiguityLevel.MEDIUM
    elif close_count == 1 and len(plans) > 1 or unresolved:
        ambiguity = AmbiguityLevel.LOW
    else:
        ambiguity = AmbiguityLevel.NONE

    risks: list[SideEffectRisk] = []
    irreversibilities: list[Irreversibility] = []
    for step in selected_steps:
        risk, irreversibility = _step_risk(getattr(step, "side_effect_class", ""))
        risks.append(risk)
        irreversibilities.append(irreversibility)

    observations = tuple(getattr(world_state, "typed_observations", ()) or ())
    applications = {
        str(getattr(item, "value", "")).strip().lower()
        for item in observations
        if "application" in str(getattr(item, "name", "")).lower()
        and _known(getattr(item, "value", ""))
    }
    current_application = getattr(world_state, "current_application", "")
    if _known(current_application):
        applications.add(str(current_application).strip().lower())

    delta = getattr(verification_result, "world_delta", None)
    new_progress = sum(
        len(tuple(getattr(delta, name, ()) or ()))
        for name in (
            "newly_verified_goal_nodes",
            "newly_verified_postconditions",
            "resolved_unknown_effects",
            "newly_invalidated_refs",
        )
    )
    if new_progress:
        progress = ProgressSignal.CLEAR
    elif repeated_replan_count:
        progress = ProgressSignal.STALLED
    elif replan_count:
        progress = ProgressSignal.PARTIAL
    else:
        progress = ProgressSignal.UNKNOWN

    summaries = (
        getattr(world_state, "browser_state_summary", None),
        getattr(world_state, "desktop_state_summary", None),
    )
    incomplete = sum(
        not _known(value)
        for value in (
            current_application,
            getattr(world_state, "current_surface", ""),
            getattr(world_state, "current_account_profile", ""),
            *(getattr(summary, "status", "") for summary in summaries),
        )
    )
    pending_permissions = tuple(
        getattr(world_state, "pending_permission_ids", ()) or ()
    )
    account_unknown = int(
        not _known(getattr(world_state, "current_account_profile", ""))
        and any("account" in str(item).lower() for item in unresolved)
    )
    families = tuple(getattr(step, "family", "") for step in selected_steps)
    direct = bool(
        len(selected_steps) == 1
        and any(
            token in str(families[0]).lower()
            for token in ("direct", "api", "deep_link", "structured")
        )
    )
    exact_candidate_count = int(bool(selected)) if len(plans) == 1 else 0
    surface_account_exact = bool(
        _known(getattr(world_state, "current_surface", ""))
        and _known(getattr(world_state, "current_account_profile", ""))
    )
    evidence_strength = str(
        getattr(verification_result, "verifier_strength", "none") or "none"
    )
    verifier_status = str(
        getattr(verification_result, "status", "unverified") or "unverified"
    )
    return ReasoningDecisionContext(
        schema_version=REASONING_CONTEXT_SCHEMA_VERSION,
        provider_id=str(provider_id or "").strip().lower(),
        model_id=str(model_id or "").strip(),
        user_reasoning_mode=str(user_reasoning_mode or "auto"),
        explicit_turn_reasoning=explicit_turn_reasoning,
        supported_reasoning_values=capability.supported_osia_values,
        provider_default_behavior=capability.provider_default_behavior,
        phase=phase,
        goal_complexity=goal_complexity,
        candidate_count=len(plans),
        close_candidate_count=close_count,
        ambiguity_level=ambiguity,
        contradiction_count=len(contradictions),
        unresolved_constraint_count=len(unresolved),
        remaining_goal_node_count=len(remaining_nodes),
        replan_count=replan_count,
        repeated_replan_count=repeated_replan_count,
        stale_ref_count=len(tuple(getattr(world_state, "stale_refs", ()) or ())),
        outcome_unknown_count=len(outcome_unknown),
        outcome_unknown_complexity=unknown_complexity,
        verifier_status=verifier_status,
        required_observation_count=len(
            tuple(getattr(verification_result, "required_observation_types", ()) or ())
        ),
        evidence_strength=evidence_strength,
        evidence_conflict=bool(contradictions),
        side_effect_risk=_max_risk(risks),
        irreversibility=_max_irreversibility(irreversibilities),
        cross_application_count=len(applications),
        previous_reasoning_decision=previous_reasoning_decision,
        progress_signal=progress,
        exact_candidate_count=exact_candidate_count,
        exact_direct_capability=direct,
        semantic_effect_exact=bool(selected and not unresolved),
        surface_account_exact=surface_account_exact,
        unresolved_permission_count=len(pending_permissions),
        unresolved_account_binding_count=account_unknown,
        dependency_count=dependency_count,
        incomplete_world_state_count=incomplete,
    )


def public_structure_field_names() -> dict[str, tuple[str, ...]]:
    """Expose bounded schema diagnostics for tests; no private reasoning exists."""

    return {
        "context": tuple(field.name for field in fields(ReasoningDecisionContext)),
        "decision": tuple(field.name for field in fields(ReasoningDecision)),
    }


__all__ = [
    "AmbiguityLevel",
    "AuthoritativeReasoningCapability",
    "DecisionConfidence",
    "DifficultyClass",
    "GoalComplexity",
    "Irreversibility",
    "OutcomeUnknownComplexity",
    "ProgressSignal",
    "ProviderParameterBehavior",
    "REASONING_CONTEXT_SCHEMA_VERSION",
    "ReasoningCapabilityValue",
    "ReasoningDecision",
    "ReasoningDecisionContext",
    "ReasoningPhase",
    "SideEffectRisk",
    "adapt_authoritative_reasoning_capability",
    "build_reasoning_decision_context",
    "capability_from_current_registry",
    "determine_reasoning_decision",
    "public_structure_field_names",
    "select_level_for_difficulty",
]
