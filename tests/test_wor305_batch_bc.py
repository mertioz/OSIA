"""Focused WOR-305 Batch B terminality reconciliation regressions."""

from __future__ import annotations

import json

import pytest

from osia.tool_attempts import (
    ToolAttemptStatus,
    ToolAttemptStore,
)
from osia.turn_terminality import TurnTerminalityController, WaitType


PENDING_TOOL_WAIT = WaitType.WAIT_PENDING_TOOL.value


def _binding(turn_id, *, conversation="conversation-1", request="request-1",
             provider="provider-a", model="model-a"):
    return {
        "conversation_id": conversation,
        "request_id": request,
        "turn_id": turn_id,
        "run_id": turn_id,
        "provider_id": provider,
        "model_id": model,
        "effective_reasoning": "medium",
    }


@pytest.fixture
def attempt_store(tmp_path, monkeypatch):
    import osia.tool_attempts as tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "get_tool_attempt_store", lambda: store)
    return store


def _controller(tmp_path, turn_id="turn-1", **binding_fields):
    controller = TurnTerminalityController(
        turn_id=turn_id,
        objective="Complete the requested work",
        execution_required=True,
        storage_dir=tmp_path / "turns",
    )
    controller.set_execution_binding(_binding(turn_id, **binding_fields))
    return controller


def _new_attempt(
    store,
    *,
    turn_id="turn-1",
    conversation_id="conversation-1",
    request_id="request-1",
    provider_id="provider-a",
    model_id="model-a",
    call_id="call-1",
    native_call_id=None,
    owner_scope="chat",
    tool_name="read_file",
    arguments=None,
    running=True,
    final_response_message_id="",
):
    arguments = dict(arguments or {"path": "same/path"})
    row = store.create_attempt(
        conversation_id=conversation_id,
        turn_id=turn_id,
        request_id=request_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        provider_id=provider_id,
        model_id=model_id,
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments(arguments),
        side_effect="read_only",
        idempotence="idempotent",
        retry_policy="never_after_dispatch",
        owner_scope=owner_scope,
        final_response_message_id=final_response_message_id,
        provider_references={
            "tool_call_id": call_id,
            "provider_native_call_id": native_call_id or call_id,
            "provider_request_id": request_id,
        },
    )
    row = store.mark_accepted(row["tool_attempt_id"])
    if running:
        row = store.mark_running(row["tool_attempt_id"])
    return row


def _finish(
    store,
    attempt_id,
    *,
    status,
    outcome,
    dispatch_state="not_dispatched",
    reason="test_terminal_receipt",
):
    return store.record_terminal_result(
        attempt_id,
        status=status,
        terminal_outcome=outcome,
        dispatch_state=dispatch_state,
        reason_code=reason,
    )[0]


@pytest.mark.parametrize(
    ("status", "outcome", "dispatch_state", "reason"),
    [
        (ToolAttemptStatus.SUCCEEDED, "success_verified", "effect_confirmed", ""),
        (
            ToolAttemptStatus.FAILED_BEFORE_EFFECT,
            "confirmed_failure_before_effect",
            "not_dispatched",
            "read_failed_before_effect",
        ),
        (
            ToolAttemptStatus.FAILED_BEFORE_EFFECT,
            "rejected_before_execution",
            "not_dispatched",
            "invalid_request_rejected",
        ),
        (
            ToolAttemptStatus.FAILED_BEFORE_EFFECT,
            "rejected_before_execution",
            "not_dispatched",
            "block_system_destruction",
        ),
        (
            ToolAttemptStatus.UNKNOWN_AFTER_START,
            "outcome_unknown",
            "effect_unknown",
            "observation_outcome_unknown",
        ),
        (
            ToolAttemptStatus.FAILED_AFTER_EFFECT,
            "failed_after_effect",
            "effect_confirmed",
            "read_failed_after_dispatch",
        ),
    ],
)
def test_terminal_toolattempt_clears_only_its_pending_projection(
    tmp_path, attempt_store, status, outcome, dispatch_state, reason,
):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store)
    attempt_id = row["tool_attempt_id"]
    controller.set_attempt_state(attempt_id, status="running")
    _finish(
        attempt_store,
        attempt_id,
        status=status,
        outcome=outcome,
        dispatch_state=dispatch_state,
        reason=reason,
    )

    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert attempt_id not in controller.snapshot()["active_attempts"]
    assert decision.wait_type != PENDING_TOOL_WAIT
    typed = [
        item for item in controller.snapshot()["typed_evidence"]
        if item["attempt_id"] == attempt_id
    ]
    assert any(item["outcome"] == outcome for item in typed)
    if outcome == "outcome_unknown":
        unknown = controller.snapshot()["unknown_effects"][attempt_id]
        assert unknown["outcome"] == "outcome_unknown"
    if reason == "block_system_destruction":
        assert controller.snapshot()["first_causal_error"]["reason_code"] == reason
        assert controller.snapshot()["hard_safety_reason"] == ""


