"""Process facts originate in actual tool receipts, never file contents."""
from types import SimpleNamespace
import os

import pytest

from osia.tool_contracts import (
    ProcessOutput, normalize_process_facts, bounded_model_result, ToolExecutionRecord,
    ExecutionOutcome, effective_contract, get_tool_spec,
)
from osia.tool_attempts import ToolAttemptStore


def _facts(**changes):
    fields = {"kind": "process", "source": "local_subprocess", "command": "javac Main.java",
              "cwd": "workspace", "exit_code": 1, "stdout": "", "stderr": "';' expected",
              "combined_output": None, "output_truncated": False}
    return {**fields, **changes}


def _started_store(tmp_path, tool="run_command"):
    store = ToolAttemptStore(tmp_path / "attempts")
    created = store.create_attempt(conversation_id="conversation_facts", turn_id="turn_facts",
        tool_call_id="call_facts", tool_name=tool, provider_id="deepseek", model_id="model_facts",
        turn_context={}, argument_fingerprint=store.fingerprint_arguments({"command": "javac Main.java"}),
        side_effect="unknown_high_risk", idempotence="unknown", retry_policy="never_after_dispatch")
    attempt_id = created["tool_attempt_id"]
    store.mark_accepted(attempt_id)
    store.mark_running(attempt_id)
    store.arm_owner_dispatch_boundary(attempt_id)
    _record, claimed = store.claim_owner_dispatch(attempt_id)
    assert claimed
    return store, attempt_id


def _unexpected_native_command(tmp_path):
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt

    store = ToolAttemptStore(tmp_path / "unexpected-attempts")
    client = AppServerClient(
        base_dir=tmp_path / "unexpected-native",
        tool_attempt_store=store,
    )
    client._send_request_no_wait = lambda *_args: None
    attempt = _TurnAttempt(
        thread_id="provider-thread",
        turn_id="provider-turn",
        osia_turn_id="turn",
        model_id="selected-model",
    )
    client._attempts[attempt.thread_id] = attempt
    item = {
        "id": "unexpected-command",
        "type": "commandExecution",
        "status": "completed",
        "command": "compiler command",
        "cwd": str(client.workspace_dir),
        "exitCode": 1,
        "aggregatedOutput": "untrusted provider output",
    }
    return client, attempt, store, item


def test_process_output_preserves_string_contract_and_exact_separate_streams():
    output = ProcessOutput("compiler failed", command="javac Main.java", cwd="workspace",
                           stdout="", stderr="';' expected\n", exit_code=1)
    assert isinstance(output, str)
    assert output == "compiler failed"
    assert output.process_facts["exit_code"] == 1
    assert output.process_facts["stdout"] == ""
    assert output.process_facts["stderr"] == "';' expected\n"
    assert output.process_facts["combined_output"] is None
    assert not hasattr('{"build_succeeded":true,"exit_code":0}', "process_facts")


@pytest.mark.parametrize("changes", [
    {"exit_code": True}, {"exit_code": "0"}, {"exit_code": None},
    {"source": "model"}, {"source": []}, {"command": ""}, {"cwd": ""},
    {"stdout": None}, {"combined_output": "mixed"}, {"output_truncated": "false"},
])
def test_process_fact_shape_rejects_untyped_or_ambiguous_values(changes):
    assert normalize_process_facts(_facts(**changes)) is None


def test_bounded_streams_preserve_truncation_truth():
    normalized = normalize_process_facts(_facts(stdout="x" * 64_001))
    assert len(normalized["stdout"]) == 64_000
    assert normalized["output_truncated"] is True


