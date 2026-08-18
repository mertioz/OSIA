from __future__ import annotations

from dataclasses import replace

import pytest

from benchmarks.wor260.phase2 import fixture_goal_contract, fixture_node
from osia.capability_adapters import VerifierStrength
from osia.tool_contracts import SideEffectClass
from osia.verifier import VerificationStatus, verify_goal_node
from osia.world_state import (
    DispatchState,
    EffectState,
    EvidenceItem,
    EvidenceStaleState,
    WorldStateInputs,
    build_world_state_view,
    postcondition_id,
)


def _world(node, *, inputs=None):
    contract = fixture_goal_contract(node)
    return build_world_state_view(
        contract,
        terminality_snapshot={
            "turn_id": "turn-verifier", "proven_postconditions": {},
            "typed_evidence": (), "active_attempts": {},
            "permission_state": "allowed", "evidence_revision": 2,
        },
        strategy_snapshot={
            "attempts": (), "mechanisms": (),
            "alternatives_still_applicable": (),
            "mission": {"action_nodes": ()},
        },
        inputs=inputs,
    )


def _evidence(node, *, strength, observed="verified", stale="fresh", effect="effect_confirmed"):
    identity = postcondition_id(node.node_id, node.observable_postconditions[0].description)
    return EvidenceItem(
        evidence_id=f"evidence:{strength}:{observed}:{stale}",
        source_type="fixture", source_attempt_id="attempt-1",
        goal_node_id=node.node_id, capability_id="fixture:capability",
        capability_family="direct_api", observation_type="structured_read_exact",
        verifier_strength=strength, semantic_effect=node.objective,
        observed_state=observed, supported_postcondition_ids=(identity,),
        dispatch_state=DispatchState.COMPLETED.value, effect_state=effect,
        stale_state=stale, evidence_revision=2,
    )


def test_provider_reported_success_is_not_goal_success_for_mutation():
    node = fixture_node(
        "update", "Update title", "title", "notes", "editor",
        side_effect=SideEffectClass.PERSISTENT_LOCAL.value,
        postcondition="title equals Phase 3",
    )
    world = _world(node)
    evidence = _evidence(node, strength=VerifierStrength.PROVIDER_REPORTED.value)

    result = verify_goal_node(node, world, (evidence,))

    assert result.status == VerificationStatus.REQUIRES_OBSERVATION.value
    assert result.satisfied_postcondition_ids == ()
    assert "provider_or_dispatch_success_not_goal_proof" in result.explanation_codes


@pytest.mark.parametrize("strength", [
    VerifierStrength.OBSERVABLE_STATE.value,
    VerifierStrength.EXACT_POSTCONDITION.value,
])
def test_current_observable_or_exact_evidence_verifies_mutation(strength):
    node = fixture_node(
        "update", "Update title", "title", "notes", "editor",
        side_effect=SideEffectClass.PERSISTENT_LOCAL.value,
        postcondition="title equals Phase 3",
    )
    world = _world(node)

    result = verify_goal_node(node, world, (_evidence(node, strength=strength),))

    assert result.status == VerificationStatus.VERIFIED.value
    assert result.world_delta.newly_verified_goal_nodes == ("update",)
    assert result.world_delta.to_revision == world.revision + 1


def test_exact_structured_read_can_verify_read_only_node():
    node = fixture_node(
        "read", "Read title", "title", "notes", "editor",
        postcondition="title equals Phase 3",
    )
    result = verify_goal_node(
        node,
        _world(node),
        (_evidence(node, strength=VerifierStrength.STRUCTURED_RESULT.value),),
    )
    assert result.status == VerificationStatus.VERIFIED.value


def test_current_contradiction_wins_over_success_evidence():
    node = fixture_node(
        "read", "Read title", "title", "notes", "editor",
        postcondition="title equals Phase 3",
    )
    good = _evidence(node, strength=VerifierStrength.EXACT_POSTCONDITION.value)
    contradiction = replace(
        good, evidence_id="evidence:contradiction",
        observed_state="observable_state_mismatch",
    )

    result = verify_goal_node(node, _world(node), (good, contradiction))

    assert result.status == VerificationStatus.CONTRADICTED.value
    assert result.satisfied_postcondition_ids == ()
    assert result.contradicted_postcondition_ids


def test_stale_evidence_does_not_verify_and_requests_fresh_observation():
    node = fixture_node(
        "read", "Read title", "title", "notes", "editor",
        postcondition="title equals Phase 3",
    )
    stale = _evidence(
        node, strength=VerifierStrength.EXACT_POSTCONDITION.value,
        stale=EvidenceStaleState.STALE.value,
    )

    result = verify_goal_node(node, _world(node), (stale,))

    assert result.status == VerificationStatus.REQUIRES_OBSERVATION.value
    assert result.world_delta.newly_invalidated_refs == ("fixture:capability",)


def test_outcome_unknown_preempts_mutation_replay_or_false_success():
    node = fixture_node(
        "send", "Send message", "message", "mail", "inbox",
        side_effect=SideEffectClass.EXTERNAL_MUTATION.value,
        postcondition="message is present",
    )
    unknown = _evidence(
        node, strength=VerifierStrength.PROVIDER_REPORTED.value,
        effect=EffectState.OUTCOME_UNKNOWN.value,
    )

    result = verify_goal_node(node, _world(node), (unknown,))

    assert result.status == VerificationStatus.OUTCOME_UNKNOWN.value
    assert "possible_effect_requires_reconciliation" in result.explanation_codes


def test_newer_stale_evidence_does_not_suppress_fresh_contradiction():
    node = fixture_node(
        "read", "Read title", "title", "notes", "editor",
        postcondition="title equals Phase 3",
    )
    contradiction = replace(
        _evidence(node, strength=VerifierStrength.EXACT_POSTCONDITION.value),
        evidence_id="fresh-contradiction", evidence_revision=2,
        observed_state="observable_state_mismatch",
    )
    newer_stale = replace(
        _evidence(
            node, strength=VerifierStrength.EXACT_POSTCONDITION.value,
            stale=EvidenceStaleState.STALE.value,
        ),
        evidence_id="newer-stale", evidence_revision=9,
    )

    result = verify_goal_node(node, _world(node), (contradiction, newer_stale))

    assert result.status == VerificationStatus.CONTRADICTED.value