def test_terminal_cancellation_clears_pending_projection(tmp_path, attempt_store):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store, running=False)
    attempt_id = row["tool_attempt_id"]
    controller.set_attempt_state(attempt_id, status="pending")

    cancelled = attempt_store.request_cancellation(attempt_id)
    assert cancelled["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert attempt_id not in controller.snapshot()["active_attempts"]
    assert decision.wait_type != PENDING_TOOL_WAIT
    assert any(
        item["outcome"] == "cancelled_proven"
        and item["attempt_id"] == attempt_id
        for item in controller.snapshot()["typed_evidence"]
    )


def test_terminal_a_and_running_b_reconcile_independently(tmp_path, attempt_store):
    controller = _controller(tmp_path)
    a = _new_attempt(attempt_store, call_id="call-a")
    _finish(
        attempt_store,
        a["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        outcome="success_verified",
        dispatch_state="effect_confirmed",
    )
    b = _new_attempt(attempt_store, call_id="call-b")
    controller.set_attempt_state(a["tool_attempt_id"], status="running")
    controller.set_attempt_state(b["tool_attempt_id"], status="running")

    decision = controller.evaluate(provider_route="provider-a:model-a")

    active = controller.snapshot()["active_attempts"]
    assert a["tool_attempt_id"] not in active
    assert b["tool_attempt_id"] in active
    assert decision.action == "wait"
    assert decision.wait_type == PENDING_TOOL_WAIT


def test_no_wait_when_every_exact_attempt_is_terminal(tmp_path, attempt_store):
    controller = _controller(tmp_path)
    rows = []
    for index in range(3):
        row = _new_attempt(attempt_store, call_id=f"call-{index}")
        rows.append(row)
        _finish(
            attempt_store,
            row["tool_attempt_id"],
            status=ToolAttemptStatus.SUCCEEDED,
            outcome="success_verified",
            dispatch_state="effect_confirmed",
        )
        controller.set_attempt_state(row["tool_attempt_id"], status="running")

    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert controller.snapshot()["active_attempts"] == {}
    assert decision.wait_type != PENDING_TOOL_WAIT


def test_genuinely_running_visual_attempt_keeps_waiting(tmp_path, attempt_store):
    controller = _controller(tmp_path)
    visual = _new_attempt(
        attempt_store,
        call_id="visual-call-running",
        tool_name="request_visual_observation",
    )
    controller.set_attempt_state(visual["tool_attempt_id"], status="running")

    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert visual["tool_attempt_id"] in controller.snapshot()["active_attempts"]
    assert decision.wait_type == PENDING_TOOL_WAIT


def test_wrong_tool_attempt_id_cannot_clear_another_attempt(
    tmp_path, attempt_store,
):
    controller = _controller(tmp_path)
    terminal = _new_attempt(attempt_store, call_id="call-terminal")
    _finish(
        attempt_store,
        terminal["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        outcome="success_verified",
        dispatch_state="effect_confirmed",
    )
    running = _new_attempt(attempt_store, call_id="call-running")
    controller.set_attempt_state(running["tool_attempt_id"], status="running")

    controller.record_typed_evidence(
        outcome="success_verified",
        evidence={"tool_attempt_id": terminal["tool_attempt_id"]},
        attempt_id=terminal["tool_attempt_id"],
    )
    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert running["tool_attempt_id"] in controller.snapshot()["active_attempts"]
    assert decision.wait_type == PENDING_TOOL_WAIT


@pytest.mark.parametrize(
    "mismatched_identity",
    [
        {"provider_native_call_id": "wrong-native-call"},
        {"turn_id": "another-turn"},
        {"owner_scope": "autonomous"},
        {"execution_epoch": "another-execution-epoch"},
    ],
)
def test_mismatched_completion_identity_cannot_clear_exact_running_attempt(
    tmp_path, attempt_store, mismatched_identity,
):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store)
    attempt_id = row["tool_attempt_id"]
    controller.set_attempt_state(attempt_id, status="running")
    evidence = {
        "tool_attempt_id": attempt_id,
        "tool_call_id": row["tool_call_id"],
        "provider_native_call_id": row["provider_references"][
            "provider_native_call_id"
        ],
        "turn_id": row["turn_id"],
        "conversation_id": row["conversation_id"],
        "request_id": row["request_id"],
        "provider_id": row["provider_id"],
        "model_id": row["model_id"],
        "owner_scope": row["owner_scope"],
        "execution_epoch": row["execution_epoch"],
        **mismatched_identity,
    }

    controller.record_typed_evidence(
        outcome="success_verified",
        evidence=evidence,
        attempt_id=attempt_id,
    )
    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert attempt_id in controller.snapshot()["active_attempts"]
    assert decision.wait_type == PENDING_TOOL_WAIT
    assert not any(
        item["attempt_id"] == attempt_id
        and item["outcome"] == "success_verified"
        for item in controller.snapshot()["typed_evidence"]
    )


def test_wrong_execution_binding_does_not_reconcile_other_provider_attempt(
    tmp_path, attempt_store,
):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store, provider_id="provider-b")
    attempt_id = row["tool_attempt_id"]
    controller.set_attempt_state(attempt_id, status="running")
    _finish(
        attempt_store,
        attempt_id,
        status=ToolAttemptStatus.SUCCEEDED,
        outcome="success_verified",
        dispatch_state="effect_confirmed",
    )

    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert attempt_id in controller.snapshot()["active_attempts"]
    assert decision.wait_type == PENDING_TOOL_WAIT


def test_restore_reconciles_terminal_toolattempt_and_stale_projection(
    tmp_path, attempt_store,
):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store)
    attempt_id = row["tool_attempt_id"]
    controller.set_attempt_state(attempt_id, status="running")
    _finish(
        attempt_store,
        attempt_id,
        status=ToolAttemptStatus.SUCCEEDED,
        outcome="success_verified",
        dispatch_state="effect_confirmed",
    )

    decision = controller.evaluate(provider_route="provider-a:model-a")

    assert attempt_id not in controller.snapshot()["active_attempts"]
    assert decision.wait_type != PENDING_TOOL_WAIT
    assert any(
        item["attempt_id"] == attempt_id
        and item["outcome"] == "success_verified"
        for item in controller.snapshot()["typed_evidence"]
    )


