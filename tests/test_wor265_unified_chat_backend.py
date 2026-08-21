"""Focused proofs for WOR-265 Chat-owned durable agent execution."""

from __future__ import annotations

import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osia import chat_routing, conversations
from osia.agent import OSIAAgent
from osia.autonomous import orchestrator as autonomous_orchestrator
from osia.autonomous import state as autonomous_state
from osia.server import server
from osia.turn_context import fixed_turn_context


def _context(text: str, launcher) -> dict:
    return {
        "launcher": launcher,
        "original_user_text": text,
        "provider_input": text,
        "history": [],
        "conversation_id": "conv_wor265",
        "request_id": "turn_wor265_1234",
        "source_request_id": "request_wor265_1234",
        "source_turn_id": "turn_wor265_1234",
        "source_assistant_message_id": "message_wor265_1234",
        "origin_ref": "desktop",
        "device_id": "",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-luna",
        "turn_context": fixed_turn_context("en", source="test"),
    }


def _install_round(agent: OSIAAgent, content: str, calls: list) -> None:
    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        del self, callback, on_first_token, guard
        calls.append(content)
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_simple_question_stays_a_direct_chat_answer():
    launches = []
    calls = []
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        "What is 2 + 2?", lambda request: launches.append(request),
    )
    _install_round(agent, "4", calls)

    assert agent.run(
        "What is 2 + 2?", history=[], on_sentence=lambda _text: None,
    ) == "4"
    assert launches == []
    assert len(calls) == 1


def test_short_tool_action_is_eligible_for_the_canonical_durable_backend():
    decision = chat_routing.evaluate_route_request(
        _context("Open Calculator and calculate 5+23.", lambda _request: {}),
        reason_code="sustained_computer_work",
        confidence=0.99,
        explicit_request=False,
    )

    assert decision["route"] == "autonomous"
    assert decision["objective"] == "Open Calculator and calculate 5+23."


def test_model_selected_normal_tool_is_replaced_before_dispatch(monkeypatch):
    launches = []
    ordinary_dispatches = []
    calls = []
    text = "Show me the current system information."
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        lambda request: launches.append(request) or {
            "status": "running",
            "task_id": "task_wor265",
            "run_id": "run_wor265",
        },
    )
    monkeypatch.setitem(
        __import__("osia.agent", fromlist=["TOOL_MAP"]).TOOL_MAP,
        "get_system_info",
        lambda: ordinary_dispatches.append(True) or "unexpected",
    )
    _install_round(
        agent,
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        calls,
    )

    final = agent.run(text, history=[], on_sentence=lambda _text: None)

    assert "Autonomous mission" in final
    assert len(calls) == 1
    assert len(launches) == 1
    assert ordinary_dispatches == []
    assert launches[0]["reason_code"] == "sustained_computer_work"


def test_one_route_call_binds_and_waits_for_one_exact_run():
    launches = []
    bindings = []
    progress = []
    context = _context(
        "Open Calculator and calculate 5+23.",
        lambda request: launches.append(request) or {
            "status": "running",
            "task_id": "task_wor265",
            "run_id": "run_wor265",
        },
    )

    def waiter(initial, request, *, progress_callback, bind_execution):
        assert initial["run_id"] == "run_wor265"
        bind_execution(initial, request)
        progress_callback({"status": "verifying", "run_id": "run_wor265"})
        return {
            **initial,
            "status": "completed",
            "progress_status": "completed",
            "terminal": True,
            "final_text": "28",
        }

    context.update({
        "waiter": waiter,
        "bind_execution": lambda result, request: bindings.append((result, request)),
        "progress_callback": progress.append,
    })
    result = chat_routing.execute_route_request(
        context,
        reason_code="sustained_computer_work",
        confidence=0.99,
        explicit_request=False,
    )

    assert result["status"] == "completed"
    assert result["final_text"] == "28"
    assert len(launches) == 1
    assert {item[0]["run_id"] for item in bindings} == {"run_wor265"}
    assert [item["status"] for item in progress] == ["planning", "verifying"]


