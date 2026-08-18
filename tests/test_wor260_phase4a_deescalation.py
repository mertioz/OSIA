from benchmarks.wor260.phase4a import reasoning_scenarios
from osia.adaptive_reasoning import DifficultyClass, determine_reasoning_decision


def _decision(identity):
    scenario = next(item for item in reasoning_scenarios() if item.scenario_id == identity)
    return scenario, determine_reasoning_decision(scenario.context, scenario.capability)


def test_resolved_ambiguity_deescalates_from_previous_high_decision():
    scenario, decision = _decision("deescalate_one_exact_candidate")
    assert scenario.context.previous_reasoning_decision.difficulty_class == DifficultyClass.HIGH
    assert decision.difficulty_class in {DifficultyClass.TRIVIAL, DifficultyClass.LOW}
    assert "ambiguity_resolved" in decision.deescalation_reason_codes


def test_resolved_contradiction_is_not_sticky():
    _, decision = _decision("deescalate_contradiction_resolved")
    assert decision.selected_osia_level == "auto"
    assert "strong_verified_state" in decision.deescalation_reason_codes


def test_resolved_stale_reference_returns_to_low_or_default():
    scenario, decision = _decision("deescalate_stale_ref_resolved")
    assert scenario.context.stale_ref_count == 0
    assert decision.selected_osia_level in {
        "auto", scenario.capability.adaptive_values[0].osia_level,
    }


def test_reconciled_outcome_unknown_does_not_retain_critical_effort():
    _, decision = _decision("deescalate_outcome_reconciled")
    assert decision.difficulty_class == DifficultyClass.TRIVIAL
    assert "outcome_unknown_reconciliation" not in decision.escalation_reason_codes


def test_clear_progress_emits_structured_deescalation_reason():
    _, decision = _decision("deescalate_one_simple_node")
    assert "progress_reduced_complexity" in decision.deescalation_reason_codes
    assert "simple_remaining_goal" in decision.deescalation_reason_codes