def test_restore_active_durable_attempt_with_missing_projection_does_not_redispatch(
    tmp_path, attempt_store,
):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store)
    before = attempt_store.get_attempt(row["tool_attempt_id"])
    assert before["dispatch_state"] == "owner_started"
    assert controller.snapshot()["active_attempts"] == {}

    controller.prepare_durable_tool_resume(row["tool_attempt_id"])

    after = attempt_store.get_attempt(row["tool_attempt_id"])
    assert after["status"] == ToolAttemptStatus.RUNNING.value
    assert after["dispatch_state"] == before["dispatch_state"]
    assert row["tool_attempt_id"] in controller.snapshot()["active_attempts"]
    assert controller.evaluate().wait_type == PENDING_TOOL_WAIT


def test_failed_runtime_reattachment_does_not_call_provider_again(
    attempt_store, tmp_path, monkeypatch,
):
    from osia.agent import OSIAAgent
    from osia.tool_attempts import WAITING_TOOL_RESULT

    monkeypatch.setattr(
        "osia.turn_terminality.CONFIG_DIR",
        str(tmp_path / "config"),
    )
    row = _new_attempt(attempt_store)
    pending = attempt_store.mark_pending(
        row["tool_attempt_id"],
        next_check_at=2_000_000_000.0,
        deadline_at=2_100_000_000.0,
    )
    agent = OSIAAgent(lang="en")
    agent.provider = row["provider_id"]
    agent.model = row["model_id"]
    agent.configure_tool_attempts(
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        request_id=row["request_id"],
        final_response_message_id="assistant-1",
    )
    binding = {
        **_binding(
            row["turn_id"],
            conversation=row["conversation_id"],
            request=row["request_id"],
            provider=row["provider_id"],
            model=row["model_id"],
        ),
    }
    monkeypatch.setattr(agent, "execution_trace_binding", lambda _turn: binding)
    monkeypatch.setattr(
        agent,
        "_restore_suspended_tool_runtime",
        lambda _attempt, _strategy: False,
    )
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "active durable restore failure fell through to a provider call"
        ),
    )
    monkeypatch.setattr(agent, "_emit_execution_trace", lambda *_args, **_kwargs: None)

    result = agent.run_terminal_turn(
        "Continue the same work",
        turn_id=row["turn_id"],
        current_conversation_id=row["conversation_id"],
        required_postconditions=("effect verified",),
        execution_required=True,
    )

    assert result == WAITING_TOOL_RESULT
    assert attempt_store.get_attempt(row["tool_attempt_id"])["status"] == (
        pending["status"]
    )
    assert row["tool_attempt_id"] in agent._terminality_controller.snapshot()[
        "active_attempts"
    ]