def test_agent_returns_durable_result_in_same_chat_without_second_provider_round():
    calls = []
    agent = OSIAAgent(lang="en")
    context = _context(
        "Open Calculator and calculate 5+23.",
        lambda _request: {
            "status": "running",
            "task_id": "task_wor265",
            "run_id": "run_wor265",
        },
    )
    context.update({
        "waiter": lambda *_args, **_kwargs: {
            "status": "completed",
            "progress_status": "completed",
            "terminal": True,
            "task_id": "task_wor265",
            "run_id": "run_wor265",
            "provider_id": "ollama",
            "model_id": agent.model,
            "final_text": "28",
        },
        "bind_execution": lambda result, request: agent.bind_chat_durable_execution(
            result, request,
        ),
    })
    agent._chat_autonomous_context = context
    tool_content = (
        '<tool_call>{"tool":"start_autonomous_mission",'
        '"reason_code":"sustained_computer_work","confidence":0.99,'
        '"explicit_request":false}</tool_call>'
    )

    def create(**_kwargs):
        calls.append(tool_content)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=tool_content,
            tool_calls=None,
        ))])

    agent.client.chat.completions.create = create

    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        final = agent.run_terminal_turn(
            "Open Calculator and calculate 5+23.",
            turn_id="turn_wor265_1234",
            history=[],
        )

    assert final == "28"
    assert len(calls) == 1
    assert agent.terminality_result("turn_wor265_1234")[
        "terminal_state"
    ] == "success_verified"


def test_unavailable_durable_runner_blocks_without_normal_chat_fallback():
    calls = []
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        "Open Calculator and calculate 5+23.",
        lambda _request: {
            "status": "unavailable",
            "error_code": "runner_transport_unavailable",
        },
    )
    tool_content = (
        '<tool_call>{"tool":"start_autonomous_mission",'
        '"reason_code":"sustained_computer_work","confidence":0.99,'
        '"explicit_request":false}</tool_call>'
    )

    def create(**_kwargs):
        calls.append(tool_content)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=tool_content,
            tool_calls=None,
        ))])

    agent.client.chat.completions.create = create
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        final = agent.run_terminal_turn(
            "Open Calculator and calculate 5+23.",
            turn_id="turn_wor265_unavailable",
            history=[],
        )

    assert "could not reach" in final
    assert len(calls) == 1
    assert agent.terminality_result("turn_wor265_unavailable")[
        "terminal_state"
    ] == "blocked_verified"


@pytest.mark.parametrize(
    "raw_status,run_state,expected",
    (
        ("in_progress", "planning", "planning"),
        ("in_progress", "running", "executing"),
        ("in_progress", "authorization_pending", "waiting"),
        ("in_progress", "verifying_tool", "verifying"),
        ("in_progress", "resuming", "recovering"),
        ("done", "completed", "completed"),
        ("failed", "failed", "blocked"),
        ("in_progress", "blocked_unknown_effect", "blocked"),
        ("aborted", "cancelled", "cancelled"),
    ),
)
def test_progress_projection_uses_only_the_public_status_vocabulary(
    raw_status, run_state, expected,
):
    projected = chat_routing.durable_progress_from_state({
        "status": raw_status,
        "run_state": run_state,
        "objective": "private objective must not be projected",
        "source_conversation_id": "conv_wor265",
        "source_request_id": "request_wor265",
        "source_turn_id": "turn_wor265",
        "run_id": "run_wor265",
        "phases": [],
    })

    assert projected["status"] == expected
    assert "objective" not in projected
    if run_state == "blocked_unknown_effect":
        assert projected["terminal"] is True
        assert projected["outcome_unknown"] is True


def test_checkpoint_terminal_result_preserves_chat_model_and_exact_answer():
    st = autonomous_state.new_task_state(
        "calculate", "chatgpt_account", "gpt-5.6-luna", [],
    )
    st.update({
        "status": "done",
        "run_state": "completed",
        "terminal_response": "28",
        "source_conversation_id": "conv_wor265",
        "source_request_id": "request_wor265",
        "source_turn_id": "turn_wor265",
    })

    result = chat_routing.terminal_result_from_state(st)

    assert result["status"] == "completed"
    assert result["final_text"] == "28"
    assert (result["provider_id"], result["model_id"]) == (
        "chatgpt_account", "gpt-5.6-luna",
    )


