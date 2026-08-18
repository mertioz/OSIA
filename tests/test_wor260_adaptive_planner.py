from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError, fields

import pytest

import osia.adaptive_planner as planner_module
import osia.capability_search as search_module
from benchmarks.wor260.phase2 import benchmark_scenarios
from osia.adaptive_planner import (
    DivergenceClassification,
    PlannerLimits,
    plan_shadow,
)


def _scenario(scenario_id: str):
    return next(
        item for item in benchmark_scenarios()
        if item.scenario_id == scenario_id
    )


def _plan(scenario_id: str):
    scenario = _scenario(scenario_id)
    return plan_shadow(
        scenario.context,
        limits=scenario.limits,
        legacy_snapshot=scenario.legacy_snapshot,
    )


def test_exact_one_step_capability_produces_immutable_shadow_plan():
    result = _plan("direct_api_exact_vs_ui")
    assert result.selected_plan is not None
    assert len(result.selected_plan.ordered_steps) == 1
    assert result.selected_plan.ordered_steps[0].capability_id == "api:invoice_42"
    with pytest.raises(FrozenInstanceError):
        result.selected_plan.revision = 2  # type: ignore[misc]


def test_discover_then_act_resolves_unknown_runtime_frontier():
    result = _plan("discover_unresolved_runtime_frontier")
    assert result.selected_plan is not None
    assert [item.role for item in result.selected_plan.ordered_steps] == [
        "discover", "act",
    ]
    assert result.selected_plan.unresolved_constraints == ()
    assert "discover_runtime_availability_before_act" in (
        result.selected_plan.explanation_codes
    )


def test_observe_then_act_template_is_supported_without_general_htn():
    result = _plan("observe_then_act")
    assert result.selected_plan is not None
    assert [item.role for item in result.selected_plan.ordered_steps] == [
        "observe", "act",
    ]


def test_target_missing_plan_is_observe_resolve_act_verify():
    result = _plan("vision_observation_then_uia_action")
    assert result.selected_plan is not None
    assert [item.role for item in result.selected_plan.ordered_steps] == [
        "observe", "resolve", "act", "verify",
    ]
    for previous, current in zip(
        result.selected_plan.ordered_steps,
        result.selected_plan.ordered_steps[1:],
    ):
        assert previous.step_id in current.dependency_ids


def test_nonvision_target_missing_returns_no_plan_only_after_full_scan():
    result = _plan("target_missing_nonvision")
    assert result.selected_plan is None
    assert result.search_evidence
    assert all(item.full_scan_performed for item in result.search_evidence)
    assert "no_plan_found_after_full_scan" in result.diagnostics
    assert all("blocked_verified" not in item for item in result.diagnostics)


def test_stale_ref_plan_resolves_before_acting_and_never_directly_acts():
    result = _plan("desktop_stale_ref_resolve")
    assert result.selected_plan is not None
    assert [item.role for item in result.selected_plan.ordered_steps] == [
        "resolve", "act",
    ]
    assert result.selected_plan.ordered_steps[0].capability_id == "uia:resolve_save"

    blocked = _plan("stale_ref_no_direct_actuation")
    assert blocked.selected_plan is None


def test_outcome_unknown_builds_reconcile_only_and_never_replays_mutation():
    result = _plan("outcome_unknown_blocks_mutation_replay")
    assert result.selected_plan is not None
    assert [item.role for item in result.selected_plan.ordered_steps] == ["reconcile"]
    assert all(
        item.capability_id != "api:send_message"
        for item in result.selected_plan.ordered_steps
    )
    assert "reconcile_before_reconsidering_mutation" in (
        result.selected_plan.explanation_codes
    )


def test_permission_required_and_unknown_remain_unresolved_without_grant():
    for scenario_id, marker in (
        ("permission_required", "n1:permission_required"),
        ("permission_unknown", "n1:permission_unknown"),
    ):
        result = _plan(scenario_id)
        assert result.selected_plan is not None
        assert marker in result.selected_plan.unresolved_constraints
        assert "no_permit_creation" in result.diagnostics


def test_disallowed_permission_has_no_plan_after_full_scan():
    result = _plan("permission_disallowed")
    assert result.selected_plan is None
    assert all(item.full_scan_performed for item in result.search_evidence)


def test_provider_success_alone_adds_exact_verifier():
    result = _plan("provider_success_needs_exact_verify")
    assert result.selected_plan is not None
    assert [item.role for item in result.selected_plan.ordered_steps] == [
        "act", "verify",
    ]
    assert "provider_success_not_exact_postcondition" in (
        result.selected_plan.explanation_codes
    )
    assert result.selected_plan.ordered_steps[-1].capability_id == "api:verify_title"


def test_exact_verifier_beats_provider_report_when_otherwise_equivalent():
    result = _plan("exact_postcondition_verifier_preferred")
    assert result.selected_plan is not None
    verifier = next(
        item for item in result.selected_plan.ordered_steps
        if item.role == "verify"
    )
    assert verifier.capability_id == "api:observable_verify_title"


