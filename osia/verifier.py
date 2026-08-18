"""Deterministic WOR-260 Phase-3 goal-node verifier.

Verification consumes only immutable goal/state/evidence descriptions.  It
does not call a model/provider, dispatch, grant permission, mutate real state,
or decide whole-turn terminality.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from osia.tool_contracts import SideEffectClass
from osia.world_state import (
    EffectState,
    EvidenceItem,
    EvidenceStaleState,
    WorldDelta,
    WorldStateView,
    goal_node_equivalence_class,
    postcondition_id,
)


_capability_adapters = importlib.import_module("osia.capability_adapters")
VerifierStrength = _capability_adapters.VerifierStrength
_goal_contract = importlib.import_module("osia.goal_contract")
GoalNodeContract = _goal_contract.GoalNodeContract
NOT_SPECIFIED = _goal_contract.NOT_SPECIFIED


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"
    REQUIRES_OBSERVATION = "requires_observation"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class VerificationResult:
    goal_node_id: str
    status: str
    satisfied_postcondition_ids: tuple[str, ...]
    unsatisfied_postcondition_ids: tuple[str, ...]
    contradicted_postcondition_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    verifier_strength: str
    required_observation_types: tuple[str, ...]
    explanation_codes: tuple[str, ...]
    world_delta: WorldDelta


_STRENGTH = {
    VerifierStrength.NONE.value: 0,
    VerifierStrength.WEAK_UNSTRUCTURED.value: 1,
    VerifierStrength.PROVIDER_REPORTED.value: 2,
    VerifierStrength.STRUCTURED_RESULT.value: 3,
    VerifierStrength.OBSERVABLE_STATE.value: 4,
    VerifierStrength.EXACT_POSTCONDITION.value: 5,
}
_CONTRADICTION_STATES = frozenset({
    "contradicted",
    "postcondition_absent",
    "target_not_observed",
    "required_postcondition_contradicted",
    "observable_state_mismatch",
})
_MUTATING_EFFECTS = frozenset({
    SideEffectClass.REVERSIBLE_LOCAL.value,
    SideEffectClass.PERSISTENT_LOCAL.value,
    SideEffectClass.EXTERNAL_MUTATION.value,
    SideEffectClass.IRREVERSIBLE_EXTERNAL.value,
    SideEffectClass.DYNAMICALLY_CLASSIFIED.value,
    SideEffectClass.UNKNOWN_HIGH_RISK.value,
})


def _normal(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _required_strength(node: GoalNodeContract, item: EvidenceItem) -> int:
    if (
        node.side_effect_class == SideEffectClass.READ_ONLY.value
        and item.observation_type in {
            "structured_read_exact", "exact_structured_result"
        }
    ):
        return _STRENGTH[VerifierStrength.STRUCTURED_RESULT.value]
    if node.side_effect_class in _MUTATING_EFFECTS:
        return _STRENGTH[VerifierStrength.OBSERVABLE_STATE.value]
    return _STRENGTH[VerifierStrength.STRUCTURED_RESULT.value]


def _required_observation_types(node: GoalNodeContract) -> tuple[str, ...]:
    surface = _normal(node.required_surface)
    application = _normal(node.application)
    if surface in {"browser", "web", "dom", "personal browser"}:
        return ("current_browser_document_state",)
    if surface not in {"", NOT_SPECIFIED, "unknown"} or application not in {
        "", NOT_SPECIFIED, "unknown"
    }:
        return ("current_application_surface_state",)
    if node.side_effect_class == SideEffectClass.READ_ONLY.value:
        return ("exact_structured_read_result",)
    return ("observable_postcondition_state",)


def _relevant_evidence(
    node: GoalNodeContract,
    evidence: Iterable[EvidenceItem],
) -> tuple[EvidenceItem, ...]:
    required = {
        postcondition_id(node.node_id, item.description)
        for item in node.observable_postconditions
    }
    return tuple(
        item for item in evidence
        if item.goal_node_id in {"", node.node_id}
        and (
            not item.supported_postcondition_ids
            or bool(required.intersection(item.supported_postcondition_ids))
            or item.goal_node_id == node.node_id
        )
    )


def verify_goal_node(
    goal_node_contract: GoalNodeContract,
    world_state: WorldStateView,
    evidence: Iterable[EvidenceItem] = (),
) -> VerificationResult:
    """Verify one node without inventing missing state or finalizing its turn."""

    if not isinstance(goal_node_contract, GoalNodeContract):
        raise TypeError("goal_node_contract_required")
    if not isinstance(world_state, WorldStateView):
        raise TypeError("world_state_required")
    if world_state.goal_contract_fingerprint == "":
        raise ValueError("world_state_goal_fingerprint_required")

    supplied = tuple(evidence) or world_state.typed_evidence
    relevant = _relevant_evidence(goal_node_contract, supplied)
    required_ids = tuple(
        postcondition_id(goal_node_contract.node_id, item.description)
        for item in goal_node_contract.observable_postconditions
    )
    satisfied = {
        item for item in required_ids
        if item in world_state.verified_postconditions
    }
    contradicted = {
        item for item in required_ids
        if item in world_state.contradicted_postconditions
    }
    supporting: set[str] = set()
    strongest = VerifierStrength.NONE.value
    strongest_rank = 0

    latest_revision_by_postcondition = {
        identity: max(
            (
                item.evidence_revision for item in relevant
                if item.stale_state != EvidenceStaleState.STALE.value
                and identity in item.supported_postcondition_ids
            ),
            default=-1,
        )
        for identity in required_ids
    }
    for item in relevant:
        if item.stale_state == EvidenceStaleState.STALE.value:
            continue
        item_ids = set(item.supported_postcondition_ids).intersection(
            required_ids
        )
        if not item_ids:
            continue
        item_ids = {
            identity for identity in item_ids
            if item.evidence_revision >= latest_revision_by_postcondition[identity]
        }
        if not item_ids:
            continue
        normalized_state = _normal(item.observed_state).replace(" ", "_")
        if normalized_state in _CONTRADICTION_STATES:
            contradicted.update(item_ids)
            supporting.add(item.evidence_id)
            continue
        rank = _STRENGTH.get(item.verifier_strength, 0)
        if rank < _required_strength(goal_node_contract, item):
            continue
        satisfied.update(item_ids)
        supporting.add(item.evidence_id)
        if rank > strongest_rank:
            strongest_rank = rank
            strongest = item.verifier_strength

    # A current contradiction wins over an older success token.  A current
    # exact proof naturally removes the contradiction when callers rebuild the
    # immutable view at the next evidence revision.
    satisfied.difference_update(contradicted)
    equivalence = goal_node_equivalence_class(goal_node_contract)
    relevant_unknown = any(
        item.effect_state == EffectState.OUTCOME_UNKNOWN.value
        and item.goal_node_id in {"", goal_node_contract.node_id}
        for item in relevant
    ) or equivalence in world_state.outcome_unknown_equivalence_classes
    latest_unknown_revision = max(
        (
            item.evidence_revision for item in relevant
            if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
            and item.goal_node_id in {"", goal_node_contract.node_id}
        ),
        default=(world_state.evidence_revision if relevant_unknown else -1),
    )
    exact_final_state = bool(required_ids) and all(
        any(
            identity in item.supported_postcondition_ids
            and item.stale_state != EvidenceStaleState.STALE.value
            and item.verifier_strength
            == VerifierStrength.EXACT_POSTCONDITION.value
            and item.evidence_revision >= latest_unknown_revision
            and _normal(item.observed_state).replace(" ", "_")
            not in _CONTRADICTION_STATES
            for item in relevant
        )
        for identity in required_ids
    )
    unsatisfied = tuple(
        item for item in required_ids
        if item not in satisfied and item not in contradicted
    )
    explanations: list[str] = ["deterministic_verifier"]
    observations: tuple[str, ...] = ()

    if (
        relevant_unknown
        and goal_node_contract.side_effect_class in _MUTATING_EFFECTS
        and not exact_final_state
    ):
        status = VerificationStatus.OUTCOME_UNKNOWN.value
        explanations.extend((
            "possible_effect_requires_reconciliation",
            "absence_of_error_is_not_success",
        ))
    elif contradicted:
        status = VerificationStatus.CONTRADICTED.value
        explanations.append("observable_postcondition_contradicted")
    elif required_ids and len(satisfied) == len(required_ids):
        status = VerificationStatus.VERIFIED.value
        explanations.append("all_node_postconditions_verified")
        if relevant_unknown and exact_final_state:
            explanations.append("exact_final_state_resolves_unknown_effect")
    elif (
        not required_ids
        and goal_node_contract.node_id in world_state.completed_goal_node_ids
    ):
        status = VerificationStatus.VERIFIED.value
        explanations.append("canonical_node_completion_preserved")
    elif unsatisfied:
        observations = _required_observation_types(goal_node_contract)
        status = VerificationStatus.REQUIRES_OBSERVATION.value
        explanations.extend((
            "postcondition_not_yet_observed",
            "provider_or_dispatch_success_not_goal_proof",
        ))
    else:
        status = VerificationStatus.UNVERIFIED.value
        explanations.append("insufficient_verification_evidence")

    newly_verified = tuple(
        item for item in sorted(satisfied)
        if item not in world_state.verified_postconditions
    )
    newly_contradicted = tuple(
        item for item in sorted(contradicted)
        if item not in world_state.contradicted_postconditions
    )
    learned = bool(newly_verified or newly_contradicted)
    delta = WorldDelta(
        from_revision=world_state.revision,
        to_revision=world_state.revision + (1 if learned else 0),
        newly_verified_goal_nodes=(
            (goal_node_contract.node_id,)
            if required_ids and len(satisfied) == len(required_ids)
            and goal_node_contract.node_id not in world_state.completed_goal_node_ids
            else ()
        ),
        newly_verified_postconditions=newly_verified,
        newly_contradicted_postconditions=newly_contradicted,
        newly_invalidated_refs=tuple(sorted(
            item.capability_id for item in relevant
            if item.stale_state == EvidenceStaleState.STALE.value
            and item.capability_id
        )),
        resolved_unknown_effects=tuple(sorted(dict.fromkeys((
            *(
                item.source_attempt_id for item in relevant
                if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
                and item.source_attempt_id
            ),
            *(
                item.attempt_id for item in world_state.known_effect_states
                if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
                and item.equivalence_class == equivalence
                and item.attempt_id
            ),
        )))) if status == VerificationStatus.VERIFIED.value else (),
        supporting_evidence_ids=tuple(sorted(supporting)),
    )
    return VerificationResult(
        goal_node_id=goal_node_contract.node_id,
        status=status,
        satisfied_postcondition_ids=tuple(sorted(satisfied)),
        unsatisfied_postcondition_ids=unsatisfied,
        contradicted_postcondition_ids=tuple(sorted(contradicted)),
        supporting_evidence_ids=tuple(sorted(supporting)),
        verifier_strength=strongest,
        required_observation_types=observations,
        explanation_codes=tuple(dict.fromkeys(explanations)),
        world_delta=delta,
    )


__all__ = [
    "VerificationResult",
    "VerificationStatus",
    "verify_goal_node",
]