def test_terminal_value_requires_matching_completed_desktop_evidence():
    st = {
        "phases": [{
            "tasks": [{
                "status": "done",
                "success_criterion": "The Calculator display shows 28.",
                "execution_memory": {
                    "desktop_observation": {
                        "uia_text_surfaces": [
                            {"label": "L’affichage est 28"},
                        ],
                    },
                },
            }],
        }],
    }

    assert autonomous_orchestrator._verified_chat_terminal_value(st) == "28"
    st["phases"][0]["tasks"][0]["execution_memory"][
        "desktop_observation"
    ]["uia_text_surfaces"] = [
        {"label": "L’affichage est 27"},
        {"label": "28"},
    ]
    assert autonomous_orchestrator._verified_chat_terminal_value(st) == ""


def _metadata(request: str, turn: str, message: str) -> dict:
    return {
        "_conversation_id": "conv_wor265",
        "_request_id": request,
        "_client_request_id": request,
        "_turn_id": turn,
        "_message_id": message,
        "_operation_id": message,
    }


def test_durable_result_commits_after_newer_turn_and_replays_idempotently(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", Path(tmp_path))
    conv = conversations.create_conversation()
    conv_id = conv["id"]
    first = _metadata("request-a", "turn-a", "message-user-a")
    second = _metadata("request-b", "turn-b", "message-user-b")
    conversations.commit_message(conv_id, "user", "run it", metadata=first)
    conversations.commit_message(conv_id, "user", "newer turn", metadata=second)
    assistant = _metadata("request-a", "turn-a", "message-assistant-a")
    assistant.update({
        "_conversation_id": conv_id,
        "_durable_run_id": "run-a",
        "_durable_status": "completed",
        "_durable_provider_id": "provider-a",
        "_durable_model_id": "model-a",
    })

    receipt = conversations.commit_durable_assistant_message(
        conv_id, "28", metadata=assistant,
    )
    replay = conversations.commit_durable_assistant_message(
        conv_id, "28", metadata=assistant,
    )

    assert receipt["committed"] is True
    assert replay["committed"] is False
    assert [item["content"] for item in conversations.get_conversation(conv_id)[
        "messages"
    ]] == ["run it", "newer turn", "28"]

    monkeypatch.setattr(server, "_current_autonomous_state", lambda: None)
    recovered = server._wait_for_chat_autonomous_result(
        {},
        {
            "conversation_id": conv_id,
            "source_request_id": "request-a",
            "source_turn_id": "turn-a",
            "source_assistant_message_id": "message-assistant-a",
        },
    )
    assert recovered["status"] == "completed"
    assert recovered["final_text"] == "28"
    assert recovered["run_id"] == "run-a"


def test_durable_commit_enriches_an_identical_chat_result_without_duplication(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", Path(tmp_path))
    conv = conversations.create_conversation()
    conv_id = conv["id"]
    user = _metadata("request-a", "turn-a", "message-user-a")
    user["_conversation_id"] = conv_id
    assistant = _metadata("request-a", "turn-a", "message-assistant-a")
    assistant["_conversation_id"] = conv_id
    conversations.commit_message(conv_id, "user", "run it", metadata=user)
    conversations.commit_message(
        conv_id, "assistant", "28", metadata=assistant,
    )
    durable = dict(assistant)
    durable.update({
        "_durable_run_id": "run-a",
        "_durable_status": "completed",
        "_durable_outcome_unknown": False,
        "_durable_provider_id": "provider-a",
        "_durable_model_id": "model-a",
    })

    enriched = conversations.commit_durable_assistant_message(
        conv_id, "28", metadata=durable,
    )
    replay = conversations.commit_durable_assistant_message(
        conv_id, "28", metadata=durable,
    )
    saved = conversations.get_conversation(conv_id)

    assert enriched["committed"] is True
    assert replay["committed"] is False
    assert len(saved["messages"]) == 2
    assert saved["messages"][-1]["_durable_run_id"] == "run-a"


def test_stop_targets_the_bound_run_once_and_preserves_outcome_unknown():
    agent = OSIAAgent()
    calls = []
    request = {
        "conversation_id": "conv_wor265",
        "source_request_id": "request_wor265",
        "source_turn_id": "turn_wor265",
    }
    result = {"task_id": "task_wor265", "run_id": "run_wor265"}
    assert agent.bind_chat_durable_execution(
        result,
        request,
        cancel_callback=lambda binding: calls.append(binding) or {
            "accepted": True,
            "outcome_unknown": True,
        },
    )

    assert agent.request_turn_cancellation("turn_wor265")
    assert agent.request_turn_cancellation("turn_wor265")
    assert len(calls) == 1
    assert calls[0]["run_id"] == "run_wor265"


def test_server_pc_snapshot_exposes_chat_progress_and_keeps_legacy_projection():
    st = autonomous_state.new_task_state("calculate", "provider", "model", [])
    st.update({
        "chat_route_type": "autonomous",
        "source_conversation_id": "conv_wor265",
        "source_request_id": "request_wor265",
        "source_turn_id": "turn_wor265",
    })

    snapshot = server._autonomous_pc_snapshot(st)

    assert snapshot["chat_execution"]["status"] == "planning"
    assert snapshot["_server_mobile_run"] is True


def test_chat_blocked_event_persists_terminal_and_releases_its_runner(monkeypatch):
    st = autonomous_state.new_task_state("calculate", "provider", "model", [])
    st.update({
        "status": "in_progress",
        "run_state": "blocked_unknown_effect",
        "chat_route_type": "autonomous",
    })
    persisted = []
    broadcasts = []
    releases = []

    class InlineThread:
        def __init__(self, *, target, args, daemon):
            del daemon
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setitem(server._autonomous, "st", st)
    monkeypatch.setitem(server._autonomous, "active", True)
    monkeypatch.setitem(server._autonomous, "interrupting", False)
    monkeypatch.setitem(server._autonomous, "gen", 265)
    monkeypatch.setitem(server._autonomous, "orch", None)
    monkeypatch.setattr(
        server, "_persist_chat_durable_terminal", persisted.append,
    )
    monkeypatch.setattr(server, "_broadcast_autonomous", broadcasts.append)
    monkeypatch.setattr(server, "_retire_document_scope", lambda *_args: None)
    monkeypatch.setattr(
        server, "_finalize_terminal_release",
        lambda orch, generation: releases.append((orch, generation)),
    )
    monkeypatch.setattr(server.threading, "Thread", InlineThread)

    server._on_autonomous_progress({"event": "blocked_unknown_effect"})

    assert persisted == [st]
    assert broadcasts[0]["event"] == "blocked_outcome_unknown"
    assert releases == [(None, 265)]


def test_legacy_checkpoint_defaults_are_additive_and_recovery_safe():
    st = autonomous_state.new_task_state("legacy", "provider", "model", [])
    st.pop("source_assistant_message_id")
    st.pop("terminal_response")

    migrated = autonomous_state._migrate_loaded_state(st)

    assert migrated["source_assistant_message_id"] == ""
    assert migrated["terminal_response"] == ""


def test_scheduled_request_remains_outside_the_durable_chat_route():
    decision = chat_routing.evaluate_route_request(
        _context("Every day at 09:00, calculate 5+23.", lambda _request: {}),
        reason_code="sustained_computer_work",
        confidence=0.99,
        explicit_request=False,
    )

    assert decision["route"] == "scheduled"


def test_transport_failure_never_falls_back_to_normal_chat_execution():
    launches = []
    result = chat_routing.execute_route_request(
        _context(
            "Open Calculator and calculate 5+23.",
            lambda request: launches.append(request) or {
                "status": "unavailable",
                "error_code": "runner_transport_unavailable",
            },
        ),
        reason_code="sustained_computer_work",
        confidence=0.99,
        explicit_request=False,
    )

    assert result["status"] == "unavailable"
    assert result["ok"] is False
    assert len(launches) == 1
