from __future__ import annotations

from benchmarks.wor260.phase4b import CriticUsefulness, critic_scenarios
from osia.adaptive_critic import CriticTriggerClass, determine_critic_trigger


def _scenario(name: str):
    return next(item for item in critic_scenarios() if item.scenario_id == name)


def test_all_oracle_unnecessary_scenarios_are_suppressed() -> None:
    unnecessary = (
        item
        for item in critic_scenarios()
        if item.usefulness == CriticUsefulness.UNNECESSARY
    )
    for scenario in unnecessary:
        decision = determine_critic_trigger(scenario.context)
        assert not decision.should_invoke_critic
        assert decision.trigger_class == CriticTriggerClass.NONE
        assert decision.suppression_codes


def test_exact_direct_and_single_candidate_are_suppressed() -> None:
    for name in ("exact_direct_api", "exact_deep_link", "one_clear_candidate"):
        assert not determine_critic_trigger(_scenario(name).context).should_invoke_critic


def test_exact_verifier_resolution_suppresses_contradiction() -> None:
    decision = determine_critic_trigger(
        _scenario("verifier_resolves_provider_contradiction").context
    )
    assert not decision.should_invoke_critic
    assert "deterministic_verifier_already_resolved" in decision.suppression_codes


def test_hard_permission_identity_modality_and_runtime_rejections_suppress() -> None:
    names = (
        "permission_hard_reject",
        "account_mismatch_hard_reject",
        "nonvision_modality_hard_reject",
        "runtime_capability_unavailable",
    )
    for name in names:
        decision = determine_critic_trigger(_scenario(name).context)
        assert not decision.should_invoke_critic
        assert any("hard_rejection" in code for code in decision.suppression_codes)


def test_unknown_effect_mutation_replay_is_never_reconsidered() -> None:
    decision = determine_critic_trigger(
        _scenario("unknown_effect_mutation_replay").context
    )
    assert not decision.should_invoke_critic
    assert "outcome_unknown_mutation_replay_blocked" in decision.suppression_codes


def test_two_candidates_with_large_margin_do_not_trigger() -> None:
    decision = determine_critic_trigger(_scenario("two_candidates_large_margin").context)
    assert not decision.should_invoke_critic
    assert "candidate_margin_decisive" in decision.suppression_codes


def test_reasoning_difficulty_vision_and_multistep_alone_do_not_trigger() -> None:
    names = ("high_reasoning_alone", "vision_observation_alone", "multistep_alone")
    assert all(
        not determine_critic_trigger(_scenario(name).context).should_invoke_critic
        for name in names
    )


def test_mutating_but_deterministic_reversible_action_does_not_trigger() -> None:
    decision = determine_critic_trigger(
        _scenario("deterministic_reversible_mutation").context
    )
    assert not decision.should_invoke_critic
    assert "deterministic_hard_constraints_decide" in decision.suppression_codes


def test_pre_block_review_cannot_bypass_a_hard_rejection() -> None:
    decision = determine_critic_trigger(
        _scenario("blocked_review_with_hard_rejection").context
    )
    assert not decision.should_invoke_critic
    assert decision.trigger_class == CriticTriggerClass.NONE
