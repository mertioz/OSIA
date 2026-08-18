from __future__ import annotations

import pytest

from benchmarks.wor260.phase2 import (
    fixture_descriptor,
    fixture_goal_contract,
    fixture_node,
)
from osia.adaptive_planner import LegacyDecisionSnapshot
from osia.adaptive_runtime import evaluate_adaptive_continuation
from osia.capability_adapters import CapabilityFamily, VerifierStrength
from osia.tool_contracts import SideEffectClass
from osia.verifier import VerificationStatus, verify_goal_node
from osia.world_state import (
    EffectState,
    EffectStateFact,
    WorldStateInputs,
    build_world_state_view,
    goal_node_equivalence_class,
)


def _fixture(source_capability: str):
    node = fixture_node(
        "send", "Send message", "message", "mail", "inbox",
        side_effect=SideEffectClass.EXTERNAL_MUTATION.value,
        postcondition="message is present",
    )
    contract = fixture_goal_contract(node)
    equivalence = goal_node_equivalence_class(node)
    frontier = ("api:send", "browser:send", "desktop:send")
    world = build_world_state_view(
        contract,
        terminality_snapshot={
            "turn_id": "turn-unknown", "proven_postconditions": {},
            "typed_evidence": (), "active_attempts": {},
            "permission_state": "allowed", "evidence_revision": 7,
        },
        strategy_snapshot={
            "attempts": (), "mechanisms": (),
            "alternatives_still_applicable": frontier,
            "mission": {"action_nodes": ()},
        },
        inputs=WorldStateInputs(
            current_application="mail", current_surface="inbox",
            known_effect_states=(EffectStateFact(
                "attempt-unknown", source_capability, equivalence,
                EffectState.OUTCOME_UNKNOWN.value, "send",
            ),),
        ),
    )
    mutators = tuple(
        fixture_descriptor(
            capability_id,
            CapabilityFamily.DIRECT_API.value
            if capability_id.startswith("api") else
            CapabilityFamily.BROWSER_DOM.value
            if capability_id.startswith("browser") else
            CapabilityFamily.DESKTOP_UIA.value,
            "act", target="message", application="mail", surface="inbox",
            effect="send message", side_effect=SideEffectClass.EXTERNAL_MUTATION.value,
            equivalence_class=equivalence,
            verifier=VerifierStrength.OBSERVABLE_STATE.value,
        )
        for capability_id in frontier
    )
    reconcile = fixture_descriptor(
        "reconcile:mail", CapabilityFamily.DIRECT_API.value, "reconcile",
        target="message", application="mail", surface="inbox",
        effect="reconcile unknown effect", side_effect=SideEffectClass.READ_ONLY.value,
        equivalence_class=equivalence,
        verifier=VerifierStrength.OBSERVABLE_STATE.value,
    )
    legacy = LegacyDecisionSnapshot(
        first_mechanism_id=frontier[1], first_family="browser_dom",
        semantic_plan=(frontier[1],), step_count=2,
        verifier_strength=VerifierStrength.PROVIDER_REPORTED.value,
        frontier_capability_ids=frontier,
    )
    return node, contract, world, (*mutators, reconcile), legacy


@pytest.mark.parametrize("source", ["api:send", "browser:send", "desktop:send"])
def test_unknown_effect_blocks_all_equivalent_mutation_routes(source):
    node, contract, world, descriptors, legacy = _fixture(source)
    verification = verify_goal_node(node, world)

    proposal = evaluate_adaptive_continuation(
        contract, world, verification, descriptors, legacy,
    )

    assert verification.status == VerificationStatus.OUTCOME_UNKNOWN.value
    assert proposal.fallback_to_legacy is False
    assert proposal.selected_next_capability == "reconcile:mail"
    assert proposal.selected_plan.ordered_steps[0].role == "reconcile"
    assert not any(
        step.capability_id in {"api:send", "browser:send", "desktop:send"}
        for step in proposal.selected_plan.ordered_steps
    )
    assert source in proposal.do_not_replay_capability_ids


def test_unknown_effect_without_read_only_reconciliation_falls_back_safely():
    node, contract, world, descriptors, legacy = _fixture("api:send")
    mutators_only = tuple(
        item for item in descriptors if item.capability_id != "reconcile:mail"
    )

    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), mutators_only, legacy,
    )

    assert proposal.fallback_to_legacy is True
    assert proposal.selected_next_capability == ""
    assert "no_safe_adaptive_candidate" in proposal.explanation_codes
