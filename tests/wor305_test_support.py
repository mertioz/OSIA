"""Minimal shared fixtures for canonical WOR-305 runtime tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from osia import agent as runtime
from osia.turn_terminality import TurnTerminalityController


def controller(tmp_path, objective="Create and build the requested project", required=()):
    value = TurnTerminalityController(
        turn_id="phase3b",
        objective=objective,
        execution_required=True,
        storage_dir=tmp_path / "turn",
    )
    value.bind_outcome_contract(required)
    return value


def scripted(monkeypatch, responses):
    from osia import conversation_context

    resolve_context_limit = conversation_context.resolve_context_limit

    def fixture_context_limit(provider, model):
        # The fake model has no catalogue entry. Allow the tool inventory and
        # receipts to fit when the test is exercising dispatch, not sizing.
        return 128_000 if model == "fixture" else resolve_context_limit(provider, model)

    monkeypatch.setattr(conversation_context, "resolve_context_limit", fixture_context_limit)
    calls = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            agent._test_controller = agent._terminality_controller
            calls.append(kwargs)
            if not responses:
                pytest.fail("Completion added an unexpected provider round", pytrace=False)
            message = SimpleNamespace(
                content=responses.pop(0),
                tool_calls=None,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)]
            )

    agent = runtime.OSIAAgent(lang="en")
    agent.provider, agent.model, agent.client = "ollama", "fixture", Client()
    monkeypatch.setattr(agent, "_resolve_route", lambda _text: ("medium", []))
    agent.configure_tool_attempts(
        conversation_id="3b",
        turn_id="3b",
        request_id="3b",
        final_response_message_id="3b-final",
    )
    return agent, calls


def call(tool, **arguments):
    return "<tool_call>" + json.dumps({"tool": tool, **arguments}) + "</tool_call>"
