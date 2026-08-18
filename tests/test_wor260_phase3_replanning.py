from __future__ import annotations

from dataclasses import fields

from benchmarks.wor260.phase2 import (
    fixture_descriptor,
    fixture_goal_contract,
    fixture_node,
)
from osia.adaptive_planner import LegacyDecisionSnapshot
from osia.adaptive_runtime import evaluate_adaptive_continuation
from osia.capability_adapters import (
    CapabilityFamily,
    RuntimeAvailability,
    VerifierStrength,
)
from osia.tool_contracts import SideEffectClass
from osia.verifier import verify_goal_node
from osia.world_state import WorldStateInputs, build_world_state_view


def test_phase3_ab_benchmark_preserves_phase2_and_runs_84_total_scenarios():
    from benchmarks.wor260.phase3 import benchmark_scenarios, run_phase3_benchmark

    report = run_phase3_benchmark()
    metrics = report.metrics
    categories = {
        category for item in benchmark_scenarios() for category in item.categories
    }

    assert metrics.phase2_scenario_count == 43
    assert metrics.phase3_scenario_count == 41
    assert metrics.total_scenario_count == 84
    assert {
        "verification", "state_change", "partial_completion",
        "cross_mechanism", "permissions", "identity", "fallback", "restart",
    }.issubset(categories)
    assert metrics.on_semantic_fidelity_rate >= metrics.off_semantic_fidelity_rate
    assert metrics.false_success_count == 0
    assert metrics.incorrect_blocked_count == 0
    assert metrics.hard_constraint_violation_count == 0
    assert metrics.permission_violation_count == 0
    assert metrics.identity_violation_count == 0
    assert metrics.outcome_unknown_replay_violation_count == 0
    assert metrics.modality_violation_count == 0
    assert metrics.completed_node_replay_count == 0
    assert metrics.direct_dispatch_violation_count == 0
    assert metrics.terminality_bypass_violation_count == 0
    assert metrics.unnecessary_fallback_count == 0
    assert metrics.outcome_unknown_reconciliation_count == 6
    assert metrics.restart_rebuild_correct_count == 3


def test_phase3_benchmark_does_not_invent_provider_or_cost_claims():
    from benchmarks.wor260.phase3 import Phase3BenchmarkMetrics

    names = {item.name for item in fields(Phase3BenchmarkMetrics)}
    assert {
        "provider_reliability_gain", "latency_savings", "token_savings",
        "provider_cost_savings", "production_success_rate",
    }.isdisjoint(names)


def _snapshots(*, frontier, failed=(), proven=()):
    attempts = tuple({
        "attempt_id": f"attempt-{index}",
        "node_id": "read",
        "mechanism_id": capability_id,
        "status": "failed",
        "failure_or_stagnation_reason": "confirmed failure before effect",
    } for index, capability_id in enumerate(failed))
    return (
        {
            "turn_id": "turn-replan",
            "proven_postconditions": {
                item: f"proof:{index}" for index, item in enumerate(proven)
            },
            "typed_evidence": (), "active_attempts": {},
            "permission_state": "allowed", "evidence_revision": 3,
        },
        {
            "attempts": attempts, "mechanisms": (),
            "alternatives_still_applicable": tuple(frontier),
            "mission": {"action_nodes": ()},
        },
    )


def _legacy(*frontier):
    return LegacyDecisionSnapshot(
        first_mechanism_id=frontier[0] if frontier else "",
        first_family="existing_execution_strategy",
        semantic_plan=tuple(frontier), step_count=1,
        verifier_strength=VerifierStrength.NONE.value,
        frontier_capability_ids=tuple(frontier),
    )


def _read_descriptor(capability_id, *, target="item", application="catalog", surface="details"):
    return fixture_descriptor(
        capability_id, CapabilityFamily.DIRECT_API.value, "act",
        target=target, application=application, surface=surface,
        effect=f"read {target}", exact_postcondition=f"{target} is visible",
        verifier=VerifierStrength.EXACT_POSTCONDITION.value,
    )