def test_multi_node_planning_preserves_completion_and_mutation_order():
    multi = _plan("read_decide_mutate")
    assert multi.selected_plan is not None
    assert [item.goal_node_id for item in multi.selected_plan.ordered_steps] == [
        "n1", "n2", "n3",
    ]

    completed = _plan("completed_node_preserved")
    assert completed.selected_plan is not None
    assert {item.goal_node_id for item in completed.selected_plan.ordered_steps} == {"n2"}
    assert "completed_goal_nodes_preserved" in completed.selected_plan.explanation_codes

    mutations = _plan("mutation_ordering_preserved")
    assert mutations.selected_plan is not None
    first, second = mutations.selected_plan.ordered_steps
    assert first.step_id in second.dependency_ids
    assert "mutation_ordering_preserved" in mutations.selected_plan.explanation_codes


def test_independent_read_nodes_have_no_artificial_dependency():
    result = _plan("independent_read_only_nodes")
    assert result.selected_plan is not None
    first, second = result.selected_plan.ordered_steps
    assert first.step_id not in second.dependency_ids
    assert "independent_read_only_nodes" in result.selected_plan.explanation_codes


def test_planner_limits_are_bounded_and_are_not_terminality_limits():
    scenario = _scenario("read_decide_mutate")
    result = plan_shadow(
        scenario.context,
        limits=PlannerLimits(
            top_k=2,
            retrieval_limit=2,
            branch_width=1,
            max_plan_steps=2,
            max_candidate_plans=1,
        ),
    )
    assert result.selected_plan is None
    assert all(item.full_scan_performed for item in result.search_evidence)
    assert not hasattr(PlannerLimits, "max_turns")
    assert not hasattr(PlannerLimits, "terminal_state")


def test_candidate_plans_and_branches_respect_all_bounds():
    scenario = _scenario("direct_api_exact_vs_ui")
    limits = PlannerLimits(
        top_k=2,
        retrieval_limit=3,
        branch_width=2,
        max_plan_steps=3,
        max_candidate_plans=2,
    )
    result = plan_shadow(scenario.context, limits=limits)
    assert len(result.candidate_plans) <= limits.max_candidate_plans
    assert all(len(item.ordered_steps) <= limits.max_plan_steps for item in result.candidate_plans)
    assert all(len(evidence.top_candidates) <= limits.top_k for evidence in result.search_evidence)


def test_legacy_divergences_are_deterministically_classified():
    better = _plan("steam_library_vs_store").legacy_comparison
    assert better is not None
    assert better.classification == DivergenceClassification.PLAUSIBLY_BETTER.value
    assert "shadow_rejects_false_equivalent_legacy_candidate" in better.relation_codes

    global_route = _plan("global_coordinates_last_resort").legacy_comparison
    assert global_route is not None
    assert global_route.classification == (
        DivergenceClassification.PLAUSIBLY_BETTER.value
    )
    assert "global_coordinates_last_resort_avoided" in global_route.relation_codes

    equivalent = _plan("provider_native_not_artificially_demoted").legacy_comparison
    assert equivalent is not None
    assert equivalent.classification == (
        DivergenceClassification.SEMANTICALLY_EQUIVALENT.value
    )

    impossible = _plan("full_scan_recovery").legacy_comparison
    assert impossible is not None
    assert impossible.classification == (
        DivergenceClassification.IMPOSSIBLE_TO_COMPARE.value
    )


def test_shadow_structures_have_no_private_reasoning_permit_or_executable_fields():
    prohibited = {
        "chain_of_thought", "private_reasoning", "reasoning_trace",
        "permit", "permission_grant", "tool_call", "executable_handle",
        "terminal_state",
    }
    for record in (
        planner_module.PlanStep,
        planner_module.AdaptivePlan,
        planner_module.ShadowPlanResult,
        search_module.SearchCandidate,
        search_module.CapabilitySearchResult,
    ):
        assert prohibited.isdisjoint({item.name for item in fields(record)})


def test_shadow_modules_do_not_import_live_engine_or_protected_runtime_seams():
    prohibited_imports = {
        "osia.agent",
        "osia.execution_strategy",
        "osia.turn_terminality",
        "osia.server.server",
        "osia.action_policy",
        "osia.tools.browser",
    }
    for module in (planner_module, search_module):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert prohibited_imports.isdisjoint(imported)


def test_shadow_modules_contain_no_discovery_network_registration_or_persistence_calls():
    source = inspect.getsource(search_module) + inspect.getsource(planner_module)
    for prohibited in (
        "requests.", "httpx.", "socket.", "subprocess.",
        "register_tool", "register_capability", "write_text(",
        "write_bytes(", "open(", "unlink(", "mkdir(",
    ):
        assert prohibited not in source
