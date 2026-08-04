"""Focused automated coverage for WOR-227 combined Phase 2 / Phase 3."""

from __future__ import annotations

import threading
import time
import asyncio
import io

from osia import agent as agent_module
from osia import tool_attempts
from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator
from osia.tool_attempts import (
    ToolAttemptCoordinator,
    ToolAttemptStatus,
    ToolAttemptStore,
    WAITING_TOOL_RESULT,
)
from osia.tool_contracts import ExecutionOutcome
from osia.turn_context import fixed_turn_context


def _attempt(
    store: ToolAttemptStore,
    *,
    conversation_id: str = "conv_phase23",
    turn_id: str = "turn_phase23",
    tool_name: str = "find_and_click",
    owner_scope: str = "chat",
):
    created = store.create_attempt(
        conversation_id=conversation_id,
        turn_id=turn_id,
        request_id="request_phase23",
        tool_call_id="call_phase23",
        tool_name=tool_name,
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={
            "language": "en", "source": "fixture", "turn_ref": turn_id,
            "mode": "single-language", "allowed_languages": ["en"],
        },
        argument_fingerprint=store.fingerprint_arguments(
            {"text": "controlled target"},
        ),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        deadline_at=time.time() + 120,
        postcondition_type="browser_state_change",
        final_response_message_id="assistant_phase23",
        owner_scope=owner_scope,
        autonomous_run_id="run.phase23" if owner_scope == "autonomous" else "",
        autonomous_task_id="task.phase23" if owner_scope == "autonomous" else "",
        autonomous_operation_id=(
            "operation.phase23" if owner_scope == "autonomous" else ""
        ),
        operation_revision=1 if owner_scope == "autonomous" else 0,
        permission_epoch=7 if owner_scope == "autonomous" else 0,
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    return store.mark_running(accepted["tool_attempt_id"])


def _mission():
    st = state.new_task_state(
        "Controlled mission",
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Controlled",
            "tasks": [{
                "id": "task.phase23",
                "objective": "Perform the controlled action",
                "success_criterion": "The controlled action is verified",
            }],
        }],
        run_id="run.phase23",
    )
    st["status"] = "in_progress"
    st["run_state"] = "running"
    st["source_permission_epoch"] = 7
    return st


class _PassiveAgent:
    messages = []
    last_run_diagnostic = None

    def reset(self):
        return None

    def activate_pending_tool_attempt(self):
        return True

    def clear_tool_attempts(self):
        return None


def test_autonomous_common_attempt_retains_chat_source_identity():
    captured = {}

    class Agent(_PassiveAgent):
        def configure_tool_attempts(self, **kwargs):
            captured.update(kwargs)

    st = _mission()
    st["source_conversation_id"] = "conv.phase23.source"
    st["source_turn_id"] = "turn.phase23.source"
    st["source_request_id"] = "request.phase23.source"
    st["source_permission_epoch"] = 227
    st["policy_epoch"] = 999
    orch = Orchestrator(Agent())
    orch._state = st

    orch._configure_common_tool_attempts(st)
    binding = captured["autonomous_binding"](
        "find_and_click", {"text": "controlled target"},
    )

    assert captured["conversation_id"] == "conv.phase23.source"
    assert captured["turn_id"] == "turn.phase23.source"
    assert captured["request_id"] == "request.phase23.source"
    assert binding["permission_epoch"] == 227


