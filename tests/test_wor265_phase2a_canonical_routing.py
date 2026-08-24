"""Focused routing proofs for WOR-265 Unified Chat."""

from __future__ import annotations

import copy
import types

import pytest

from osia import chat_routing, execution_strategy
from osia.agent import OSIAAgent
from osia.goal_contract import goal_contract_from_semantic_mission
from osia.turn_context import fixed_turn_context


_DESKTOP_REQUEST = "ouvre calculatrice et calcule 5+23"


def _context(text: str, launcher, *, suffix: str = "repeat") -> dict:
    return {
        "launcher": launcher,
        "original_user_text": text,
        "provider_input": text,
        "history": [],
        "conversation_id": f"conv_wor265_{suffix}",
        "request_id": f"request_wor265_{suffix}",
        "source_request_id": f"request_wor265_{suffix}",
        "source_turn_id": f"turn_wor265_{suffix}",
        "source_assistant_message_id": f"message_wor265_{suffix}",
        "origin_ref": "desktop",
        "device_id": "",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-terra",
        "turn_context": fixed_turn_context("fr", source="test"),
    }


def _install_provider_output(agent: OSIAAgent, content: str, calls: list) -> None:
    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        del self, callback, on_first_token, guard
        calls.append(content)
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def _strategy(text: str):
    return execution_strategy.build_execution_strategy(
        text,
        (
            "open_application",
            "open_browser",
            "search_web",
            "gmail_send_draft",
            "mcp_call",
        ),
    )


def test_repeated_fresh_simple_turns_always_stay_direct_chat():
    launches = []
    routes = []
    for index in range(5):
        text = "2 + 2"
        context = _context(
            text,
            lambda request: launches.append(request),
            suffix=f"simple_{index}",
        )
        routes.append(
            chat_routing.canonical_route_contract(
                context,
                strategy=_strategy(text),
            )
        )
        provider_calls = []
        agent = OSIAAgent(lang="en")
        agent._chat_autonomous_context = context
        _install_provider_output(agent, "4", provider_calls)

        assert agent.run(
            text, history=[], on_sentence=lambda _text: None,
        ) == "4"
        assert provider_calls == ["4"]

    assert {item["route"] for item in routes} == {"normal"}
    assert {item["semantic_fingerprint"] for item in routes} == {
        routes[0]["semantic_fingerprint"]
    }
    assert launches == []


@pytest.mark.parametrize(
    "text",
    (
        _DESKTOP_REQUEST,
        "Open the browser and search the web for current Cloudflare docs.",
        "Use Gmail to send Alice the project status.",
        "Create an issue in Jira through the connected MCP server.",
    ),
)
def test_execution_categories_share_the_canonical_durable_chat_route(text):
    launches = []
    contract = chat_routing.canonical_route_contract(
        _context(text, lambda request: launches.append(request), suffix="category"),
        strategy=_strategy(text),
    )

    assert contract["route"] == "durable_chat"
    assert contract["execution_required"] is True
    assert contract["classification_source"] == (
        "semantic_mission_goal_contract"
    )
    assert contract["schema_version"] == (
        chat_routing.ROUTING_CONTRACT_SCHEMA_VERSION
    )
    assert launches == []


def test_old_autonomous_wording_cannot_create_a_new_autonomous_route():
    text = "Use autonomous mode to open Calculator and calculate 5+23."
    contract = chat_routing.canonical_route_contract(
        _context(text, lambda _request: pytest.fail("legacy launcher called")),
        strategy=_strategy(text),
    )

    assert contract["route"] == "durable_chat"
    assert contract["explicit_request"] is True
    assert contract["route"] != "autonomous"


def test_action_turn_stays_in_the_chat_provider_loop_without_legacy_launcher():
    launches = []
    provider_calls = []
    context = _context(
        _DESKTOP_REQUEST,
        lambda request: launches.append(request),
        suffix="same_chat",
    )
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = context
    _install_provider_output(agent, "I am continuing in this Chat.", provider_calls)

    result = agent.run(
        _DESKTOP_REQUEST,
        history=[],
        on_sentence=lambda _text: None,
    )

    assert result
    assert "Autonomous" not in result
    # The durable Chat controller asks the same effective provider for a fresh
    # next decision; it never transfers ownership to the legacy launcher.
    assert provider_calls == [
        "I am continuing in this Chat.",
        "I am continuing in this Chat.",
    ]
    assert launches == []


def test_route_and_goal_contract_are_provider_output_independent():
    context = _context(_DESKTOP_REQUEST, lambda _request: {})
    strategy = _strategy(_DESKTOP_REQUEST)
    before = chat_routing.canonical_route_contract(context, strategy=strategy)
    snapshot_before = copy.deepcopy(strategy.snapshot())

    for ignored_provider_output in (
        "I would answer directly.",
        "I would use a tool.",
        "I would write a numbered plan.",
    ):
        varied = dict(context)
        varied["provider_output"] = ignored_provider_output
        assert chat_routing.canonical_route_contract(
            varied, strategy=strategy,
        ) == before

    snapshot_after = strategy.snapshot()
    assert snapshot_after == snapshot_before
    assert before["route"] == "durable_chat"
    assert before["goal_contract_fingerprint"]


def test_new_execution_snapshot_has_goal_authority_not_plan_task_authority():
    text = "Inspect the Calculator desktop without clicking or sending."
    strategy = _strategy(text)
    binding = {
        "conversation_id": "conv_wor265_goal",
        "request_id": "request_wor265_goal",
        "turn_id": "turn_wor265_goal",
        "run_id": "run_wor265_goal",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-terra",
        "effective_reasoning": "high",
    }
    goal = goal_contract_from_semantic_mission(
        strategy.mission,
        execution_binding=binding,
    )
    strategy.bind_goal_contract(goal)

    snapshot = strategy.snapshot()
    contract = snapshot["goal_contract"]

    assert not ({"plan", "phases", "tasks", "subtasks"} & set(snapshot))
    assert contract["root_objective"] == text
    assert contract["execution_binding"] == binding
    assert {item["value"] for item in contract["global_constraints"]} >= {
        "activate",
        "send",
    }


def test_scheduled_request_remains_a_distinct_temporal_route():
    text = "Every weekday at 09:00, prepare a status report."
    contract = chat_routing.canonical_route_contract(
        _context(text, lambda _request: pytest.fail("legacy launcher called")),
        strategy=_strategy(text),
    )

    assert contract["route"] == "scheduled"
    assert contract["execution_required"] is False
    assert contract["reason_code"] == "scheduled_request"
