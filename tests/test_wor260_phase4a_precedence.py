from dataclasses import replace

from benchmarks.wor260.phase4a import reasoning_scenarios
from osia.adaptive_reasoning import (
    ProviderParameterBehavior,
    determine_reasoning_decision,
)


def _scenario(identity):
    return next(item for item in reasoning_scenarios() if item.scenario_id == identity)


def test_turn_level_reasoning_wins_over_complexity_and_saved_setting():
    scenario = _scenario("critical_equivalent_mutations")
    context = replace(
        scenario.context,
        explicit_turn_reasoning="low",
        user_reasoning_mode="high",
    )
    decision = determine_reasoning_decision(context, scenario.capability)
    assert decision.selected_osia_level == "low"
    assert decision.adaptive_allowed is False
    assert "explicit_user_override" in decision.escalation_reason_codes


def test_explicit_model_or_provider_setting_wins_when_turn_has_no_directive():
    scenario = _scenario("override_provider_setting")
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.selected_osia_level == "high"
    assert decision.adaptive_allowed is False


def test_explicit_supported_custom_value_is_preserved_exactly():
    scenario = _scenario("override_supported_custom")
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.selected_osia_level == "ultra"
    assert decision.selected_native_value == "ultra"
    assert decision.adaptive_allowed is False


def test_existing_none_alias_maps_only_to_declared_off_value():
    scenario = _scenario("override_existing_none_alias")
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.selected_osia_level == "off"
    assert decision.selected_native_value == "none"


def test_unsupported_explicit_value_is_blocked_without_silent_remap():
    scenario = _scenario("simple_direct_api")
    decision = determine_reasoning_decision(
        replace(scenario.context, explicit_turn_reasoning="ultra"),
        scenario.capability,
    )
    assert decision.adaptive_allowed is False
    assert decision.selected_osia_level is None
    assert decision.selected_native_value is None
    assert decision.unsupported_reason == "unsupported_explicit_reasoning"
    assert decision.provider_parameter_behavior == ProviderParameterBehavior.OVERRIDE_BLOCKED


def test_explicit_auto_enables_adaptive_policy_instead_of_freezing_a_level():
    scenario = _scenario("high_close_candidates")
    decision = determine_reasoning_decision(
        replace(scenario.context, explicit_turn_reasoning="auto"),
        scenario.capability,
    )
    assert decision.adaptive_allowed is True
    assert decision.selected_osia_level != "auto"
