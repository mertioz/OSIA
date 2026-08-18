from dataclasses import FrozenInstanceError, replace

import pytest

from benchmarks.wor260.phase4a import reasoning_scenarios
from osia.adaptive_reasoning import (
    DifficultyClass,
    ProviderParameterBehavior,
    ReasoningPhase,
    build_reasoning_decision_context,
    determine_reasoning_decision,
    public_structure_field_names,
)
from osia.adaptive_planner import AdaptivePlan, PlanStep, ShadowPlanResult
from osia.verifier import VerificationResult
from osia.world_state import (
    StateFact,
    StateSummary,
    StrategySnapshotView,
    WorldDelta,
    WorldStateView,
)


def _scenario(identity):
    return next(item for item in reasoning_scenarios() if item.scenario_id == identity)


def test_context_and_decision_are_immutable_and_have_no_private_reasoning_fields():
    scenario = _scenario("simple_direct_api")
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    with pytest.raises(FrozenInstanceError):
        scenario.context.candidate_count = 9
    with pytest.raises(FrozenInstanceError):
        decision.selected_osia_level = "high"
    names = public_structure_field_names()
    flattened = " ".join((*names["context"], *names["decision"]))
    assert "chain_of_thought" not in flattened
    assert "reasoning_text" not in flattened
    assert "provider_payload" not in flattened


def test_exact_registry_capability_preserves_declared_native_mapping():
    scenario = _scenario("simple_direct_api")
    capability = scenario.capability
    assert capability.provider_id == "openai"
    assert capability.model_id == "gpt-5.5"
    assert capability.supported_osia_values == (
        "auto", "off", "low", "medium", "high", "xhigh",
    )
    assert capability.value_for("low").native_value == "low"
    assert capability.value_for("low").provider_parameter_name == "reasoning_effort"


def test_simple_exact_state_uses_provider_default_without_escalation():
    scenario = _scenario("simple_direct_api")
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.difficulty_class == DifficultyClass.TRIVIAL
    assert decision.selected_osia_level == "auto"
    assert decision.selected_native_value is None
    assert decision.provider_parameter_behavior == ProviderParameterBehavior.OMIT_PROVIDER_DEFAULT
    assert "exact_direct_capability" in decision.deescalation_reason_codes


def test_context_validation_rejects_negative_counts():
    scenario = _scenario("simple_direct_api")
    with pytest.raises(ValueError, match="counts_must_be_non_negative"):
        replace(scenario.context, replan_count=-1)


def test_difficulty_changes_only_cognitive_budget_fields():
    simple = _scenario("simple_direct_api")
    critical = _scenario("critical_equivalent_mutations")
    low_decision = determine_reasoning_decision(simple.context, simple.capability)
    high_decision = determine_reasoning_decision(
        replace(
            critical.context,
            provider_id=simple.capability.provider_id,
            model_id=simple.capability.model_id,
            supported_reasoning_values=simple.capability.supported_osia_values,
            provider_default_behavior=simple.capability.provider_default_behavior,
        ),
        simple.capability,
    )
    fields = public_structure_field_names()["decision"]
    assert "permission" not in " ".join(fields)
    assert "terminal" not in " ".join(fields)
    assert "dispatch" not in " ".join(fields)
    assert low_decision.selected_osia_level != high_decision.selected_osia_level


def test_phase3_adapter_projects_real_world_verifier_and_plan_objects():
    scenario = _scenario("simple_direct_api")
    step = PlanStep(
        step_id="step-1",
        goal_node_id="node-1",
        role="act",
        capability_id="api:exact",
        family="direct_api",
        expected_semantic_effect="read:item",
        preconditions=(),
        expected_world_delta_description="item observed",
        required_postcondition_or_verifier="pc-1",
        side_effect_class="read_only",
        dependency_ids=(),
        fallback_eligible=False,
        explanation_codes=("exact_semantic_match",),
    )
    plan = AdaptivePlan(
        goal_contract_fingerprint="goal-1",
        revision=1,
        ordered_steps=(step,),
        expected_final_postconditions=("pc-1",),
        unresolved_constraints=(),
        alternatives=(),
        confidence_class="high",
        explanation_codes=("single_exact_candidate",),
        fingerprint="plan-1",
    )
    shadow = ShadowPlanResult(
        candidate_plans=(plan,),
        selected_plan=plan,
        search_evidence=(),
        unresolved_state=(),
        legacy_comparison=None,
        diagnostics=(),
    )
    world = WorldStateView(
        schema_version="1.0",
        turn_id="turn-1",
        goal_contract_fingerprint="goal-1",
        active_goal_node_id="node-1",
        completed_goal_node_ids=(),
        remaining_goal_node_ids=("node-1",),
        current_application="catalogue",
        current_surface="items",
        current_account_profile="account-1",
        browser_state_summary=StateSummary(status="available"),
        desktop_state_summary=StateSummary(status="available"),
        effective_model_capabilities=("text",),
        vision_available=False,
        permission_states=(),
        active_attempt_ids=(),
        pending_permission_ids=(),
        known_effect_states=(),
        outcome_unknown_equivalence_classes=(),
        stale_refs=(),
        accessibility_quality="rich",
        typed_observations=(StateFact("application", "catalogue", "fixture"),),
        typed_evidence=(),
        verified_postconditions=(),
        remaining_postconditions=("pc-1",),
        contradicted_postconditions=(),
        strategy_snapshot=StrategySnapshotView(
            current_mechanism="api:exact",
            current_layer=0,
            frontier_capability_ids=("api:exact",),
            attempted_capability_ids=(),
            failed_before_effect_capability_ids=(),
            stale_capability_ids=(),
            frozen_reason="",
            terminal_blocker="",
        ),
        available_strategy_families=("direct_api",),
        revision=1,
        evidence_revision=1,
    )
    verification = VerificationResult(
        goal_node_id="node-1",
        status="verified",
        satisfied_postcondition_ids=("pc-1",),
        unsatisfied_postcondition_ids=(),
        contradicted_postcondition_ids=(),
        supporting_evidence_ids=("evidence-1",),
        verifier_strength="exact_postcondition",
        required_observation_types=(),
        explanation_codes=("verified",),
        world_delta=WorldDelta(
            from_revision=1,
            to_revision=2,
            newly_verified_postconditions=("pc-1",),
        ),
    )
    context = build_reasoning_decision_context(
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        user_reasoning_mode="auto",
        phase=ReasoningPhase.POST_ACTION_VERIFICATION,
        capability=scenario.capability,
        world_state=world,
        verification_result=verification,
        shadow_plan_result=shadow,
    )
    assert context.candidate_count == 1
    assert context.exact_direct_capability is True
    assert context.semantic_effect_exact is True
    assert context.surface_account_exact is True
    assert context.progress_signal.value == "clear"
    decision = determine_reasoning_decision(context, scenario.capability)
    assert decision.selected_osia_level == "auto"
