"""Minimal shared fixtures for canonical WOR-305 runtime tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

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
    calls = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            agent._test_controller = agent._terminality_controller
            calls.append(kwargs)
            assert responses, "Completion added an unexpected provider round"
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