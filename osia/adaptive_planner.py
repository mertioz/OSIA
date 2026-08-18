"""Bounded, deterministic WOR-260 adaptive planner in strict shadow mode.

This module proposes immutable descriptions only.  It never imports or calls
the live ``ExecutionStrategy``, creates permissions/permits, dispatches tools,
persists state, or mutates turn terminality.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from itertools import product

from osia.capability_adapters import (
    AdaptiveCapabilityDescriptor,
    VerifierStrength,
)
from osia.capability_search import (
    CandidateEligibility,
    CapabilitySearchLimits,
    CapabilitySearchResult,
    SearchCandidate,
    ShadowPlanningContext,
    StepRole,
    search_capabilities,
)
from osia.goal_contract import GoalNodeContract
from osia.tool_contracts import SideEffectClass


class ConfidenceClass(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


class DivergenceClassification(str, Enum):
    PLAUSIBLY_BETTER = "plausibly_better"
    SEMANTICALLY_EQUIVALENT = "semantically_equivalent"
    WORSE = "worse"
    UNRESOLVED = "unresolved"
    IMPOSSIBLE_TO_COMPARE = "impossible_to_compare"


@dataclass(frozen=True)
class PlannerLimits:
    top_k: int = 6
    retrieval_limit: int = 12
    branch_width: int = 3
    max_plan_steps: int = 12
    max_candidate_plans: int = 8

    def __post_init__(self) -> None:
        if min(
            self.top_k,
            self.retrieval_limit,
            self.branch_width,
            self.max_plan_steps,
            self.max_candidate_plans,
        ) < 1:
            raise ValueError("planner_limits_must_be_positive")


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    goal_node_id: str
    role: str
    capability_id: str
    family: str
    expected_semantic_effect: str
    preconditions: tuple[str, ...]
    expected_world_delta_description: str
    required_postcondition_or_verifier: str
    side_effect_class: str
    dependency_ids: tuple[str, ...]
    fallback_eligible: bool
    explanation_codes: tuple[str, ...]


@dataclass(frozen=True)
class AdaptivePlan:
    goal_contract_fingerprint: str
    revision: int
    ordered_steps: tuple[PlanStep, ...]
    expected_final_postconditions: tuple[str, ...]
    unresolved_constraints: tuple[str, ...]
    alternatives: tuple[str, ...]
    confidence_class: str
    explanation_codes: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class LegacyDecisionSnapshot:
    first_mechanism_id: str
    first_family: str = "unknown"
    semantic_plan: tuple[str, ...] = ()
    step_count: int = 0
    verifier_strength: str = VerifierStrength.NONE.value
    frontier_capability_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyComparison:
    same_first_mechanism: bool
    same_semantic_plan: bool
    relation_codes: tuple[str, ...]
    classification: str


@dataclass(frozen=True)
class ShadowPlanResult:
    candidate_plans: tuple[AdaptivePlan, ...]
    selected_plan: AdaptivePlan | None
    search_evidence: tuple[CapabilitySearchResult, ...]
    unresolved_state: tuple[str, ...]
    legacy_comparison: LegacyComparison | None
    diagnostics: tuple[str, ...]


_BLOCKING_UNRESOLVED = frozenset({
    "personal_browser_authority_unknown",
    "vision_authority_unknown",
})
_VERIFY_STRENGTH = {
    VerifierStrength.NONE.value: 0,
    VerifierStrength.WEAK_UNSTRUCTURED.value: 1,
    VerifierStrength.PROVIDER_REPORTED.value: 2,
    VerifierStrength.STRUCTURED_RESULT.value: 3,
    VerifierStrength.OBSERVABLE_STATE.value: 4,
    VerifierStrength.EXACT_POSTCONDITION.value: 5,
}


def _descriptor_map(
    context: ShadowPlanningContext,
) -> dict[str, AdaptiveCapabilityDescriptor]:
    return {item.capability_id: item for item in context.capabilities}


def _candidate_map(
    result: CapabilitySearchResult,
) -> dict[str, SearchCandidate]:
    return {
        item.capability_id: item
        for item in (*result.eligible, *result.unresolved, *result.hard_rejected)
    }


def _postconditions(node: GoalNodeContract) -> tuple[str, ...]:
    return tuple(item.description for item in node.observable_postconditions)


def _step(
    node: GoalNodeContract,
    candidate: SearchCandidate,
    descriptor: AdaptiveCapabilityDescriptor,
    role: str,
    index: int,
    dependencies: tuple[str, ...],
) -> PlanStep:
    step_id = f"{node.node_id}:{index:02d}:{role}:{candidate.capability_id}"
    effect = "; ".join(descriptor.semantic_effects) or descriptor.description
    required = (
        "; ".join(_postconditions(node))
        if role in {StepRole.ACT.value, StepRole.VERIFY.value}
        else ""
    )
    explanations = tuple(dict.fromkeys((
        *candidate.explanation_codes,
        f"role_{role}",
    )))
    return PlanStep(
        step_id=step_id,
        goal_node_id=node.node_id,
        role=role,
        capability_id=candidate.capability_id,
        family=candidate.family,
        expected_semantic_effect=effect or "explicit descriptor semantic effect",
        preconditions=descriptor.preconditions,
        expected_world_delta_description=(
            "read-only evidence/state refinement"
            if descriptor.side_effect_class == SideEffectClass.READ_ONLY.value
            else effect or "descriptor-declared semantic state change"
        ),
        required_postcondition_or_verifier=required,
        side_effect_class=descriptor.side_effect_class,
        dependency_ids=dependencies,
        fallback_eligible=(
            descriptor.side_effect_class == SideEffectClass.READ_ONLY.value
            or candidate.reversibility in {"reversible", "not_applicable"}
        ),
        explanation_codes=explanations,
    )


def _fingerprint_plan(
    goal_fingerprint: str,
    revision: int,
    steps: tuple[PlanStep, ...],
    unresolved: tuple[str, ...],
) -> str:
    payload = {
        "goal_contract_fingerprint": goal_fingerprint,
        "revision": revision,
        "steps": [
            {
                "step_id": item.step_id,
                "goal_node_id": item.goal_node_id,
                "role": item.role,
                "capability_id": item.capability_id,
                "dependencies": list(item.dependency_ids),
            }
            for item in steps
        ],
        "unresolved": list(unresolved),
    }
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _make_plan(
    context: ShadowPlanningContext,
    steps: tuple[PlanStep, ...],
    unresolved: tuple[str, ...],
    explanations: tuple[str, ...],
    *,
    revision: int = 1,
) -> AdaptivePlan:
    postconditions = tuple(
        item.description
        for node in context.goal_contract.nodes
        if node.node_id not in context.completed_goal_node_ids
        for item in node.observable_postconditions
    )
    if unresolved:
        confidence = ConfidenceClass.UNRESOLVED.value
    elif steps and all(
        "exact_semantic_effect" in item.explanation_codes
        for item in steps if item.role == StepRole.ACT.value
    ):
        confidence = ConfidenceClass.HIGH.value
    elif steps:
        confidence = ConfidenceClass.MEDIUM.value
    else:
        confidence = ConfidenceClass.LOW.value
    fingerprint = _fingerprint_plan(
        context.goal_contract.fingerprint, revision, steps, unresolved
    )
    return AdaptivePlan(
        goal_contract_fingerprint=context.goal_contract.fingerprint,
        revision=revision,
        ordered_steps=steps,
        expected_final_postconditions=postconditions,
        unresolved_constraints=unresolved,
        alternatives=(),
        confidence_class=confidence,
        explanation_codes=tuple(dict.fromkeys(explanations)),
        fingerprint=fingerprint,
    )


def _role_candidates(
    result: CapabilitySearchResult,
    role: str,
    *,
    include_unresolved: bool = True,
) -> tuple[SearchCandidate, ...]:
    pool = tuple(
        item for item in result.top_candidates
        if include_unresolved
        or item.eligibility == CandidateEligibility.ELIGIBLE.value
    )
    return tuple(
        item for item in pool if role in item.suggested_roles
    )


def _candidate_by_id(
    result: CapabilitySearchResult,
    capability_id: str,
) -> SearchCandidate | None:
    candidate = next((
        item for item in result.top_candidates
        if item.capability_id == capability_id
    ), None)
    if candidate is None or candidate.eligibility == CandidateEligibility.HARD_REJECTED.value:
        return None
    return candidate


def _best_verifiers(
    result: CapabilitySearchResult,
) -> tuple[SearchCandidate, ...]:
    return tuple(sorted(
        _role_candidates(result, StepRole.VERIFY.value, include_unresolved=False),
        key=lambda item: (
            -_VERIFY_STRENGTH.get(item.verifier_strength, 0),
            -item.soft_score,
            item.capability_id,
        ),
    ))


def _requires_separate_verifier(
    node: GoalNodeContract,
    act: SearchCandidate,
) -> bool:
    return bool(node.observable_postconditions) and act.verifier_strength != (
        VerifierStrength.EXACT_POSTCONDITION.value
    )


def _node_plans(
    context: ShadowPlanningContext,
    node: GoalNodeContract,
    result: CapabilitySearchResult,
    limits: PlannerLimits,
) -> tuple[AdaptivePlan, ...]:
    descriptors = _descriptor_map(context)
    plans: list[AdaptivePlan] = []
    act_candidates = _role_candidates(result, StepRole.ACT.value)
    verifier_candidates = _best_verifiers(result)
    observe_candidates = _role_candidates(
        result, StepRole.OBSERVE.value, include_unresolved=False
    )
    resolve_candidates = _role_candidates(
        result, StepRole.RESOLVE.value, include_unresolved=False
    )
    discover_candidates = _role_candidates(
        result, StepRole.DISCOVER.value, include_unresolved=False
    )

    for act in act_candidates[:limits.branch_width]:
        if any(item in _BLOCKING_UNRESOLVED for item in act.unresolved_reasons):
            continue
        descriptor = descriptors.get(act.capability_id)
        if descriptor is None:
            continue
        sequence: list[tuple[str, SearchCandidate]] = []
        unresolved = list(act.unresolved_reasons)
        explanations = list(act.explanation_codes)

        if "runtime_unknown" in unresolved and discover_candidates:
            sequence.append((StepRole.DISCOVER.value, discover_candidates[0]))
            unresolved.remove("runtime_unknown")
            explanations.append("discover_runtime_availability_before_act")

        if "requires_observation" in unresolved:
            if not observe_candidates:
                continue
            sequence.append((StepRole.OBSERVE.value, observe_candidates[0]))
            unresolved.remove("requires_observation")
            explanations.append("targeted_observation_before_actuation")

        if "target_missing_requires_observation" in unresolved:
            if not observe_candidates or not resolve_candidates:
                continue
            sequence.append((StepRole.OBSERVE.value, observe_candidates[0]))
            sequence.append((StepRole.RESOLVE.value, resolve_candidates[0]))
            unresolved.remove("target_missing_requires_observation")
            explanations.extend((
                "targeted_observation_before_resolution",
                "exact_resolution_before_actuation",
            ))

        if "stale_ref_requires_reresolution" in unresolved:
            stale = next((
                item for item in context.stale_references
                if item.capability_id in {act.capability_id, "*"}
            ), None)
            resolver = (
                _candidate_by_id(result, stale.safe_reresolution_capability_id)
                if stale is not None else None
            )
            if resolver is None or StepRole.RESOLVE.value not in resolver.suggested_roles:
                continue
            if all(item.capability_id != resolver.capability_id for _, item in sequence):
                sequence.append((StepRole.RESOLVE.value, resolver))
            unresolved.remove("stale_ref_requires_reresolution")
            explanations.append("stale_ref_requires_reresolution")

        sequence.append((StepRole.ACT.value, act))
        if _requires_separate_verifier(node, act):
            if not verifier_candidates:
                continue
            sequence.append((StepRole.VERIFY.value, verifier_candidates[0]))
            explanations.append("provider_success_not_exact_postcondition")

        if len(sequence) > limits.max_plan_steps:
            continue
        steps: list[PlanStep] = []
        previous: tuple[str, ...] = ()
        valid = True
        for index, (role, candidate) in enumerate(sequence, start=1):
            step_descriptor = descriptors.get(candidate.capability_id)
            if step_descriptor is None:
                valid = False
                break
            built = _step(node, candidate, step_descriptor, role, index, previous)
            steps.append(built)
            previous = (built.step_id,)
        if not valid:
            continue
        plans.append(_make_plan(
            context,
            tuple(steps),
            tuple(dict.fromkeys(f"{node.node_id}:{item}" for item in unresolved)),
            tuple(explanations),
        ))

    if not plans and any(
        item.outcome == "outcome_unknown"
        for item in context.previous_attempt_effects
    ):
        reconciler = next(iter(_role_candidates(
            result, StepRole.RECONCILE.value, include_unresolved=False
        )), None)
        if reconciler is not None:
            descriptor = descriptors.get(reconciler.capability_id)
            if descriptor is not None:
                step = _step(
                    node, reconciler, descriptor,
                    StepRole.RECONCILE.value, 1, (),
                )
                plans.append(_make_plan(
                    context,
                    (step,),
                    (f"{node.node_id}:mutation_reconsideration_requires_new_state",),
                    (
                        "outcome_unknown_replay_blocked",
                        "reconcile_before_reconsidering_mutation",
                    ),
                ))
    return tuple(plans[:limits.max_candidate_plans])


def _plan_quality(
    plan: AdaptivePlan,
    evidence: tuple[CapabilitySearchResult, ...],
) -> tuple[int, int, int, str]:
    candidates = {
        item.capability_id: item
        for result in evidence
        for item in (*result.eligible, *result.unresolved)
    }
    act_score = sum(
        candidates[item.capability_id].soft_score
        for item in plan.ordered_steps
        if item.role == StepRole.ACT.value and item.capability_id in candidates
    )
    verifier_strength = max((
        _VERIFY_STRENGTH.get(candidates[item.capability_id].verifier_strength, 0)
        for item in plan.ordered_steps
        if item.capability_id in candidates
    ), default=0)
    return (
        len(plan.unresolved_constraints),
        -verifier_strength,
        len(plan.ordered_steps) * 4 - act_score,
        plan.fingerprint,
    )


def _combine_node_plans(
    context: ShadowPlanningContext,
    per_node: tuple[tuple[AdaptivePlan, ...], ...],
    evidence: tuple[CapabilitySearchResult, ...],
    limits: PlannerLimits,
) -> tuple[AdaptivePlan, ...]:
    combinations = product(*(items[:limits.branch_width] for items in per_node))
    combined: list[AdaptivePlan] = []
    for choices in combinations:
        steps: list[PlanStep] = []
        unresolved: list[str] = []
        explanations: list[str] = []
        previous_last = ""
        previous_effect = ""
        for choice in choices:
            node_steps = list(choice.ordered_steps)
            if node_steps and previous_last:
                current_effect = node_steps[0].side_effect_class
                independent_reads = (
                    previous_effect == SideEffectClass.READ_ONLY.value
                    and current_effect == SideEffectClass.READ_ONLY.value
                )
                if not independent_reads:
                    node_steps[0] = replace(
                        node_steps[0],
                        dependency_ids=tuple(dict.fromkeys((
                            *node_steps[0].dependency_ids, previous_last,
                        ))),
                    )
                    explanations.append("mutation_ordering_preserved")
                else:
                    explanations.append("independent_read_only_nodes")
            steps.extend(node_steps)
            if node_steps:
                previous_last = node_steps[-1].step_id
                previous_effect = node_steps[-1].side_effect_class
            unresolved.extend(choice.unresolved_constraints)
            explanations.extend(choice.explanation_codes)
        if len(steps) > limits.max_plan_steps:
            continue
        if context.completed_goal_node_ids:
            explanations.append("completed_goal_nodes_preserved")
        combined.append(_make_plan(
            context,
            tuple(steps),
            tuple(dict.fromkeys(unresolved)),
            tuple(explanations),
        ))
        if len(combined) >= limits.max_candidate_plans:
            break
    combined.sort(key=lambda item: _plan_quality(item, evidence))
    fingerprints = tuple(item.fingerprint for item in combined)
    return tuple(
        replace(
            item,
            alternatives=tuple(value for value in fingerprints if value != item.fingerprint),
        )
        for item in combined
    )


def compare_legacy_snapshot(
    selected_plan: AdaptivePlan | None,
    snapshot: LegacyDecisionSnapshot | None,
    search_evidence: tuple[CapabilitySearchResult, ...],
    context: ShadowPlanningContext,
) -> LegacyComparison | None:
    if snapshot is None:
        return None
    if selected_plan is None:
        return LegacyComparison(
            same_first_mechanism=False,
            same_semantic_plan=False,
            relation_codes=("shadow_no_plan_after_full_scan",),
            classification=DivergenceClassification.UNRESOLVED.value,
        )
    first_act = next((
        item for item in selected_plan.ordered_steps
        if item.role == StepRole.ACT.value
    ), selected_plan.ordered_steps[0] if selected_plan.ordered_steps else None)
    if first_act is None:
        return LegacyComparison(
            False, False, ("shadow_has_no_first_mechanism",),
            DivergenceClassification.UNRESOLVED.value,
        )
    shadow_semantic = tuple(
        item.capability_id for item in selected_plan.ordered_steps
        if item.role in {StepRole.ACT.value, StepRole.RECONCILE.value}
    )
    same_first = first_act.capability_id == snapshot.first_mechanism_id
    same_semantic = bool(snapshot.semantic_plan) and shadow_semantic == snapshot.semantic_plan
    if same_first:
        return LegacyComparison(
            True,
            same_semantic,
            ("same_first_mechanism", "same_semantic_plan" if same_semantic else "semantic_plan_detail_differs"),
            DivergenceClassification.SEMANTICALLY_EQUIVALENT.value,
        )

    descriptor_ids = {item.capability_id for item in context.capabilities}
    if snapshot.first_mechanism_id not in descriptor_ids:
        return LegacyComparison(
            False,
            same_semantic,
            ("legacy_candidate_absent_from_descriptors",),
            DivergenceClassification.IMPOSSIBLE_TO_COMPARE.value,
        )
    rejected = next((
        item for result in search_evidence for item in result.hard_rejected
        if item.capability_id == snapshot.first_mechanism_id
    ), None)
    if rejected is not None:
        important = {
            "semantic_target_mismatch", "required_surface_mismatch",
            "application_mismatch", "account_mismatch",
            "personal_browser_authority_mismatch", "permission_disallowed",
            "outcome_unknown_replay_blocked", "modality_unavailable",
            "stale_ref_cannot_actuate", "required_postcondition_mismatch",
            "runtime_unavailable",
        }
        if important.intersection(rejected.hard_rejection_reasons):
            return LegacyComparison(
                False,
                same_semantic,
                ("different_first_mechanism", "shadow_rejects_false_equivalent_legacy_candidate", *rejected.hard_rejection_reasons),
                DivergenceClassification.PLAUSIBLY_BETTER.value,
            )

    descriptors = _descriptor_map(context)
    legacy_descriptor = descriptors.get(snapshot.first_mechanism_id)
    shadow_descriptor = descriptors.get(first_act.capability_id)
    if (
        legacy_descriptor is not None
        and shadow_descriptor is not None
        and legacy_descriptor.family == "vision_actuation_global"
        and shadow_descriptor.family != "vision_actuation_global"
        and not selected_plan.unresolved_constraints
    ):
        return LegacyComparison(
            False,
            same_semantic,
            ("different_first_mechanism", "global_coordinates_last_resort_avoided"),
            DivergenceClassification.PLAUSIBLY_BETTER.value,
        )

    if snapshot.frontier_capability_ids and first_act.capability_id not in snapshot.frontier_capability_ids:
        return LegacyComparison(
            False,
            same_semantic,
            ("shadow_capability_absent_from_legacy_frontier",),
            DivergenceClassification.UNRESOLVED.value,
        )
    candidate_lookup = {
        item.capability_id: item
        for result in search_evidence for item in (*result.eligible, *result.unresolved)
    }
    shadow_verifier = max((
        _VERIFY_STRENGTH.get(candidate_lookup[item.capability_id].verifier_strength, 0)
        for item in selected_plan.ordered_steps
        if item.capability_id in candidate_lookup
    ), default=0)
    legacy_verifier = _VERIFY_STRENGTH.get(snapshot.verifier_strength, 0)
    if (
        not selected_plan.unresolved_constraints
        and shadow_verifier > legacy_verifier
        and (snapshot.step_count <= 0 or len(selected_plan.ordered_steps) <= snapshot.step_count)
    ):
        return LegacyComparison(
            False,
            same_semantic,
            ("different_first_mechanism", "shadow_stronger_verification"),
            DivergenceClassification.PLAUSIBLY_BETTER.value,
        )
    if same_semantic:
        return LegacyComparison(
            False,
            True,
            ("different_first_mechanism", "same_semantic_plan"),
            DivergenceClassification.SEMANTICALLY_EQUIVALENT.value,
        )
    if selected_plan.unresolved_constraints:
        classification = (
            DivergenceClassification.WORSE.value
            if snapshot.first_mechanism_id in candidate_lookup
            and candidate_lookup[snapshot.first_mechanism_id].eligibility == CandidateEligibility.ELIGIBLE.value
            else DivergenceClassification.UNRESOLVED.value
        )
        return LegacyComparison(
            False,
            False,
            ("different_first_mechanism", "shadow_has_unresolved_constraints"),
            classification,
        )
    return LegacyComparison(
        False,
        False,
        ("different_first_mechanism", "semantic_equivalence_not_proven"),
        DivergenceClassification.UNRESOLVED.value,
    )


def plan_shadow(
    context: ShadowPlanningContext,
    *,
    limits: PlannerLimits | None = None,
    legacy_snapshot: LegacyDecisionSnapshot | None = None,
) -> ShadowPlanResult:
    """Build bounded shadow proposals; never return a production terminal state."""

    if not isinstance(context, ShadowPlanningContext):
        raise TypeError("context_must_be_shadow_planning_context")
    selected_limits = limits or PlannerLimits()
    search_limits = CapabilitySearchLimits(
        retrieval_limit=selected_limits.retrieval_limit,
        top_k=selected_limits.top_k,
    )
    remaining = tuple(
        node for node in context.goal_contract.nodes
        if node.node_id not in context.completed_goal_node_ids
    )
    if not remaining:
        plan = _make_plan(
            context, (), (), ("all_goal_nodes_already_completed",)
        )
        comparison = compare_legacy_snapshot(
            plan, legacy_snapshot, (), context
        )
        return ShadowPlanResult(
            candidate_plans=(plan,),
            selected_plan=plan,
            search_evidence=(),
            unresolved_state=(),
            legacy_comparison=comparison,
            diagnostics=("shadow_only", "no_dispatch", "no_terminality_mutation"),
        )

    evidence: list[CapabilitySearchResult] = []
    per_node: list[tuple[AdaptivePlan, ...]] = []
    for node in remaining:
        initial = search_capabilities(
            context, goal_node=node, limits=search_limits, full_scan=False
        )
        plans = _node_plans(context, node, initial, selected_limits)
        if not plans:
            full = search_capabilities(
                context, goal_node=node, limits=search_limits, full_scan=True
            )
            plans = _node_plans(context, node, full, selected_limits)
            evidence.append(full)
        else:
            evidence.append(initial)
        per_node.append(plans)

    evidence_tuple = tuple(evidence)
    candidate_plans: tuple[AdaptivePlan, ...] = ()
    if all(per_node):
        candidate_plans = _combine_node_plans(
            context, tuple(per_node), evidence_tuple, selected_limits
        )

    if not candidate_plans and any(
        not item.full_scan_performed for item in evidence_tuple
    ):
        evidence = []
        per_node = []
        for node in remaining:
            full = search_capabilities(
                context, goal_node=node, limits=search_limits, full_scan=True
            )
            evidence.append(full)
            per_node.append(_node_plans(
                context, node, full, selected_limits
            ))
        evidence_tuple = tuple(evidence)
        if all(per_node):
            candidate_plans = _combine_node_plans(
                context, tuple(per_node), evidence_tuple, selected_limits
            )

    selected = candidate_plans[0] if candidate_plans else None
    unresolved = (
        selected.unresolved_constraints
        if selected is not None
        else tuple(f"{node.node_id}:no_plan_found_after_full_scan" for node in remaining)
    )
    comparison = compare_legacy_snapshot(
        selected, legacy_snapshot, evidence_tuple, context
    )
    diagnostics = [
        "shadow_only",
        "bounded_top_k",
        "bounded_branch_width",
        "bounded_plan_steps",
        "no_executable_handles",
        "no_permit_creation",
        "no_dispatch",
        "no_persistence",
        "no_terminality_mutation",
    ]
    if selected is None:
        diagnostics.append("no_plan_found_after_full_scan")
    return ShadowPlanResult(
        candidate_plans=candidate_plans,
        selected_plan=selected,
        search_evidence=evidence_tuple,
        unresolved_state=unresolved,
        legacy_comparison=comparison,
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "AdaptivePlan",
    "ConfidenceClass",
    "DivergenceClassification",
    "LegacyComparison",
    "LegacyDecisionSnapshot",
    "PlanStep",
    "PlannerLimits",
    "ShadowPlanResult",
    "compare_legacy_snapshot",
    "plan_shadow",
]