def test_structural_wait_preserves_unrelated_running_attempt(tmp_path, attempt_store):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store)
    controller.set_attempt_state(row["tool_attempt_id"], status="running")

    controller.mark_structural_wait("WAIT_WITHOUT_PENDING_TOOL")

    assert row["tool_attempt_id"] in controller.snapshot()["active_attempts"]
    assert controller.evaluate().wait_type == PENDING_TOOL_WAIT


def test_reconciliation_exhaustion_does_not_clear_a_nonterminal_toolattempt(
    tmp_path, attempt_store,
):
    controller = _controller(tmp_path)
    row = _new_attempt(attempt_store)
    attempt_id = row["tool_attempt_id"]
    controller.set_attempt_state(attempt_id, status="running")
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"dispatch": "possible", "item_id": row["tool_call_id"]},
        attempt_id=attempt_id,
    )

    assert controller.mark_reconciliation_exhausted(
        attempt_id,
        evidence={"checks": "complete"},
    )

    assert attempt_id in controller.snapshot()["active_attempts"]
    assert controller.evaluate().wait_type == PENDING_TOOL_WAIT


def test_repeated_identical_commands_remain_independent_new_requests(
    attempt_store, tmp_path,
):
    controller = _controller(tmp_path)
    attempt_ids = []
    for index in range(20):
        row = _new_attempt(
            attempt_store,
            call_id=f"new-call-{index}",
            arguments={"path": "same/path"},
        )
        attempt_ids.append(row["tool_attempt_id"])
        _finish(
            attempt_store,
            row["tool_attempt_id"],
            status=ToolAttemptStatus.SUCCEEDED,
            outcome="success_verified",
            dispatch_state="effect_confirmed",
        )
        controller.set_attempt_state(row["tool_attempt_id"], status="running")

    assert len(set(attempt_ids)) == 20
    assert controller.evaluate().wait_type != PENDING_TOOL_WAIT
    assert controller.snapshot()["active_attempts"] == {}


def test_restore_terminal_attempt_uses_receipt_path_instead_of_pending_runtime(
    tmp_path, attempt_store, monkeypatch,
):
    from osia.agent import OSIAAgent

    row = _new_attempt(
        attempt_store,
        final_response_message_id="final-1",
    )
    controller = _controller(tmp_path, turn_id=row["turn_id"])
    controller.set_attempt_state(row["tool_attempt_id"], status="running")
    _finish(
        attempt_store,
        row["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        outcome="success_verified",
        dispatch_state="effect_confirmed",
    )
    agent = OSIAAgent(lang="en")
    agent.provider = row["provider_id"]
    agent.model = row["model_id"]
    agent.configure_tool_attempts(
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        request_id=row["request_id"],
        final_response_message_id="final-1",
    )
    agent._terminality_controller = controller
    agent._terminality_strategy = None
    binding = _binding(
        row["turn_id"],
        conversation=row["conversation_id"],
        request=row["request_id"],
        provider=row["provider_id"],
        model=row["model_id"],
    )
    controller.set_execution_binding(binding)
    monkeypatch.setattr(agent, "execution_trace_binding", lambda _turn: binding)

    assert not agent._restore_suspended_tool_runtime(row, None)
    assert agent._pending_tool_runtime is None
    assert row["tool_attempt_id"] not in controller.snapshot()["active_attempts"]
    assert any(
        item["attempt_id"] == row["tool_attempt_id"]
        and item["outcome"] == "success_verified"
        for item in controller.snapshot()["typed_evidence"]
    )
