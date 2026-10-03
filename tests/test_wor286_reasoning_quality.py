"""Provider-bound reasoning and completion truth at the canonical Chat seam."""

from types import SimpleNamespace

import pytest

from osia import reasoning
from osia.agent import OSIAAgent, ProviderOutputTruncatedError
from osia.chatgpt_account import provider as account_provider
from osia.main import _resolve_desktop_chat_route_binding
from osia.server import server


_MODEL = "gpt-5.6-sol"
_SETTINGS = {
    "selected_model_provider": "chatgpt_account",
    "selected_model": _MODEL,
    "reasoning": {
        "default": "auto",
        "providers": {},
        "models": {f"chatgpt_account:{_MODEL}": "high"},
    },
}


class _AccountCatalogue:
    def __init__(self, *, cached=(), refreshed=(), error=None):
        self.models = list(cached)
        self.refreshed = list(refreshed)
        self.error = error
        self.refreshes = 0

    def cached_models(self):
        return list(self.models)

    def list_models(self, *, force=False):
        assert force
        self.refreshes += 1
        if self.error is not None:
            raise self.error
        self.models = list(self.refreshed)
        return list(self.models)


def _model_metadata(*efforts):
    return [{
        "id": _MODEL,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"id": effort} for effort in efforts],
    }]


def _install_catalogue(monkeypatch, catalogue):
    monkeypatch.setattr(account_provider, "get_account_provider", lambda: catalogue)
    monkeypatch.setattr(
        account_provider, "get_cached_account_models", catalogue.cached_models,
    )


def test_cold_account_catalogue_preserves_explicit_effort_at_binding(monkeypatch):
    catalogue = _AccountCatalogue(refreshed=_model_metadata("low", "high"))
    _install_catalogue(monkeypatch, catalogue)
    monkeypatch.setattr("osia.config.load_settings", lambda: _SETTINGS)

    binding = server._resolve_chat_route_binding()
    request = reasoning.build_reasoning_request(
        "chatgpt_account", _MODEL,
        {"default": binding["effective_reasoning"]},
    )

    assert catalogue.refreshes == 1
    assert binding["effective_reasoning"] == "high"
    assert request["kwargs"] == {"effort": "high"}
    assert _SETTINGS["reasoning"]["models"][f"chatgpt_account:{_MODEL}"] == "high"


def test_warm_account_catalogue_keeps_effort_without_refresh(monkeypatch):
    catalogue = _AccountCatalogue(cached=_model_metadata("high"))
    _install_catalogue(monkeypatch, catalogue)
    monkeypatch.setattr("osia.config.load_settings", lambda: _SETTINGS)

    assert server._resolve_chat_route_binding()["effective_reasoning"] == "high"
    assert catalogue.refreshes == 0


def test_desktop_cold_route_keeps_explicit_effort(monkeypatch):
    catalogue = _AccountCatalogue(refreshed=_model_metadata("high"))
    _install_catalogue(monkeypatch, catalogue)
    monkeypatch.setattr("osia.config.load_settings", lambda: _SETTINGS)

    assert _resolve_desktop_chat_route_binding()["effective_reasoning"] == "high"
    assert catalogue.refreshes == 1


def test_account_none_option_keeps_provider_wire_spelling(monkeypatch):
    catalogue = _AccountCatalogue(cached=_model_metadata("none", "high"))
    _install_catalogue(monkeypatch, catalogue)

    request = reasoning.build_reasoning_request(
        "chatgpt_account", _MODEL,
        {"default": "off", "providers": {}, "models": {}},
    )
    assert request["diagnostic"]["effective_reasoning"] == "off"
    assert request["kwargs"] == {"effort": "none"}


@pytest.mark.parametrize(
    "catalogue",
    [
        _AccountCatalogue(refreshed=_model_metadata("low")),
        _AccountCatalogue(error=RuntimeError("catalogue unavailable")),
    ],
)
def test_unsupported_or_unavailable_account_effort_fails_without_rewrite(
    monkeypatch, catalogue,
):
    _install_catalogue(monkeypatch, catalogue)
    monkeypatch.setattr("osia.config.load_settings", lambda: _SETTINGS)
    writes = []
    monkeypatch.setattr(reasoning, "set_reasoning_settings", writes.append)

    with pytest.raises(ValueError, match="^ERROR:"):
        server._resolve_chat_route_binding()
    assert writes == []
    assert _SETTINGS["reasoning"]["models"][f"chatgpt_account:{_MODEL}"] == "high"


def test_account_adapter_does_not_swallow_reasoning_validation_error(monkeypatch):
    agent = OSIAAgent.__new__(OSIAAgent)
    agent.provider, agent.model = "chatgpt_account", _MODEL
    agent._trace_reasoning_binding = "high"
    monkeypatch.setattr(
        reasoning, "build_reasoning_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("ERROR: unsupported")),
    )
    with pytest.raises(ValueError, match="ERROR: unsupported"):
        agent._reasoning_request()