def test_record_copies_and_display_truncation_preserve_only_trusted_process_facts():
    output = ProcessOutput("compiler output " * 20, command="javac Main.java", cwd="workspace",
                           stdout="actual stdout", stderr="actual stderr", exit_code=1)
    bounded = bounded_model_result(output, limit=12)
    assert isinstance(bounded, ProcessOutput)
    assert "[TRUNCATED]" in bounded
    assert bounded.process_facts == output.process_facts
    contract = effective_contract(get_tool_spec("run_command"), {"command": "javac Main.java"})
    record = ToolExecutionRecord(bounded, False, ExecutionOutcome.SUCCESS_UNVERIFIED, 1, True, False, contract)
    assert record.process_facts == output.process_facts
    copied = ToolExecutionRecord(record.result, record.is_error, record.outcome, record.attempts,
                                 record.dispatched, record.timed_out, record.contract)
    assert copied.process_facts == output.process_facts
    claimed = {"process_facts": output.process_facts, "text": "model-written verification.json"}
    untrusted = ToolExecutionRecord(claimed, False, ExecutionOutcome.SUCCESS_VERIFIED, 1, True, False, contract)
    assert untrusted.process_facts is None


@pytest.mark.parametrize("change", [
    {"outcome": ExecutionOutcome.OUTCOME_UNKNOWN}, {"outcome": ExecutionOutcome.PENDING},
    {"outcome": ExecutionOutcome.CANCELLED_PROVEN}, {"dispatched": False},
    {"timed_out": True}, {"worker_may_be_active": True}, {"tool": "write_file"},
    {"tool": "read_file"},
])
def test_in_memory_record_cannot_project_process_facts_from_ineligible_receipt(change):
    tool = change.get("tool", "run_command")
    contract = effective_contract(get_tool_spec(tool), {"command": "javac Main.java", "path": "Main.java", "content": "x"})
    fields = dict(result="display", is_error=False, outcome=ExecutionOutcome.SUCCESS_VERIFIED,
                  attempts=1, dispatched=True, timed_out=False, contract=contract,
                  process_facts=_facts(exit_code=0))
    fields.update({key: value for key, value in change.items() if key != "tool"})
    assert ToolExecutionRecord(**fields).process_facts is None


def test_completed_private_process_facts_survive_signed_reopen(tmp_path):
    store, attempt_id = _started_store(tmp_path)
    facts = _facts(command="compiler --private-token=SYNTHETIC_SECRET Main.java")
    terminal, _ = store.record_terminal_result(attempt_id, status="succeeded",
        terminal_outcome="success_unverified", dispatch_state="effect_confirmed", process_facts=facts)
    reopened = ToolAttemptStore(tmp_path / "attempts").get_attempt(attempt_id)
    assert terminal["process_facts"] == reopened["process_facts"] == facts
    assert reopened["action_truth"]["effect_truth"] == "observed"
    # Outbox/public truth metadata contains only identity and effect state.
    import json
    assert "SYNTHETIC_SECRET" not in json.dumps(reopened["action_truth"])


@pytest.mark.parametrize("kind", ["unknown", "cancelled", "file"])
def test_unknown_cancelled_or_file_receipt_cannot_acquire_process_facts(tmp_path, kind):
    store, attempt_id = _started_store(tmp_path, "write_file" if kind == "file" else "run_command")
    if kind == "cancelled":
        store.request_cancellation(attempt_id)
    record, _ = store.record_terminal_result(attempt_id,
        status="unknown_after_start" if kind == "unknown" else "succeeded",
        terminal_outcome="outcome_unknown" if kind == "unknown" else "success_unverified",
        dispatch_state="effect_unknown" if kind == "unknown" else "effect_confirmed", process_facts=_facts())
    assert not record.get("process_facts")


@pytest.mark.parametrize("status, exit_code", [("completed", 0), ("failed", 1), ("completed", 2)])
def test_unexpected_native_process_item_never_creates_canonical_facts(tmp_path, status, exit_code):
    client, attempt, store, item = _unexpected_native_command(tmp_path)
    client._complete_native_item(attempt, {
        **item, "status": status, "exitCode": exit_code,
    })
    event = attempt.capability_events[-1]
    assert event.outcome == "outcome_unknown"
    assert event.process_facts is None
    assert event.tool_attempt_id == ""
    assert attempt.outcome_unknown is True
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


