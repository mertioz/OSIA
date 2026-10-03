"""WOR-260 Phase 4B rare Critic policy and contracts (shadow only).

The structures in this module describe when a future evaluation-only model
call could be useful and the compact facts such a call may exchange.  They do
not call a provider, expose tools, dispatch work, grant permission, mutate a
goal, resolve outcome-unknown state, or decide whole-turn terminality.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Mapping

CRITIC_CONTEXT_SCHEMA_VERSION = "1.0"
CRITIC_REQUEST_SCHEMA_VERSION = "1.0"
CRITIC_EVALUATION_SCHEMA_VERSION = "1.0"


class AmbiguityLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


class DifficultyClass(str, Enum):
    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL_RECONCILIATION = "critical_reconciliation"


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


class CandidateScoreMarginClass(str, Enum):
    DETERMINISTIC = "deterministic"
    LARGE = "large"
    MODERATE = "moderate"
    NARROW = "narrow"
    TIED = "tied"
    UNKNOWN = "unknown"


class CriticTriggerClass(str, Enum):
    NONE = "none"
    PLAN_AMBIGUITY = "plan_ambiguity"
    EVIDENCE_CONTRADICTION = "evidence_contradiction"
    DIFFICULT_RECONCILIATION = "difficult_reconciliation"
    IRREVERSIBLE_ACTION_REVIEW = "irreversible_action_review"
    REPEATED_REPLANNING = "repeated_replanning"
    PRE_BLOCKED_REVIEW = "pre_blocked_review"


class CriticBenefitClass(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MinimumRequiredEvaluation(str, Enum):
    NONE = "none"
    COMPARE_CANDIDATES = "compare_candidates"
    CHECK_EVIDENCE_CONSISTENCY = "check_evidence_consistency"
    SELECT_OBSERVATION = "select_observation"
    REVIEW_CONSEQUENCES = "review_consequences"
    REVIEW_REMAINING_STRATEGIES = "review_remaining_strategies"


class CriticEvaluationKind(str, Enum):
    CANDIDATE_COMPARISON = "candidate_comparison"
    EVIDENCE_REVIEW = "evidence_review"
    RECONCILIATION_REVIEW = "reconciliation_review"
    CONSEQUENCE_REVIEW = "consequence_review"
    PRE_BLOCK_REVIEW = "pre_block_review"


class EvaluationCriterion(str, Enum):
    PRESERVE_GOAL_SEMANTICS = "preserve_goal_semantics"
    CONSISTENT_WITH_EVIDENCE = "consistent_with_evidence"
    IDENTIFY_REQUIRED_OBSERVATION = "identify_required_observation"
    ASSESS_VERIFICATION_SUFFICIENCY = "assess_verification_sufficiency"
    REVIEW_SUPPLIED_CANDIDATES_ONLY = "review_supplied_candidates_only"


class CriticVerdict(str, Enum):
    PREFER_CANDIDATE = "prefer_candidate"
    EQUIVALENT_CANDIDATES = "equivalent_candidates"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REQUIRE_OBSERVATION = "require_observation"
    REJECT_ALL_SUPPLIED_CANDIDATES = "reject_all_supplied_candidates"
    NO_MATERIAL_BENEFIT = "no_material_benefit"


class PermissionState(str, Enum):
    ALLOWED = "allowed"
    REQUIRED = "required"
    DISALLOWED = "disallowed"
    UNKNOWN = "unknown"


class AccountBindingState(str, Enum):
    EXACT = "exact"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class ModalityState(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class RuntimeCapabilityState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CriticTriggerContext:
    schema_version: str
    goal_contract_fingerprint: str
    goal_node_id: str
    planner_candidate_count: int
    close_candidate_count: int
    candidate_score_margin_class: CandidateScoreMarginClass
    candidate_semantic_difference_count: int
    ambiguity_level: AmbiguityLevel
    unresolved_constraint_count: int
    contradictory_evidence_count: int
    verifier_status: str
    verifier_strength: str
    evidence_conflict: bool
    replan_count: int
    repeated_replan_count: int
    stale_ref_count: int
    outcome_unknown_count: int
    outcome_unknown_complexity: OutcomeUnknownComplexity
    side_effect_class: SideEffectRisk
    irreversibility_class: Irreversibility
    permission_state: PermissionState
    account_binding_state: AccountBindingState
    cross_application_count: int
    dependency_complexity: int
    remaining_goal_node_count: int
    reasoning_difficulty_class: DifficultyClass
    selected_reasoning_effort: str
    legacy_adaptive_disagreement: bool
    prior_critic_revision: int
    progress_signal: ProgressSignal
    exact_direct_structured_capability: bool = False
    exact_postcondition_evidence: bool = False
    deterministic_constraint_resolution: bool = False
    legal_observation_route_count: int = 0
    pre_blocked_review: bool = False
    safe_alternatives_exhausted: bool = False
    hard_rejection_codes: tuple[str, ...] = ()
    modality_state: ModalityState = ModalityState.SUPPORTED
    runtime_capability_state: RuntimeCapabilityState = (
        RuntimeCapabilityState.AVAILABLE
    )
    outcome_unknown_mutation_replay_blocked: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != CRITIC_CONTEXT_SCHEMA_VERSION:
            raise ValueError("unsupported_critic_context_schema")
        count_fields = (
            "planner_candidate_count",
            "close_candidate_count",
            "candidate_semantic_difference_count",
            "unresolved_constraint_count",
            "contradictory_evidence_count",
            "replan_count",
            "repeated_replan_count",
            "stale_ref_count",
            "outcome_unknown_count",
            "cross_application_count",
            "dependency_complexity",
            "remaining_goal_node_count",
            "prior_critic_revision",
            "legal_observation_route_count",
        )
        if any(int(getattr(self, name)) < 0 for name in count_fields):
            raise ValueError("critic_context_counts_must_be_non_negative")
        if self.close_candidate_count > self.planner_candidate_count:
            raise ValueError("close_candidates_exceed_planner_candidates")


@dataclass(frozen=True)
class CriticTriggerDecision:
    should_invoke_critic: bool
    trigger_class: CriticTriggerClass
    benefit_class: CriticBenefitClass
    reason_codes: tuple[str, ...]
    suppression_codes: tuple[str, ...]
    minimum_required_evaluation: MinimumRequiredEvaluation
    request_scope: tuple[str, ...]
    critic_revision: int
    confidence_class: DecisionConfidence


_BASE_REQUEST_SCOPE = (
    "goal_contract_summary",
    "active_goal_node",
    "candidate_summaries",
    "hard_constraint_summary",
    "typed_evidence_summaries",
    "verification_summary",
    "identity_and_permission_summary",
    "outcome_unknown_summary",
    "evaluation_criteria",
)
_UNRESOLVED_VERIFIER_STATES = frozenset(
    {"unverified", "contradicted", "requires_observation", "outcome_unknown"}
)
_STRONG_VERIFIER_STRENGTHS = frozenset(
    {"observable_state", "exact_postcondition"}
)


def _suppression_codes(context: CriticTriggerContext) -> tuple[str, ...]:
    codes: list[str] = []
    if context.hard_rejection_codes:
        codes.append("hard_rejection_already_decided")
    if context.permission_state != PermissionState.ALLOWED:
        codes.append(f"permission_{context.permission_state.value}")
    if context.account_binding_state != AccountBindingState.EXACT:
        codes.append(f"account_binding_{context.account_binding_state.value}")
    if context.modality_state != ModalityState.SUPPORTED:
        codes.append(f"modality_{context.modality_state.value}")
    if context.runtime_capability_state != RuntimeCapabilityState.AVAILABLE:
        codes.append(
            f"runtime_capability_{context.runtime_capability_state.value}"
        )
    return tuple(dict.fromkeys(codes))


def _no_critic(
    context: CriticTriggerContext,
    *suppression_codes: str,
    confidence: DecisionConfidence = DecisionConfidence.HIGH,
) -> CriticTriggerDecision:
    return CriticTriggerDecision(
        should_invoke_critic=False,
        trigger_class=CriticTriggerClass.NONE,
        benefit_class=CriticBenefitClass.NONE,
        reason_codes=(),
        suppression_codes=tuple(dict.fromkeys(suppression_codes)),
        minimum_required_evaluation=MinimumRequiredEvaluation.NONE,
        request_scope=(),
        critic_revision=context.prior_critic_revision + 1,
        confidence_class=confidence,
    )


def _invoke(
    context: CriticTriggerContext,
    trigger_class: CriticTriggerClass,
    benefit_class: CriticBenefitClass,
    minimum_evaluation: MinimumRequiredEvaluation,
    *reason_codes: str,
) -> CriticTriggerDecision:
    confidence = (
        DecisionConfidence.MEDIUM
        if context.evidence_conflict
        or context.candidate_score_margin_class
        in {CandidateScoreMarginClass.UNKNOWN, CandidateScoreMarginClass.TIED}
        else DecisionConfidence.HIGH
    )
    return CriticTriggerDecision(
        should_invoke_critic=True,
        trigger_class=trigger_class,
        benefit_class=benefit_class,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        suppression_codes=(),
        minimum_required_evaluation=minimum_evaluation,
        request_scope=_BASE_REQUEST_SCOPE,
        critic_revision=context.prior_critic_revision + 1,
        confidence_class=confidence,
    )


def determine_critic_trigger(
    context: CriticTriggerContext,
) -> CriticTriggerDecision:
    """Return a deterministic rare-trigger recommendation.

    Hard rejections are terminal for this advisory policy.  The Critic is
    considered only among already-admissible plans or observations.
    """

    hard_suppressions = _suppression_codes(context)
    if hard_suppressions:
        return _no_critic(context, *hard_suppressions)

    verifier_resolved = bool(
        context.verifier_status == "verified"
        and context.verifier_strength in _STRONG_VERIFIER_STRENGTHS
        and context.exact_postcondition_evidence
        and not context.evidence_conflict
    )
    if verifier_resolved:
        return _no_critic(context, "deterministic_verifier_already_resolved")
    if context.deterministic_constraint_resolution:
        return _no_critic(context, "deterministic_hard_constraints_decide")

    difficult_reconciliation = bool(
        context.outcome_unknown_count
        and context.outcome_unknown_complexity
        == OutcomeUnknownComplexity.COMPLEX
        and context.legal_observation_route_count >= 2
        and context.verifier_status in _UNRESOLVED_VERIFIER_STATES
    )
    if difficult_reconciliation:
        return _invoke(
            context,
            CriticTriggerClass.DIFFICULT_RECONCILIATION,
            CriticBenefitClass.HIGH,
            MinimumRequiredEvaluation.SELECT_OBSERVATION,
            "complex_outcome_unknown",
            "multiple_legal_read_only_observations",
            "deterministic_reconciliation_not_resolved",
        )

    evidence_contradiction = bool(
        (context.evidence_conflict or context.contradictory_evidence_count)
        and context.verifier_status in _UNRESOLVED_VERIFIER_STATES
        and (
            context.legal_observation_route_count >= 2
            or context.planner_candidate_count >= 2
        )
    )
    if evidence_contradiction:
        benefit = (
            CriticBenefitClass.HIGH
            if context.contradictory_evidence_count >= 2
            or context.evidence_conflict
            else CriticBenefitClass.MEDIUM
        )
        return _invoke(
            context,
            CriticTriggerClass.EVIDENCE_CONTRADICTION,
            benefit,
            MinimumRequiredEvaluation.CHECK_EVIDENCE_CONSISTENCY,
            "current_evidence_conflicts",
            "verifier_cannot_resolve_deterministically",
        )

    irreversible_choice = bool(
        context.planner_candidate_count >= 2
        and context.close_candidate_count >= 2
        and context.candidate_semantic_difference_count >= 1
        and context.candidate_score_margin_class
        in {CandidateScoreMarginClass.NARROW, CandidateScoreMarginClass.TIED}
        and context.side_effect_class
        in {SideEffectRisk.HIGH, SideEffectRisk.CRITICAL, SideEffectRisk.UNKNOWN}
        and context.irreversibility_class
        in {
            Irreversibility.DIFFICULT,
            Irreversibility.IRREVERSIBLE,
            Irreversibility.UNKNOWN,
        }
    )
    if irreversible_choice:
        return _invoke(
            context,
            CriticTriggerClass.IRREVERSIBLE_ACTION_REVIEW,
            CriticBenefitClass.HIGH,
            MinimumRequiredEvaluation.REVIEW_CONSEQUENCES,
            "multiple_legal_materially_different_plans",
            "hard_to_reverse_consequences",
        )

    repeated_replanning = bool(
        context.repeated_replan_count >= 2
        and context.planner_candidate_count >= 2
        and context.progress_signal
        in {ProgressSignal.PARTIAL, ProgressSignal.STALLED, ProgressSignal.UNKNOWN}
    )
    if repeated_replanning:
        return _invoke(
            context,
            CriticTriggerClass.REPEATED_REPLANNING,
            CriticBenefitClass.MEDIUM,
            MinimumRequiredEvaluation.REVIEW_REMAINING_STRATEGIES,
            "repeated_failed_before_effect_replans",
            "multiple_legal_strategies_remain",
        )

    pre_blocked = bool(
        context.pre_blocked_review
        and context.safe_alternatives_exhausted
        and (
            context.planner_candidate_count >= 1
            or context.ambiguity_level
            in {AmbiguityLevel.MEDIUM, AmbiguityLevel.HIGH}
            or context.evidence_conflict
        )
    )
    if pre_blocked:
        return _invoke(
            context,
            CriticTriggerClass.PRE_BLOCKED_REVIEW,
            CriticBenefitClass.MEDIUM,
            MinimumRequiredEvaluation.REVIEW_REMAINING_STRATEGIES,
            "canonical_safe_alternatives_appear_exhausted",
            "bounded_semantic_review_before_block",
        )

    close_plan_ambiguity = bool(
        context.planner_candidate_count >= 2
        and context.close_candidate_count >= 2
        and context.candidate_semantic_difference_count >= 1
        and context.candidate_score_margin_class
        in {CandidateScoreMarginClass.NARROW, CandidateScoreMarginClass.TIED}
        and context.ambiguity_level
        in {AmbiguityLevel.MEDIUM, AmbiguityLevel.HIGH}
    )
    if close_plan_ambiguity:
        benefit = (
            CriticBenefitClass.HIGH
            if context.candidate_score_margin_class
            == CandidateScoreMarginClass.TIED
            and context.candidate_semantic_difference_count >= 2
            else CriticBenefitClass.MEDIUM
        )
        return _invoke(
            context,
            CriticTriggerClass.PLAN_AMBIGUITY,
            benefit,
            MinimumRequiredEvaluation.COMPARE_CANDIDATES,
            "multiple_close_admissible_plans",
            "material_semantic_plan_differences",
            "no_deterministic_preference",
        )

    simple_suppressions: list[str] = []
    if context.planner_candidate_count <= 1:
        simple_suppressions.append("one_admissible_candidate")
    if context.exact_direct_structured_capability:
        simple_suppressions.append("exact_direct_structured_capability")
    if context.candidate_score_margin_class in {
        CandidateScoreMarginClass.DETERMINISTIC,
        CandidateScoreMarginClass.LARGE,
    }:
        simple_suppressions.append("candidate_margin_decisive")
    if context.progress_signal == ProgressSignal.CLEAR:
        simple_suppressions.append("clear_progress")
    if context.remaining_goal_node_count <= 1:
        simple_suppressions.append("simple_remaining_goal")
    if context.outcome_unknown_count == 0:
        simple_suppressions.append("no_outcome_unknown")
    if context.repeated_replan_count == 0:
        simple_suppressions.append("no_repeated_replanning")
    if not context.evidence_conflict and not context.contradictory_evidence_count:
        simple_suppressions.append("no_evidence_conflict")
    return _no_critic(context, *simple_suppressions)


@dataclass(frozen=True)
class GoalContractSummary:
    fingerprint: str
    root_objective_code: str
    active_goal_node_id: str


@dataclass(frozen=True)
class ActiveGoalNodeSummary:
    node_id: str
    objective_code: str
    target_kind: str
    application_id: str
    surface_id: str
    account_binding_id: str
    side_effect_class: str
    postcondition_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidatePlanSummary:
    candidate_id: str
    capability_families: tuple[str, ...]
    semantic_effect_codes: tuple[str, ...]
    step_count: int
    ranking_bucket: str
    verifier_strength: str
    side_effect_class: str
    irreversibility_class: str
    supported_postcondition_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.step_count < 0:
            raise ValueError("candidate_step_count_must_be_non_negative")


@dataclass(frozen=True)
class CandidateRejectionSummary:
    candidate_id: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceSummary:
    evidence_id: str
    observation_type: str
    observed_state_code: str
    verifier_strength: str
    supported_postcondition_ids: tuple[str, ...]
    contradicted_postcondition_ids: tuple[str, ...]
    evidence_revision: int


@dataclass(frozen=True)
class VerificationSummary:
    status: str
    verifier_strength: str
    satisfied_postcondition_ids: tuple[str, ...]
    unsatisfied_postcondition_ids: tuple[str, ...]
    contradicted_postcondition_ids: tuple[str, ...]
    required_observation_types: tuple[str, ...]


@dataclass(frozen=True)
class IdentityBindingSummary:
    account_binding_state: AccountBindingState
    account_binding_id: str
    surface_binding_id: str
    session_binding_id: str


@dataclass(frozen=True)
class OutcomeUnknownSummary:
    count: int
    complexity: OutcomeUnknownComplexity
    opaque_equivalence_class_ids: tuple[str, ...]
    permitted_observation_types: tuple[str, ...]
    mutation_replay_blocked: bool


@dataclass(frozen=True)
class CriticRequest:
    request_id: str
    schema_version: str
    evaluation_kind: CriticEvaluationKind
    turn_id: str
    evaluation_revision: int
    goal_contract_summary: GoalContractSummary
    active_goal_node: ActiveGoalNodeSummary
    remaining_postcondition_ids: tuple[str, ...]
    candidate_plans: tuple[CandidatePlanSummary, ...]
    hard_constraint_codes: tuple[str, ...]
    already_rejected_candidates: tuple[CandidateRejectionSummary, ...]
    typed_evidence_summaries: tuple[EvidenceSummary, ...]
    verification_summary: VerificationSummary
    world_state_revision: int
    contradiction_codes: tuple[str, ...]
    permission_state: PermissionState
    identity_binding_summary: IdentityBindingSummary
    outcome_unknown_summary: OutcomeUnknownSummary
    evaluation_criteria: tuple[EvaluationCriterion, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CRITIC_REQUEST_SCHEMA_VERSION:
            raise ValueError("unsupported_critic_request_schema")
        if self.evaluation_revision < 0 or self.world_state_revision < 0:
            raise ValueError("critic_request_revisions_must_be_non_negative")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_plans)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("critic_request_candidate_ids_must_be_unique")
        rejected_ids = {
            item.candidate_id for item in self.already_rejected_candidates
        }
        if rejected_ids.intersection(candidate_ids):
            raise ValueError("rejected_candidates_cannot_be_supplied_candidates")
        if not self.evaluation_criteria:
            raise ValueError("critic_request_requires_evaluation_criteria")


@dataclass(frozen=True)
class CriticEvaluation:
    request_id: str
    schema_version: str
    turn_id: str
    goal_contract_fingerprint: str
    goal_node_id: str
    world_state_revision: int
    evaluation_revision: int
    verdict: CriticVerdict
    preferred_candidate_id: str | None
    rejected_candidate_ids: tuple[str, ...]
    required_observation_types: tuple[str, ...]
    violated_constraint_codes: tuple[str, ...]
    supported_postcondition_ids: tuple[str, ...]
    confidence_class: DecisionConfidence
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AuthoritativeCriticConstraints:
    request_id: str
    turn_id: str
    goal_contract_fingerprint: str
    goal_node_id: str
    world_state_revision: int
    evaluation_revision: int
    admissible_candidate_ids: tuple[str, ...]
    hard_rejected_candidate_ids: tuple[str, ...]
    known_capability_families: tuple[str, ...]
    known_postcondition_ids: tuple[str, ...]
    compatible_observation_types: tuple[str, ...]
    known_constraint_codes: tuple[str, ...]
    permission_state: PermissionState
    account_binding_state: AccountBindingState
    outcome_unknown_count: int


@dataclass(frozen=True)
class CriticEvaluationValidation:
    accepted: bool
    evaluation: CriticEvaluation | None
    rejection_codes: tuple[str, ...]
    fallback_to_no_critic: bool


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field_name}_must_be_array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name}_must_contain_strings")
    return tuple(value)


def _coerce_evaluation(
    evaluation: CriticEvaluation | Mapping[str, object],
) -> CriticEvaluation:
    if isinstance(evaluation, CriticEvaluation):
        return evaluation
    if not isinstance(evaluation, Mapping):
        raise ValueError("evaluation_must_be_object")
    allowed = {item.name for item in fields(CriticEvaluation)}
    supplied = {str(key) for key in evaluation}
    if supplied != allowed:
        unknown = supplied - allowed
        missing = allowed - supplied
        if unknown:
            raise ValueError("unknown_response_fields")
        if missing:
            raise ValueError("missing_response_fields")
    revision = evaluation["evaluation_revision"]
    world_revision = evaluation["world_state_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValueError("evaluation_revision_must_be_integer")
    if isinstance(world_revision, bool) or not isinstance(world_revision, int):
        raise ValueError("world_state_revision_must_be_integer")
    preferred = evaluation["preferred_candidate_id"]
    if preferred is not None and not isinstance(preferred, str):
        raise ValueError("preferred_candidate_id_must_be_string_or_null")
    return CriticEvaluation(
        request_id=str(evaluation["request_id"]),
        schema_version=str(evaluation["schema_version"]),
        turn_id=str(evaluation["turn_id"]),
        goal_contract_fingerprint=str(
            evaluation["goal_contract_fingerprint"]
        ),
        goal_node_id=str(evaluation["goal_node_id"]),
        world_state_revision=world_revision,
        evaluation_revision=revision,
        verdict=CriticVerdict(str(evaluation["verdict"])),
        preferred_candidate_id=preferred,
        rejected_candidate_ids=_string_tuple(
            evaluation["rejected_candidate_ids"], "rejected_candidate_ids"
        ),
        required_observation_types=_string_tuple(
            evaluation["required_observation_types"],
            "required_observation_types",
        ),
        violated_constraint_codes=_string_tuple(
            evaluation["violated_constraint_codes"],
            "violated_constraint_codes",
        ),
        supported_postcondition_ids=_string_tuple(
            evaluation["supported_postcondition_ids"],
            "supported_postcondition_ids",
        ),
        confidence_class=DecisionConfidence(
            str(evaluation["confidence_class"])
        ),
        reason_codes=_string_tuple(
            evaluation["reason_codes"], "reason_codes"
        ),
    )


def _reject(*codes: str) -> CriticEvaluationValidation:
    return CriticEvaluationValidation(
        accepted=False,
        evaluation=None,
        rejection_codes=tuple(dict.fromkeys(codes)),
        fallback_to_no_critic=True,
    )


def validate_critic_evaluation(
    request: CriticRequest,
    evaluation: CriticEvaluation | Mapping[str, object],
    authoritative_constraints: AuthoritativeCriticConstraints,
) -> CriticEvaluationValidation:
    """Validate untrusted structured output and fail open to no-Critic.

    The validator only determines whether the response is a well-formed
    advisory statement about the supplied request.  Acceptance never dispatches
    or applies the statement.
    """

    try:
        parsed = _coerce_evaluation(evaluation)
    except (KeyError, TypeError, ValueError):
        return _reject("malformed_critic_evaluation")

    authority = authoritative_constraints
    request_candidate_ids = {
        item.candidate_id for item in request.candidate_plans
    }
    rejected_by_request = {
        item.candidate_id for item in request.already_rejected_candidates
    }
    context_matches = bool(
        authority.request_id == request.request_id
        and authority.turn_id == request.turn_id
        and authority.goal_contract_fingerprint
        == request.goal_contract_summary.fingerprint
        and authority.goal_node_id == request.active_goal_node.node_id
        and authority.world_state_revision == request.world_state_revision
        and authority.evaluation_revision == request.evaluation_revision
    )
    if not context_matches:
        return _reject("authoritative_context_mismatch")
    if parsed.schema_version != CRITIC_EVALUATION_SCHEMA_VERSION:
        return _reject("unsupported_critic_evaluation_schema")
    if parsed.request_id != request.request_id or parsed.turn_id != request.turn_id:
        return _reject("cross_turn_or_request_response")
    if (
        parsed.goal_contract_fingerprint
        != request.goal_contract_summary.fingerprint
        or parsed.goal_node_id != request.active_goal_node.node_id
    ):
        return _reject("goal_contract_or_node_mismatch")
    if parsed.evaluation_revision != request.evaluation_revision:
        return _reject("stale_evaluation_revision")
    if parsed.world_state_revision != request.world_state_revision:
        return _reject("stale_world_state_revision")

    mentioned_candidates = set(parsed.rejected_candidate_ids)
    if parsed.preferred_candidate_id:
        mentioned_candidates.add(parsed.preferred_candidate_id)
    hard_rejected = set(authority.hard_rejected_candidate_ids)
    if mentioned_candidates.intersection(hard_rejected | rejected_by_request):
        return _reject("attempt_to_revive_hard_rejected_candidate")
    if not mentioned_candidates.issubset(request_candidate_ids):
        return _reject("unknown_candidate_id")
    if not mentioned_candidates.issubset(
        set(authority.admissible_candidate_ids)
    ):
        return _reject("candidate_not_authoritatively_admissible")

    request_postconditions = set(request.remaining_postcondition_ids)
    if not set(parsed.supported_postcondition_ids).issubset(
        request_postconditions.intersection(authority.known_postcondition_ids)
    ):
        return _reject("invented_postcondition")
    permitted_observations = set(
        request.verification_summary.required_observation_types
    ) | set(request.outcome_unknown_summary.permitted_observation_types)
    if not set(parsed.required_observation_types).issubset(
        permitted_observations.intersection(
            authority.compatible_observation_types
        )
    ):
        return _reject("incompatible_or_invented_observation")
    if not set(parsed.violated_constraint_codes).issubset(
        set(request.hard_constraint_codes).intersection(
            authority.known_constraint_codes
        )
    ):
        return _reject("invented_constraint")

    if (
        authority.permission_state != request.permission_state
        or authority.account_binding_state
        != request.identity_binding_summary.account_binding_state
        or authority.outcome_unknown_count != request.outcome_unknown_summary.count
    ):
        return _reject("authoritative_state_mismatch")

    if parsed.verdict == CriticVerdict.PREFER_CANDIDATE:
        if not parsed.preferred_candidate_id:
            return _reject("preferred_candidate_required")
    elif parsed.preferred_candidate_id is not None:
        return _reject("preferred_candidate_incompatible_with_verdict")
    if (
        parsed.verdict == CriticVerdict.REQUIRE_OBSERVATION
        and not parsed.required_observation_types
    ):
        return _reject("required_observation_missing")
    if (
        parsed.verdict == CriticVerdict.NO_MATERIAL_BENEFIT
        and (
            parsed.rejected_candidate_ids
            or parsed.required_observation_types
            or parsed.violated_constraint_codes
            or parsed.supported_postcondition_ids
        )
    ):
        return _reject("no_material_benefit_must_be_empty")

    return CriticEvaluationValidation(
        accepted=True,
        evaluation=parsed,
        rejection_codes=(),
        fallback_to_no_critic=False,
    )


def public_structure_field_names() -> dict[str, tuple[str, ...]]:
    """Expose the exact structured surface for privacy/adversarial tests."""

    structures = (
        CriticTriggerContext,
        CriticTriggerDecision,
        GoalContractSummary,
        ActiveGoalNodeSummary,
        CandidatePlanSummary,
        CandidateRejectionSummary,
        EvidenceSummary,
        VerificationSummary,
        IdentityBindingSummary,
        OutcomeUnknownSummary,
        CriticRequest,
        CriticEvaluation,
        AuthoritativeCriticConstraints,
        CriticEvaluationValidation,
    )
    return {
        structure.__name__: tuple(item.name for item in fields(structure))
        for structure in structures
    }


__all__ = [
    "CRITIC_CONTEXT_SCHEMA_VERSION",
    "CRITIC_EVALUATION_SCHEMA_VERSION",
    "CRITIC_REQUEST_SCHEMA_VERSION",
    "AccountBindingState",
    "ActiveGoalNodeSummary",
    "AmbiguityLevel",
    "AuthoritativeCriticConstraints",
    "CandidatePlanSummary",
    "CandidateRejectionSummary",
    "CandidateScoreMarginClass",
    "CriticBenefitClass",
    "CriticEvaluation",
    "CriticEvaluationKind",
    "CriticEvaluationValidation",
    "CriticRequest",
    "CriticTriggerClass",
    "CriticTriggerContext",
    "CriticTriggerDecision",
    "CriticVerdict",
    "DecisionConfidence",
    "DifficultyClass",
    "EvaluationCriterion",
    "EvidenceSummary",
    "GoalContractSummary",
    "IdentityBindingSummary",
    "Irreversibility",
    "MinimumRequiredEvaluation",
    "ModalityState",
    "OutcomeUnknownSummary",
    "OutcomeUnknownComplexity",
    "PermissionState",
    "RuntimeCapabilityState",
    "ProgressSignal",
    "SideEffectRisk",
    "VerificationSummary",
    "determine_critic_trigger",
    "public_structure_field_names",
    "validate_critic_evaluation",
]
