"""A system-destruction refusal is a durable action result, not turn finality."""

import json

from osia import action_policy
from osia import agent as runtime
from osia.tool_contracts import ExecutionOutcome
from osia.turn_terminality import TurnTerminalityController
from tests.test_wor305_phase3a_no_replay import dispatch
from wor305_test_support import scripted


def _synthetic_barrier(monkeypatch):
    original = action_policy._system_mutation_barrier

    def classify(tool_name, arguments):
        if tool_name == "run_command" and arguments.get("command") == "synthetic-block":
            return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
        return original(tool_name, arguments)

    monkeypatch.setattr(action_policy, "_system_mutation_barrier", classify)


def _call(tool, **arguments):
    return "<tool_call>" + json.dumps({"tool": tool, **arguments}) + "</tool_call>"


def test_repeated_barrier_refusals_are_durable_without_dispatch_or_replay(dispatch, monkeypatch, tmp_path):
    _synthetic_barrier(monkeypatch)
    monkeypatch.setitem(runtime.TOOL_MAP, "run_command", lambda **_: (_ for _ in ()).throw(
        AssertionError("blocked action dispatched")))
    path = tmp_path / "harmless.txt"
    path.write_text("safe observation", encoding="utf-8")
    controller = TurnTerminalityController(
        turn_id="barrier-repeat", objective="inspect a harmless file",
        execution_required=True, storage_dir=tmp_path / "turn",
    )
    all_records = []
    for index in range(2):
        arguments = {"command": "synthetic-block"}
        blocked = runtime._execute_tool_with_policy(
            "run_command", arguments, allow_retry=False, lang="en",
            preflight_error=action_policy.guard_tool_call("inspect", "run_command", arguments),
            attempt_context={
                "enabled": True, "store": dispatch.store,
                "conversation_id": "repeat", "turn_id": "repeat",
                "request_id": "request", "tool_call_id": f"blocked_{index}",
                "provider_id": "ollama", "model_id": "fixture",
            },
        )
        all_records.append(blocked)
        assert blocked.outcome is ExecutionOutcome.REJECTED_BEFORE_EXECUTION
        assert blocked.is_error and not blocked.dispatched
        assert blocked.result == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
        controller.record_execution_record("run_command", blocked)
        receipt = dispatch.store.get_attempt(blocked.tool_attempt_id)
        assert receipt["status"] == "failed_before_effect"
        assert receipt["terminal_outcome"] == "rejected_before_execution"
        assert receipt["dispatch_state"] == "not_dispatched"
        harmless = dispatch("read_file", {"path": str(path)})
        all_records.append(harmless)
        assert harmless.dispatched and not harmless.is_error
        assert "safe observation" in str(harmless.result)
        controller.record_execution_record("read_file", harmless)
    state = controller.snapshot()
    assert controller.evaluate().action == "continue"
    assert state["hard_safety_reason"] == ""
    assert state["unknown_effects"] == {}
    assert [item["outcome"] for item in state["typed_evidence"]] == [
        "rejected_before_execution", "success_verified",
        "rejected_before_execution", "success_verified",
    ]
    assert len({record.tool_attempt_id for record in all_records}) == 4
    assert "run_command" in runtime._active_chat_tool_names()


def test_same_selected_model_continues_to_real_read_and_final(dispatch, monkeypatch, tmp_path):
    _synthetic_barrier(monkeypatch)
    monkeypatch.setitem(runtime.TOOL_MAP, "run_command", lambda **_: (_ for _ in ()).throw(
        AssertionError("blocked action dispatched")))
    path = tmp_path / "harmless.txt"
    path.write_text("safe observation", encoding="utf-8")
    agent, calls = scripted(monkeypatch, [
        _call("run_command", command="synthetic-block"),
        _call("read_file", path=str(path)),
        "The harmless file contains safe observation.",
    ])
    result = agent.run_terminal_turn("Inspect the harmless file.", turn_id="3b")
    assert result == "The harmless file contains safe observation."
    assert len(calls) == 3
    assert all(call["model"] == "fixture" for call in calls)
    assert "BLOCK_SYSTEM_DESTRUCTION" in str(calls[1]["messages"])
    assert "safe observation" in str(calls[2]["messages"])
    state = agent._test_controller.snapshot()
    assert state["terminal_state"] == "success_verified"
    assert state["terminal_reason"] == "selected_model_completed"
    assert state["hard_safety_reason"] == ""
    outcomes = [item["outcome"] for item in state["typed_evidence"]]
    # One exact ToolAttempt can project the same factual result through both
    # execution and receipt reconciliation; duplicate ledger entries do not
    # mean the rejected action was dispatched again.
    assert "rejected_before_execution" in outcomes
    assert "success_verified" in outcomes
    assert outcomes.index("rejected_before_execution") < outcomes.index("success_verified")
