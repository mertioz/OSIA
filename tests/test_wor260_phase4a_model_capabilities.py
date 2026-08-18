from dataclasses import replace

from benchmarks.wor260.phase4a import reasoning_scenarios
from osia.adaptive_reasoning import (
    ProviderParameterBehavior,
    capability_from_current_registry,
    determine_reasoning_decision,
)


def _scenario(identity):
    return next(item for item in reasoning_scenarios() if item.scenario_id == identity)


def test_representative_static_pairs_are_read_from_current_registry():
    expected = {
        ("openai", "gpt-5.5"): ("auto", "off", "low", "medium", "high", "xhigh"),
        ("deepseek", "deepseek-v4-pro"): ("auto", "off", "high", "max"),
        ("groq", "openai/gpt-oss-120b"): ("auto", "low", "medium", "high"),
        ("mistral", "mistral-medium-3-5"): ("auto", "off", "high"),
    }
    for pair, values in expected.items():
        assert capability_from_current_registry(*pair).supported_osia_values == values


def test_chatgpt_account_uses_model_list_metadata_path(monkeypatch):
    monkeypatch.setattr(
        "osia.chatgpt_account.provider.get_cached_account_models",
        lambda: [{
            "id": "gpt-runtime",
            "supportedReasoningEfforts": [
                {"id": item} for item in ("low", "medium", "high", "ultra")
            ],
            "defaultReasoningEffort": "medium",
        }],
    )
    capability = capability_from_current_registry("chatgpt_account", "gpt-runtime")
    assert capability.supported_osia_values == (
        "auto", "low", "medium", "high", "ultra",
    )
    assert capability.provider_default_value == "medium"
    assert capability.source_authority == "chatgpt_app_server:model/list"


def test_unknown_pair_never_infers_from_model_name():
    capability = capability_from_current_registry("openai", "gpt-5.5-unknown-suffix")
    assert capability.configurable is False
    assert capability.supported_osia_values == ()


def test_non_configurable_pair_omits_parameter_and_uses_provider_default():
    scenario = _scenario("unsupported_xai_model")
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    assert decision.selected_osia_level == "auto"
    assert decision.selected_native_value is None
    assert decision.provider_parameter_behavior == ProviderParameterBehavior.OMIT_PROVIDER_DEFAULT
    assert decision.unsupported_reason == "reasoning_not_configurable"


def test_context_capability_snapshot_mismatch_fails_safe():
    scenario = _scenario("simple_direct_api")
    decision = determine_reasoning_decision(
        replace(scenario.context, supported_reasoning_values=("auto", "ultra")),
        scenario.capability,
    )
    assert decision.selected_osia_level is None
    assert decision.unsupported_reason == "capability_snapshot_mismatch"


def test_partial_support_selects_only_declared_values():
    for identity in (
        "partial_deepseek_high_max",
        "partial_mistral_off_high",
        "partial_qwen_auto_off",
    ):
        scenario = _scenario(identity)
        decision = determine_reasoning_decision(scenario.context, scenario.capability)
        assert decision.selected_osia_level in scenario.capability.supported_osia_values
