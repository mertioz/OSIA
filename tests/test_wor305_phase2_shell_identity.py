"""Generic shell facts distinguish commands without reopening unknown effects."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from osia import agent as agent_module
from osia.action_truth import (
    ActionIntent, ActionTruthCoordinator, CanonicalEffectClass,
    action_target_for_tool, gate_action, semantic_effect_identity,
)
from osia.capability_federation import mutation_equivalence_arguments
from osia.tool_attempts import ToolAttemptStore
from osia.tool_contracts import ExecutionOutcome, ToolExecutionRecord, effective_contract, get_tool_spec
from osia.turn_terminality import TurnTerminalityController


def _identity(store, arguments, *, name="run_command", node="node_android"):
    equivalence = mutation_equivalence_arguments(name, arguments)
    return semantic_effect_identity(
        turn_id="turn_android", semantic_node_id=node,
        attempted_effect="requested_android_work", operation=name,
        target=action_target_for_tool(name, arguments),
        equivalence_fingerprint=store.fingerprint_arguments(equivalence),
        argument_fingerprint=store.fingerprint_arguments(arguments),
    )


@pytest.mark.parametrize("changed", [
    {"command": "adb install app.apk"},
    {"command": "adb -s device_b devices -l"},
    {"command": "adb devices -l", "cwd": "project_b"},
    {"command": "adb devices -l", "args": ["--different"]},
])
def test_distinct_commands_arguments_targets_and_cwd_have_distinct_identity(tmp_path, changed):
    store = ToolAttemptStore(tmp_path)
    first = {"command": "adb devices -l", "cwd": "project_a"}
    second = {"cwd": "project_a", **changed}
    assert _identity(store, first) != _identity(store, second)


def test_quoted_whitespace_is_not_collapsed():
    assert mutation_equivalence_arguments("bash", {"command": 'send "a b"'}) != (
        mutation_equivalence_arguments("bash", {"command": 'send "a  b"'})
    )


def test_alias_timeout_and_strategy_node_cannot_reopen_identical_effect(tmp_path):
    store = ToolAttemptStore(tmp_path)
    original = _identity(store, {"command": "adb install app.apk", "timeout": 30})
    alternate = _identity(
        store, {"command": "adb install app.apk", "timeout": 120},
        name="bash", node="node_other_plan",
    )
    assert original == alternate


def test_strong_semantic_binding_cannot_discard_shell_arguments_without_equivalence(tmp_path):
    store = ToolAttemptStore(tmp_path)
    identities = {
        semantic_effect_identity(
            turn_id="turn_android", semantic_node_id="same_node",
            attempted_effect="same_effect", operation="run_command", target={},
            argument_fingerprint=store.fingerprint_arguments({"command": command}),
        )
        for command in ("adb devices", "adb install app.apk")
    }
    assert len(identities) == 2


def _dispatch(store, command, *, tool="run_command", timeout=None, sequence=1):
    arguments = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    context = {
        "enabled": True, "store": store,
        "conversation_id": "conversation_shell", "turn_id": "turn_shell",
        "request_id": "request_shell", "tool_call_id": "call_shell_" + str(sequence),
        "provider_id": "deepseek", "model_id": "test_model",
        "action_binding": {
            "semantic_node_id": "same_node", "attempted_effect": "android_work",
            "operation": tool, "target": action_target_for_tool(tool, arguments),
        },
    }
    result = agent_module._execute_tool_with_policy(
        tool, arguments, attempt_context=context, lang="en", allow_retry=False,
    )
    return result, context


def test_completed_devices_install_verify_dispatch_distinct_operations_and_keep_outcomes_unverified(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(agent_module.TOOL_MAP, "run_command", lambda command, **_: calls.append(command) or "command returned")
    store = ToolAttemptStore(tmp_path)
    commands = ["adb devices -l", "adb install app.apk", "adb shell pm path org.example.app"]
    contexts = []
    for sequence, command in enumerate(commands):
        result, context = _dispatch(store, command, sequence=sequence)
        assert result.outcome is ExecutionOutcome.SUCCESS_UNVERIFIED
        contexts.append(context)
    assert calls == commands
    records = [store.get_attempt(context["tool_attempt_id"]) for context in contexts]
    assert len({record["action_truth"]["semantic_effect_id"] for record in records}) == 3
    assert all(record["action_truth"]["effect_truth"] == "observed" for record in records)
    assert all(record["action_truth"]["reconciliation_status"] == "pending" for record in records)
    # Raw commands/arguments are represented by private fingerprints on disk.
    serialized = json.dumps(records)
    assert not any(command in serialized for command in commands)


def test_unknown_action_history_does_not_veto_fresh_dispatches(monkeypatch, tmp_path):
    calls = []

    def uncertain(command, **_):
        calls.append(command)
        return "ERROR: connection lost after dispatch"

    monkeypatch.setitem(agent_module.TOOL_MAP, "run_command", uncertain)
    monkeypatch.setitem(agent_module.TOOL_MAP, "bash", uncertain)
    store = ToolAttemptStore(tmp_path)
    first, _ = _dispatch(store, "adb install app.apk", timeout=30)
    assert first.outcome is ExecutionOutcome.OUTCOME_UNKNOWN

    reopened = ToolAttemptStore(tmp_path)
    second, _ = _dispatch(
        reopened, "adb install app.apk", tool="bash", timeout=120, sequence=2,
    )
    third, _ = _dispatch(
        reopened, "adb install other.apk", sequence=3,
    )

    assert second.dispatched and second.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert third.dispatched and third.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert len({first.tool_attempt_id, second.tool_attempt_id, third.tool_attempt_id}) == 3
    assert calls == [
        "adb install app.apk", "adb install app.apk", "adb install other.apk",
    ]

def test_action_truth_history_is_factual_and_cannot_veto_new_requests(monkeypatch, tmp_path):
    monkeypatch.setitem(agent_module.TOOL_MAP, "run_command", lambda **_: "command returned")
    store = ToolAttemptStore(tmp_path)
    _, context = _dispatch(store, "adb devices -l")
    truth = store.get_attempt(context["tool_attempt_id"])["action_truth"]
    action = ActionTruthCoordinator(store).action_record(truth["action_id"])
    proposals = [
        ActionIntent(CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK,
                     semantic_effect_id=action.semantic_effect_id),
        ActionIntent(CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK,
                     semantic_effect_id="different_effect",
                     dependency_action_ids=(action.action_id,)),
        ActionIntent(CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK,
                     conflict_scope=action.conflict_scope),
    ]

    for proposal in proposals:
        decision = gate_action(proposal, [action])
        assert decision.allowed
        assert decision.reason_code == "new_request_allowed"

def _controller(tmp_path):
    controller = TurnTerminalityController(
        turn_id="turn_shell", objective="Compile the project and verify it",
        required_postconditions=("original_objective_satisfied",),
        execution_required=True, storage_dir=tmp_path,
    )
    controller.bind_outcome_contract()
    return controller


def _shell_record(**overrides):
    fields = dict(
        result="compiler output", is_error=False,
        outcome=ExecutionOutcome.SUCCESS_UNVERIFIED, attempts=1,
        dispatched=True, timed_out=False, worker_may_be_active=False,
        tool_attempt_id="attempt_shell_result",
        contract=effective_contract(get_tool_spec("run_command"), {"command": "javac Main.java"}),
    )
    fields.update(overrides)
    return ToolExecutionRecord(**fields)


def test_completed_command_fact_without_durable_attempt_keeps_projection_active(tmp_path):
    controller = _controller(tmp_path)
    controller.set_attempt_state("attempt_shell_result", status="running")
    controller.record_execution_record("run_command", _shell_record())
    state = controller.snapshot()

    assert state["typed_evidence"][-1]["outcome"] == "command_completed"
    # This fixture deliberately has no matching durable ToolAttempt. A local
    # ToolExecutionRecord is a fact, but it cannot terminalize another attempt.
    assert state["active_attempts"]["attempt_shell_result"]["status"] == "running"
    assert not state["unknown_effects"]
    assert not state["proven_postconditions"]
    assert controller.evaluate().action == "wait"

@pytest.mark.parametrize("overrides", [
    {"outcome": ExecutionOutcome.OUTCOME_UNKNOWN, "is_error": True},
    {"timed_out": True},
    {"worker_may_be_active": True},
    {"dispatched": False},
    {"is_error": True},
])
def test_ambiguous_or_unconfirmed_shell_receipts_are_not_promoted(tmp_path, overrides):
    controller = _controller(tmp_path)
    controller.record_execution_record("run_command", _shell_record(**overrides))
    state = controller.snapshot()
    assert state["typed_evidence"][-1]["outcome"] != "command_completed"
    assert state["unknown_effects"]
    assert not state["proven_postconditions"]


def test_known_shell_result_cannot_clear_prior_unknown_effect(tmp_path):
    controller = _controller(tmp_path)
    controller.record_execution_record("run_command", _shell_record(outcome=ExecutionOutcome.OUTCOME_UNKNOWN))
    controller.record_execution_record("run_command", _shell_record())
    assert controller.snapshot()["unknown_effects"]
    assert controller.evaluate().terminal_state != "success_verified"


def test_non_shell_unverified_mutation_still_requires_reconciliation(tmp_path):
    controller = _controller(tmp_path)
    record = _shell_record(contract=effective_contract(
        get_tool_spec("write_file"), {"path": "Main.java", "content": "class Main {}"},
    ))
    controller.record_execution_record("write_file", record)
    state = controller.snapshot()
    assert state["typed_evidence"][-1]["outcome"] == "success_unverified"
    assert state["unknown_effects"]


def test_compiler_exit_failure_is_feedback_and_same_build_can_be_dispatched_again(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "run_command",
        lambda command, **_: calls.append(command)
        or "Main.java: compiler error\n[exit code 1]",
    )
    store = ToolAttemptStore(tmp_path / "attempts")
    first, _ = _dispatch(store, "javac Main.java")
    controller = _controller(tmp_path / "turn")
    controller.record_execution_record("run_command", first)

    assert "[exit code 1]" in first.result
    assert controller.snapshot()["typed_evidence"][-1]["outcome"] == "command_completed"
    assert not controller.snapshot()["proven_postconditions"]

    rebuilt, _ = _dispatch(store, "javac Main.java", sequence=2)
    assert rebuilt.dispatched
    assert rebuilt.tool_attempt_id != first.tool_attempt_id
    assert len(calls) == 2
