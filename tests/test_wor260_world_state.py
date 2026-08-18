from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict

import pytest

from benchmarks.wor260.phase2 import fixture_goal_contract, fixture_node
from osia.world_state import (
    EffectState,
    EffectStateFact,
    StateSummary,
    UNKNOWN_STATE,
    WorldStateInputs,
    build_world_state_view,
    goal_node_equivalence_class,
    postcondition_id,
)


def _snapshots(*, proven=(), typed_evidence=(), attempts=()):
    terminality = {
        "turn_id": "turn-phase3-fixture",
        "proven_postconditions": {item: f"proof:{index}" for index, item in enumerate(proven)},
        "typed_evidence": tuple(typed_evidence),
        "active_attempts": {},
        "permission_state": "allowed",
        "evidence_revision": 4,
    }
    strategy = {
        "attempts": tuple(attempts),
        "mechanisms": (),
        "alternatives_still_applicable": ("api:read", "browser:read"),
        "mission": {"action_nodes": ()},
    }
    return terminality, strategy


def test_world_state_is_immutable_explicit_and_reconstructable():
    node = fixture_node(
        "read", "Read the selected item", "item-42", "catalog", "details",
        postcondition="item-42 is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots()
    inputs = WorldStateInputs(
        current_application="catalog",
        current_surface="details",
        current_account_profile="profile-a",
        browser_state_summary=StateSummary(
            authority="browser", identity="tab-7", surface="details",
            epoch="9", freshness="fresh", status="ready",
        ),
        effective_model_capabilities=("text",),
        vision_available=False,
        revision=3,
    )

    first = build_world_state_view(
        contract,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=inputs,
    )
    rebuilt = build_world_state_view(
        contract,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=inputs,
    )

    assert first == rebuilt
    assert asdict(first) == asdict(rebuilt)
    assert first.active_goal_node_id == "read"
    assert first.remaining_postconditions == (
        postcondition_id("read", "item-42 is visible"),
    )
    with pytest.raises(FrozenInstanceError):
        first.revision = 99


def test_unknown_state_and_partial_completion_are_never_invented():
    first = fixture_node(
        "first", "Read first", "first", "catalog", "details",
        postcondition="first is visible",
    )
    second = fixture_node(
        "second", "Read second", "second", "catalog", "details",
        postcondition="second is visible",
    )
    contract = fixture_goal_contract(first, second)
    terminality, strategy = _snapshots(proven=("first is visible",))

    world = build_world_state_view(
        contract,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
    )

    assert world.current_application == UNKNOWN_STATE
    assert world.current_surface == UNKNOWN_STATE
    assert world.current_account_profile == UNKNOWN_STATE
    assert world.completed_goal_node_ids == ("first",)
    assert world.active_goal_node_id == "second"
    assert world.remaining_goal_node_ids == ("second",)


def test_outcome_unknown_is_normalized_to_one_cross_mechanism_class():
    node = fixture_node(
        "send", "Send the message", "message-7", "mail", "inbox",
        side_effect="external_mutation", postcondition="message-7 is present",
    )
    contract = fixture_goal_contract(node)
    equivalence = goal_node_equivalence_class(node)
    terminality, strategy = _snapshots()
    inputs = WorldStateInputs(known_effect_states=(
        EffectStateFact(
            "attempt-1", "api:send", equivalence,
            EffectState.OUTCOME_UNKNOWN.value, "send",
        ),
        EffectStateFact(
            "attempt-2", "browser:send", equivalence,
            EffectState.OUTCOME_UNKNOWN.value, "send",
        ),
    ))

    world = build_world_state_view(
        contract,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=inputs,
    )

    assert world.outcome_unknown_equivalence_classes == (equivalence,)
    assert {item.capability_id for item in world.known_effect_states} == {
        "api:send", "browser:send",
    }


def test_world_state_retains_only_normalized_evidence_not_raw_payloads():
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots(
        typed_evidence=({
            "outcome": "success_verified",
            "attempt_id": "attempt-1",
            "postcondition": "item is visible",
            "evidence_token": "opaque-token",
            "reason_code": "exact structured read",
            "raw_provider_payload": "secret-provider-body",
        },),
        attempts=({
            "attempt_id": "attempt-1", "node_id": "read",
            "mechanism_id": "api:read", "status": "succeeded",
        },),
    )

    world = build_world_state_view(
        contract,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
    )
    rendered = repr(world)

    assert "secret-provider-body" not in rendered
    assert "opaque-token" not in rendered
    assert world.typed_evidence
    assert world.typed_evidence[0].evidence_id.startswith("evidence_")


def test_duplicate_postcondition_text_is_not_assigned_to_arbitrary_node():
    first = fixture_node(
        "first", "Read first", "first", "catalog", "details",
        postcondition="selection is visible",
    )
    second = fixture_node(
        "second", "Read second", "second", "catalog", "details",
        postcondition="selection is visible",
    )
    contract = fixture_goal_contract(first, second)
    terminality, strategy = _snapshots(proven=("selection is visible",))

    world = build_world_state_view(
        contract, terminality_snapshot=terminality,
        strategy_snapshot=strategy,
    )

    assert world.verified_postconditions == ()
    assert world.completed_goal_node_ids == ()
    assert world.remaining_goal_node_ids == ("first", "second")
