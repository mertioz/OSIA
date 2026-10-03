"""ToolAttempt and ActionTruth stay factual; selected Chat owns completion."""

from osia.tool_contracts import (
    ExecutionOutcome,
    ProcessOutput,
    ToolExecutionRecord,
    effective_contract,
    get_tool_spec,
)
from osia.turn_terminality import TurnTerminalityController


def controller(tmp_path, required=()):
    value = TurnTerminalityController(
        turn_id="typed",
        objective="Create, compile and verify the project",
        execution_required=False,
        storage_dir=tmp_path,
    )
    value.bind_outcome_contract(required)
    return value


def record(tool, result, args, *, identity="attempt", failure=False):
    outcome = (
        ExecutionOutcome.FAILED_AFTER_EFFECT if failure
        else ExecutionOutcome.SUCCESS_VERIFIED
    )
    return ToolExecutionRecord(
        result, failure, outcome, 1, True, False,
        effective_contract(get_tool_spec(tool), args),
        tool_attempt_id=identity,
    )


def test_file_receipt_does_not_become_a_process_fact_or_gate_model_final(tmp_path):
    c = controller(tmp_path, ["APK compiled"])
    path = tmp_path / "verification.json"
    path.write_text('{"compiled":true}')
    args = {"path": str(path), "content": path.read_text()}
    written = record("write_file", "Successfully wrote verification.json", args)
    c.record_execution_record("write_file", written, arguments=args)

    final = "The generated file exists; compilation is not verified."
    assert c.accept_completion(final) == final
    evidence = c.snapshot()["typed_evidence"][-1]
    assert [fact["kind"] for fact in evidence["facts"]] == ["artifact"]
    assert c.receipt_reference(written.result)
    assert not c.snapshot()["proven_postconditions"]


def test_nonzero_process_fact_returns_to_the_same_model_as_feedback(tmp_path):
    c = controller(tmp_path, ["APK built"])
    args = {"command": "javac Main.java"}
    failed_output = ProcessOutput(
        "compiler failed",
        command=args["command"],
        cwd=str(tmp_path),
        stdout="",
        stderr="error: semicolon expected",
        exit_code=1,
    )
    failed = record("run_command", failed_output, args, identity="bad", failure=True)
    c.record_execution_record("run_command", failed, arguments=args)
    assert c.receipt_reference(failed_output)

    assert c.accept_completion("The build succeeded.") == ""
    decision = c.evaluate()
    assert decision.action == "continue"
    assert c.snapshot()["completion_text"] == ""
    assert "exited with codes: 1" in c.snapshot()["completion_feedback"]
    continuation = c.continuation_context(decision)
    assert "semicolon expected" in continuation
    assert not c.snapshot()["proven_postconditions"]

    good_output = ProcessOutput(
        "compiled", command=args["command"], cwd=str(tmp_path),
        stdout="", stderr="", exit_code=0,
    )
    good = record("run_command", good_output, args, identity="good")
    c.record_execution_record("run_command", good, arguments=args)
    final = "Compilation completed; installation is still unverified."
    assert c.accept_completion(final) == final
    process_facts = [
        fact
        for item in c.snapshot()["typed_evidence"]
        for fact in item.get("facts", [])
        if fact.get("kind") == "process"
    ]
    assert [fact["properties"]["exit_code"] for fact in process_facts] == [1, 0]
    assert not c.snapshot()["proven_postconditions"]


def test_optional_result_notes_can_be_added_after_actions_without_creating_facts(tmp_path):
    c = controller(tmp_path, ["APK built"])
    c.record_typed_evidence(
        outcome="command_completed",
        evidence="a real process result",
        attempt_id="attempt",
        facts=[{"kind": "process", "properties": {"exit_code": 0}}],
    )
    prior = list(c.snapshot()["typed_evidence"])
    notes = c.declare_outcomes([
        {"id": "install", "result": "APK installed", "kind": "observation",
         "target": {"resource": "org.example"},
         "expected": {"property": "present", "equals": True}},
    ])
    assert notes["declared"] is True
    assert c.snapshot()["typed_evidence"] == prior
    final = "The build result is recorded; installation has not been verified."
    assert c.accept_completion(final) == final


def test_artifact_freshness_is_rechecked_before_a_final_is_accepted(tmp_path):
    c = controller(tmp_path)
    path = tmp_path / "report.txt"
    path.write_text("A")
    args = {"path": str(path), "content": "A"}
    written = record("write_file", "wrote A", args)
    c.record_execution_record("write_file", written, arguments=args)
    evidence = c.snapshot()["typed_evidence"][-1]
    assert evidence["facts"][0]["kind"] == "artifact"
    assert c.accept_completion(
        "The file is present; build status is unknown."
    ) == "The file is present; build status is unknown."

    path.write_text("B")
    assert c.accept_completion("The old file observation is still current.") == ""
    assert c.snapshot()["typed_evidence"][-1]["stale"] is True
    assert "stale" in c.snapshot()["completion_feedback"].lower()
    assert c.evaluate().action == "continue"


def test_outcome_unknown_stays_factual_and_does_not_veto_a_new_final(tmp_path):
    c = controller(tmp_path)
    c.record_typed_evidence(
        outcome="outcome_unknown",
        evidence="lost response",
        attempt_id="uncertain",
        effectful=True,
    )
    final = "The installation outcome is unknown and is not verified."
    assert c.accept_completion(final) == final
    assert "uncertain" in c.snapshot()["unknown_effects"]
    assert [item["outcome"] for item in c.snapshot()["typed_evidence"]] == [
        "outcome_unknown",
    ]
    assert c.evaluate().terminal_state == "success_verified"


def test_cancellation_still_prevents_a_final_for_the_cancelled_turn(tmp_path):
    c = controller(tmp_path)
    c.request_cancellation(effect_possible=False)
    assert c.accept_completion("The selected model finished.") == ""
    assert c.snapshot()["cancellation"]["requested"] is True
    assert c.evaluate().terminal_state == "user_cancelled"


def test_metadata_and_arbitrary_output_never_become_process_facts(tmp_path):
    c = controller(tmp_path, ["APK built"])
    note = {"id": "build", "result": "APK built", "kind": "process",
            "target": {"command": "javac Main.java"},
            "expected": {"exit_code": 0}}
    metadata = record("declare_outcomes", "declared", {"outcomes": [note]})
    c.record_execution_record(
        "declare_outcomes", metadata, arguments={"outcomes": [note]},
    )
    assert c.receipt_reference(metadata.result) == ""

    shell = record("run_command", "JAVAC_EXIT=0", {"command": "javac Main.java"})
    c.record_execution_record(
        "run_command", shell, arguments={"command": "javac Main.java"},
    )
    assert c.receipt_reference(shell.result) == ""
