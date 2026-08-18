from __future__ import annotations

from benchmarks.wor260.phase2 import (
    fixture_descriptor,
    fixture_goal_contract,
    fixture_node,
)
from osia.adaptive_planner import LegacyDecisionSnapshot
from osia.adaptive_runtime import evaluate_adaptive_continuation
from osia.capability_adapters import CapabilityFamily, VerifierStrength
from osia.verifier import verify_goal_node
from osia.world_state import WorldStateInputs, build_world_state_view


def _case(*, account="not_specified", current_account="unknown"):
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        account=account, postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    world = build_world_state_view(
        contract,
        terminality_snapshot={
            "turn_id": "turn-fallback", "proven_postconditions": {},
            "typed_evidence": (), "active_attempts": {},
            "permission_state": "allowed", "evidence_revision": 1,
        },
        strategy_snapshot={
            "attempts": (), "mechanisms": (),
            "alternatives_still_applicable": ("api:read",),
            "mission": {"action_nodes": ()},
        },
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
            current_account_profile=current_account,
        ),
    )
    descriptor = fixture_descriptor(
        "api:read", CapabilityFamily.DIRECT_API.value, "act",
        target="item", application="catalog", surface="details",
        account=account, effect="read item",
        exact_postcondition="item is visible",
        verifier=VerifierStrength.EXACT_POSTCONDITION.value,
    )
    legacy = LegacyDecisionSnapshot(
        first_mechanism_id="api:read", first_family="direct_api",
        semantic_plan=("api:read",), step_count=1,
        verifier_strength=VerifierStrength.EXACT_POSTCONDITION.value,
        frontier_capability_ids=("api:read",),
    )
    return node, contract, world, descriptor, legacy


def test_missing_descriptors_falls_back_to_legacy():
    node, contract, world, _, legacy = _case()
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), (), legacy,
    )
    assert proposal.fallback_to_legacy is True
    assert "missing_capability_metadata" in proposal.explanation_codes


def test_malformed_descriptors_fall_back_without_exception():
    node, contract, world, _, legacy = _case()
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), (object(),), legacy,
    )
    assert proposal.fallback_to_legacy is True
    assert "malformed_capability_metadata" in proposal.explanation_codes


def test_planner_exception_falls_back_without_changing_legacy_snapshot(monkeypatch):
    node, contract, world, descriptor, legacy = _case()

    def explode(*args, **kwargs):
        raise RuntimeError("fixture planner failure")

    monkeypatch.setattr("osia.adaptive_runtime.plan_shadow", explode)
    before = repr(legacy)
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), (descriptor,), legacy,
    )

    assert repr(legacy) == before
    assert proposal.fallback_to_legacy is True
    assert "planner_failed" in proposal.explanation_codes


def test_unresolved_required_account_falls_back_without_guessing():
    node, contract, world, descriptor, legacy = _case(account="profile-a")
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), (descriptor,), legacy,
    )
    assert proposal.fallback_to_legacy is True
    assert "account_identity_unresolved" in proposal.explanation_codes


def test_verification_node_mismatch_is_safe_legacy_fallback():
    node, contract, world, descriptor, legacy = _case()
    other = fixture_node(
        "other", "Read other", "other", "catalog", "details",
        postcondition="other is visible",
    )
    other_world = build_world_state_view(
        fixture_goal_contract(other),
        terminality_snapshot={
            "turn_id": "turn-other", "proven_postconditions": {},
            "typed_evidence": (), "active_attempts": {},
            "permission_state": "allowed", "evidence_revision": 1,
        },
        strategy_snapshot={
            "attempts": (), "mechanisms": (),
            "alternatives_still_applicable": (),
            "mission": {"action_nodes": ()},
        },
    )
    mismatched = verify_goal_node(other, other_world)

    proposal = evaluate_adaptive_continuation(
        contract, world, mismatched, (descriptor,), legacy,
    )

    assert proposal.fallback_to_legacy is True
    assert "verification_node_mismatch" in proposal.explanation_codes