def test_wake_callback_and_reconnect_share_one_continuation(tmp_path):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    running = _attempt(store)
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() + 60,
        deadline_at=time.time() + 120,
    )
    resumes = []
    final_count = 0
    final_event = threading.Event()

    def finalize(*_args):
        nonlocal final_count
        final_count += 1
        final_event.set()
        return True

    coordinator.register_suspended(
        pending["tool_attempt_id"],
        resume=lambda attempt_id, _payload: resumes.append(attempt_id) or "done",
        finalize=finalize,
    )
    assert coordinator.activate(pending["tool_attempt_id"])
    terminal, _ = store.record_terminal_result(
        pending["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified",
    )

    # Callback, wake and reconnect all target the same durable identity.  Only
    # the atomic continuation claim may invoke the continuation.
    coordinator.notify_terminal(pending["tool_attempt_id"], terminal)
    coordinator.trigger_recovery(
        reason="windows_resume", now=time.time() + 300, overdue_only=False,
    )
    coordinator.request_reconciliation("conv_phase23")
    coordinator.notify_terminal(pending["tool_attempt_id"], terminal)

    assert final_event.wait(2)
    assert resumes == [pending["tool_attempt_id"]]
    assert final_count == 1
    assert store.get_attempt(pending["tool_attempt_id"])[
        "continuation_state"
    ] == "complete"


def test_lifecycle_gap_only_triggers_read_only_recovery(monkeypatch, tmp_path):
    coordinator = ToolAttemptCoordinator(ToolAttemptStore(tmp_path))
    triggers = []
    monkeypatch.setattr(
        coordinator,
        "trigger_recovery",
        lambda **kwargs: triggers.append(kwargs) or [],
    )
    coordinator._last_lifecycle_wall -= 90
    coordinator._last_lifecycle_monotonic -= 90

    assert coordinator.observe_lifecycle_tick(expected_interval=15) is True
    assert triggers == [{
        "reason": "lifecycle_gap",
        "now": triggers[0]["now"],
        "overdue_only": True,
    }]


def test_parallel_wake_and_timer_run_one_read_only_verification(tmp_path):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    running = _attempt(store)
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() + 60,
        deadline_at=time.time() + 120,
    )
    entered = threading.Event()
    release = threading.Event()
    verification_count = 0

    def verify(_attempt_id):
        nonlocal verification_count
        verification_count += 1
        entered.set()
        release.wait(1)
        return None

    coordinator.register_suspended(
        pending["tool_attempt_id"],
        resume=lambda *_args: "unused",
        finalize=lambda *_args: True,
        verify=verify,
    )
    assert coordinator.activate(pending["tool_attempt_id"])
    first = threading.Thread(
        target=coordinator._timer_fired,
        args=(pending["tool_attempt_id"],),
    )
    second = threading.Thread(
        target=coordinator._timer_fired,
        args=(pending["tool_attempt_id"],),
    )
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert verification_count == 1
    with coordinator._lock:
        runtime = coordinator._runtimes[pending["tool_attempt_id"]]
        if runtime.timer is not None:
            runtime.timer.cancel()


def test_pending_autonomous_tool_does_not_consume_retry(monkeypatch):
    st = _mission()
    orch = Orchestrator(_PassiveAgent())
    orch._state = st
    monkeypatch.setattr(state, "save_task_state", lambda _state: None)

    def pending_run(_prompt):
        state.bind_tool_attempt(
            st,
            attempt_id="attempt.phase23",
            operation_id="operation.phase23",
            operation_revision=1,
            next_reconciliation_at=time.time() + 30,
        )
        st["run_state"] = "waiting_tool"
        return WAITING_TOOL_RESULT

    monkeypatch.setattr(orch, "_agent_run", pending_run)

    assert orch._step(st) is False
    task = st["phases"][0]["tasks"][0]
    assert task["status"] == "waiting_tool"
    assert task["attempts"] == 0
    assert st["cursor"] == {"phase": 0, "task": 0}
    assert st["run_state"] == "waiting_tool"


def test_terminal_race_resumes_exact_autonomous_task_once(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    monkeypatch.setattr(state, "save_task_state", lambda _state: None)
    running = _attempt(store, owner_scope="autonomous")
    terminal, _ = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified",
    )
    st = _mission()
    state.bind_tool_attempt(
        st,
        attempt_id=running["tool_attempt_id"],
        operation_id="operation.phase23",
        operation_revision=1,
        next_reconciliation_at=None,
    )
    orch = Orchestrator(_PassiveAgent())
    orch._state = st
    starts = []
    monkeypatch.setattr(orch, "_start_tool_resume_loop", lambda: starts.append(1))

    assert orch._on_common_tool_final(running["tool_attempt_id"], "", None)
    assert orch._on_common_tool_final(running["tool_attempt_id"], "", None)

    task = st["phases"][0]["tasks"][0]
    assert starts == [1]
    assert task["status"] == "resuming"
    assert task["tool_continuation_claimed"] is True
    assert orch._pending_tool_completion == (
        running["tool_attempt_id"], terminal["status"],
    )


