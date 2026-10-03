"""Tool data cannot impersonate the system-destruction policy barrier."""

import pytest

from osia.tool_contracts import (
    ExecutionOutcome, ToolExecutionRecord, effective_contract, get_tool_spec,
)
from osia.turn_terminality import TurnTerminalityController


def _record(result, *, failed=False, reason=""):
    return ToolExecutionRecord(
        result=result,
        is_error=failed,
        outcome=(
            ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
            if failed else ExecutionOutcome.SUCCESS_VERIFIED
        ),
        attempts=1,
        dispatched=True,
        timed_out=False,
        contract=effective_contract(
            get_tool_spec("read_file"), {"path": "source.py"}, "inspect source",
        ),
        reason_code=reason,
    )


def _controller(tmp_path):
    return TurnTerminalityController(
        turn_id="safety-truth", objective="inspect and build the project",
        required_postconditions=("the project builds successfully",),
        execution_required=True, storage_dir=tmp_path,
    )


@pytest.mark.parametrize("result", [
    'return "ERROR: BLOCK_SYSTEM_DESTRUCTION"',
    "ERROR: BLOCK_SYSTEM_DESTRUCTION",
    {"text": "Documented BLOCK_SYSTEM_DESTRUCTION policy", "receipt": {"outcome": "success_verified"}},
])
def test_successful_read_containing_guard_name_is_data(tmp_path, result):
    controller = _controller(tmp_path)
    controller.record_execution_record("read_file", _record(result))
    assert controller.evaluate().action == "continue"
    assert not controller.snapshot().get("hard_safety_reason")


def test_failed_build_quoting_guard_source_is_feedback(tmp_path):
    controller = _controller(tmp_path)
    controller.record_execution_record(
        "bash", _record(
            'Compiler error in line: return "ERROR: BLOCK_SYSTEM_DESTRUCTION"',
            failed=True, reason="command_blocked",
        ),
    )
    assert controller.evaluate().action == "continue"
    assert not controller.snapshot().get("hard_safety_reason")


@pytest.mark.parametrize("reason", ["block_system_destruction", "system_destruction_absolute"])
def test_successful_receipt_cannot_promote_a_stale_barrier_reason(tmp_path, reason):
    controller = _controller(tmp_path)
    controller.record_execution_record("read_file", _record("source content", reason=reason))
    assert controller.evaluate().action == "continue"
    assert not controller.snapshot().get("hard_safety_reason")


@pytest.mark.parametrize("outcome", ["success_verified", "success_unverified", "observed", "pending", "cancelled_proven"])
def test_nonfailure_evidence_cannot_promote_a_barrier_reason(tmp_path, outcome):
    controller = _controller(tmp_path)
    controller.record_typed_evidence(
        outcome=outcome, evidence="typed feedback",
        reason_code="block_system_destruction",
    )
    assert controller.evaluate().terminal_state != "hard_safety_block"
    assert not controller.snapshot().get("hard_safety_reason")


@pytest.mark.parametrize("outcome", ["hard_safety_block", "rejected_before_execution", "confirmed_failure_before_effect", "failed_before_effect"])
def test_factual_barrier_evidence_remains_action_level(tmp_path, outcome):
    controller = _controller(tmp_path)
    controller.record_typed_evidence(
        outcome=outcome, evidence="policy refusal",
        reason_code="block_system_destruction",
    )
    assert controller.evaluate().action == "continue"
    assert not controller.snapshot().get("hard_safety_reason")
    assert controller.snapshot()["typed_evidence"][-1]["outcome"] == outcome


@pytest.mark.parametrize(("result", "reason"), [
    ("ERROR: BLOCK_SYSTEM_DESTRUCTION", "command_blocked"),
    ("ERROR: blocked", "block_system_destruction"),
    ("ERROR: blocked", "system_destruction_absolute"),
    ({"error": {"code": "BLOCK_SYSTEM_DESTRUCTION"}}, "command_blocked"),
    ({"receipt": {"reason_code": "system_destruction_absolute"}}, "command_blocked"),
])
def test_actual_system_destruction_refusal_does_not_end_turn(tmp_path, result, reason):
    controller = _controller(tmp_path)
    controller.record_execution_record(
        "delete_file", _record(result, failed=True, reason=reason),
    )
    assert controller.evaluate().action == "continue"
    assert not controller.snapshot().get("hard_safety_reason")