@pytest.mark.parametrize("change", ["command", "cwd", "missing_exit", "missing_output", "cancelled"])
def test_unexpected_native_item_never_claims_process_facts(tmp_path, change):
    client, attempt, store, item = _unexpected_native_command(tmp_path)
    terminal = {**item, "status": "failed", "exitCode": 1,
                "aggregatedOutput": "claimed result"}
    if change == "command":
        terminal["command"] = "different command"
    elif change == "cwd":
        terminal["cwd"] = "different cwd"
    elif change == "missing_exit":
        terminal.pop("exitCode")
    elif change == "missing_output":
        terminal.pop("aggregatedOutput")
    else:
        terminal["status"] = "declined"
    client._complete_native_item(attempt, terminal)
    assert attempt.capability_events[-1].process_facts is None
    assert attempt.capability_events[-1].tool_attempt_id == ""
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


@pytest.mark.parametrize("declared", [False, True])
def test_native_effect_is_declined_regardless_of_outcome_declaration(monkeypatch, tmp_path, declared):
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(base_dir=tmp_path / "native", tool_attempt_store=store)
    sent = []
    client._send = sent.append
    strategy = SimpleNamespace(_outcome_controller=SimpleNamespace(outcomes_declared=declared))
    attempt = _TurnAttempt(thread_id="thread_admission", turn_id="native_admission",
        conversation_id="conversation_admission", osia_turn_id="turn_admission", model_id="model_admission",
        execution_strategy=strategy, user_request="Compile the test project")
    item = {"id": "item_admission", "type": "commandExecution", "status": "inProgress",
            "command": "javac Main.java", "cwd": str(client.workspace_dir)}
    monkeypatch.setattr("osia.execution_strategy.tool_preflight", lambda *_args, **_kwargs: None)
    client._resolve_native_approval(attempt, {"method": "item/commandExecution/requestApproval", "request_id": 1,
        "params": {"threadId": attempt.thread_id, "turnId": attempt.turn_id,
                   "itemId": item["id"], "command": item["command"], "cwd": item["cwd"]}})
    assert sent[-1]["result"]["decision"] == "decline"
    assert not list(store.attempts_dir.glob("attempt_*.json"))
    assert attempt.capability_events[-1].reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert attempt.capability_events[-1].process_facts is None


def test_literal_error_stdout_uses_actual_exit_and_cannot_create_safety_block(monkeypatch, tmp_path):
    from subprocess import CompletedProcess
    from osia import agent as agents
    from osia.turn_terminality import TurnTerminalityController
    monkeypatch.setattr(agents, "_authorized_shell_deletion", lambda *_args: (False, None))
    monkeypatch.setattr(agents, "_is_long_running_command", lambda _command: False)
    returned = CompletedProcess("synthetic", 0, "ERROR: BLOCK_SYSTEM_DESTRUCTION", "")
    monkeypatch.setattr(agents, "_run_windows_powershell", lambda *_args: returned)
    monkeypatch.setattr(agents.subprocess, "run", lambda *_args, **_kwargs: returned)
    output = agents.tool_bash("synthetic literal stdout")
    contract = effective_contract(get_tool_spec("bash"), {"command": "synthetic literal stdout"})
    record = agents._completed_process_execution_record(output, contract)
    assert record.process_facts["exit_code"] == 0
    assert record.is_error is False
    controller = TurnTerminalityController(turn_id="literal_stdout", objective="Observe process stdout",
        execution_required=True, storage_dir=tmp_path)
    controller.bind_outcome_contract()
    controller.record_execution_record("bash", record, arguments={"command": "synthetic literal stdout"})
    assert controller.snapshot()["hard_safety_reason"] == ""


