"""New model requests always dispatch; history remains factual."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import json
import sys

import pytest

from osia import agent as runtime
from osia.action_truth import ActionIntent, CanonicalEffectClass, gate_action, retry_decision
from osia.tool_attempts import ToolAttemptStore
from osia.tool_contracts import ExecutionOutcome


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    import osia.tool_attempts as attempts
    monkeypatch.setattr(attempts, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(attempts, "_DEFAULT_STORE", None)
    monkeypatch.setattr(attempts, "_DEFAULT_COORDINATOR", None)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(attempts, "get_tool_attempt_coordinator", lambda: SimpleNamespace(notify_terminal=lambda *_: None))
    monkeypatch.setattr(runtime, "_configured_tool_timeout_seconds", lambda *a, **kw: 30.0)
    import osia.tool_contracts as contracts
    persistence = contracts.persistence_contract
    monkeypatch.setattr(contracts, "persistence_contract", lambda *a, **kw: replace(persistence(*a, **kw), release_after_seconds=30.0))
    store = ToolAttemptStore(tmp_path / "attempts")
    records = []

    def run(tool, arguments):
        context = dict(enabled=True, store=store, conversation_id="repeat", turn_id="repeat",
                       request_id="request", tool_call_id="call_" + str(len(records)),
                       provider_id="ollama", model_id="fixture")
        record = runtime._execute_tool_with_policy(tool, arguments, attempt_context=context,
                                                   allow_retry=False, lang="en")
        records.append(record)
        return record
    run.store, run.records = store, records
    return run


def assert_attempts(dispatch, count):
    assert len(dispatch.records) == count
    assert all(r.dispatched for r in dispatch.records)
    ids = [r.tool_attempt_id for r in dispatch.records]
    assert len(set(ids)) == count
    for ref in ids:
        row = dispatch.store.get_attempt(ref)
        assert row["action_truth"]["dispatch_started_at"]


def test_reads_listings_and_aba_writes(dispatch, tmp_path):
    path = str(tmp_path / "Main.java")  # No Java is run.
    observed = []
    for content in ("A", "B", "A", "A"):
        assert not dispatch("write_file", dict(path=path, content=content)).is_error
        for _ in range(2):
            result = dispatch("read_file", dict(path=path))
            assert not result.is_error
            observed.append(str(result.result))
        for _ in range(2):
            assert not dispatch("list_files", dict(path=str(tmp_path))).is_error
    assert Path(path).read_text() == "A"
    assert observed[0] != observed[2]
    assert observed[0] == observed[4] == observed[6]
    assert_attempts(dispatch, 20)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_identical_real_shell_commands(dispatch, exit_code):
    command = ("& " if sys.platform == "win32" else "") + f'"{sys.executable}" -c "import sys; print(123); sys.exit({exit_code})"'
    if sys.platform == "win32":
        command += "; exit $LASTEXITCODE"
    for _ in range(2):
        result = dispatch("run_command", {"command": command})
        assert result.process_facts["exit_code"] == exit_code
    assert_attempts(dispatch, 2)


def test_failed_build_edit_same_build(dispatch, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("broken")
    script = "import pathlib,sys; sys.exit(0 if pathlib.Path(" + repr(str(source)) + ").read_text() == 'fixed' else 1)"
    command = ("& " if sys.platform == "win32" else "") + f'"{sys.executable}" -c "{script}"'
    if sys.platform == "win32":
        command += "; exit $LASTEXITCODE"
    first = dispatch("run_command", {"command": command})
    dispatch("write_file", {"path": str(source), "content": "fixed"})
    second = dispatch("run_command", {"command": command})
    assert first.is_error and not second.is_error
    assert_attempts(dispatch, 3)


def test_identical_unknown_command_new_attempt_after_reopen(dispatch, monkeypatch):
    calls = []
    def unknown(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("result lost after dispatch")
    monkeypatch.setitem(runtime.TOOL_MAP, "run_command", unknown)
    first = dispatch("run_command", {"command": "adb install app.apk"})
    assert first.outcome in {ExecutionOutcome.OUTCOME_UNKNOWN, ExecutionOutcome.PENDING}
    assert first.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert dispatch.store.get_attempt(first.tool_attempt_id)["status"] == "unknown_after_start"
    second = dispatch("run_command", {"command": "adb install app.apk"})
    assert second.dispatched and len(calls) == 2
    assert_attempts(dispatch, 2)


@pytest.mark.parametrize("state,truth", [("effect_verified", "verified"), ("effect_possible", "possible"),
    ("failed_before_effect", "not_possible"), ("cancelled", "possible")])
def test_factual_history_has_no_dispatch_authority(state, truth):
    assert retry_decision(effect_class="non_idempotent_write", state=state,
                          effect_truth=truth, dispatch_started=True).allowed
    assert gate_action(ActionIntent(CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
                                   semantic_effect_id="same", conflict_scope="same"),
                       [{"semantic_effect_id": "same", "current_state": state,
                         "effect_truth": truth}]).allowed


def test_legacy_persisted_barriers_are_inert(dispatch, monkeypatch):
    monkeypatch.setattr(dispatch.store, "_terminal_records_and_barriers",
                        lambda: pytest.fail("History must not be queried for permission to execute"))
    args = dict(conversation_id="repeat", turn_id="repeat")
    assert not dispatch.store.has_dispatch_barrier(**args, tool_name="write_file", argument_fingerprint="same")
    assert not dispatch.store.has_terminal_barrier(**args, tool_name="write_file", argument_fingerprint="same")
    assert not dispatch.store.has_equivalent_terminal_barrier(**args, equivalence_fingerprint="same")
    assert not dispatch.store.has_unknown_effect_barrier(**args)


def test_legacy_settled_unknown_journal_does_not_lock_conversation(dispatch, monkeypatch):
    import time
    monkeypatch.setitem(runtime.TOOL_MAP, "run_command", lambda **_: "returned")
    old = dispatch.store.create_attempt(conversation_id="repeat", turn_id="repeat",
        tool_call_id="legacy", tool_name="run_command", provider_id="ollama", model_id="fixture",
        turn_context={}, argument_fingerprint=dispatch.store.fingerprint_arguments({"command": "same"}),
        side_effect="unknown_high_risk", idempotence="non_idempotent", retry_policy="never_after_dispatch")
    ref = old["tool_attempt_id"]
    dispatch.store.mark_accepted(ref)
    dispatch.store.arm_owner_dispatch_boundary(ref)
    dispatch.store.mark_running(ref)
    dispatch.store.claim_owner_dispatch(ref)
    dispatch.store.mark_waiting_verification(ref, next_check_at=time.time() + 2,
        deadline_at=time.time() + 60, reason_code="effect_possible_unverified", owner_settled=True)
    result = dispatch("run_command", {"command": "same"})
    assert result.dispatched and result.tool_attempt_id != ref
    assert dispatch.store.get_attempt(ref)["status"] == "waiting_verification"


def test_document_adapter_never_substitutes_cached_result():
    adapter = runtime._SameTurnDocumentSingleFlight()
    calls = []
    def execute(args):
        calls.append(args)
        return object()
    a, reused_a, _ = adapter.execute("read_document", {"path": "x"}, execute)
    b, reused_b, _ = adapter.execute("read_document", {"path": "x"}, execute)
    assert a is not b and len(calls) == 2 and not reused_a and not reused_b


def test_model_loop_retries_failed_read_after_file_created(dispatch, monkeypatch, tmp_path):
    path = str(tmp_path / "appears.txt")
    actions = [("read_file", {"path": path}), ("write_file", {"path": path, "content": "new"}),
               ("read_file", {"path": path}), ("read_file", {"path": path})]
    responses = ["<tool_call>" + json.dumps({"tool": name, **args}) + "</tool_call>" for name, args in actions]
    responses.append("Finished the scripted sequence.")
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def create(self, **kwargs):
            assert responses, "Unexpected extra provider request"
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=responses.pop(0), tool_calls=None))])
    agent = runtime.OSIAAgent(lang="en")
    agent.provider, agent.model, agent.client = "ollama", "fixture", Client()
    monkeypatch.setattr(agent, "_resolve_route", lambda _: ("medium", []))
    agent.configure_tool_attempts(conversation_id="loop", turn_id="loop", request_id="loop", final_response_message_id="loop-final")
    reads = []
    original = runtime.TOOL_MAP["read_file"]
    monkeypatch.setitem(runtime.TOOL_MAP, "read_file", lambda **kw: reads.append(kw) or original(**kw))
    agent.run("Read, create, and read twice.", history=[], canonical_output=False)
    assert len(reads) == 4  # The first failed read also has its normal technical retry.


def test_unknown_history_does_not_force_reconciliation_segment(tmp_path):
    from osia.turn_terminality import TurnTerminalityController
    controller = TurnTerminalityController(turn_id="unknown", objective="repeat", execution_required=True, storage_dir=tmp_path)
    controller.record_typed_evidence(outcome="outcome_unknown", evidence="lost result", attempt_id="old")
    assert controller.snapshot()["unknown_effects"]
    assert controller.evaluate().action == "continue"
    assert controller.begin_invocation(provider_route="fixture", strategy_id="model_choice")


def test_unknown_unfinished_call_can_resume_without_inspection():
    call = {"tool": "run_command", "command": "adb install app.apk"}
    for requested in ([call], [{"tool": "resolve_unfinished_execution", "decision": "resume"}]):
        agent = SimpleNamespace(_unfinished_execution={"call": call, "outcome": "outcome_unknown"},
                                _continuation_resolution_pending=True, _continuation_reconciled=False)
        calls, decision = runtime._apply_unfinished_execution_decision(agent, requested)
        assert calls == [call] and decision == "resume"


def test_visual_identical_action_dispatches_with_current_target(tmp_path):
    # Synthetic pixels/actuator only: never operates the real desktop.
    from tests.test_wor289_computer_use_v4 import _Harness
    harness = _Harness(tmp_path / "visual")
    frame = harness.screenshot()
    action = harness.click(frame, effect_id="same_effect")
    first = harness.loop.execute(action)
    second = harness.loop.execute(action)
    assert first["status"] == second["status"] == "DISPATCHED"
    assert first["attempt_id"] != second["attempt_id"]
    assert len(harness.dispatches) == 2


def test_provider_native_command_cannot_bypass_canonical_dispatch(tmp_path):
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt

    store = ToolAttemptStore(tmp_path / "native-attempts")
    client = AppServerClient(base_dir=tmp_path / "native", tool_attempt_store=store)
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(
        thread_id="provider-thread", turn_id="provider-turn",
        conversation_id="conversation", osia_turn_id="turn",
        model_id="selected", user_request="Run command",
    )
    client._attempts[attempt.thread_id] = attempt

    client._handle_server_request({
        "id": 2,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "item-second",
            "command": "echo marker",
        },
    })

    assert sent[-1] == {"id": 2, "result": {"decision": "decline"}}
    assert attempt.capability_events[-1].reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert attempt.capability_events[-1].tool_attempt_id == ""
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_stale_preflight_still_blocks_but_history_does_not():
    from osia.execution_strategy import preflight_refusal_is_dispatch_blocking
    assert preflight_refusal_is_dispatch_blocking("stale_target_requires_reresolution")
    for reason in ("effect_verified_replay_closed", "identical_failed_action_without_new_evidence",
                   "mechanism_retired_after_success", "goal_effect_already_completed",
                   "ambiguous_effect_requires_reconciliation", "primitive_already_completed"):
        assert not preflight_refusal_is_dispatch_blocking(reason)
