from dataclasses import replace

from benchmarks.wor260.phase4a import reasoning_scenarios
from osia.adaptive_reasoning_runtime import (
    ADAPTIVE_REASONING_BINDING_SCHEMA,
    BoundReasoningContext,
    integrate_adaptive_reasoning,
)
from osia.reasoning import build_reasoning_request


def _fixture(identity, *, turn_id="turn-a", revision=3):
    scenario = next(
        item for item in reasoning_scenarios() if item.scenario_id == identity
    )
    return scenario, BoundReasoningContext(
        schema=ADAPTIVE_REASONING_BINDING_SCHEMA,
        turn_id=turn_id,
        revision=revision,
        goal_fingerprint="goal-a",
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        conversation_id="conversation-a",
        source="canonical_snapshots",
        decision_context=scenario.context,
    )


def _integrate(identity, monkeypatch, *, setting="auto", gate="1"):
    scenario, bound = _fixture(identity)
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", gate)
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": setting, "providers": {}, "models": {}},
    )
    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=bound,
        turn_id="turn-a",
        revision=3,
        conversation_id="conversation-a",
    )
    return scenario, legacy, result


def test_gate_on_auto_uses_supported_value_through_wor209_builder(monkeypatch):
    scenario, legacy, result = _integrate(
        "high_multi_app_mission", monkeypatch,
    )

    assert result.applied is True
    assert result.request["kwargs"] != legacy["kwargs"]
    assert result.decision.selected_osia_level in (
        scenario.capability.supported_osia_values
    )
    assert result.request["diagnostic"]["selection_source"] == "adaptive_auto"
    assert result.request["diagnostic"]["provider_parameter_value"] == (
        result.decision.selected_native_value
    )


def test_gate_off_is_exact_legacy_without_policy_application(monkeypatch):
    _, legacy, result = _integrate(
        "high_multi_app_mission", monkeypatch, gate="false",
    )

    assert result.applied is False
    assert result.reason_code == "gate_off"
    assert result.request == legacy
    assert result.decision is None


def test_explicit_turn_or_saved_reasoning_is_never_overridden(monkeypatch):
    for setting in ("low", "high", "xhigh"):
        _, legacy, result = _integrate(
            "critical_equivalent_mutations", monkeypatch, setting=setting,
        )
        assert result.applied is False
        assert result.request == legacy
        assert result.reason_code == "explicit_or_rejected_reasoning"


def test_partial_exact_pair_emits_only_a_declared_value(monkeypatch):
    scenario, _, result = _integrate(
        "partial_deepseek_high_max", monkeypatch,
    )

    assert result.applied is True
    assert result.decision.selected_osia_level in ("off", "high", "max")
    assert result.decision.selected_osia_level in (
        scenario.capability.supported_osia_values
    )


def test_non_configurable_and_unknown_pairs_preserve_omission(monkeypatch):
    for identity in ("unsupported_xai_model", "unknown_exact_pair"):
        _, legacy, result = _integrate(identity, monkeypatch)
        assert result.applied is False
        assert result.request == legacy
        assert result.request["kwargs"] == {}


def test_chatgpt_account_uses_exact_model_metadata_or_stays_dormant(monkeypatch):
    scenario, bound = _fixture("simple_exact_deep_link")
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    monkeypatch.setattr(
        "osia.chatgpt_account.provider.get_cached_account_models",
        lambda: [],
    )
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": "auto", "providers": {}, "models": {}},
    )

    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=bound,
        turn_id="turn-a",
        revision=3,
        conversation_id="conversation-a",
    )

    assert result.applied is False
    assert result.request == legacy
    assert result.request["kwargs"] == {}


def test_stale_capability_snapshot_is_replaced_by_exact_registry_metadata(monkeypatch):
    scenario, bound = _fixture("high_multi_app_mission")
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    mismatched = replace(
        bound,
        decision_context=replace(
            bound.decision_context,
            supported_reasoning_values=("auto", "ultra"),
        ),
    )
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": "auto", "providers": {}, "models": {}},
    )

    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=mismatched,
        turn_id="turn-a",
        revision=3,
        conversation_id="conversation-a",
    )

    assert result.applied is True
    assert result.decision.selected_osia_level in (
        scenario.capability.supported_osia_values
    )
    assert result.decision.selected_osia_level != "ultra"
    assert result.reason_code == "adaptive_supported_value_applied"