class _Completions:
    def __init__(self, *, reason="stop", stream=False):
        self.reason = reason
        self.stream = stream

    def create(self, **kwargs):
        if self.stream:
            return [
                SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="partial answer"),
                    finish_reason=None,
                )]),
                SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=""),
                    finish_reason=self.reason,
                )]),
            ]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="partial answer", tool_calls=None),
            finish_reason=self.reason,
        )])


def _api_agent(completions):
    agent = OSIAAgent(lang="en")
    agent.provider = "deepseek"
    agent.model = "deepseek-flash"
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        timeout=30.0,
    )
    agent._disable_hierarchy_for_turn = True
    return agent


@pytest.mark.parametrize("stream", [False, True])
def test_length_completion_cannot_be_returned_as_normal_final(stream):
    agent = _api_agent(_Completions(reason="length", stream=stream))
    callback = (lambda _text: None) if stream else None

    with pytest.raises(ProviderOutputTruncatedError) as caught:
        agent.run("A simple informational request.", history=[], on_sentence=callback)
    assert caught.value.finish_reason == "length"
    assert caught.value.reason_code == "provider_output_truncated"
    assert agent.last_run_diagnostic["finish_reason"] == "length"
    assert not any(
        message.get("role") == "assistant"
        and "partial answer" in str(message.get("content") or "")
        for message in agent.messages
    )


@pytest.mark.parametrize("stream", [False, True])
def test_normal_completion_remains_unchanged(stream):
    agent = _api_agent(_Completions(reason="stop", stream=stream))
    callback = (lambda _text: None) if stream else None

    assert agent.run(
        "A simple informational request.", history=[], on_sentence=callback,
    ) == "partial answer"


def test_deepseek_max_reasoning_still_reaches_provider():
    request = reasoning.build_reasoning_request(
        "deepseek", "deepseek-flash",
        {"default": "max", "providers": {}, "models": {}},
    )
    assert request["kwargs"] == {
        "extra_body": {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        },
    }


def test_server_projects_truncated_provider_final_as_explicit_error(monkeypatch):
    from osia import turn_terminality

    binding = {
        "conversation_id": "conv",
        "request_id": "req",
        "turn_id": "turn",
        "run_id": "run",
        "provider_id": "deepseek",
        "model_id": "deepseek-flash",
        "effective_reasoning": "max",
    }

    class _Controller:
        turn_id = "turn"
        final_text = ""
        reason_code = ""

        def snapshot(self):
            return {
                "execution_binding": binding,
                "first_causal_error": {
                    "reason_code": "provider_output_truncated",
                },
            }

        def reserve_final(self, _state, *, reason_code, provider_route, final_text):
            assert provider_route == "deepseek:deepseek-flash"
            self.reason_code = reason_code
            self.final_text = final_text
            return True

        def set_strategy_frontier(self, _frontier):
            pass

        def attach_final_projection(self, **_kwargs):
            pass

    controller = _Controller()
    agent = SimpleNamespace(_terminality_controller=controller)
    monkeypatch.setattr(
        turn_terminality, "load_persisted_execution_binding",
        lambda _turn_id: binding,
    )
    monkeypatch.setattr(
        turn_terminality, "load_persisted_terminal_result",
        lambda _turn_id: {
            "execution_binding": binding,
            "final_text": controller.final_text,
        } if controller.final_text else None,
    )

    result = server._reserve_failed_server_turn_terminal(
        agent, binding, objective="Answer completely", language="en",
    )
    assert controller.reason_code == "provider_output_truncated"
    assert result["final_text"].startswith("ERROR:")
    assert "incomplete" in result["final_text"]


def test_canonical_driver_records_truncation_and_does_not_accept_partial_final():
    agent = OSIAAgent.__new__(OSIAAgent)
    agent.provider, agent.model, agent.lang = "deepseek", "deepseek-flash", "en"
    agent._resume_unknown_effect = False
    agent._emit_execution_trace = lambda *args, **kwargs: None
    agent.run = lambda *args, **kwargs: (_ for _ in ()).throw(
        ProviderOutputTruncatedError()
    )
    recorded = {}

    class _Controller:
        def begin_invocation(self, **_kwargs):
            return "invocation"

        def snapshot(self):
            return {"unknown_effects": {}}

        def record_typed_evidence(self, **kwargs):
            recorded["reason_code"] = kwargs["reason_code"]

        def record_provider_invocation_completed(self, invocation_id, **kwargs):
            recorded["invocation_id"] = invocation_id
            recorded["native_status"] = kwargs["native_status"]

    with pytest.raises(ProviderOutputTruncatedError):
        agent._drive_terminal_turn(
            _Controller(), None,
            initial_input="Answer completely",
            initial_strategy_id="provider_initial",
            history=[],
            on_sentence=None,
            on_first_token=None,
            current_conversation_id="conv",
            turn_context=None,
            canonical_output=True,
        )
    assert recorded == {
        "reason_code": "provider_output_truncated",
        "invocation_id": "invocation",
        "native_status": "truncated",
    }
