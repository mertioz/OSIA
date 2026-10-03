"""Canonical chat receipts cannot be contradicted by legacy strategy state."""

import json

from osia import agent as runtime
from osia import execution_strategy as legacy
from osia.tool_contracts import ExecutionOutcome
from tests.test_wor305_phase3a_no_replay import dispatch
from tests.test_wor305_phase3b_completion import call, controller, scripted


def test_canonical_chat_receipt_continuation_is_factual_and_repeats_remain_legal(
    dispatch, monkeypatch, tmp_path,
):
    import osia.tool_attempts as attempts

    marker = "WOR305_RECEIPT_TRUTH_OK"
    path = tmp_path / "receipt.txt"
    path.write_text(marker, encoding="utf-8")
    objective = (
        "Read the exact UTF-8 file at this path using read_file and report "
        f"the observed content: {path}"
    )
    monkeypatch.setattr(attempts, "get_tool_attempt_store", lambda: dispatch.store)

    agent, provider_calls = scripted(monkeypatch, [
        call("read_file", path=str(path)),
        call("read_file", path=str(path)),
        f"The file contains {marker}.",
    ])
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-sol"

    # A restored legacy strategy contains an unverified objective postcondition.
    persisted = legacy.build_execution_strategy(
        objective,
        {"read_file"},
        postconditions=(f"the observed file content is {marker}",),
    ).snapshot()
    stale_strategy = legacy.ExecutionStrategy.from_snapshot(
        json.loads(json.dumps(persisted))
    )
    assert "unverified_outcomes" in stale_strategy.model_instruction()
    assert stale_strategy.required_postconditions

    execution_contexts = []
    execution_records = []
    original_execute = runtime._execute_tool_with_policy

    def capture_execution(*args, **kwargs):
        execution_contexts.append(legacy.current_execution_strategy())
        record = original_execute(*args, **kwargs)
        execution_records.append(record)
        return record

    monkeypatch.setattr(runtime, "_execute_tool_with_policy", capture_execution)
    token = legacy._ACTIVE_STRATEGY.set(stale_strategy)
    try:
        final = agent.run(objective, current_conversation_id="receipt-truth")
    finally:
        legacy._ACTIVE_STRATEGY.reset(token)

    assert final == f"The file contains {marker}."
    assert len(provider_calls) == 3  # Two identical calls reached the executor.
    assert all(item["model"] == "gpt-5.6-sol" for item in provider_calls)
    assert agent.provider == "chatgpt_account"
    assert execution_contexts == [None, None]
    assert agent.last_execution_strategy_snapshot is None
    assert "read_file" in runtime.TOOL_MAP
    assert "read_file" in runtime._active_chat_tool_names()

    continuation = json.dumps(
        provider_calls[1]["messages"], ensure_ascii=False, sort_keys=True,
    )
    tool_result_message = next(
        message["content"]
        for message in provider_calls[1]["messages"]
        if message.get("role") == "user"
        and "<tool_result>" in str(message.get("content") or "")
    )
    assert any(
        message.get("role") == "user"
        and objective in str(message.get("content") or "")
        for message in provider_calls[1]["messages"]
    )
    assert "<tool_result>" in tool_result_message
    assert marker in tool_result_message.split("<tool_result>", 1)[1].split(
        "</tool_result>", 1,
    )[0]
    assert "[OSIA execution state." not in continuation
    assert "unverified_outcomes" not in continuation

    rows = [
        dispatch.store.get_attempt(record.tool_attempt_id)
        for record in execution_records
    ]
    assert len(rows) == 2
    assert len({row["tool_attempt_id"] for row in rows}) == 2
    for row in rows:
        assert row["status"] == "succeeded"
        assert row["terminal_outcome"] == "success_verified"
        assert row["dispatch_state"] == "effect_confirmed"
        assert row["provider_id"] == "chatgpt_account"
        assert row["model_id"] == "gpt-5.6-sol"
        assert row["action_truth"]["operation"] == "read_file"
        assert row["action_truth"]["effect_truth"] == "verified"
        assert row["action_truth"]["current_state"] == "effect_verified"
        assert row["action_truth"]["current_reason"] == "tool_receipt"

    objective_state = controller(tmp_path, objective)
    objective_state.record_execution_record(
        "read_file", execution_records[0], arguments={"path": str(path)},
    )
    assert not objective_state.snapshot()["proven_postconditions"]


def test_failure_and_unknown_receipts_keep_their_factual_outcomes(
    dispatch, monkeypatch, tmp_path,
):
    missing = dispatch(
        "read_file", {"path": str(tmp_path / "does-not-exist.txt")},
    )
    failed = dispatch.store.get_attempt(missing.tool_attempt_id)
    assert missing.outcome is ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert failed["status"] == "failed_before_effect"
    assert failed["terminal_outcome"] == "confirmed_failure_before_effect"

    def lose_result(**_kwargs):
        raise ConnectionError("synthetic post-dispatch result loss")

    monkeypatch.setitem(runtime.TOOL_MAP, "run_command", lose_result)
    unknown = dispatch("run_command", {"command": "synthetic-readonly-test"})
    uncertain = dispatch.store.get_attempt(unknown.tool_attempt_id)
    assert unknown.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert uncertain["status"] == "unknown_after_start"
    assert uncertain["terminal_outcome"] == "outcome_unknown"
    assert uncertain["dispatch_state"] == "effect_unknown"


def test_legacy_strategy_remains_available_without_canonical_chat_receipts(
    monkeypatch,
):
    agent, _provider_calls = scripted(monkeypatch, ["Four."])
    agent.clear_tool_attempts()

    assert agent.run("What is 2 + 2?") == "Four."
    assert agent.last_execution_strategy_snapshot is not None
