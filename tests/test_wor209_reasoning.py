from types import SimpleNamespace

import pytest

from osia import reasoning
from osia.agent import OSIAAgent
from osia.config import normalize_reasoning_level
from osia.mobile_model_catalogue import _safe_model
from osia.models import HARDCODED_MODELS


EXPECTED_STATIC_OPTIONS = {
    ("anthropic", "claude-opus-4-8"): ["auto", "off", "low", "medium", "high", "xhigh", "max"],
    ("anthropic", "claude-opus-4-7"): ["auto", "off", "low", "medium", "high", "xhigh", "max"],
    ("anthropic", "claude-opus-4-6"): ["auto", "off", "low", "medium", "high", "max"],
    ("anthropic", "claude-sonnet-4-6"): ["auto", "off", "low", "medium", "high", "max"],
    ("anthropic", "claude-haiku-4-5-20251001"): [],
    ("openai", "gpt-5.5"): ["auto", "off", "low", "medium", "high", "xhigh"],
    ("openai", "gpt-5.4"): ["auto", "off", "low", "medium", "high", "xhigh"],
    ("openai", "gpt-5.4-mini"): ["auto", "off", "low", "medium", "high", "xhigh"],
    ("openai", "gpt-4.1"): [],
    ("openai", "gpt-4.1-mini"): [],
    ("openai", "gpt-4.1-nano"): [],
    ("deepseek", "deepseek-flash"): ["auto", "off", "low", "high", "max"],
    ("google", "gemini-3.5-flash"): ["auto", "minimal", "low", "medium", "high"],
    ("google", "gemini-3.1-pro-preview"): ["auto", "low", "medium", "high"],
    ("google", "gemini-3-flash-preview"): ["auto", "minimal", "low", "medium", "high"],
    ("google", "gemini-3.1-flash-lite"): ["auto", "minimal", "low", "medium", "high"],
    ("google", "gemini-2.5-flash"): ["auto", "off", "minimal", "low", "medium", "high"],
    ("google", "gemini-2.5-flash-lite"): ["auto", "off", "minimal", "low", "medium", "high"],
    ("xai", "grok-3"): [],
    ("xai", "grok-3-mini"): [],
    ("groq", "llama-3.3-70b-versatile"): [],
    ("groq", "llama-3.1-8b-instant"): [],
    ("groq", "qwen/qwen3-32b"): [],
    ("groq", "openai/gpt-oss-120b"): ["auto", "low", "medium", "high"],
    ("groq", "openai/gpt-oss-20b"): ["auto", "low", "medium", "high"],
    ("groq", "moonshotai/kimi-k2-instruct"): [],
    ("mistral", "mistral-large-2512"): [],
    ("mistral", "mistral-medium-3-5"): ["auto", "off", "high"],
    ("mistral", "mistral-small-2603"): [],
    ("mistral", "codestral-2508"): [],
}