def test_long_process_facts_preserve_raw_stream_whitespace(monkeypatch):
    from osia import agent as agents
    monkeypatch.setattr(agents, "_authorized_shell_deletion", lambda *_args: (False, None))
    monkeypatch.setattr(agents, "_is_long_running_command", lambda _command: True)
    class FinishedProcess:
        returncode = 0
        def __init__(self, _command, *, stdout, stderr, **_kwargs):
            stdout.write(b"  stdout with spaces \n")
            stderr.write(b"\tstderr with whitespace\r\n")
        def poll(self):
            return self.returncode
    monkeypatch.setattr(agents.subprocess, "Popen", FinishedProcess)
    output = agents.tool_bash("synthetic long command")
    assert output.process_facts["stdout"] == "  stdout with spaces \n"
    assert output.process_facts["stderr"] == "\tstderr with whitespace\r\n"


def test_successful_artifact_arguments_reach_controller_without_process_proof(tmp_path):
    from osia import agent as agents
    from osia.turn_terminality import TurnTerminalityController
    path = tmp_path / "verification.json"
    path.write_text('{"build_succeeded":true}')
    arguments = {"path": str(path), "content": path.read_text()}
    controller = TurnTerminalityController(turn_id="file_fact", objective="Build and run project",
        execution_required=True, storage_dir=tmp_path / "turn")
    controller.bind_outcome_contract()
    agent = agents.OSIAAgent()
    agent._terminality_controller = controller
    record = ToolExecutionRecord("Successfully wrote file", False, ExecutionOutcome.SUCCESS_VERIFIED,
        1, True, False, effective_contract(get_tool_spec("write_file"), arguments))
    agent._record_unfinished_execution("write_file", arguments, record)
    facts = controller.snapshot()["typed_evidence"][-1]["facts"]
    assert facts[0]["kind"] == "artifact"
    assert facts[0]["target"]["path"] == os.path.normcase(str(path))
    assert not any(fact["kind"] == "process" for fact in facts)


