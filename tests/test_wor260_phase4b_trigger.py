from __future__ import annotations

from benchmarks.wor260.phase4b import CriticUsefulness, critic_scenarios
from osia.adaptive_critic import CriticTriggerClass, determine_critic_trigger


def _scenario(name: str):
    return next(item for item in critic_scenarios() if item.scenario_id == name)


def test_all_required_and_optional_scenarios_trigger() -> None:
    useful = (
        item
        for item in critic_scenarios()
        if item.usefulness != CriticUsefulness.UNNECESSARY
    )
    for scenario in useful:
        decision = determine_critic_trigger(scenario.context)
        assert decision.should_invoke_critic
        assert decision.trigger_class in scenario.acceptable_trigger_classes
        assert decision.request_scope


def test_close_viable_plans_trigger_plan_ambiguity() -> None:
    decision = determine_critic_trigger(_scenario("close_two_tied_routes").context)
    assert decision.trigger_class == CriticTriggerClass.PLAN_AMBIGUITY
    assert "no_deterministic_preference" in decision.reason_codes


def test_unresolved_evidence_conflict_triggers_evidence_review() -> None:
    decision = determine_critic_trigger(
        _scenario("provider_success_state_contradiction").context
    )
    assert decision.trigger_class == CriticTriggerClass.EVIDENCE_CONTRADICTION


def test_complex_unknown_effect_triggers_read_only_reconciliation_review() -> None:
    decision = determine_critic_trigger(
        _scenario("unknown_effect_multiple_read_routes").context
    )
    assert decision.trigger_class == CriticTriggerClass.DIFFICULT_RECONCILIATION
    assert "multiple_legal_read_only_observations" in decision.reason_codes


def test_irreversible_review_requires_multiple_legal_close_plans() -> None:
    decision = determine_critic_trigger(_scenario("irreversible_legal_choice").context)
    assert decision.trigger_class == CriticTriggerClass.IRREVERSIBLE_ACTION_REVIEW


def test_repeated_replanning_and_pre_block_are_distinct_triggers() -> None:
    repeated = determine_critic_trigger(
        _scenario("repeated_replans_world_changes").context
    )
    pre_block = determine_critic_trigger(
        _scenario("pre_block_obscure_supplied_candidate").context
    )
    assert repeated.trigger_class == CriticTriggerClass.REPEATED_REPLANNING
    assert pre_block.trigger_class == CriticTriggerClass.PRE_BLOCKED_REVIEW


def test_trigger_revision_is_monotonic_but_has_no_state_mutation() -> None:
    scenario = _scenario("close_surface_preserving_plans")
    decision = determine_critic_trigger(scenario.context)
    assert decision.critic_revision == scenario.context.prior_critic_revision + 1
    assert scenario.context.prior_critic_revision == 0
