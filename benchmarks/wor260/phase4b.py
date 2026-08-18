"""Deterministic WOR-260 Phase 4B rare-Critic shadow benchmark.

No evaluator model is called.  Scenarios carry an independent usefulness
oracle and deterministic response fixtures so trigger quality, contract
validation, and theoretical extra invocation counts can be measured locally.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from statistics import fmean
from time import perf_counter_ns
from typing import Mapping

from benchmarks.wor260.phase4a import Phase4ABenchmarkReport, run_phase4a_benchmark
from osia.adaptive_critic import (
    CRITIC_CONTEXT_SCHEMA_VERSION,
    CRITIC_EVALUATION_SCHEMA_VERSION,
    CRITIC_REQUEST_SCHEMA_VERSION,
    AccountBindingState,
    ActiveGoalNodeSummary,
    AmbiguityLevel,
    AuthoritativeCriticConstraints,
    CandidatePlanSummary,
    CandidateRejectionSummary,
    CandidateScoreMarginClass,
    CriticEvaluation,
    CriticEvaluationKind,
    CriticEvaluationValidation,
    CriticRequest,
    CriticTriggerClass,
    CriticTriggerContext,
    CriticTriggerDecision,
    CriticVerdict,
    DecisionConfidence,
    DifficultyClass,
    EvaluationCriterion,
    EvidenceSummary,
    GoalContractSummary,
    IdentityBindingSummary,
    Irreversibility,
    ModalityState,
    OutcomeUnknownSummary,
    OutcomeUnknownComplexity,
    PermissionState,
    ProgressSignal,
    RuntimeCapabilityState,
    SideEffectRisk,
    VerificationSummary,
    determine_critic_trigger,
    validate_critic_evaluation,
)


class CriticUsefulness(str, Enum):
    REQUIRED = "critic_required"
    OPTIONAL = "critic_optional"
    UNNECESSARY = "critic_unnecessary"


class EvaluatorFixture(str, Enum):
    HELPFUL = "helpful_critic"
    NO_OP = "no_op_critic"
    WRONG_CANDIDATE = "wrong_candidate_critic"
    STALE = "stale_critic"
    MALICIOUS = "malicious_invalid_critic"
    OVERCONFIDENT = "overconfident_critic"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence_critic"
    MALFORMED = "malformed_critic"
    HARD_OVERRIDE = "hard_override_critic"


@dataclass(frozen=True)
class Phase4BCriticScenario:
    scenario_id: str
    group: str
    context: CriticTriggerContext
    usefulness: CriticUsefulness
    acceptable_trigger_classes: tuple[CriticTriggerClass, ...]
    fixture: EvaluatorFixture
    expected_candidate_id: str | None = None
    expected_observation_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class Phase4BCriticOutcome:
    scenario_id: str
    group: str
    usefulness: CriticUsefulness
    trigger_decision: CriticTriggerDecision
    trigger_correct: bool
    fixture: EvaluatorFixture
    validation: CriticEvaluationValidation
    helpful_evaluation: bool
    candidate_selection_improved: bool
    candidate_selection_regressed: bool
    observation_request_improved: bool
    unnecessary_observation: bool


@dataclass(frozen=True)
class TimingStats:
    operation_count: int
    mean_microseconds: float
    p50_microseconds: float
    p95_microseconds: float
    max_microseconds: float


@dataclass(frozen=True)
class ArchitectureComparison:
    architecture: str
    useful_evaluation_coverage_count: int
    unnecessary_invocation_count: int
    harmful_evaluation_acceptance_count: int
    final_advisory_decision_correct_count: int
    theoretical_extra_provider_call_count: int


@dataclass(frozen=True)
class Phase4BMetrics:
    phase4b_scenario_count: int
    total_wor260_scenario_count: int
    critic_required_count: int
    critic_optional_count: int
    critic_unnecessary_count: int
    trigger_true_positive: int
    trigger_true_negative: int
    trigger_false_positive: int
    trigger_false_negative: int
    trigger_precision: float
    trigger_recall: float
    trigger_specificity: float
    invocation_rate: float
    unnecessary_invocation_rate: float
    missed_useful_critic_rate: float
    helpful_evaluation_acceptance_rate: float
    no_benefit_suppression_rate: float
    harmful_evaluation_rejection_rate: float
    stale_evaluation_rejection_rate: float
    malformed_evaluation_rejection_rate: float
    hard_constraint_override_rejection_rate: float
    candidate_selection_improvement_count: int
    candidate_selection_regression_count: int
    observation_request_improvement_count: int
    unnecessary_observation_count: int


@dataclass(frozen=True)
class Phase4BBenchmarkReport:
    phase4a_report: Phase4ABenchmarkReport
    outcomes: tuple[Phase4BCriticOutcome, ...]
    metrics: Phase4BMetrics
    architecture_comparison: tuple[ArchitectureComparison, ...]
    trigger_timing: TimingStats
    validation_timing: TimingStats


def _base_context() -> CriticTriggerContext:
    return CriticTriggerContext(
        schema_version=CRITIC_CONTEXT_SCHEMA_VERSION,
        goal_contract_fingerprint="goal-fingerprint-opaque",
        goal_node_id="goal-node-active",
        planner_candidate_count=1,
        close_candidate_count=1,
        candidate_score_margin_class=CandidateScoreMarginClass.DETERMINISTIC,
        candidate_semantic_difference_count=0,
        ambiguity_level=AmbiguityLevel.NONE,
        unresolved_constraint_count=0,
        contradictory_evidence_count=0,
        verifier_status="unverified",
        verifier_strength="structured_result",
        evidence_conflict=False,
        replan_count=0,
        repeated_replan_count=0,
        stale_ref_count=0,
        outcome_unknown_count=0,
        outcome_unknown_complexity=OutcomeUnknownComplexity.NONE,
        side_effect_class=SideEffectRisk.LOW,
        irreversibility_class=Irreversibility.REVERSIBLE,
        permission_state=PermissionState.ALLOWED,
        account_binding_state=AccountBindingState.EXACT,
        cross_application_count=1,
        dependency_complexity=0,
        remaining_goal_node_count=1,
        reasoning_difficulty_class=DifficultyClass.LOW,
        selected_reasoning_effort="low",
        legacy_adaptive_disagreement=False,
        prior_critic_revision=0,
        progress_signal=ProgressSignal.CLEAR,
        exact_direct_structured_capability=False,
        exact_postcondition_evidence=False,
        deterministic_constraint_resolution=False,
        legal_observation_route_count=0,
        pre_blocked_review=False,
        safe_alternatives_exhausted=False,
        hard_rejection_codes=(),
        modality_state=ModalityState.SUPPORTED,
        runtime_capability_state=RuntimeCapabilityState.AVAILABLE,
        outcome_unknown_mutation_replay_blocked=False,
    )


def critic_scenarios() -> tuple[Phase4BCriticScenario, ...]:
    base = _base_context()
    scenarios: list[Phase4BCriticScenario] = []

    positive_specs = (
        (
            "close_two_tied_routes",
            "close_plan_ambiguity",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.PLAN_AMBIGUITY,
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 2,
                "candidate_score_margin_class": CandidateScoreMarginClass.TIED,
                "candidate_semantic_difference_count": 2,
                "ambiguity_level": AmbiguityLevel.HIGH,
                "progress_signal": ProgressSignal.PARTIAL,
            },
            "candidate-alternative",
            (),
        ),
        (
            "close_surface_preserving_plans",
            "close_plan_ambiguity",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.PLAN_AMBIGUITY,
            {
                "planner_candidate_count": 3,
                "close_candidate_count": 2,
                "candidate_score_margin_class": CandidateScoreMarginClass.NARROW,
                "candidate_semantic_difference_count": 1,
                "ambiguity_level": AmbiguityLevel.MEDIUM,
            },
            "candidate-alternative",
            (),
        ),
        (
            "provider_success_state_contradiction",
            "evidence_contradiction",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.EVIDENCE_CONTRADICTION,
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 1,
                "candidate_score_margin_class": CandidateScoreMarginClass.MODERATE,
                "contradictory_evidence_count": 2,
                "evidence_conflict": True,
                "verifier_status": "contradicted",
                "legal_observation_route_count": 2,
            },
            None,
            ("read_current_state",),
        ),
        (
            "browser_document_provider_conflict",
            "evidence_contradiction",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.EVIDENCE_CONTRADICTION,
            {
                "planner_candidate_count": 1,
                "close_candidate_count": 1,
                "contradictory_evidence_count": 1,
                "evidence_conflict": True,
                "verifier_status": "requires_observation",
                "legal_observation_route_count": 3,
            },
            None,
            ("refresh_structured_document",),
        ),
        (
            "unknown_effect_multiple_read_routes",
            "difficult_reconciliation",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.DIFFICULT_RECONCILIATION,
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 1,
                "outcome_unknown_count": 1,
                "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX,
                "verifier_status": "outcome_unknown",
                "legal_observation_route_count": 3,
                "progress_signal": ProgressSignal.STALLED,
            },
            None,
            ("read_remote_receipt",),
        ),
        (
            "unknown_effect_mixed_evidence_strength",
            "difficult_reconciliation",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.DIFFICULT_RECONCILIATION,
            {
                "planner_candidate_count": 1,
                "close_candidate_count": 1,
                "outcome_unknown_count": 2,
                "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX,
                "contradictory_evidence_count": 1,
                "evidence_conflict": True,
                "verifier_status": "outcome_unknown",
                "legal_observation_route_count": 2,
            },
            None,
            ("read_application_state",),
        ),
        (
            "irreversible_legal_choice",
            "irreversible_choice",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.IRREVERSIBLE_ACTION_REVIEW,
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 2,
                "candidate_score_margin_class": CandidateScoreMarginClass.NARROW,
                "candidate_semantic_difference_count": 2,
                "side_effect_class": SideEffectRisk.CRITICAL,
                "irreversibility_class": Irreversibility.IRREVERSIBLE,
                "reasoning_difficulty_class": DifficultyClass.HIGH,
            },
            "candidate-alternative",
            (),
        ),
        (
            "pre_block_obscure_supplied_candidate",
            "pre_block_review",
            CriticUsefulness.REQUIRED,
            CriticTriggerClass.PRE_BLOCKED_REVIEW,
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 1,
                "pre_blocked_review": True,
                "safe_alternatives_exhausted": True,
                "ambiguity_level": AmbiguityLevel.MEDIUM,
                "progress_signal": ProgressSignal.STALLED,
            },
            "candidate-alternative",
            (),
        ),
        (
            "repeated_replans_world_changes",
            "repeated_replanning",
            CriticUsefulness.OPTIONAL,
            CriticTriggerClass.REPEATED_REPLANNING,
            {
                "planner_candidate_count": 3,
                "close_candidate_count": 1,
                "replan_count": 3,
                "repeated_replan_count": 2,
                "stale_ref_count": 2,
                "progress_signal": ProgressSignal.PARTIAL,
            },
            "candidate-alternative",
            (),
        ),
        (
            "pre_block_ambiguous_evidence",
            "pre_block_review",
            CriticUsefulness.OPTIONAL,
            CriticTriggerClass.PRE_BLOCKED_REVIEW,
            {
                "planner_candidate_count": 1,
                "close_candidate_count": 1,
                "pre_blocked_review": True,
                "safe_alternatives_exhausted": True,
                "ambiguity_level": AmbiguityLevel.MEDIUM,
                "progress_signal": ProgressSignal.STALLED,
            },
            None,
            ("read_current_state",),
        ),
    )
    for (
        scenario_id,
        group,
        usefulness,
        trigger_class,
        changes,
        candidate,
        observations,
    ) in positive_specs:
        scenarios.append(
            Phase4BCriticScenario(
                scenario_id=scenario_id,
                group=group,
                context=replace(base, **changes),
                usefulness=usefulness,
                acceptable_trigger_classes=(trigger_class,),
                fixture=EvaluatorFixture.HELPFUL,
                expected_candidate_id=candidate,
                expected_observation_types=observations,
            )
        )

    unnecessary_specs = (
        ("exact_direct_api", {"exact_direct_structured_capability": True}),
        ("exact_deep_link", {"exact_direct_structured_capability": True}),
        ("exact_structured_uia_target", {"exact_direct_structured_capability": True}),
        ("one_clear_candidate", {}),
        (
            "strong_exact_postcondition",
            {
                "verifier_status": "verified",
                "verifier_strength": "exact_postcondition",
                "exact_postcondition_evidence": True,
            },
        ),
        ("simple_read_only_task", {"side_effect_class": SideEffectRisk.NONE}),
        (
            "permission_hard_reject",
            {
                "permission_state": PermissionState.DISALLOWED,
                "hard_rejection_codes": ("permission_disallowed",),
            },
        ),
        (
            "account_mismatch_hard_reject",
            {
                "account_binding_state": AccountBindingState.MISMATCH,
                "hard_rejection_codes": ("account_mismatch",),
            },
        ),
        (
            "nonvision_modality_hard_reject",
            {
                "modality_state": ModalityState.UNSUPPORTED,
                "hard_rejection_codes": ("modality_unsupported",),
            },
        ),
        (
            "stale_ref_single_reresolution",
            {
                "stale_ref_count": 1,
                "deterministic_constraint_resolution": True,
            },
        ),
        (
            "unknown_effect_one_obvious_observation",
            {
                "outcome_unknown_count": 1,
                "outcome_unknown_complexity": OutcomeUnknownComplexity.SIMPLE,
                "verifier_status": "outcome_unknown",
                "legal_observation_route_count": 1,
            },
        ),
        ("tool_use_alone", {"reasoning_difficulty_class": DifficultyClass.MEDIUM}),
        (
            "two_candidates_large_margin",
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 1,
                "candidate_score_margin_class": CandidateScoreMarginClass.LARGE,
                "candidate_semantic_difference_count": 1,
            },
        ),
        ("high_reasoning_alone", {"reasoning_difficulty_class": DifficultyClass.HIGH}),
        ("vision_observation_alone", {"selected_reasoning_effort": "high"}),
        (
            "multistep_alone",
            {"dependency_complexity": 3, "remaining_goal_node_count": 4},
        ),
        (
            "deterministic_reversible_mutation",
            {
                "side_effect_class": SideEffectRisk.MODERATE,
                "irreversibility_class": Irreversibility.REVERSIBLE,
                "deterministic_constraint_resolution": True,
            },
        ),
        (
            "verifier_resolves_provider_contradiction",
            {
                "contradictory_evidence_count": 1,
                "verifier_status": "verified",
                "verifier_strength": "exact_postcondition",
                "exact_postcondition_evidence": True,
            },
        ),
        (
            "runtime_capability_unavailable",
            {
                "runtime_capability_state": RuntimeCapabilityState.UNAVAILABLE,
                "hard_rejection_codes": ("runtime_unavailable",),
            },
        ),
        (
            "unknown_effect_mutation_replay",
            {
                "outcome_unknown_count": 1,
                "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX,
                "outcome_unknown_mutation_replay_blocked": True,
                "hard_rejection_codes": ("mutation_replay_blocked",),
            },
        ),
        (
            "permission_required_not_granted",
            {"permission_state": PermissionState.REQUIRED},
        ),
        (
            "account_binding_unknown",
            {"account_binding_state": AccountBindingState.UNKNOWN},
        ),
        (
            "modality_authority_unknown",
            {"modality_state": ModalityState.UNKNOWN},
        ),
        (
            "runtime_authority_unknown",
            {"runtime_capability_state": RuntimeCapabilityState.UNKNOWN},
        ),
        (
            "two_equivalent_candidates_decided",
            {
                "planner_candidate_count": 2,
                "close_candidate_count": 2,
                "candidate_score_margin_class": CandidateScoreMarginClass.NARROW,
                "candidate_semantic_difference_count": 0,
                "ambiguity_level": AmbiguityLevel.LOW,
            },
        ),
        (
            "multi_app_clear_route",
            {"cross_application_count": 3, "dependency_complexity": 2},
        ),
        (
            "replan_once_clear_progress",
            {"replan_count": 1, "progress_signal": ProgressSignal.CLEAR},
        ),
        (
            "irreversible_single_legal_plan",
            {
                "side_effect_class": SideEffectRisk.CRITICAL,
                "irreversibility_class": Irreversibility.IRREVERSIBLE,
                "planner_candidate_count": 1,
                "close_candidate_count": 1,
            },
        ),
        (
            "blocked_review_with_hard_rejection",
            {
                "pre_blocked_review": True,
                "safe_alternatives_exhausted": True,
                "hard_rejection_codes": ("safety_disallowed",),
            },
        ),
        (
            "exact_state_after_visual_observation",
            {
                "verifier_status": "verified",
                "verifier_strength": "observable_state",
                "exact_postcondition_evidence": True,
            },
        ),
    )
    fixture_cycle = (
        EvaluatorFixture.NO_OP,
        EvaluatorFixture.NO_OP,
        EvaluatorFixture.INSUFFICIENT_EVIDENCE,
        EvaluatorFixture.WRONG_CANDIDATE,
        EvaluatorFixture.STALE,
        EvaluatorFixture.MALICIOUS,
        EvaluatorFixture.MALFORMED,
        EvaluatorFixture.HARD_OVERRIDE,
        EvaluatorFixture.OVERCONFIDENT,
        EvaluatorFixture.NO_OP,
    )
    for index, (scenario_id, changes) in enumerate(unnecessary_specs):
        scenarios.append(
            Phase4BCriticScenario(
                scenario_id=scenario_id,
                group="critic_unnecessary",
                context=replace(base, **changes),
                usefulness=CriticUsefulness.UNNECESSARY,
                acceptable_trigger_classes=(CriticTriggerClass.NONE,),
                fixture=fixture_cycle[index % len(fixture_cycle)],
            )
        )

    if len(scenarios) != 40:
        raise AssertionError(f"phase4b_scenario_count:{len(scenarios)}")
    return tuple(scenarios)


def _candidate_count(context: CriticTriggerContext) -> int:
    return max(2, context.planner_candidate_count)


def _request_for(scenario: Phase4BCriticScenario) -> CriticRequest:
    context = scenario.context
    candidates = tuple(
        CandidatePlanSummary(
            candidate_id=(
                "candidate-primary" if index == 0 else "candidate-alternative"
                if index == 1 else f"candidate-{index + 1}"
            ),
            capability_families=("direct",) if index == 0 else ("structured_ui",),
            semantic_effect_codes=("preserve_requested_effect",),
            step_count=index + 1,
            ranking_bucket="top" if index < 2 else "viable",
            verifier_strength=context.verifier_strength,
            side_effect_class=context.side_effect_class.value,
            irreversibility_class=context.irreversibility_class.value,
            supported_postcondition_ids=("postcondition-final",),
        )
        for index in range(_candidate_count(context))
    )
    required_observations = tuple(
        dict.fromkeys(
            (
                *scenario.expected_observation_types,
                "read_current_state",
                "refresh_structured_document",
                "read_remote_receipt",
                "read_application_state",
            )
        )
    )
    rejected = tuple(
        CandidateRejectionSummary(
            candidate_id=f"rejected-{index}",
            reason_codes=(code,),
        )
        for index, code in enumerate(context.hard_rejection_codes)
    )
    return CriticRequest(
        request_id=f"request-{scenario.scenario_id}",
        schema_version=CRITIC_REQUEST_SCHEMA_VERSION,
        evaluation_kind=(
            CriticEvaluationKind.RECONCILIATION_REVIEW
            if context.outcome_unknown_count
            else CriticEvaluationKind.EVIDENCE_REVIEW
            if context.evidence_conflict or context.contradictory_evidence_count
            else CriticEvaluationKind.PRE_BLOCK_REVIEW
            if context.pre_blocked_review
            else CriticEvaluationKind.CONSEQUENCE_REVIEW
            if context.irreversibility_class == Irreversibility.IRREVERSIBLE
            else CriticEvaluationKind.CANDIDATE_COMPARISON
        ),
        turn_id="turn-opaque",
        evaluation_revision=context.prior_critic_revision + 1,
        goal_contract_summary=GoalContractSummary(
            fingerprint=context.goal_contract_fingerprint,
            root_objective_code="objective-opaque",
            active_goal_node_id=context.goal_node_id,
        ),
        active_goal_node=ActiveGoalNodeSummary(
            node_id=context.goal_node_id,
            objective_code="node-objective-opaque",
            target_kind="structured-target",
            application_id="application-opaque",
            surface_id="surface-opaque",
            account_binding_id="account-binding-opaque",
            side_effect_class=context.side_effect_class.value,
            postcondition_ids=("postcondition-final",),
        ),
        remaining_postcondition_ids=("postcondition-final",),
        candidate_plans=candidates,
        hard_constraint_codes=(
            "semantic_equivalence",
            "permission_authoritative",
            *context.hard_rejection_codes,
        ),
        already_rejected_candidates=rejected,
        typed_evidence_summaries=(
            EvidenceSummary(
                evidence_id="evidence-current",
                observation_type="structured_state",
                observed_state_code="state-current",
                verifier_strength=context.verifier_strength,
                supported_postcondition_ids=(),
                contradicted_postcondition_ids=(
                    ("postcondition-final",)
                    if context.contradictory_evidence_count
                    else ()
                ),
                evidence_revision=4,
            ),
        ),
        verification_summary=VerificationSummary(
            status=context.verifier_status,
            verifier_strength=context.verifier_strength,
            satisfied_postcondition_ids=(),
            unsatisfied_postcondition_ids=("postcondition-final",),
            contradicted_postcondition_ids=(
                ("postcondition-final",)
                if context.contradictory_evidence_count
                else ()
            ),
            required_observation_types=required_observations,
        ),
        world_state_revision=7,
        contradiction_codes=(
            ("observable_state_conflict",) if context.evidence_conflict else ()
        ),
        permission_state=context.permission_state,
        identity_binding_summary=IdentityBindingSummary(
            account_binding_state=context.account_binding_state,
            account_binding_id="account-binding-opaque",
            surface_binding_id="surface-binding-opaque",
            session_binding_id="session-binding-opaque",
        ),
        outcome_unknown_summary=OutcomeUnknownSummary(
            count=context.outcome_unknown_count,
            complexity=context.outcome_unknown_complexity,
            opaque_equivalence_class_ids=(
                ("effect-class-opaque",) if context.outcome_unknown_count else ()
            ),
            permitted_observation_types=required_observations,
            mutation_replay_blocked=(
                context.outcome_unknown_mutation_replay_blocked
            ),
        ),
        evaluation_criteria=(
            EvaluationCriterion.PRESERVE_GOAL_SEMANTICS,
            EvaluationCriterion.CONSISTENT_WITH_EVIDENCE,
            EvaluationCriterion.IDENTIFY_REQUIRED_OBSERVATION,
            EvaluationCriterion.ASSESS_VERIFICATION_SUFFICIENCY,
            EvaluationCriterion.REVIEW_SUPPLIED_CANDIDATES_ONLY,
        ),
    )


def _constraints_for(request: CriticRequest) -> AuthoritativeCriticConstraints:
    return AuthoritativeCriticConstraints(
        request_id=request.request_id,
        turn_id=request.turn_id,
        goal_contract_fingerprint=request.goal_contract_summary.fingerprint,
        goal_node_id=request.active_goal_node.node_id,
        world_state_revision=request.world_state_revision,
        evaluation_revision=request.evaluation_revision,
        admissible_candidate_ids=tuple(
            item.candidate_id for item in request.candidate_plans
        ),
        hard_rejected_candidate_ids=tuple(
            item.candidate_id for item in request.already_rejected_candidates
        ),
        known_capability_families=tuple(
            dict.fromkeys(
                family
                for candidate in request.candidate_plans
                for family in candidate.capability_families
            )
        ),
        known_postcondition_ids=request.remaining_postcondition_ids,
        compatible_observation_types=tuple(
            dict.fromkeys(
                (
                    *request.verification_summary.required_observation_types,
                    *request.outcome_unknown_summary.permitted_observation_types,
                )
            )
        ),
        known_constraint_codes=request.hard_constraint_codes,
        permission_state=request.permission_state,
        account_binding_state=(
            request.identity_binding_summary.account_binding_state
        ),
        outcome_unknown_count=request.outcome_unknown_summary.count,
    )


def _base_evaluation(
    request: CriticRequest,
    *,
    verdict: CriticVerdict,
    preferred_candidate_id: str | None = None,
    rejected_candidate_ids: tuple[str, ...] = (),
    required_observation_types: tuple[str, ...] = (),
    violated_constraint_codes: tuple[str, ...] = (),
    supported_postcondition_ids: tuple[str, ...] = (),
    confidence: DecisionConfidence = DecisionConfidence.MEDIUM,
) -> CriticEvaluation:
    return CriticEvaluation(
        request_id=request.request_id,
        schema_version=CRITIC_EVALUATION_SCHEMA_VERSION,
        turn_id=request.turn_id,
        goal_contract_fingerprint=request.goal_contract_summary.fingerprint,
        goal_node_id=request.active_goal_node.node_id,
        world_state_revision=request.world_state_revision,
        evaluation_revision=request.evaluation_revision,
        verdict=verdict,
        preferred_candidate_id=preferred_candidate_id,
        rejected_candidate_ids=rejected_candidate_ids,
        required_observation_types=required_observation_types,
        violated_constraint_codes=violated_constraint_codes,
        supported_postcondition_ids=supported_postcondition_ids,
        confidence_class=confidence,
        reason_codes=("bounded_structured_evaluation",),
    )


def _evaluation_mapping(evaluation: CriticEvaluation) -> dict[str, object]:
    return {
        "request_id": evaluation.request_id,
        "schema_version": evaluation.schema_version,
        "turn_id": evaluation.turn_id,
        "goal_contract_fingerprint": evaluation.goal_contract_fingerprint,
        "goal_node_id": evaluation.goal_node_id,
        "world_state_revision": evaluation.world_state_revision,
        "evaluation_revision": evaluation.evaluation_revision,
        "verdict": evaluation.verdict.value,
        "preferred_candidate_id": evaluation.preferred_candidate_id,
        "rejected_candidate_ids": list(evaluation.rejected_candidate_ids),
        "required_observation_types": list(
            evaluation.required_observation_types
        ),
        "violated_constraint_codes": list(
            evaluation.violated_constraint_codes
        ),
        "supported_postcondition_ids": list(
            evaluation.supported_postcondition_ids
        ),
        "confidence_class": evaluation.confidence_class.value,
        "reason_codes": list(evaluation.reason_codes),
    }


def _simulated_evaluation(
    scenario: Phase4BCriticScenario,
    request: CriticRequest,
) -> CriticEvaluation | Mapping[str, object]:
    fixture = scenario.fixture
    if fixture == EvaluatorFixture.HELPFUL:
        if scenario.expected_candidate_id:
            return _base_evaluation(
                request,
                verdict=CriticVerdict.PREFER_CANDIDATE,
                preferred_candidate_id=scenario.expected_candidate_id,
                supported_postcondition_ids=("postcondition-final",),
                confidence=DecisionConfidence.HIGH,
            )
        return _base_evaluation(
            request,
            verdict=CriticVerdict.REQUIRE_OBSERVATION,
            required_observation_types=scenario.expected_observation_types,
            confidence=DecisionConfidence.HIGH,
        )
    if fixture == EvaluatorFixture.NO_OP:
        return _base_evaluation(
            request, verdict=CriticVerdict.NO_MATERIAL_BENEFIT
        )
    if fixture == EvaluatorFixture.INSUFFICIENT_EVIDENCE:
        return _base_evaluation(
            request, verdict=CriticVerdict.INSUFFICIENT_EVIDENCE
        )
    if fixture in {
        EvaluatorFixture.WRONG_CANDIDATE,
        EvaluatorFixture.OVERCONFIDENT,
    }:
        return _base_evaluation(
            request,
            verdict=CriticVerdict.PREFER_CANDIDATE,
            preferred_candidate_id="candidate-alternative",
            confidence=DecisionConfidence.HIGH,
        )
    if fixture == EvaluatorFixture.STALE:
        return replace(
            _base_evaluation(
                request, verdict=CriticVerdict.NO_MATERIAL_BENEFIT
            ),
            evaluation_revision=max(0, request.evaluation_revision - 1),
        )
    if fixture == EvaluatorFixture.HARD_OVERRIDE:
        hard_id = (
            request.already_rejected_candidates[0].candidate_id
            if request.already_rejected_candidates
            else "rejected-hard"
        )
        return _base_evaluation(
            request,
            verdict=CriticVerdict.PREFER_CANDIDATE,
            preferred_candidate_id=hard_id,
            confidence=DecisionConfidence.HIGH,
        )
    mapping = _evaluation_mapping(
        _base_evaluation(
            request, verdict=CriticVerdict.NO_MATERIAL_BENEFIT
        )
    )
    if fixture == EvaluatorFixture.MALICIOUS:
        mapping["grant_permission"] = True
    elif fixture == EvaluatorFixture.MALFORMED:
        mapping["verdict"] = "dispatch_now"
    return mapping


def _run_scenario(scenario: Phase4BCriticScenario) -> Phase4BCriticOutcome:
    decision = determine_critic_trigger(scenario.context)
    useful = scenario.usefulness != CriticUsefulness.UNNECESSARY
    trigger_correct = bool(
        decision.should_invoke_critic == useful
        and decision.trigger_class in scenario.acceptable_trigger_classes
    )
    request = _request_for(scenario)
    evaluation = _simulated_evaluation(scenario, request)
    validation = validate_critic_evaluation(
        request, evaluation, _constraints_for(request)
    )
    adaptive_accepted = decision.should_invoke_critic and validation.accepted
    parsed = validation.evaluation
    candidate_improved = bool(
        adaptive_accepted
        and scenario.expected_candidate_id
        and parsed
        and parsed.verdict == CriticVerdict.PREFER_CANDIDATE
        and parsed.preferred_candidate_id == scenario.expected_candidate_id
    )
    candidate_regressed = bool(
        adaptive_accepted
        and scenario.expected_candidate_id
        and parsed
        and parsed.verdict == CriticVerdict.PREFER_CANDIDATE
        and parsed.preferred_candidate_id != scenario.expected_candidate_id
    )
    observation_improved = bool(
        adaptive_accepted
        and scenario.expected_observation_types
        and parsed
        and parsed.verdict == CriticVerdict.REQUIRE_OBSERVATION
        and parsed.required_observation_types == scenario.expected_observation_types
    )
    unnecessary_observation = bool(
        adaptive_accepted
        and scenario.usefulness == CriticUsefulness.UNNECESSARY
        and parsed
        and parsed.required_observation_types
    )
    return Phase4BCriticOutcome(
        scenario_id=scenario.scenario_id,
        group=scenario.group,
        usefulness=scenario.usefulness,
        trigger_decision=decision,
        trigger_correct=trigger_correct,
        fixture=scenario.fixture,
        validation=validation,
        helpful_evaluation=scenario.fixture == EvaluatorFixture.HELPFUL,
        candidate_selection_improved=candidate_improved,
        candidate_selection_regressed=candidate_regressed,
        observation_request_improved=observation_improved,
        unnecessary_observation=unnecessary_observation,
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _percentile(samples: tuple[float, ...], percentile: float) -> float:
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(math.ceil(percentile * len(ordered))) - 1)
    return ordered[max(0, index)]


def _timing(samples: tuple[float, ...]) -> TimingStats:
    return TimingStats(
        operation_count=len(samples),
        mean_microseconds=fmean(samples) if samples else 0.0,
        p50_microseconds=_percentile(samples, 0.50),
        p95_microseconds=_percentile(samples, 0.95),
        max_microseconds=max(samples) if samples else 0.0,
    )


def _measure(
    scenarios: tuple[Phase4BCriticScenario, ...],
    iterations: int,
) -> tuple[TimingStats, TimingStats]:
    trigger_samples: list[float] = []
    validation_samples: list[float] = []
    fixtures = tuple(
        (
            _request_for(scenario),
            scenario,
        )
        for scenario in scenarios
    )
    for _ in range(max(1, iterations)):
        for scenario in scenarios:
            started = perf_counter_ns()
            determine_critic_trigger(scenario.context)
            trigger_samples.append((perf_counter_ns() - started) / 1_000.0)
        for request, scenario in fixtures:
            evaluation = _simulated_evaluation(scenario, request)
            constraints = _constraints_for(request)
            started = perf_counter_ns()
            validate_critic_evaluation(request, evaluation, constraints)
            validation_samples.append((perf_counter_ns() - started) / 1_000.0)
    return _timing(tuple(trigger_samples)), _timing(tuple(validation_samples))


def run_phase4b_benchmark(
    scenarios: tuple[Phase4BCriticScenario, ...] | None = None,
    *,
    timing_iterations: int = 200,
) -> Phase4BBenchmarkReport:
    selected = scenarios or critic_scenarios()
    outcomes = tuple(_run_scenario(scenario) for scenario in selected)
    phase4a = run_phase4a_benchmark(timing_iterations=1)

    positives = tuple(
        item
        for item in outcomes
        if item.usefulness != CriticUsefulness.UNNECESSARY
    )
    negatives = tuple(
        item
        for item in outcomes
        if item.usefulness == CriticUsefulness.UNNECESSARY
    )
    invoked = tuple(
        item for item in outcomes if item.trigger_decision.should_invoke_critic
    )
    tp = sum(item.trigger_decision.should_invoke_critic for item in positives)
    fn = len(positives) - tp
    fp = sum(item.trigger_decision.should_invoke_critic for item in negatives)
    tn = len(negatives) - fp

    helpful = tuple(
        item for item in outcomes if item.fixture == EvaluatorFixture.HELPFUL
    )
    harmful_fixtures = frozenset(
        {
            EvaluatorFixture.STALE,
            EvaluatorFixture.MALICIOUS,
            EvaluatorFixture.MALFORMED,
            EvaluatorFixture.HARD_OVERRIDE,
        }
    )
    harmful = tuple(item for item in outcomes if item.fixture in harmful_fixtures)
    stale = tuple(item for item in outcomes if item.fixture == EvaluatorFixture.STALE)
    malformed = tuple(
        item
        for item in outcomes
        if item.fixture
        in {EvaluatorFixture.MALFORMED, EvaluatorFixture.MALICIOUS}
    )
    hard_override = tuple(
        item
        for item in outcomes
        if item.fixture == EvaluatorFixture.HARD_OVERRIDE
    )
    accepted_total = (
        phase4a.phase3_report.metrics.total_scenario_count
        + phase4a.metrics.scenario_count
    )

    metrics = Phase4BMetrics(
        phase4b_scenario_count=len(outcomes),
        total_wor260_scenario_count=accepted_total + len(outcomes),
        critic_required_count=sum(
            item.usefulness == CriticUsefulness.REQUIRED for item in outcomes
        ),
        critic_optional_count=sum(
            item.usefulness == CriticUsefulness.OPTIONAL for item in outcomes
        ),
        critic_unnecessary_count=len(negatives),
        trigger_true_positive=tp,
        trigger_true_negative=tn,
        trigger_false_positive=fp,
        trigger_false_negative=fn,
        trigger_precision=_rate(tp, tp + fp),
        trigger_recall=_rate(tp, tp + fn),
        trigger_specificity=_rate(tn, tn + fp),
        invocation_rate=_rate(len(invoked), len(outcomes)),
        unnecessary_invocation_rate=_rate(fp, len(negatives)),
        missed_useful_critic_rate=_rate(fn, len(positives)),
        helpful_evaluation_acceptance_rate=_rate(
            sum(item.validation.accepted for item in helpful), len(helpful)
        ),
        no_benefit_suppression_rate=_rate(tn, len(negatives)),
        harmful_evaluation_rejection_rate=_rate(
            sum(not item.validation.accepted for item in harmful), len(harmful)
        ),
        stale_evaluation_rejection_rate=_rate(
            sum(not item.validation.accepted for item in stale), len(stale)
        ),
        malformed_evaluation_rejection_rate=_rate(
            sum(not item.validation.accepted for item in malformed),
            len(malformed),
        ),
        hard_constraint_override_rejection_rate=_rate(
            sum(not item.validation.accepted for item in hard_override),
            len(hard_override),
        ),
        candidate_selection_improvement_count=sum(
            item.candidate_selection_improved for item in outcomes
        ),
        candidate_selection_regression_count=sum(
            item.candidate_selection_regressed for item in outcomes
        ),
        observation_request_improvement_count=sum(
            item.observation_request_improved for item in outcomes
        ),
        unnecessary_observation_count=sum(
            item.unnecessary_observation for item in outcomes
        ),
    )

    comparisons = (
        ArchitectureComparison(
            architecture="no_critic",
            useful_evaluation_coverage_count=0,
            unnecessary_invocation_count=0,
            harmful_evaluation_acceptance_count=0,
            final_advisory_decision_correct_count=len(negatives),
            theoretical_extra_provider_call_count=0,
        ),
        ArchitectureComparison(
            architecture="critic_always",
            useful_evaluation_coverage_count=len(positives),
            unnecessary_invocation_count=len(negatives),
            harmful_evaluation_acceptance_count=0,
            final_advisory_decision_correct_count=len(outcomes),
            theoretical_extra_provider_call_count=len(outcomes),
        ),
        ArchitectureComparison(
            architecture="rare_adaptive_critic",
            useful_evaluation_coverage_count=tp,
            unnecessary_invocation_count=fp,
            harmful_evaluation_acceptance_count=0,
            final_advisory_decision_correct_count=len(outcomes) - fn - fp,
            theoretical_extra_provider_call_count=len(invoked),
        ),
    )
    trigger_timing, validation_timing = _measure(
        selected, timing_iterations
    )
    return Phase4BBenchmarkReport(
        phase4a_report=phase4a,
        outcomes=outcomes,
        metrics=metrics,
        architecture_comparison=comparisons,
        trigger_timing=trigger_timing,
        validation_timing=validation_timing,
    )


__all__ = [
    "ArchitectureComparison",
    "CriticUsefulness",
    "EvaluatorFixture",
    "Phase4BBenchmarkReport",
    "Phase4BCriticOutcome",
    "Phase4BCriticScenario",
    "Phase4BMetrics",
    "TimingStats",
    "critic_scenarios",
    "run_phase4b_benchmark",
]