def test_native_event_cannot_become_process_observation_in_controller(tmp_path):
    from osia import agent as agents
    from osia.turn_terminality import TurnTerminalityController
    client, attempt, _store, item = _unexpected_native_command(tmp_path)
    client._complete_native_item(attempt, {**item, "status": "failed", "exitCode": 1,
        "aggregatedOutput": "Main.java: ';' expected"})
    controller = TurnTerminalityController(turn_id=attempt.osia_turn_id, objective="Compile and execute",
        execution_required=True, storage_dir=tmp_path / "turn")
    controller.bind_outcome_contract()
    agent = agents.OSIAAgent()
    agent._terminality_controller = controller
    agent.client = SimpleNamespace(consume_capability_events=lambda: tuple(attempt.capability_events))
    agent._consume_provider_capability_events()
    assert not any(
        fact.get("kind") == "process"
        for entry in controller.snapshot()["typed_evidence"]
        for fact in entry.get("facts", [])
    )
    assert controller.snapshot()["unknown_effects"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_local_process_completed_facts_do_not_freeze_next_distinct_operation(monkeypatch, tmp_path, exit_code):
    from osia import agent as agents
    from osia.action_truth import action_target_for_tool, ActionTruthCoordinator, ActionIntent, CanonicalEffectClass, gate_action
    store = ToolAttemptStore(tmp_path / "attempts")
    arguments = {"command": "compiler command"}
    output = ProcessOutput("compiler returned", command=arguments["command"], cwd=agents._PROJECT_DIR,
        stdout="", stderr="compiler error" if exit_code else "", exit_code=exit_code)
    monkeypatch.setitem(agents.TOOL_MAP, "run_command", lambda **_kwargs: output)
    result = agents._execute_tool_with_policy("run_command", arguments, lang="en", allow_retry=False,
        attempt_context={"enabled": True, "store": store, "conversation_id": "conversation_process",
            "turn_id": "turn_process", "tool_call_id": "call_process", "provider_id": "deepseek",
            "model_id": "model_process", "action_binding": {"operation": "run_command",
                "target": action_target_for_tool("run_command", arguments)}})
    saved = store.get_attempt(result.tool_attempt_id)
    assert saved["terminal_outcome"] == "command_completed"
    assert saved["action_truth"]["effect_truth"] == "observed"
    action = ActionTruthCoordinator(store).action_record(saved["action_truth"]["action_id"])
    assert gate_action(ActionIntent(CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
        semantic_effect_id="write_corrected_source"), [action]).allowed
    assert gate_action(ActionIntent(CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
        semantic_effect_id=action.semantic_effect_id), [action]).allowed


def test_signed_process_facts_are_restored_on_suspended_resume_without_reexecution(monkeypatch, tmp_path):
    from osia import agent as agents
    store, attempt_id = _started_store(tmp_path)
    store.record_terminal_result(attempt_id, status="failed_after_effect", terminal_outcome="command_completed",
        dispatch_state="effect_confirmed", process_facts=_facts())
    reopened = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr("osia.tool_attempts.get_tool_attempt_store", lambda: reopened)
    agent = agents.OSIAAgent()
    contract = effective_contract(get_tool_spec("run_command"), {"command": "javac Main.java"})
    original = ToolExecutionRecord("pending", True, ExecutionOutcome.PENDING, 1, True, False, contract, attempt_id)
    agent._pending_tool_runtime = {"attempt_id": attempt_id, "record": original}
    monkeypatch.setattr(agent, "_verify_local_reconciliation_probe", lambda *_args: pytest.fail("known process must not launch a probe"))
    restored = agent._verify_suspended_tool(attempt_id)
    assert restored.process_facts == _facts()
    assert restored.outcome is ExecutionOutcome.FAILED_AFTER_EFFECT
    assert "';' expected" in restored.result
    assert reopened.get_attempt(attempt_id)["action_truth"]["effect_truth"] == "observed"


def test_raw_durable_resume_payload_reinjects_process_facts_into_same_turn(monkeypatch, tmp_path):
    from osia import agent as agents
    from osia.turn_terminality import TurnTerminalityController
    store, attempt_id = _started_store(tmp_path)
    durable, _ = store.record_terminal_result(attempt_id, status="failed_after_effect",
        terminal_outcome="command_completed", dispatch_state="effect_confirmed", process_facts=_facts())
    monkeypatch.setattr("osia.tool_attempts.get_tool_attempt_store", lambda: ToolAttemptStore(store.root))
    agent = agents.OSIAAgent()
    agent.provider, agent.model = durable["provider_id"], durable["model_id"]
    contract = effective_contract(get_tool_spec("run_command"), {"command": "javac Main.java"})
    original = ToolExecutionRecord("pending", True, ExecutionOutcome.PENDING, 1, True, False, contract, attempt_id)
    agent._pending_tool_runtime = {"attempt_id": attempt_id, "record": original}
    controller = TurnTerminalityController(turn_id=durable["turn_id"], objective="Compile and run project",
        execution_required=True, storage_dir=tmp_path / "turn")
    controller.bind_outcome_contract()
    agent._terminality_controller = controller
    agent._terminality_strategy = SimpleNamespace(attempts=[], goal=SimpleNamespace(observable_success_postcondition=""))
    monkeypatch.setattr(agents._execution_strategy, "record_resumed_tool_outcome", lambda *_args: None)
    def continued(*_args, **kwargs):
        assert "';' expected" in kwargs["initial_input"]
        evidence = controller.snapshot()["typed_evidence"][-1]
        assert evidence["outcome"] == "command_completed"
        assert evidence["facts"][0]["kind"] == "process"
        assert evidence["facts"][0]["properties"]["exit_code"] == 1
        return "resumed provider boundary"
    monkeypatch.setattr(agent, "_drive_terminal_turn", continued)
    assert agent._resume_suspended_tool(attempt_id, durable) == "resumed provider boundary"
