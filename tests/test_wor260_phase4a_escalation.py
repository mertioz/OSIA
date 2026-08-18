from dataclasses import replace

from benchmarks.wor260.phase4a import reasoning_scenarios
from osia.adaptive_reasoning import (
    DifficultyClass,
    determine_reasoning_decision,
)


def _decision(identity):
    scenario = next(item for item in reasoning_scenarios() if item.scenario_id == identity)
    return scenario, determine_reasoning_decision(scenario.context, scenario.capability)


def test_multiple_close_candidates_escalate_to_moderate_effort():
    _, decision = _decision("moderate_two_candidates")
    assert decision.difficulty_class == DifficultyClass.MEDIUM
    assert "multiple_close_candidates" in decision.escalation_reason_codes


def test_contradictory_evidence_reaches_high_but_not_unconditionally_max():
    scenario, decision = _decision("high_contradictory_observations")
    assert decision.difficulty_class == DifficultyClass.HIGH
    assert "contradictory_evidence" in decision.escalation_reason_codes
    assert decision.selected_osia_level == "high"
    assert decision.selected_osia_level in scenario.capability.supported_osia_values


def test_complex_outcome_unknown_reconciliation_uses_highest_supported_value():
    scenario, decision = _decision("critical_equivalent_mutations")
    assert decision.difficulty_class == DifficultyClass.CRITICAL_RECONCILIATION
    assert decision.selected_osia_level == scenario.capability.adaptive_values[-1].osia_level
    assert "outcome_unknown_reconciliation" in decision.escalation_reason_codes


def test_repeated_replanning_never_exceeds_declared_capability():
    scenario, decision = _decision("high_repeated_replan")
    assert decision.selected_osia_level in scenario.capability.supported_osia_values
    assert scenario.capability.value_for(decision.selected_osia_level) is not None


def test_complexity_cannot_invoke_provider_or_dispatch(monkeypatch):
    scenario = next(
        item for item in reasoning_scenarios()
        if item.scenario_id == "critical_contradictory_reconciliation"
    )
    monkeypatch.setattr(
        "osia.reasoning.build_reasoning_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider call")),
    )
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.selected_osia_level in scenario.capability.supported_osia_values


def test_permission_signal_can_raise_review_effort_but_creates_no_authority():
    scenario = next(item for item in reasoning_scenarios() if item.scenario_id == "simple_direct_api")
    decision = determine_reasoning_decision(
        replace(scenario.context, unresolved_permission_count=1),
        scenario.capability,
    )
    assert "unresolved_permission_state" in decision.escalation_reason_codes
    assert not hasattr(decision, "permission_granted")
    assert not hasattr(decision, "account_binding")
    assert not hasattr(decision, "vision_enabled")
    assert not hasattr(decision, "terminal_status")
    assert not hasattr(decision, "dispatch")


def test_shadow_policy_cannot_persist_reasoning_settings(monkeypatch):
    scenario = next(item for item in reasoning_scenarios() if item.scenario_id == "high_close_candidates")
    monkeypatch.setattr(
        "osia.config.set_reasoning_settings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("persistence")),
    )
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.adaptive_allowed is True