def test_terminal_success_advances_without_provider_or_action_replay(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(state, "save_task_state", lambda _state: None)
    running = _attempt(store, owner_scope="autonomous")
    terminal, _ = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified",
    )
    st = _mission()
    state.bind_tool_attempt(
        st,
        attempt_id=running["tool_attempt_id"],
        operation_id="operation.phase23",
        operation_revision=1,
        next_reconciliation_at=None,
    )
    orch = Orchestrator(_PassiveAgent())
    orch._state = st
    phase = st["phases"][0]
    task = phase["tasks"][0]

    assert orch._continue_after_common_tool(
        st, 0, 0, phase, task, running["tool_attempt_id"], terminal["status"],
    ) is True
    assert task["status"] == "done"
    assert st["cursor"] == {"phase": 1, "task": 0}
    assert st["provider"] == "fixture-provider"
    assert st["model"] == "fixture-model"


def test_terminal_receipt_remains_authoritative_if_continuation_errors(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(state, "save_task_state", lambda _state: None)
    running = _attempt(store, owner_scope="autonomous")
    store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified",
    )
    st = _mission()
    state.bind_tool_attempt(
        st,
        attempt_id=running["tool_attempt_id"],
        operation_id="operation.phase23",
        operation_revision=1,
        next_reconciliation_at=None,
    )
    orch = Orchestrator(_PassiveAgent())
    orch._state = st
    starts = []
    monkeypatch.setattr(orch, "_start_tool_resume_loop", lambda: starts.append(1))

    assert orch._on_common_tool_final(
        running["tool_attempt_id"], "", RuntimeError("provider unavailable"),
    )
    assert starts == [1]
    assert st["run_state"] == "resuming"


def test_restart_recovery_rejects_wrong_source_turn_without_dispatch(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    monkeypatch.setattr(state, "save_task_state", lambda _state: None)
    running = _attempt(store, owner_scope="autonomous")
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() + 30,
        deadline_at=time.time() + 120,
    )
    st = _mission()
    st["source_conversation_id"] = "conv_phase23"
    st["source_turn_id"] = "different_turn"
    st["source_request_id"] = "request_phase23"
    state.bind_tool_attempt(
        st,
        attempt_id=pending["tool_attempt_id"],
        operation_id="operation.phase23",
        operation_revision=1,
        next_reconciliation_at=pending["next_check_at"],
    )
    st["run_state"] = "waiting_tool"
    orch = Orchestrator(_PassiveAgent())

    orch.resume(st)

    assert st["run_state"] == "blocked_unknown_effect"
    assert st["phases"][0]["tasks"][0]["last_error"] == (
        "common_tool_attempt_identity_mismatch"
    )


def test_chat_to_autonomous_attaches_existing_attempt_without_dispatch(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    running = _attempt(store)
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() + 30,
        deadline_at=time.time() + 120,
    )
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "find_and_click",
        lambda **_kwargs: dispatches.append(1) or "unexpected dispatch",
    )
    binding = {
        "run_id": "run.phase23",
        "task_id": "task.phase23",
        "operation_id": "operation.phase23",
        "operation_revision": 1,
        "permission_epoch": 7,
    }
    context = {
        "enabled": True,
        "store": store,
        "conversation_id": "conv_phase23",
        "turn_id": "turn_phase23",
        "request_id": "request_phase23",
        "tool_call_id": "attached_call",
        "provider_id": "fixture-provider",
        "model_id": "fixture-model",
        "turn_context": fixed_turn_context(
            "en", source="fixture", reference="turn_phase23",
        ),
        "final_response_message_id": "assistant_phase23",
        "owner_scope": "autonomous",
        "autonomous_binding": lambda *_args: binding,
        "existing_attempt_id": pending["tool_attempt_id"],
    }

    result = agent_module._execute_tool_with_policy(
        "find_and_click",
        {"text": "controlled target"},
        lang="en",
        attempt_context=context,
    )

    assert result.outcome is ExecutionOutcome.PENDING
    assert result.tool_attempt_id == pending["tool_attempt_id"]
    assert dispatches == []
    assert len(list(store.attempts_dir.glob("attempt_*.json"))) == 1
    attached = store.get_attempt(pending["tool_attempt_id"])
    assert attached["autonomous_run_id"] == "run.phase23"
    assert attached["autonomous_task_id"] == "task.phase23"
    assert attached["permission_epoch"] == 7
    persisted = next(store.attempts_dir.glob("attempt_*.json")).read_text(
        encoding="utf-8",
    )
    assert "controlled target" not in persisted


