"""Focused WOR-262 durable waiting-tool recovery and lock regressions."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import Mock

import pytest

from osia import execution_strategy as es
from osia import tool_attempts
from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator
from osia.tool_attempts import (
    ToolAttemptCoordinator,
    ToolAttemptStatus,
    ToolAttemptStore,
)


OBJECTIVE = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
ACTION = "Enter and evaluate 347 × 29 in Calculator."
CRITERION = "Calculator displays the exact result 10063."
WINDOW = "surface-calculator-window"
OPERATION_ID = "operation.recovery-lock"
TOOLS = {
    "activate_desktop_surface",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
}


def _frozen_strategy():
    strategy = es.build_execution_strategy(
        ACTION,
        TOOLS,
        postcondition=CRITERION,
        semantic_context=OBJECTIVE,
    )
    strategy._latest_desktop_observation = {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": WINDOW,
        "window_generation": "calculator-generation-1",
        "accessibility_quality": "rich_target_resolved",
        "target_resolution": "exact_target",
        "target_foreground": False,
        "target_minimized": False,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "chatgpt",
        "observed_at": time.time(),
    }
    decision = es.tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": WINDOW, "objective": "Calculator"},
        strategy=strategy,
    )
    assert decision is not None and decision.allowed
    es.record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="success_unverified",
        is_error=True,
        reason_code="desktop_primitive_requires_exact_reconciliation",
        result={
            "receipt": {
                "surface_ref": WINDOW,
                "primitive": {"kind": "desktop_window_focus"},
            },
        },
        strategy=strategy,
    )
    return strategy, decision


def _waiting_state(strategy=None):
    st = state.new_task_state(
        OBJECTIVE,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Calculate and verify",
            "tasks": [{
                "objective": ACTION,
                "success_criterion": CRITERION,
            }],
        }],
        run_id="run.recovery-lock",
        language="en",
    )
    st["status"] = "in_progress"
    st["run_state"] = "waiting_tool"
    st["source_permission_epoch"] = 262
    task = st["phases"][0]["tasks"][0]
    if strategy is not None:
        task["execution_memory"] = es.export_execution_memory(strategy)
    st["operation_records"] = [{
        "operation_id": OPERATION_ID,
        "task_id": f"task:{task['id']}",
        "phase_id": "phase:0",
        "requirement_id": "",
        "plan_revision": st["plan_revision"],
        "tool_identity": "activate_desktop_surface",
        "normalized_call_hash": "fixture-call-hash",
        "autonomous_equivalence_hash": "fixture-call-hash",
        "private_scope_hash": "fixture-private-scope",
        "grant_ref": "",
        "authorization_source": "full_access",
        "effect_class": "external_mutation",
        "policy_epoch": 262,
        "status": "waiting_tool",
        "dispatch_attempt_count": 1,
        "started_at": "2026-08-21T12:00:00",
        "completed_at": None,
        "execution_outcome": "pending",
        "safe_reason_code": "waiting_tool_result",
    }]
    return st, task


def _bound_attempt(store, st, task, *, terminal=False):
    created = store.create_attempt(
        conversation_id=f"autonomous.{st['run_id']}",
        turn_id=task["id"],
        request_id=st["run_id"],
        tool_call_id="call.recovery-lock",
        tool_name="activate_desktop_surface",
        provider_id=st["provider"],
        model_id=st["model"],
        turn_context={"language": "en"},
        argument_fingerprint=store.fingerprint_arguments({
            "surface_ref": WINDOW,
            "objective": "Calculator",
        }),
        side_effect="external_mutation",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        deadline_at=time.time() + 120.0,
        postcondition_type="explicit_postcondition",
        final_response_message_id="assistant.recovery-lock",
        owner_scope="autonomous",
        autonomous_run_id=st["run_id"],
        autonomous_task_id=task["id"],
        autonomous_operation_id=OPERATION_ID,
        operation_revision=0,
        permission_epoch=262,
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    if terminal:
        attempt, _created = store.record_terminal_result(
            running["tool_attempt_id"],
            status=ToolAttemptStatus.SUCCEEDED,
            terminal_outcome="success_verified",
            dispatch_state="effect_confirmed",
            reason_code="local_reconciliation_completed",
        )
    else:
        attempt = store.mark_waiting_verification(
            running["tool_attempt_id"],
            next_check_at=time.time() + 30.0,
            deadline_at=time.time() + 120.0,
            reason_code="restart_reconciliation_pending",
            owner_settled=True,
        )
    state.bind_tool_attempt(
        st,
        attempt_id=attempt["tool_attempt_id"],
        operation_id=OPERATION_ID,
        operation_revision=0,
        next_reconciliation_at=attempt.get("next_check_at"),
    )
    st["run_state"] = "waiting_tool"
    return attempt


class _RecoveryAgent:
    provider = "fixture-provider"
    model = "fixture-model"

    def __init__(self, *, restorable=True):
        self.restorable = restorable
        self.restored = []
        self.configured = None
        self._pending_tool_runtime = None
        self.run_calls = []

    def configure_tool_attempts(self, **kwargs):
        self.configured = kwargs

    def _restore_suspended_tool_runtime(self, attempt, strategy):
        self.restored.append((attempt["tool_attempt_id"], strategy))
        return self.restorable

    def reset(self):
        return None

    def run(self, prompt):
        self.run_calls.append(prompt)
        raise AssertionError("recovered mutation/provider turn must not replay")


def test_01_recovered_waiting_attempt_restores_same_checkpoint(monkeypatch, tmp_path):
    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store = ToolAttemptStore(tmp_path)
    attempt = _bound_attempt(store, st, task)
    coordinator = Mock(spec=ToolAttemptCoordinator)
    coordinator.trigger_recovery.return_value = [attempt["tool_attempt_id"]]
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    agent = _RecoveryAgent()
    emitted = []
    orchestrator = Orchestrator(agent, emitted.append)
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _st: None)
    monkeypatch.setattr(orchestrator, "_strategy_for_task", lambda *_args: strategy)

    orchestrator._state = st
    orchestrator._recover_waiting_tool_checkpoint(st)

    assert agent.restored == [(attempt["tool_attempt_id"], strategy)]
    assert task["tool_attempt_id"] == attempt["tool_attempt_id"]
    assert task["tool_operation_id"] == OPERATION_ID
    assert st["run_state"] == "waiting_tool"
    coordinator.trigger_recovery.assert_called_once_with(
        reason="autonomous_resume", overdue_only=False,
    )
    assert any(item.get("event") == "waiting_tool" for item in emitted)


def test_02_restored_runtime_executes_only_verifier_callback(monkeypatch, tmp_path):
    from osia import agent as agent_module

    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store = ToolAttemptStore(tmp_path)
    attempt = _bound_attempt(store, st, task)
    coordinator = Mock(spec=ToolAttemptCoordinator)
    coordinator.activate.return_value = True
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    agent = object.__new__(agent_module.OSIAAgent)
    agent._tool_attempt_metadata = {
        "owner_scope": "autonomous",
        "final_callback": Mock(return_value=True),
        "status_callback": Mock(),
    }
    agent._pending_tool_runtime = None
    agent._pending_tool_attached_existing = False
    agent.last_pending_tool_attempt_id = ""
    verified = object()
    agent._verify_local_reconciliation_probe = Mock(return_value=verified)

    assert agent._restore_suspended_tool_runtime(attempt, strategy) is True

    callbacks = coordinator.register_suspended.call_args.kwargs
    assert callbacks["resume"](attempt["tool_attempt_id"], object()) == ""
    assert callbacks["verify"](attempt["tool_attempt_id"]) is verified
    agent._verify_local_reconciliation_probe.assert_called_once()
    coordinator.activate.assert_called_once_with(attempt["tool_attempt_id"])


def test_03_successful_recovered_reconciliation_clears_wait(monkeypatch, tmp_path):
    strategy, decision = _frozen_strategy()
    original = strategy.attempt_by_id(decision.attempt_id)
    assert original is not None
    observed_at = original.dispatched_at + 0.01
    strategy._latest_desktop_observation.update({
        "target_foreground": True,
        "foreground_application": "calculatorapp",
        "observed_at": observed_at,
    })
    assert strategy.reconcile(
        succeeded=True,
        evidence="fresh exact Calculator foreground evidence",
        evidence_details={
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "evidence_outcome": "confirmed_success",
            "reason_code": "exact_desktop_target_focus_observed",
            "postcondition": "application_foreground",
            "observed_application": "calculator",
            "observed_surface": WINDOW,
            "window_generation": "calculator-generation-1",
            "structural_epoch": 2,
            "observed_at": observed_at,
        },
    )
    st, task = _waiting_state(_frozen_strategy()[0])
    store = ToolAttemptStore(tmp_path)
    terminal = _bound_attempt(store, st, task, terminal=True)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    resolution = {
        "durable_attempt_id": terminal["tool_attempt_id"],
        "strategy_attempt_id": decision.attempt_id,
        "semantic_node_id": decision.node_id,
        "mechanism_id": decision.mechanism_id,
        "postcondition": "application_foreground",
        "target_application": "calculator",
        "target_surface": WINDOW,
        "resolution": "completed",
        "observed_at": observed_at,
    }
    agent = _RecoveryAgent()
    agent._pending_tool_runtime = {
        "attempt_id": terminal["tool_attempt_id"],
        "reconciliation_strategy": strategy,
        "reconciliation_resolution": resolution,
    }
    orchestrator = Orchestrator(agent)
    orchestrator._state = st
    orchestrator._recovered_tool_attempts.add(terminal["tool_attempt_id"])
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _st: None)

    assert orchestrator._continue_after_common_tool(
        st, 0, 0, st["phases"][0], task,
        terminal["tool_attempt_id"], terminal["status"],
    ) is True
    assert st["run_state"] == "running"
    assert task["status"] == "in_progress"
    assert task["tool_attempt_id"] == ""
    assert task["execution_memory"]["completed_effects"]


def test_04_valid_recovery_keeps_single_runner_owned(monkeypatch, tmp_path):
    from osia.server import server

    lock_file = tmp_path / "autonomous.lock"
    monkeypatch.setattr(server, "_AUTONOMOUS_LOCK_FILE", lock_file)
    monkeypatch.setitem(server._autonomous, "active", False)
    monkeypatch.setitem(server._autonomous, "gen", 0)
    generation = server._acquire_single_runner("mission.recovery-lock")
    try:
        assert generation == 1
        assert server._acquire_single_runner("mission.concurrent") is None
        assert json.loads(lock_file.read_text(encoding="utf-8"))["task_id"] == (
            "mission.recovery-lock"
        )
    finally:
        server._release_single_runner()


def test_05_completed_recovery_releases_then_accepts_new_mission(
    monkeypatch, tmp_path,
):
    from osia.server import server

    lock_file = tmp_path / "autonomous.lock"
    monkeypatch.setattr(server, "_AUTONOMOUS_LOCK_FILE", lock_file)
    monkeypatch.setitem(server._autonomous, "active", False)
    monkeypatch.setitem(server._autonomous, "gen", 0)
    generation = server._acquire_single_runner("mission.recovered")
    owner = Mock()

    server._finalize_terminal_release(owner, generation)

    assert owner.stop.call_count == 1
    assert server._autonomous["active"] is False
    assert not lock_file.exists()
    next_generation = server._acquire_single_runner("mission.new")
    try:
        assert next_generation == generation + 1
    finally:
        server._release_single_runner()


def test_06_unrecoverable_pending_effect_never_replays_and_fails_truthfully(
    monkeypatch, tmp_path,
):
    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store = ToolAttemptStore(tmp_path)
    attempt = _bound_attempt(store, st, task)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    agent = _RecoveryAgent(restorable=False)
    emitted = []
    orchestrator = Orchestrator(agent, emitted.append)
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _st: None)
    monkeypatch.setattr(orchestrator, "_strategy_for_task", lambda *_args: strategy)
    orchestrator._state = st

    orchestrator._recover_waiting_tool_checkpoint(st)

    assert agent.run_calls == []
    assert st["status"] == "failed"
    assert st["run_state"] == "failed"
    assert task["status"] == "blocked_unknown_effect"
    assert task["tool_attempt_id"] == attempt["tool_attempt_id"]
    assert st["ambiguity_barriers"]
    assert emitted[-1]["event"] == "failed"


def test_07_terminal_receipt_without_projection_cannot_return_to_waiting(
    monkeypatch, tmp_path,
):
    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store = ToolAttemptStore(tmp_path)
    terminal = _bound_attempt(store, st, task, terminal=True)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    agent = _RecoveryAgent()
    emitted = []
    orchestrator = Orchestrator(agent, emitted.append)
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _st: None)
    orchestrator._state = st
    orchestrator._recovered_tool_attempts.add(terminal["tool_attempt_id"])

    assert orchestrator._continue_after_common_tool(
        st, 0, 0, st["phases"][0], task,
        terminal["tool_attempt_id"], terminal["status"],
    ) is False
    assert st["status"] == "failed"
    assert st["run_state"] == "failed"
    assert task["status"] == "failed"
    assert task["last_error"] == "correlated_reconciliation_not_projected"
    assert st["ambiguity_barriers"] == []
    assert emitted[-1]["event"] == "failed"


def test_08_recovered_unknown_terminal_preserves_barrier_and_ends_run(
    monkeypatch, tmp_path,
):
    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store = ToolAttemptStore(tmp_path)
    attempt = _bound_attempt(store, st, task)
    terminal, _created = store.record_terminal_result(
        attempt["tool_attempt_id"],
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
        reason_code="reconciliation_hard_deadline",
    )
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    agent = _RecoveryAgent()
    emitted = []
    orchestrator = Orchestrator(agent, emitted.append)
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _st: None)
    orchestrator._state = st
    orchestrator._recovered_tool_attempts.add(terminal["tool_attempt_id"])

    assert orchestrator._on_common_tool_final(
        terminal["tool_attempt_id"], "", None,
    ) is True
    assert st["status"] == "failed"
    assert task["status"] == "blocked_unknown_effect"
    assert st["ambiguity_barriers"]
    assert agent.run_calls == []
    assert emitted[-1]["event"] == "failed"


def test_09_restart_round_trip_preserves_attempt_and_owner(monkeypatch, tmp_path):
    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store_dir = tmp_path / "tool-attempts"
    store = ToolAttemptStore(store_dir)
    attempt = _bound_attempt(store, st, task)
    monkeypatch.setattr(state, "TASK_STATE_FILE", tmp_path / "task-state.json")

    state.save_task_state(st)
    loaded = state.load_task_state()
    reloaded_attempt = ToolAttemptStore(store_dir).get_attempt(
        attempt["tool_attempt_id"],
    )

    loaded_task = loaded["phases"][0]["tasks"][0]
    assert loaded["run_state"] == "waiting_tool"
    assert loaded_task["tool_attempt_id"] == attempt["tool_attempt_id"]
    assert reloaded_attempt["owner_scope"] == "autonomous"
    assert reloaded_attempt["autonomous_run_id"] == loaded["run_id"]
    assert reloaded_attempt["autonomous_task_id"] == loaded_task["id"]
    assert reloaded_attempt["autonomous_operation_id"] == OPERATION_ID


def test_10_terminal_recovery_does_not_publish_spurious_running(
    monkeypatch,
):
    from osia.server import server

    st = {"status": "in_progress", "run_state": "waiting_tool"}

    class _Orchestrator:
        def resume(self, recovered):
            recovered["status"] = "failed"
            recovered["run_state"] = "failed"

        def block_tool_recovery(self, *_args):
            raise AssertionError("model identity is exact in this fixture")

    class _Agent:
        provider = "fixture-provider"
        model = "fixture-model"

        def set_turn_context(self, _context):
            return None

    broadcasts = []
    monkeypatch.setattr(server, "_ensure_autonomous_engine", lambda: (_Agent(), _Orchestrator()))
    monkeypatch.setattr(server, "_orch_loop_alive", lambda: False)
    monkeypatch.setattr(server._astate, "load_task_state", lambda: st)
    monkeypatch.setattr(server._astate, "save_task_state", lambda _st: None)
    monkeypatch.setattr(server._aauthorization, "revalidate_loaded_authorization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server._astate, "resume_state_is_valid", lambda _st: True)
    monkeypatch.setattr(server, "fixed_turn_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server, "_broadcast_autonomous", broadcasts.append)
    monkeypatch.setattr(server, "_release_single_runner_if_gen", lambda _gen: None)
    monkeypatch.setattr(server, "_autonomous_failure_state", lambda **_kwargs: {})
    monkeypatch.setitem(server._autonomous, "active", True)
    monkeypatch.setitem(server._autonomous, "gen", 41)
    monkeypatch.setitem(server._autonomous, "st", st)

    from osia import mobile_model_catalogue

    monkeypatch.setattr(
        mobile_model_catalogue,
        "resolve_current_autonomous_selection",
        lambda **_kwargs: {
            "provider_id": "fixture-provider",
            "model_id": "fixture-model",
            "fallback_occurred": False,
        },
    )
    monkeypatch.setattr(
        mobile_model_catalogue, "apply_autonomous_agent_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        server._adiagnostics,
        "actual_provider_model",
        lambda *_args: ("fixture-provider", "fixture-model"),
    )

    server._autonomous_resume_worker(41, "mobile.recovery-lock")

    assert not any(frame.get("event") == "running" for frame in broadcasts)


def test_11_orphan_deadline_notifies_reattached_autonomous_runtime(
    monkeypatch, tmp_path,
):
    strategy, _decision = _frozen_strategy()
    st, task = _waiting_state(strategy)
    store = ToolAttemptStore(tmp_path)
    attempt = _bound_attempt(store, st, task)
    store.mark_waiting_verification(
        attempt["tool_attempt_id"],
        next_check_at=time.time() - 2.0,
        deadline_at=time.time() - 1.0,
        reason_code="restart_reconciliation_pending",
        owner_settled=True,
    )
    coordinator = ToolAttemptCoordinator(store)
    notified = threading.Event()
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    coordinator.register_terminal_observer(
        attempt["tool_attempt_id"],
        "autonomous.recovery-lock",
        lambda *_args: notified.set(),
    )

    recovered = tool_attempts.recover_orphaned_tool_attempts()

    assert [item["tool_attempt_id"] for item in recovered] == [
        attempt["tool_attempt_id"],
    ]
    assert notified.wait(2.0)
    terminal = store.get_attempt(attempt["tool_attempt_id"])
    assert terminal["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert terminal["dispatch_state"] == "effect_unknown"


def test_12_exact_live_wait_blocks_verification_and_replan(monkeypatch):
    st = state.new_task_state(
        OBJECTIVE,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Calculate and verify",
            "tasks": [{
                "objective": ACTION,
                "success_criterion": CRITERION,
            }],
        }],
        run_id="run.live-wait",
        language="en",
    )
    st["status"] = "in_progress"
    st["run_state"] = "running"
    st["source_permission_epoch"] = 262
    task = st["phases"][0]["tasks"][0]
    attempt_id = "attempt.live-wait"

    class _LiveWaitAgent:
        provider = "fixture-provider"
        model = "fixture-model"

        def __init__(self):
            self._pending_tool_runtime = None
            self.last_pending_tool_attempt_id = ""
            self._autonomous_resume_exact_call = None
            self.activations = 0

        def reset(self):
            return None

        def clear_tool_attempts(self):
            raise AssertionError("exact live wait must not be cleared")

        def activate_pending_tool_attempt(self):
            self.activations += 1
            return True

    agent = _LiveWaitAgent()
    emitted = []
    orchestrator = Orchestrator(agent, emitted.append)
    orchestrator._state = st
    strategy = Mock()
    strategy.snapshot.return_value = {
        "goal": {"requires_execution": True},
        "succeeded": False,
        "frozen_reason": "ambiguous_effect",
        "alternatives_still_applicable": [],
    }
    monkeypatch.setattr(
        orchestrator, "_strategy_for_task", lambda *_args: strategy,
    )
    monkeypatch.setattr(
        orchestrator, "_capture_semantic_frontier", lambda *_args: None,
    )
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _st: None)
    monkeypatch.setattr(
        orchestrator,
        "_verify_outcome",
        Mock(side_effect=AssertionError(
            "verification cannot run while the exact tool is pending"
        )),
    )
    monkeypatch.setattr(
        orchestrator,
        "_replan",
        Mock(side_effect=AssertionError(
            "replan cannot run while the exact tool is pending"
        )),
    )

    def _live_wait(_prompt, **_kwargs):
        state.bind_tool_attempt(
            st,
            attempt_id=attempt_id,
            operation_id=OPERATION_ID,
            operation_revision=0,
            next_reconciliation_at=time.time() + 1.0,
        )
        st["run_state"] = "waiting_tool"
        agent.last_pending_tool_attempt_id = attempt_id
        agent._pending_tool_runtime = {"attempt_id": attempt_id}
        # Reproduce the physical failure shape: the provider/terminality layer
        # returned provisional prose even though the durable wait was bound.
        return "The action is still being checked."

    monkeypatch.setattr(orchestrator, "_agent_run", _live_wait)

    assert orchestrator._step(st) is False
    assert st["run_state"] == "waiting_tool"
    assert task["status"] == "waiting_tool"
    assert task["attempts"] == 0
    assert task["tool_attempt_id"] == attempt_id
    assert agent.activations == 1
    assert emitted[-1]["event"] == "waiting_tool"
