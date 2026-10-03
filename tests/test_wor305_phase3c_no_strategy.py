"""Canonical Chat takes procedure from the selected model, never strategy state."""
import json
import sys
from types import SimpleNamespace

import pytest

from osia import agent as runtime, execution_strategy as legacy
from tests.test_wor305_phase3a_no_replay import dispatch
from tests.test_wor305_phase3b_completion import call, controller, scripted


@pytest.fixture
def forbid_strategy(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Canonical turn consulted ExecutionStrategy")
    for name in ("build_execution_strategy", "tool_preflight", "record_tool_outcome",
                 "record_resumed_tool_outcome", "select_local_reconciliation_probe"):
        monkeypatch.setattr(legacy, name, forbidden)
    monkeypatch.setattr(legacy.ExecutionStrategy, "__init__", forbidden)
    monkeypatch.setattr(legacy.ExecutionStrategy, "from_snapshot", forbidden)
    return forbidden


@pytest.mark.parametrize("objective,answer", [
    ("What is 2 + 2?", "Four."), ("Write a dinner invitation.", "Join us for dinner on Friday.")])
def test_direct_final_without_strategy(dispatch, monkeypatch, forbid_strategy, objective, answer):
    a, calls = scripted(monkeypatch, [answer])
    assert a.run_terminal_turn(objective, turn_id="3c-direct") == answer
    assert len(calls) == 1
    assert not a._test_controller.snapshot().get("strategy_snapshot")
    assert a.last_execution_strategy_snapshot is None


def test_model_order_repeats_and_shell_without_strategy(dispatch, monkeypatch, tmp_path, forbid_strategy):
    path = tmp_path / "order.txt"
    command = ("& " if sys.platform == "win32" else "") + f'"{sys.executable}" -c "print(305)"'
    responses = [call("write_file", path=str(path), content="first") +
                 call("read_file", path=str(path)) +
                 call("write_file", path=str(path), content="second"),
                 call("run_command", command=command), call("run_command", command=command),
                 call("read_file", path=str(path)), "Done."]
    a, requests = scripted(monkeypatch, responses)
    original = runtime._execute_tool_with_policy
    order = []
    def execute(name, args, **kwargs):
        assert legacy.current_execution_strategy() is None
        record = original(name, args, **kwargs)
        order.append((name, str(record.result)))
        return record
    monkeypatch.setattr(runtime, "_execute_tool_with_policy", execute)
    # Even an ambient legacy context must not cross into a new canonical turn.
    token = legacy._ACTIVE_STRATEGY.set(SimpleNamespace(frontier=[], exhausted=True, completed=True))
    try:
        assert a.run_terminal_turn("Create, inspect, change the file; run the command twice, inspect again.", turn_id="3c-order") == "Done."
    finally:
        legacy._ACTIVE_STRATEGY.reset(token)
    assert [name for name, _ in order] == ["write_file", "read_file", "write_file", "run_command", "run_command", "read_file"]
    assert "first" in order[1][1] and "second" in order[-1][1]
    assert path.read_text() == "second"
    state = a._test_controller.snapshot()
    assert not state["outcome_contract"]["declared"]
    assert len([f for e in state["typed_evidence"] for f in e.get("facts", []) if f.get("kind") == "process"]) == 2
    assert not any(m.get("_execution_strategy") for r in requests for m in r["messages"])


def test_optional_notes_use_controller_directly(dispatch, monkeypatch, forbid_strategy):
    a, _ = scripted(monkeypatch, [call("declare_outcomes", outcomes=[{"id": "draft", "result": "invitation"}]), "Join us for dinner."])
    assert a.run_terminal_turn("Write an invitation", turn_id="3c-notes") == "Join us for dinner."
    assert a._test_controller.snapshot()["outcome_contract"]["requirements"] == [{"id": "draft", "result": "invitation"}]


@pytest.mark.parametrize("strategy_state", [
    {"strategy_frontier": [], "mechanisms_exhausted": True},
    {"remaining_work": [], "proven_postconditions": {"original_objective_satisfied": "strategy says done"}},
    {"frozen_reason": "strategy_blocked", "final_diagnostic": {"code": "blocked"}},
    {"latest_desktop_observation": {"visual_failure_reason": "CAPTURE_PROTECTED"}},
])
def test_strategy_snapshot_cannot_terminalize_or_override_model(tmp_path, strategy_state):
    c = controller(tmp_path)
    before = c.snapshot()
    c.set_strategy_snapshot(strategy_state)
    assert c.snapshot() == before  # No duplicate mutable strategy facts.
    c.set_strategy_frontier(())
    assert c.evaluate().action == "continue"
    assert not c.reserve_final("success_verified", reason_code="all_required_postconditions_proven", final_text="invented")
    assert not c.reserve_final("blocked_verified", reason_code="strategy_blocked", final_text="invented")
    assert c.accept_completion("The requested answer.") == "The requested answer."
    decision = c.evaluate()
    assert decision.reason_code == "selected_model_completed"
    assert c.reserve_final(decision.terminal_state, reason_code=decision.reason_code, final_text="The requested answer.")


def test_old_persisted_frontier_and_completion_are_non_authoritative(tmp_path):
    c = controller(tmp_path)
    c._state.update(strategy_frontier=["preferred_tool"], remaining_work=[],
                    strategy_snapshot={"complete": True, "blocked": True})
    c._persist()
    assert c.evaluate().action == "continue"
    c.accept_completion("Done.")
    assert c.reserve_final("success_verified", reason_code="selected_model_completed", final_text="Done.")


def test_canonical_pending_does_not_select_strategy_probe(dispatch, monkeypatch, forbid_strategy, tmp_path):
    a = runtime.OSIAAgent(lang="en")
    a._terminality_controller = controller(tmp_path)
    a._terminality_strategy = None
    assert a._verify_local_reconciliation_probe("attempt", {}, {}, None) is None


def test_native_approval_is_declined_without_strategy(dispatch, monkeypatch, tmp_path, forbid_strategy):
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
    from osia.tool_attempts import ToolAttemptStore
    client = AppServerClient(base_dir=tmp_path / "native", tool_attempt_store=ToolAttemptStore(tmp_path / "native-attempts"))
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(thread_id="thread", turn_id="native", conversation_id="conversation",
        osia_turn_id="3c", client_request_id="request", model_id="selected", user_request="Run command")
    item = {"id": "command", "type": "commandExecution", "command": "echo 305", "cwd": str(client.workspace_dir)}
    client._resolve_native_approval(attempt, {"method": "item/commandExecution/requestApproval", "request_id": 1,
        "params": {"threadId": "thread", "turnId": "native", "itemId": "command", "command": item["command"], "cwd": item["cwd"]}})
    assert sent[-1]["result"]["decision"] == "decline"
    assert attempt.capability_events[-1].reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert attempt.capability_events[-1].outcome == "rejected_before_execution"
    assert attempt.events.empty()


def test_durable_receipt_resumes_without_strategy(dispatch, monkeypatch, tmp_path, forbid_strategy):
    command = ("& " if sys.platform == "win32" else "") + f'"{sys.executable}" -c "print(305)"'
    result = dispatch("run_command", {"command": command})
    assert result.process_facts["exit_code"] == 0
    monkeypatch.setattr("osia.tool_attempts.get_tool_attempt_store", lambda: dispatch.store)
    a = runtime.OSIAAgent(lang="en")
    a.provider, a.model = "ollama", "fixture"
    from osia.turn_terminality import TurnTerminalityController

    durable = dispatch.store.get_attempt(result.tool_attempt_id)
    a._terminality_controller = c = TurnTerminalityController(
        turn_id=durable["turn_id"],
        objective="Create and build the requested project",
        execution_required=True,
        storage_dir=tmp_path / "resume-turn",
    )
    a._terminality_strategy = None
    c.set_attempt_state(result.tool_attempt_id, status="running")
    def drive(actual_controller, strategy, **kwargs):
        assert actual_controller is c and strategy is None
        assert "Repeating an action is allowed" in kwargs["initial_input"]
        state = c.snapshot()
        assert not state["active_attempts"]
        process_evidence = next(
            item for item in reversed(state["typed_evidence"])
            if item.get("facts")
        )
        assert process_evidence["facts"][0]["properties"]["exit_code"] == 0
        return "Observed 305."
    monkeypatch.setattr(a, "_drive_terminal_turn", drive)
    assert a._resume_suspended_tool(result.tool_attempt_id, result) == "Observed 305."


def test_restore_terminal_receipt_uses_factual_continuation_without_strategy(dispatch, monkeypatch, tmp_path, forbid_strategy):
    result = dispatch("write_file", {"path": str(tmp_path / "restore.txt"), "content": "receipt"})
    row = dispatch.store.get_attempt(result.tool_attempt_id)
    monkeypatch.setattr("osia.tool_attempts.get_tool_attempt_store", lambda: dispatch.store)
    registrations = []
    monkeypatch.setattr("osia.tool_attempts.get_tool_attempt_coordinator",
                        lambda: SimpleNamespace(register_suspended=lambda *args, **kw: registrations.append((args, kw))))
    a = runtime.OSIAAgent(lang="en")
    a.provider, a.model = row["provider_id"], row["model_id"]
    a.configure_tool_attempts(conversation_id=row["conversation_id"], turn_id=row["turn_id"], request_id=row["request_id"], final_response_message_id="restored-final")
    from osia.turn_terminality import TurnTerminalityController
    a._terminality_controller = c = TurnTerminalityController(
        turn_id=row["turn_id"], objective="Create requested file", execution_required=True, storage_dir=tmp_path / "restore-turn")
    c.bind_outcome_contract()
    binding = {key: row[key] for key in ("conversation_id", "turn_id", "request_id", "provider_id", "model_id")}
    binding.update(run_id=row["turn_id"], effective_reasoning="medium")
    c.set_execution_binding(binding)
    monkeypatch.setattr(a, "execution_trace_binding", lambda _: binding)
    c.set_attempt_state(result.tool_attempt_id, status="running")
    assert not a._restore_suspended_tool_runtime(row, None)
    assert registrations == []
    assert a._pending_tool_runtime is None
    assert result.tool_attempt_id not in c.snapshot()["active_attempts"]
    assert any(
        item["attempt_id"] == result.tool_attempt_id
        and item["outcome"] == "success_verified"
        for item in c.snapshot()["typed_evidence"]
    )