def test_handoff_binds_pending_receipt_before_mission_dispatch(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    monkeypatch.setattr(state, "save_task_state", lambda _state: None)
    running = _attempt(store)
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() + 30,
        deadline_at=time.time() + 120,
    )
    st = _mission()
    st["source_conversation_id"] = "conv_phase23"
    st["source_turn_id"] = "turn_phase23"
    st["source_request_id"] = "request_phase23"
    st["attached_tool_attempt_id"] = pending["tool_attempt_id"]
    orch = Orchestrator(_PassiveAgent())
    starts = []
    monkeypatch.setattr(orch, "_start_tool_resume_loop", lambda: starts.append(1))

    assert orch.attach_existing_tool_attempt(st, pending["tool_attempt_id"])

    task = st["phases"][0]["tasks"][0]
    assert len(st["phases"]) == 1
    assert len(st["phases"][0]["tasks"]) == 1
    assert task["objective"] == (
        "Continue the exact suspended source tool attempt without redispatch."
    )
    assert st["run_state"] == "waiting_tool"
    assert task["tool_attempt_id"] == pending["tool_attempt_id"]
    assert task["attempts"] == 0
    bound = store.get_attempt(pending["tool_attempt_id"])
    assert bound["owner_scope"] == "autonomous"
    assert bound["autonomous_run_id"] == st["run_id"]
    assert bound["autonomous_task_id"] == task["id"]
    assert bound["permission_epoch"] == 7

    terminal, _created = store.record_terminal_result(
        pending["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified",
    )
    coordinator.notify_terminal(pending["tool_attempt_id"], terminal)
    deadline = time.time() + 2
    while not starts and time.time() < deadline:
        time.sleep(0.01)
    assert starts == [1]
    assert st["run_state"] == "resuming"


def test_handoff_rejects_wrong_source_without_rebinding(
    monkeypatch, tmp_path,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    running = _attempt(store)
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() + 30,
        deadline_at=time.time() + 120,
    )
    st = _mission()
    st["source_conversation_id"] = "conv_phase23"
    st["source_turn_id"] = "different_turn"
    st["source_request_id"] = "request_phase23"
    st["attached_tool_attempt_id"] = pending["tool_attempt_id"]
    orch = Orchestrator(_PassiveAgent())

    assert not orch.attach_existing_tool_attempt(
        st, pending["tool_attempt_id"],
    )
    unchanged = store.get_attempt(pending["tool_attempt_id"])
    assert unchanged["owner_scope"] == "chat"
    assert unchanged["autonomous_run_id"] == ""
    assert st["phases"][0]["tasks"][0]["objective"] == (
        "Perform the controlled action"
    )
    assert st["phases"][0]["tasks"][0]["tool_attempt_id"] == ""


def test_autonomous_attempt_binding_is_immutable(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _attempt(store)
    bound = store.bind_autonomous(
        running["tool_attempt_id"],
        run_id="run.phase23",
        task_id="task.phase23",
        operation_id="operation.phase23",
        operation_revision=1,
        permission_epoch=7,
    )

    with __import__("pytest").raises(tool_attempts.IllegalTransition):
        store.bind_autonomous(
            bound["tool_attempt_id"],
            run_id="run.phase23",
            task_id="task.phase23",
            operation_id="operation.phase23",
            operation_revision=2,
            permission_epoch=8,
        )

    unchanged = store.get_attempt(bound["tool_attempt_id"])
    assert unchanged["operation_revision"] == 1
    assert unchanged["permission_epoch"] == 7


def test_cancellation_requested_is_not_cancelled_proven(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _attempt(store, owner_scope="autonomous")
    requested = store.request_cancellation(running["tool_attempt_id"])

    assert requested["status"] == ToolAttemptStatus.CANCELLATION_REQUESTED.value
    assert requested["status"] != ToolAttemptStatus.CANCELLED_PROVEN.value
    assert requested["dispatch_state"] != "not_dispatched"


def test_pre_dispatch_cancellation_is_atomically_proven(tmp_path):
    store = ToolAttemptStore(tmp_path)
    created = store.create_attempt(
        conversation_id="conv.cancel.before",
        turn_id="turn.cancel.before",
        request_id="request.cancel.before",
        tool_call_id="call.cancel.before",
        tool_name="write_file",
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={"language": "en", "source": "fixture"},
        argument_fingerprint=store.fingerprint_arguments(
            {"path": "synthetic.txt", "content": "synthetic"}
        ),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])

    cancelled = store.request_cancellation(accepted["tool_attempt_id"])
    dispatch_gate = store.mark_running(accepted["tool_attempt_id"])

    assert cancelled["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    assert cancelled["dispatch_state"] == "cancelled_before_effect"
    assert cancelled["terminal_outcome"] == "cancelled_proven"
    assert dispatch_gate == cancelled


def test_accepted_tool_cancelled_by_status_callback_never_dispatches(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path)
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda path, content: dispatches.append((path, content)) or "unexpected",
    )

    def cancel_at_start(status, attempt_id):
        if status == "action_starting":
            store.request_cancellation(attempt_id)

    record = agent_module._execute_tool_with_policy(
        "write_file",
        {"path": "synthetic.txt", "content": "synthetic"},
        lang="en",
        attempt_context={
            "enabled": True,
            "store": store,
            "conversation_id": "conv.cancel.callback",
            "turn_id": "turn.cancel.callback",
            "request_id": "request.cancel.callback",
            "tool_call_id": "call.cancel.callback",
            "provider_id": "fixture-provider",
            "model_id": "fixture-model",
            "turn_context": fixed_turn_context("en", source="fixture"),
            "final_response_message_id": "assistant.cancel.callback",
            "status_callback": cancel_at_start,
        },
    )

    durable = store.get_attempt(record.tool_attempt_id)
    assert record.outcome is ExecutionOutcome.CANCELLED_PROVEN
    assert record.attempts == 0
    assert record.dispatched is False
    assert dispatches == []
    assert durable["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value


def test_proven_tool_cancellation_finishes_with_one_canonical_final():
    agent = object.__new__(agent_module.OSIAAgent)
    agent.lang = "en"
    agent.messages = []
    agent.turn_context = None
    agent._document_private_run = False
    spoken = []

    result = agent._finish_cancelled_tool_stop(on_sentence=spoken.append)

    assert result == "The action was cancelled before any effect was confirmed."
    assert spoken == [result]
    assert agent.messages == [{"role": "assistant", "content": result}]


def test_chat_cancel_before_attempt_queues_one_bounded_intent(monkeypatch):
    from osia.server import server

    class Store:
        @staticmethod
        def find_compatible_suspended_turn(_conversation_id):
            return None

    class Coordinator:
        store = Store()

    class Channel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(dict(frame))

    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", Coordinator())

    async def exercise():
        conversation_id = "conv.cancel.queued"
        blocker = asyncio.create_task(asyncio.sleep(60))
        meta = server._new_turn_meta(conversation_id, "request.cancel.queued")
        server._ACTIVE_TURNS_BY_CONV[conversation_id] = blocker
        server._ACTIVE_TURN_META_BY_CONV[conversation_id] = meta
        channel = Channel()
        try:
            handled = await server._handle_waiting_tool_command(
                channel, conversation_id, "cancel",
            )
            assert handled is True
            assert meta["_tool_cancel_requested"] is True
            assert channel.frames[-1]["status"] == "cancellation_requested"
        finally:
            server._ACTIVE_TURNS_BY_CONV.pop(conversation_id, None)
            server._ACTIVE_TURN_META_BY_CONV.pop(conversation_id, None)
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    asyncio.run(exercise())


def test_chat_queued_cancel_targets_exact_accepted_attempt(tmp_path, monkeypatch):
    from osia.server import server

    store = ToolAttemptStore(tmp_path)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    created = store.create_attempt(
        conversation_id="conv.cancel.exact",
        turn_id="turn.cancel.exact",
        request_id="request.cancel.exact",
        tool_call_id="call.cancel.exact",
        tool_name="write_file",
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={"language": "en", "source": "fixture"},
        argument_fingerprint=store.fingerprint_arguments(
            {"path": "synthetic.txt", "content": "synthetic"}
        ),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])

    assert server._request_exact_tool_attempt_cancellation(
        accepted["tool_attempt_id"]
    ) is True
    durable = store.get_attempt(accepted["tool_attempt_id"])
    assert durable["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    assert durable["dispatch_state"] == "cancelled_before_effect"


def test_chat_queued_cancel_remains_for_later_tool_boundaries(
    tmp_path, monkeypatch,
):
    from osia.server import server

    store = ToolAttemptStore(tmp_path)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    meta = {"_tool_cancel_requested": True}
    attempt_ids = []
    for index in range(2):
        created = store.create_attempt(
            conversation_id="conv.cancel.turn",
            turn_id="turn.cancel.turn",
            request_id="request.cancel.turn",
            tool_call_id=f"call.cancel.turn.{index}",
            tool_name="write_file",
            provider_id="fixture-provider",
            model_id="fixture-model",
            turn_context={"language": "en", "source": "fixture"},
            argument_fingerprint=store.fingerprint_arguments(
                {"path": f"synthetic-{index}.txt", "content": "synthetic"}
            ),
            side_effect="external_mutation",
            idempotence="non_idempotent",
            retry_policy="never_after_dispatch",
        )
        accepted = store.mark_accepted(created["tool_attempt_id"])
        attempt_ids.append(accepted["tool_attempt_id"])
        assert server._apply_queued_tool_cancellation(
            meta, accepted["tool_attempt_id"]
        ) is True

    assert meta["_tool_cancel_requested"] is True
    assert all(
        store.get_attempt(attempt_id)["status"]
        == ToolAttemptStatus.CANCELLED_PROVEN.value
        for attempt_id in attempt_ids
    )


def test_autonomous_queued_cancel_binds_first_attempt_and_proves_no_dispatch(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    st = _mission()
    orch = Orchestrator(_PassiveAgent())
    orch._state = st
    orch._safe_save = lambda _state: None
    events = []
    orch._progress_cb = lambda event: events.append(dict(event))

    assert orch.request_tool_cancellation() is True
    assert st["run_state"] == "cancellation_requested"

    created = store.create_attempt(
        conversation_id="autonomous.run.phase23",
        turn_id="task.phase23",
        request_id="run.phase23",
        tool_call_id="call.auto.cancel.before",
        tool_name="write_file",
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={"language": "en", "source": "fixture"},
        argument_fingerprint=store.fingerprint_arguments(
            {"path": "synthetic.txt", "content": "synthetic"}
        ),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        owner_scope="autonomous",
        autonomous_run_id="run.phase23",
        autonomous_task_id="task.phase23",
        autonomous_operation_id="operation.cancel.before",
        operation_revision=0,
        permission_epoch=7,
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    orch._on_common_tool_status("action_starting", accepted["tool_attempt_id"])

    durable = store.get_attempt(accepted["tool_attempt_id"])
    task = state.current_task(st)[2]
    assert durable["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    assert durable["dispatch_state"] == "cancelled_before_effect"
    assert task["tool_attempt_id"] == accepted["tool_attempt_id"]
    assert task["attempts"] == 0
    assert st["run_state"] == "cancelled_proven"
    assert events[-1]["event"] == "cancelled_proven"


def test_autonomous_abort_after_terminal_read_only_attempt_stops_before_retry(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    running = _attempt(
        store,
        tool_name="inspect_browser_page",
        owner_scope="autonomous",
    )
    terminal, _ = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified_read_only",
    )
    st = _mission()
    task = state.current_task(st)[2]
    state.bind_tool_attempt(
        st,
        attempt_id=terminal["tool_attempt_id"],
        operation_id="operation.phase23",
        operation_revision=1,
        next_reconciliation_at=None,
    )
    orch = Orchestrator(_PassiveAgent())
    orch._state = st
    orch._safe_save = lambda _state: None
    events = []
    orch._progress_cb = lambda event: events.append(dict(event))

    assert orch.request_tool_cancellation() is True
    assert st["run_state"] == "cancellation_requested"
    assert store.get_attempt(terminal["tool_attempt_id"])["status"] == (
        ToolAttemptStatus.SUCCEEDED.value
    )
    assert orch._step(st) is False

    assert st["status"] == "aborted"
    assert st["run_state"] == "cancelled_proven"
    assert task["status"] == "cancelled_proven"
    assert task["attempts"] == 0
    assert st["ambiguity_barriers"] == []
    assert events[-1]["event"] == "cancelled_proven"


def test_windows_wake_hook_only_posts_live_reconciliation(monkeypatch):
    from windows import wake_recovery

    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_request(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(wake_recovery.urllib.request, "urlopen", open_request)

    assert wake_recovery._trigger_live_reconciliation(io.StringIO()) is True
    assert len(requests) == 1
    assert requests[0][0].full_url.endswith("/internal/tool_attempt_reconcile")
    assert requests[0][0].get_method() == "POST"
    assert requests[0][1] == 4.0


def test_loopback_wake_endpoint_returns_only_a_count(monkeypatch):
    from osia.server import server

    class Coordinator:
        def trigger_recovery(self, **kwargs):
            assert kwargs == {
                "reason": "windows_resume_event",
                "overdue_only": True,
            }
            return ["private-attempt-one", "private-attempt-two"]

    monkeypatch.setattr(
        tool_attempts, "_DEFAULT_COORDINATOR", Coordinator(),
    )
    request = type("Request", (), {
        "client": type("Client", (), {"host": "127.0.0.1"})(),
    })()

    result = asyncio.run(server._internal_tool_attempt_reconcile(request))

    assert result == {"ok": True, "triggered": 2}
    assert "attempt" not in str(result).lower()


def test_desktop_loopback_accepts_exact_chat_source_correlation(monkeypatch):
    from osia.server import server

    payload = {
        "objective": "Continue the exact controlled mission.",
        "source_refs": [],
        "request_id": "request.phase23.route",
        "source_request_id": "request.phase23.source",
        "source_turn_id": "turn.phase23.source",
        "conversation_id": "conversation.phase23",
        "provider_id": "fixture-provider",
        "model_id": "fixture-model",
        "reason_code": "dependent_phases",
        "confidence": 0.99,
        "explicit_request": True,
        "language": "en",
    }
    captured = []
    monkeypatch.setattr(
        server,
        "_launch_autonomous_from_chat",
        lambda request: captured.append(request) or {"status": "running"},
    )

    class Request:
        client = type("Client", (), {"host": "127.0.0.1"})()

        async def json(self):
            return dict(payload)

    result = asyncio.run(server.internal_chat_autonomous_start(Request()))

    assert result == {"status": "running"}
    assert captured[0]["source_request_id"] == "request.phase23.source"
    assert captured[0]["source_turn_id"] == "turn.phase23.source"


def test_reconnect_projects_pending_attempt_as_waiting(monkeypatch):
    from osia.server import server

    class Store:
        def find_compatible_suspended_turn(self, conversation_id):
            assert conversation_id == "conv.phase23.reconnect"
            return {"status": "pending"}

    monkeypatch.setattr(
        tool_attempts,
        "_DEFAULT_STORE",
        Store(),
    )
    server._ACTIVE_TURN_META_BY_CONV["conv.phase23.reconnect"] = {
        "_tool_attempt_status": "action_in_progress",
    }
    try:
        assert server._active_turn_working_status(
            "conv.phase23.reconnect"
        ) == "waiting_tool_result"
    finally:
        server._ACTIVE_TURN_META_BY_CONV.pop("conv.phase23.reconnect", None)


def test_fresh_status_schedules_durable_autonomous_recovery_once(monkeypatch):
    from osia.server import server

    acquisitions = []
    workers = []

    def acquire(task_id=None):
        acquisitions.append(task_id)
        return 27 if len(acquisitions) == 1 else None

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            workers.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(server, "_acquire_single_runner", acquire)
    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    monkeypatch.setitem(server._autonomous, "active", False)
    recovery_loop = object()
    state = {
        "status": "in_progress",
        "run_state": "waiting_tool",
        "task_id": "mission.phase23.recovery",
    }

    assert server._schedule_durable_autonomous_recovery(
        state,
        origin_ref="mobile.phase23",
        loop=recovery_loop,
        document_owner_device="pixel.phase23",
    ) is True
    assert server._schedule_durable_autonomous_recovery(
        state,
        origin_ref="mobile.phase23",
        loop=recovery_loop,
        document_owner_device="pixel.phase23",
    ) is False

    assert acquisitions == [
        "mission.phase23.recovery", "mission.phase23.recovery",
    ]
    assert workers == [
        (server._autonomous_resume_worker, (27, "mobile.phase23"), True),
    ]
    assert server._autonomous["loop"] is recovery_loop
    assert server._autonomous["origin_ref"] == "mobile.phase23"
    assert server._autonomous["document_owner_device"] == "pixel.phase23"

    assert server._schedule_durable_autonomous_recovery(
        {"status": "in_progress", "run_state": "authorization_pending"},
        origin_ref="mobile.phase23",
        loop=recovery_loop,
    ) is False


def test_full_access_revalidation_preserves_durable_tool_state():
    from types import SimpleNamespace

    from osia.action_policy import PermissionMode
    from osia.autonomous import authorization

    class Store:
        def load_state(self):
            return SimpleNamespace(
                permission_mode=PermissionMode.FULL_ACCESS,
                policy_epoch=205,
            )

    policy = SimpleNamespace(store=Store())
    state = {
        "status": "in_progress",
        "run_state": "waiting_tool",
        "revocation": {"revoked": False},
    }

    authorization.revalidate_loaded_authorization(
        state,
        origin_ref="mobile.phase23",
        policy=policy,
    )

    assert state["run_state"] == "waiting_tool"
    assert state["authorization_source"] == "full_access"
    assert state["policy_epoch"] == 205


def test_server_startup_schedules_saved_durable_checkpoint(monkeypatch):
    from osia.server import server

    recovery_loop = object()
    state = {
        "status": "in_progress",
        "run_state": "waiting_tool",
        "authorization_origin_ref": "mobile.saved.origin",
    }
    scheduled = []
    monkeypatch.setattr(server._astate, "load_task_state", lambda: state)
    monkeypatch.setattr(
        server.asyncio,
        "get_running_loop",
        lambda: recovery_loop,
    )
    monkeypatch.setattr(
        server,
        "_schedule_durable_autonomous_recovery",
        lambda st, **kwargs: scheduled.append((st, kwargs)) or True,
    )

    asyncio.run(server._recover_autonomous_checkpoint_at_startup())

    assert scheduled == [(
        state,
        {
            "origin_ref": "mobile.saved.origin",
            "loop": recovery_loop,
        },
    )]


def test_server_startup_retries_after_outgoing_owner_releases(monkeypatch):
    from osia.server import server

    state = {
        "status": "in_progress",
        "run_state": "waiting_tool",
        "authorization_origin_ref": "mobile.saved.origin",
    }
    calls = []

    async def no_wait(_seconds):
        return None

    def schedule(st, **kwargs):
        calls.append((st, kwargs))
        return len(calls) == 2

    monkeypatch.setattr(server._astate, "load_task_state", lambda: state)
    monkeypatch.setattr(server.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        server,
        "_schedule_durable_autonomous_recovery",
        schedule,
    )

    asyncio.run(server._retry_durable_autonomous_recovery("loop.phase23"))

    assert len(calls) == 2
    assert calls[0][1] == {
        "origin_ref": "mobile.saved.origin",
        "loop": "loop.phase23",
    }