ACCOUNT_MODELS = [
    ("gpt-5.6-sol", "low", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ("gpt-5.6-terra", "medium", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ("gpt-5.6-luna", "medium", ["low", "medium", "high", "xhigh", "max"]),
    ("gpt-5.5", "medium", ["low", "medium", "high", "xhigh"]),
    ("gpt-5.4", "medium", ["low", "medium", "high", "xhigh"]),
    ("gpt-5.4-mini", "medium", ["low", "medium", "high", "xhigh"]),
    ("gpt-5.3-codex-spark", "high", ["low", "medium", "high", "xhigh"]),
]


@pytest.fixture
def account_metadata(monkeypatch):
    models = [
        {
            "id": model_id,
            "defaultReasoningEffort": default,
            "supportedReasoningEfforts": [
                {"id": effort, "description": f"safe {effort}"}
                for effort in efforts
            ],
        }
        for model_id, default, efforts in ACCOUNT_MODELS
    ]
    monkeypatch.setattr(
        "osia.chatgpt_account.provider.get_cached_account_models",
        lambda: models,
    )
    return models


def _ids(provider, model):
    return [item["id"] for item in reasoning.get_reasoning_options(provider, model)]


def test_every_registry_model_has_an_exact_authoritative_option_set():
    registry_pairs = {(item["provider"], item["id"]) for item in HARDCODED_MODELS}
    # WOR-355 adds documented models; the historical option contracts remain.
    assert set(EXPECTED_STATIC_OPTIONS) <= registry_pairs
    for pair, expected in EXPECTED_STATIC_OPTIONS.items():
        assert _ids(*pair) == expected

    # Current runtime-discovered pairs not present in the hardcoded registry.
    assert _ids("groq", "qwen/qwen3.6-27b") == ["auto", "off"]
    assert _ids("groq", "allam-2-7b") == []
    assert _ids("ollama", "gemma4:latest") == []


def test_account_options_and_defaults_come_only_from_app_server(account_metadata):
    for model_id, default, efforts in ACCOUNT_MODELS:
        capability = reasoning.get_reasoning_capability("chatgpt_account", model_id)
        assert [item["id"] for item in capability["options"]] == ["auto", *efforts]
        assert capability["provider_default"] == default
        assert capability["evidence"] == "chatgpt_app_server:model/list"
    assert reasoning.get_reasoning_options("chatgpt_account", "not-in-runtime") == []


@pytest.mark.parametrize(
    ("provider", "model", "option_id", "parameter_name", "parameter_value"),
    [
        ("anthropic", "claude-opus-4-8", "off", "thinking.type", "disabled"),
        ("anthropic", "claude-opus-4-8", "xhigh", "output_config.effort", "xhigh"),
        ("anthropic", "claude-sonnet-4-6", "max", "output_config.effort", "max"),
        ("openai", "gpt-5.5", "off", "reasoning_effort", "none"),
        ("openai", "gpt-5.4-mini", "xhigh", "reasoning_effort", "xhigh"),
        ("deepseek", "deepseek-flash", "high", "reasoning_effort", "high"),
        ("deepseek", "deepseek-flash", "max", "reasoning_effort", "max"),
        ("google", "gemini-3.5-flash", "minimal", "reasoning_effort", "minimal"),
        ("google", "gemini-3.1-pro-preview", "medium", "reasoning_effort", "medium"),
        ("google", "gemini-2.5-flash", "off", "reasoning_effort", "none"),
        ("groq", "openai/gpt-oss-120b", "low", "reasoning_effort", "low"),
        ("groq", "qwen/qwen3.6-27b", "off", "reasoning_effort", "none"),
        ("mistral", "mistral-medium-3-5", "high", "reasoning_effort", "high"),
    ],
)
def test_ui_id_maps_to_exact_native_parameter(
    provider, model, option_id, parameter_name, parameter_value
):
    request = reasoning.build_reasoning_request(
        provider,
        model,
        {"models": {model: option_id}},
    )
    diagnostic = request["diagnostic"]
    assert diagnostic["requested_reasoning"] == option_id
    assert diagnostic["effective_reasoning"] == option_id
    assert diagnostic["mapping_state"] == "accepted"
    assert diagnostic["provider_parameter_name"] == parameter_name
    assert diagnostic["provider_parameter_value"] == parameter_value
    assert request["kwargs"]


def test_payload_harness_captures_every_supported_static_option():
    for pair, option_ids in EXPECTED_STATIC_OPTIONS.items():
        for option_id in option_ids:
            request = reasoning.build_reasoning_request(
                *pair,
                {"models": {pair[1]: option_id}},
            )
            assert request["diagnostic"]["effective_reasoning"] == option_id
            assert request["diagnostic"]["mapping_state"] == "accepted"
            assert bool(request["kwargs"]) is (option_id != "auto")


def test_payload_harness_captures_every_account_option(account_metadata):
    for model_id, _default, efforts in ACCOUNT_MODELS:
        key = f"chatgpt_account:{model_id}"
        for option_id in ["auto", *efforts]:
            request = reasoning.build_reasoning_request(
                "chatgpt_account",
                model_id,
                {"models": {key: option_id}},
            )
            assert request["diagnostic"]["effective_reasoning"] == option_id
            assert request["kwargs"] == (
                {} if option_id == "auto" else {"effort": option_id}
            )


def test_invalid_deepseek_saved_value_is_rejected_without_remapping(monkeypatch):
    current = {"default": "auto", "models": {"deepseek-flash": "normal"}}
    saved = []
    monkeypatch.setattr(reasoning, "get_reasoning_settings", lambda: current)
    monkeypatch.setattr(reasoning, "set_reasoning_settings", saved.append)
    with pytest.raises(ValueError, match="unsupported DeepSeek reasoning"):
        reasoning.build_reasoning_request("deepseek", "deepseek-flash")
    assert saved == []


def test_syntax_normalization_preserves_runtime_ids_for_model_validation():
    assert normalize_reasoning_level("ULTRA") == "ultra"
    assert normalize_reasoning_level("not valid!") == "auto"


def test_unsupported_models_omit_selector_and_keep_empty_payload():
    for pair in (
        ("openai", "gpt-4.1"),
        ("xai", "grok-3"),
        ("groq", "llama-3.3-70b-versatile"),
        ("mistral", "mistral-large-2512"),
        ("ollama", "gemma4:latest"),
    ):
        assert reasoning.get_reasoning_options(*pair) == []
        assert reasoning.build_reasoning_kwargs(*pair) == {}


def test_pc_mobile_catalogue_serializes_the_same_canonical_ids(account_metadata):
    pairs = [
        pair for pair, options in EXPECTED_STATIC_OPTIONS.items() if options
    ] + [("groq", "qwen/qwen3.6-27b")]
    pairs += [("chatgpt_account", item[0]) for item in ACCOUNT_MODELS]
    for provider, model in pairs:
        mobile = _safe_model(
            {"id": model, "label": model, "provider": provider},
            provider,
        )
        assert [item["id"] for item in mobile["reasoning"]["options"]] == _ids(
            provider, model
        )


class _CaptureCompletions:
    def __init__(self, error=None):
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        delta = SimpleNamespace(content="ok")
        return [SimpleNamespace(choices=[SimpleNamespace(delta=delta)])]


def _agent_with_capture(completions):
    agent = OSIAAgent.__new__(OSIAAgent)
    agent.provider = "deepseek"
    agent.model = "deepseek-flash"
    agent.messages = [{"role": "user", "content": "harmless"}]
    agent.turn_context = None
    agent._chatgpt_conversation_id = None
    agent._execution_generation_local = SimpleNamespace()
    agent.lang = "en"
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        timeout=30.0,
    )
    return agent


def test_adapter_records_sanitized_accepted_payload(monkeypatch):
    completions = _CaptureCompletions()
    agent = _agent_with_capture(completions)
    receipts = []
    monkeypatch.setattr(
        reasoning,
        "get_reasoning_settings",
        lambda: {"default": "auto", "providers": {}, "models": {"deepseek-flash": "high"}},
    )
    monkeypatch.setattr(reasoning, "record_reasoning_outcome", lambda diagnostic, **state: receipts.append((diagnostic, state)))

    assert list(agent._stream_deltas()) == ["ok"]
    assert completions.kwargs["extra_body"]["reasoning_effort"] == "high"
    diagnostic, state = receipts[0]
    assert diagnostic["requested_reasoning"] == "high"
    assert diagnostic["effective_reasoning"] == "high"
    assert diagnostic["provider_parameter_name"] == "reasoning_effort"
    assert state == {"accepted": True}
    assert "harmless" not in repr(diagnostic)


def test_adapter_records_sanitized_rejection(monkeypatch):
    completions = _CaptureCompletions(RuntimeError("private provider response"))
    agent = _agent_with_capture(completions)
    receipts = []
    monkeypatch.setattr(
        reasoning,
        "get_reasoning_settings",
        lambda: {"default": "auto", "providers": {}, "models": {"deepseek-flash": "max"}},
    )
    monkeypatch.setattr(reasoning, "record_reasoning_outcome", lambda diagnostic, **state: receipts.append((diagnostic, state)))

    with pytest.raises(RuntimeError):
        list(agent._stream_deltas())
    diagnostic, state = receipts[0]
    assert diagnostic["effective_reasoning"] == "max"
    assert state["accepted"] is False
    assert isinstance(state["error"], RuntimeError)
    assert "private provider response" not in repr(diagnostic)