def test_failed_before_effect_capability_is_replaced_by_remaining_alternative():
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots(
        frontier=("api:failed", "api:remaining"), failed=("api:failed",),
    )
    world = build_world_state_view(
        contract, terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
        ),
    )

    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world),
        (_read_descriptor("api:failed"), _read_descriptor("api:remaining")),
        _legacy("api:failed", "api:remaining"),
    )

    assert proposal.fallback_to_legacy is False
    assert proposal.selected_next_capability == "api:remaining"


def test_completed_first_node_is_not_replayed_and_second_node_is_selected():
    first = fixture_node(
        "first", "Read first", "first", "catalog", "details",
        postcondition="first is visible",
    )
    second = fixture_node(
        "second", "Read second", "second", "catalog", "details",
        postcondition="second is visible",
    )
    contract = fixture_goal_contract(first, second)
    terminality, strategy = _snapshots(
        frontier=("api:first", "api:second"), proven=("first is visible",),
    )
    world = build_world_state_view(
        contract, terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
        ),
    )
    descriptors = (
        _read_descriptor("api:first", target="first"),
        _read_descriptor("api:second", target="second"),
    )

    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(second, world), descriptors,
        _legacy("api:first", "api:second"),
    )

    assert world.completed_goal_node_ids == ("first",)
    assert proposal.fallback_to_legacy is False
    assert proposal.selected_next_capability == "api:second"
    assert all(
        step.goal_node_id != "first"
        for step in proposal.selected_plan.ordered_steps
    )


def test_newly_verified_active_node_advances_to_third_node_without_replay():
    nodes = tuple(
        fixture_node(
            name, f"Read {name}", name, "catalog", "details",
            postcondition=f"{name} is visible",
        )
        for name in ("first", "second", "third")
    )
    contract = fixture_goal_contract(*nodes)
    terminality, strategy = _snapshots(
        frontier=("api:second", "api:third"), proven=("first is visible",),
    )
    world = build_world_state_view(
        contract, terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
        ),
    )
    from osia.world_state import EvidenceItem, postcondition_id
    identity = postcondition_id("second", "second is visible")
    evidence = EvidenceItem(
        evidence_id="exact-second", source_type="fixture",
        source_attempt_id="attempt-second", goal_node_id="second",
        capability_id="api:second", capability_family="direct_api",
        observation_type="structured_read_exact",
        verifier_strength=VerifierStrength.EXACT_POSTCONDITION.value,
        semantic_effect="read second", observed_state="verified",
        supported_postcondition_ids=(identity,), dispatch_state="completed",
        effect_state="read_only", stale_state="fresh", evidence_revision=3,
    )
    verification = verify_goal_node(nodes[1], world, (evidence,))

    proposal = evaluate_adaptive_continuation(
        contract, world, verification,
        (
            _read_descriptor("api:second", target="second"),
            _read_descriptor("api:third", target="third"),
        ),
        _legacy("api:second", "api:third"),
    )

    assert verification.status == "verified"
    assert proposal.fallback_to_legacy is False
    assert proposal.selected_next_capability == "api:third"
    assert {step.goal_node_id for step in proposal.selected_plan.ordered_steps} == {"third"}


def test_unavailable_runtime_descriptor_never_becomes_selected_next_action():
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots(frontier=("api:down", "api:up"))
    world = build_world_state_view(
        contract, terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
        ),
    )
    down = fixture_descriptor(
        "api:down", CapabilityFamily.DIRECT_API.value, "act",
        target="item", application="catalog", surface="details",
        effect="read item", availability=RuntimeAvailability.UNAVAILABLE.value,
        exact_postcondition="item is visible",
    )

    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world),
        (down, _read_descriptor("api:up")), _legacy("api:down", "api:up"),
    )

    assert proposal.selected_next_capability == "api:up"
    assert proposal.selected_next_capability != "api:down"


def test_required_account_unknown_falls_back_instead_of_guessing_identity():
    node = fixture_node(
        "read", "Read private item", "item", "catalog", "details",
        account="profile-a", postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots(frontier=("api:read",))
    world = build_world_state_view(
        contract, terminality_snapshot=terminality,
        strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
        ),
    )

    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world),
        (_read_descriptor("api:read"),), _legacy("api:read"),
    )

    assert proposal.fallback_to_legacy is True
    assert "account_identity_unresolved" in proposal.explanation_codes
