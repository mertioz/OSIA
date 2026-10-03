"""Focused deterministic WOR-252 retry/resume outcomes."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from osia import action_policy, tool_attempts
from osia.agent import OSIAAgent, TOOL_MAP
from osia.execution_strategy import execution_strategy_scope
from osia.mcp_client import registry as mcp_registry
from osia.permission_outcome import PermissionOutcome, PermissionResult
from osia.tool_attempts import ToolAttemptCoordinator, ToolAttemptStore


class _SequenceCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_by_call = []

    def create(self, **kwargs):
        self.messages_by_call.append(copy.deepcopy(kwargs.get("messages") or []))
        if self.calls >= len(self.responses):
            raise AssertionError("unexpected provider round")
        content = self.responses[self.calls]
        self.calls += 1
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _agent_with(*responses):
    agent = OSIAAgent(model="wor252-model", lang="fr")
    agent.provider = "ollama"
    completions = _SequenceCompletions(responses)
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    return agent, completions


def _configure(agent, suffix: str) -> None:
    agent.configure_tool_attempts(
        conversation_id="wor252-conversation",
        request_id=f"wor252-request-{suffix}",
        turn_id=f"wor252-turn-{suffix}",
        final_response_message_id=f"wor252-final-{suffix}",
    )


def _records(store: ToolAttemptStore) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(store.attempts_dir.glob("attempt_*.json"))
    ]


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    action_policy.clear_pending_execution_permit()
    previous_provider = mcp_registry.get_confirmation_provider()
    mcp_registry.set_confirmation_provider(None)
    yield store
    action_policy.clear_pending_execution_permit()
    mcp_registry.set_confirmation_provider(previous_provider)


def test_partial_success_retry_executes_only_unfinished_new_attempt(
    monkeypatch, _isolated_runtime,
):
    from osia import execution_strategy as strategy_module

    dispatches = []
    state = {"block_command": True}

    monkeypatch.setitem(
        TOOL_MAP, "get_system_info",
        lambda **_kwargs: dispatches.append("read") or "system-ok",
    )
    monkeypatch.setitem(
        TOOL_MAP, "run_command",
        lambda **_kwargs: dispatches.append("command") or "command-ok",
    )

    def gate(_request, tool_name, _arguments):
        if tool_name == "run_command" and state["block_command"]:
            return (
                "ERROR: execution strategy blocked this call before dispatch "
                "(synthetic_retry_fixture). Preserve the exact goal and use the "
                "earliest safe semantically equivalent alternative."
            )
        return None

    monkeypatch.setattr(action_policy, "guard_tool_call", gate)
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call",
        lambda *_args: False,
    )

    agent, completions = _agent_with(
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        '<tool_call>{"tool":"run_command","command":"echo wor252"}</tool_call>',
        '<tool_call>{"tool":"resolve_unfinished_execution",'
        '"decision":"resume"}</tool_call>',
        "Les étapes demandées sont terminées.",
        '<tool_call>{"tool":"resolve_unfinished_execution",'
        '"decision":"resume"}</tool_call>',
        "Les étapes demandées sont terminées.",
    )
    request = "Effectue la lecture puis la commande demandée."
    # The exact strategy binding must stay inherited across the resumed turn;
    # WOR-252 refuses a mutating resume whose binding is missing.
    with strategy_module.execution_strategy_scope(
        request, {"get_system_info", "run_command"},
    ):
        _configure(agent, "original")
        _first = agent.run(request)
        assert dispatches == ["read"]
        records = _records(_isolated_runtime)
        # WOR-301: a blocked pre-dispatch call is durably recorded as a
        # zero-dispatch failure while the read-only step actually executed.
        assert [
            item["tool_name"] for item in records
            if item["status"] != "failed_before_effect"
        ] == ["get_system_info"]
        blocked = [item for item in records if item["tool_name"] == "run_command"]
        assert blocked
        assert all(item["status"] == "failed_before_effect" for item in blocked)

        state["block_command"] = False
        _configure(agent, "retry")
        final = agent.run("réessaye")
    assert final == "Les étapes demandées sont terminées."
    assert dispatches == ["read", "command"]
    records = _records(_isolated_runtime)
    assert [item["status"] for item in records].count("succeeded") == 2
    command_records = [item for item in records if item["tool_name"] == "run_command"]
    assert {item["request_id"] for item in command_records} == {
        "wor252-request-original", "wor252-request-retry",
    }
    assert completions.calls == 6


def test_completed_mutation_is_not_replayed_or_reclassified(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        TOOL_MAP, "write_file",
        lambda **_kwargs: dispatches.append("write") or "file-created",
    )
    monkeypatch.setattr(action_policy, "guard_tool_call", lambda *_args: None)
    monkeypatch.setattr(
        action_policy, "pending_execution_permit_matches", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call", lambda *_args: False,
    )
    call = (
        '<tool_call>{"tool":"write_file","path":"C:/synthetic/wor252.md",'
        '"content":"done"}</tool_call>'
    )
    agent, _completions = _agent_with(
        call,
        call,
        "The file was created once.",
    )
    _configure(agent, "same-turn-completed")

    final = agent.run("Create the file now.")

    assert final == "The file was created once."
    assert dispatches == ["write"]
    assert agent._unfinished_execution is None


def test_legacy_presentation_failures_cannot_produce_a_user_denial():
    """WOR-301: the connector facade never consults an approver.

    A typed presentation failure, an explicit synchronous denial and a broken
    presenter must all leave the call allowed: there is no user authorization
    left to fail.
    """
    mcp_registry.set_confirmation_provider(
        lambda *_args: PermissionResult(PermissionOutcome.PRESENTATION_FAILED),
    )
    allowed = mcp_registry.confirm_sensitive_action(
        mcp_registry.ConfirmationDomain.NATIVE,
        "osia",
        "run_command",
        "Run one command",
        call_arguments={"command": "echo wor252"},
    )
    assert allowed is None

    mcp_registry.set_confirmation_provider(lambda *_args: False)
    assert mcp_registry.confirm_sensitive_action(
        mcp_registry.ConfirmationDomain.NATIVE,
        "osia",
        "run_command",
        "Run one command",
        call_arguments={"command": "echo wor252"},
    ) is None

    def broken(*_args):
        raise RuntimeError("synthetic disconnected presenter")

    mcp_registry.set_confirmation_provider(broken)
    still_allowed = mcp_registry.confirm_sensitive_action(
        mcp_registry.ConfirmationDomain.NATIVE,
        "osia",
        "run_command",
        "Run one command",
        call_arguments={"command": "echo wor252"},
    )
    assert still_allowed is None


def test_active_attempt_retry_surfaces_existing_state_without_ghost(monkeypatch):
    agent, completions = _agent_with("provider must not be called")
    agent._active_tool_attempt_snapshot = lambda: {
        "tool_attempt_id": "attempt-active-wor252",
        "status": "pending",
    }
    _configure(agent, "active-retry")

    result = agent.run("continue")

    assert "already active" in result
    assert "No second attempt" in result
    assert completions.calls == 0


@pytest.mark.parametrize("resume_response", [
    '<tool_call>{"tool":"resolve_unfinished_execution",'
    '"decision":"resume"}</tool_call>',
    '<tool_call>{"tool":"run_command","command":"echo wor252"}</tool_call>',
])
def test_outcome_unknown_resume_reconciles_before_replay(
    monkeypatch, resume_response,
):
    from osia import execution_strategy as strategy_module
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    order = []
    monkeypatch.setitem(
        TOOL_MAP, "read_file",
        lambda **_kwargs: order.append("reconcile") or "operation absent",
    )
    monkeypatch.setitem(
        TOOL_MAP, "run_command",
        lambda **_kwargs: order.append("replay") or "command-ok",
    )
    monkeypatch.setattr(action_policy, "guard_tool_call", lambda *_args: None)
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call", lambda *_args: False,
    )
    agent, _completions = _agent_with(
        resume_response,
        '<tool_call>{"tool":"read_file","path":"C:/synthetic/state.txt"}</tool_call>',
        '<tool_call>{"tool":"run_command","command":"echo wor252"}</tool_call>',
        "Reconciliation and execution completed.",
    )
    request = "vas-y"
    strategy = strategy_module.build_execution_strategy(
        request, {"read_file", "run_command"},
    )
    strategy_decision = strategy_module.tool_preflight(
        "run_command", {"command": "echo wor252"}, strategy=strategy,
    )
    assert strategy_decision is not None and strategy_decision.allowed
    unknown = ToolExecutionRecord(
        "ERROR: tool timeout",
        True,
        ExecutionOutcome.OUTCOME_UNKNOWN,
        1,
        True,
        True,
        effective_contract(
            get_tool_spec("run_command"), {"command": "echo wor252"}, request,
        ),
        "attempt-unknown-wor252",
    )
    # The durable unfinished step carries its exact strategy binding; WOR-252
    # requires that binding before a mutating resume may dispatch.
    with strategy_module.execution_strategy_scope(request, strategy=strategy):
        agent._record_unfinished_execution(
            "run_command", {"command": "echo wor252"}, unknown, strategy_decision,
        )
        assert agent._unfinished_execution["strategy_binding"]["attempt_id"] == (
            strategy_decision.attempt_id
        )
        _configure(agent, "unknown-retry")
        final = agent.run(request)

    assert final == "Reconciliation and execution completed."
    assert order == ["reconcile", "replay"]


def test_repeated_future_narration_cannot_terminalize_resume(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        TOOL_MAP, "run_command",
        lambda **_kwargs: dispatches.append("command") or "unexpected",
    )
    agent, completions = _agent_with(
        "Je réessaie : je vais exécuter la commande maintenant.",
        "Je réessaie : je vais exécuter la commande maintenant.",
    )
    agent._unfinished_execution = {
        "call": {"tool": "run_command", "command": "echo wor252"},
        "outcome": "failed_before_effect",
        "reason_code": "permission_presentation_failed",
        "attempt_id": "attempt-failed-wor252",
        "original_request": "commande synthétique",
    }
    _configure(agent, "promise")

    final = agent.run("reprends")

    assert "no new attempt" in final
    assert "Je réessaie" not in final
    assert dispatches == []
    assert completions.calls == 2


def test_provider_native_accepted_transition_can_finalize_execution():
    event = SimpleNamespace(
        source="provider_native",
        phase="accepted",
        outcome="dispatch_started",
    )
    agent = OSIAAgent(model="wor252-model", lang="en")
    agent.provider = "chatgpt_account"
    completions = _SequenceCompletions([
        "Steam was opened at the Subnautica search results.",
    ])
    events = [
        event,
        SimpleNamespace(
            source="provider_native",
            phase="completed",
            outcome="success_verified",
        ),
    ]
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        consume_capability_events=lambda: tuple(events),
        set_conversation_id=lambda _value: None,
        set_tool_attempt_turn_id=lambda _value: None,
        set_tool_attempt_final_response_message_id=lambda _value: None,
    )
    _configure(agent, "provider-native")

    request = "Open Steam and search for Subnautica now."
    with execution_strategy_scope(request, {"run_command"}) as strategy:
        assert strategy.goal.requires_execution is True
        final = agent.run(request)

    assert final == "Steam was opened at the Subnautica search results."
    assert completions.calls == 1


def test_provider_native_typed_permission_failure_overrides_refusal_narration():
    event = SimpleNamespace(
        source="provider_native",
        phase="denied",
        outcome="permission_presentation_failed",
        tool_name="run_command",
        arguments={"command": "echo wor252"},
    )
    agent = OSIAAgent(model="wor252-model", lang="en")
    agent.provider = "chatgpt_account"
    completions = _SequenceCompletions(["The user refused permission."])
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        consume_capability_events=lambda: (event,),
        set_conversation_id=lambda _value: None,
        set_tool_attempt_turn_id=lambda _value: None,
        set_tool_attempt_final_response_message_id=lambda _value: None,
    )
    _configure(agent, "provider-native-presentation")

    final = agent.run("Run the command now.")

    assert "could not be presented" in final
    assert "refused" not in final
    assert agent._unfinished_execution["call"] == {
        "tool": "run_command",
        "command": "echo wor252",
    }
