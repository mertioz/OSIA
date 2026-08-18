from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from osia.execution_strategy import (
    SemanticActionNode,
    SemanticMission,
    derive_semantic_mission,
)
from osia.goal_contract import (
    ConditionOperator,
    GoalContractValidationError,
    NOT_SPECIFIED,
    ObservablePostcondition,
    StateCondition,
    goal_contract_errors,
    goal_contract_from_semantic_mission,
    validate_goal_contract,
)
from osia.tool_contracts import SideEffectClass


def _manual_node(action: str, fingerprint: str, *, order: int) -> SemanticActionNode:
    return SemanticActionNode(
        target_object=f"target-{action}",
        required_application="",
        required_surface="",
        required_account="",
        requested_action=action,
        observable_success_postcondition=f"{action} is observably complete",
        fingerprint=fingerprint,
        requires_execution=True,
        required_postconditions=(f"{action} is observably complete",),
        node_id=f"node-{order}",
        order=order,
        source_clause=f"{action} target",
    )


def test_conversion_is_deterministic_immutable_and_does_not_mutate_source():
    mission = derive_semantic_mission(
        "Open Steam and then search for Subnautica"
    )
    before = mission.public_dict()

    first = goal_contract_from_semantic_mission(mission)
    second = goal_contract_from_semantic_mission(mission)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert mission.public_dict() == before
    assert json.loads(json.dumps(first.to_dict()))["fingerprint"] == first.fingerprint
    with pytest.raises(FrozenInstanceError):
        first.root_objective = "changed"
    with pytest.raises(FrozenInstanceError):
        first.nodes[0].target = "changed"


def test_runtime_status_and_evidence_do_not_change_canonical_fingerprint():
    mission = derive_semantic_mission("Search Subnautica inside Steam Library")
    baseline = goal_contract_from_semantic_mission(mission)
    mission.action_nodes[0].status = "succeeded"
    mission.action_nodes[0].evidence_links = ("runtime:evidence",)

    after_runtime_updates = goal_contract_from_semantic_mission(mission)

    assert after_runtime_updates.fingerprint == baseline.fingerprint
    assert after_runtime_updates == baseline


def test_node_order_and_legacy_fingerprint_are_preserved():
    mission = derive_semantic_mission("Open Steam and then open Spotify")
    contract = goal_contract_from_semantic_mission(mission)

    assert contract.legacy_fingerprint == mission.fingerprint
    assert [item.node_id for item in contract.nodes] == [
        item.node_id for item in mission.action_nodes
    ]
    assert [item.objective for item in contract.nodes] == [
        item.source_clause for item in mission.action_nodes
    ]


def test_account_profile_is_preserved_but_never_invented():
    unspecified = goal_contract_from_semantic_mission(
        derive_semantic_mission("Search for invoice 42")
    )
    generic = goal_contract_from_semantic_mission(
        derive_semantic_mission("Search my account for invoice 42")
    )

    assert unspecified.nodes[0].required_account_profile == NOT_SPECIFIED
    assert generic.nodes[0].required_account_profile == "user_account"
    assert "@" not in generic.nodes[0].required_account_profile


def test_per_node_side_effects_distinguish_read_local_and_external_actions():
    mission = SemanticMission(
        objective="Search, open, and send",
        action_nodes=(
            _manual_node("search", "read", order=0),
            _manual_node("open", "local", order=1),
            _manual_node("send", "external", order=2),
        ),
        fingerprint="legacy-mixed",
    )

    contract = goal_contract_from_semantic_mission(mission)

    assert [item.side_effect_class for item in contract.nodes] == [
        SideEffectClass.READ_ONLY.value,
        SideEffectClass.REVERSIBLE_LOCAL.value,
        SideEffectClass.EXTERNAL_MUTATION.value,
    ]


def test_required_and_forbidden_same_state_is_rejected():
    contract = goal_contract_from_semantic_mission(
        derive_semantic_mission("Open Steam at my Library")
    )
    state = StateCondition(
        "surface", ConditionOperator.EQUALS.value, "library", "steam"
    )
    contradictory_node = replace(
        contract.nodes[0],
        desired_state=(state,),
        forbidden_false_success_states=(state,),
    )
    contradictory = replace(contract, nodes=(contradictory_node,))

    assert "required_state_is_also_forbidden" in goal_contract_errors(contradictory)
    with pytest.raises(GoalContractValidationError) as exc:
        validate_goal_contract(contradictory)
    assert "required_state_is_also_forbidden" in exc.value.errors


def test_duplicate_identity_invalid_effect_and_empty_postcondition_are_rejected():
    contract = goal_contract_from_semantic_mission(
        derive_semantic_mission("Open Steam and then open Spotify")
    )
    empty = ObservablePostcondition(
        "", NOT_SPECIFIED, NOT_SPECIFIED, NOT_SPECIFIED, NOT_SPECIFIED
    )
    invalid_first = replace(
        contract.nodes[0],
        side_effect_class="made_up_safe_effect",
        observable_postconditions=(empty,),
    )
    duplicate_second = replace(contract.nodes[1], node_id=invalid_first.node_id)
    invalid = replace(contract, nodes=(invalid_first, duplicate_second))

    errors = goal_contract_errors(invalid)
    assert "duplicate_node_identity" in errors
    assert "invalid_side_effect_class" in errors
    assert "postcondition_has_no_semantic_meaning" in errors


def test_steam_library_and_store_have_distinct_false_success_surfaces():
    library = goal_contract_from_semantic_mission(
        derive_semantic_mission("Search Subnautica inside Steam Library")
    )
    store = goal_contract_from_semantic_mission(
        derive_semantic_mission("Search Subnautica inside Steam Store")
    )

    assert library.fingerprint != store.fingerprint
    assert library.nodes[0].required_surface == "library"
    assert store.nodes[0].required_surface == "store"
    assert library.nodes[0].forbidden_false_success_states[0].value == "library"
    assert store.nodes[0].forbidden_false_success_states[0].value == "store"
    assert all(
        item.operator == ConditionOperator.NOT_EQUALS.value
        for item in (
            *library.nodes[0].forbidden_false_success_states,
            *store.nodes[0].forbidden_false_success_states,
        )
    )
