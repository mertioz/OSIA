"""Focused WOR-262 atomic Desktop activation checkpoint regressions."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from osia import execution_strategy as es
from osia import tool_attempts
from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
ACTION = "Enter and evaluate 347 × 29 in Calculator."
CRITERION = "Calculator displays the exact result 10063."
WINDOW = "surface-calculator-window"
GENERATION = "calculator-generation-1"
TOOLS = {
    "activate_desktop_surface",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
}


def _background_observation(observed_at: float | None = None) -> dict:
    return {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": WINDOW,
        "window_generation": GENERATION,
        "accessibility_quality": "rich_target_resolved",
        "target_resolution": "exact_target",
        "target_foreground": False,
        "target_minimized": False,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "chatgpt",
        "observed_at": observed_at or time.time(),
    }


def _frozen_activation_strategy():
    strategy = es.build_execution_strategy(
        ACTION,
        TOOLS,
        postcondition=CRITERION,
        semantic_context=TASK,
    )
    strategy._latest_desktop_observation = _background_observation()
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
    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt is not None
    assert attempt.attempted_effect == "application_foreground"
    assert strategy.frozen_reason
    return strategy, decision, attempt


def _reconcile_activation(strategy, decision, attempt) -> dict:
    observed_at = attempt.dispatched_at + 0.01
    strategy._latest_desktop_observation = {
        **_background_observation(observed_at),
        "target_foreground": True,
        "foreground_application": "calculatorapp",
    }
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
            "window_generation": GENERATION,
            "structural_epoch": 2,
            "observed_at": observed_at,
        },
    )
    return {
        "durable_attempt_id": "",
        "strategy_attempt_id": decision.attempt_id,
        "semantic_node_id": decision.node_id,
        "mechanism_id": decision.mechanism_id,
        "postcondition": "application_foreground",
        "target_application": "calculator",
        "target_surface": WINDOW,
        "resolution": "completed",
        "reason_code": "exact_desktop_target_focus_observed",
        "observed_at": observed_at,
    }


def _waiting_state(strategy) -> tuple[dict, dict]:
    st = state.new_task_state(
        TASK,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Calculate and verify",
            "tasks": [{
                "objective": ACTION,
                "success_criterion": CRITERION,
            }],
        }],
        run_id="run.atomic-activation",
        language="en",
    )
    st["status"] = "in_progress"
    st["run_state"] = "waiting_tool"
    st["source_permission_epoch"] = 262
    task = st["phases"][0]["tasks"][0]
    task["execution_memory"] = es.export_execution_memory(strategy)
    return st, task


def _terminal_attempt(monkeypatch, tmp_path, st, task):
    store = ToolAttemptStore(tmp_path)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    created = store.create_attempt(
        conversation_id="conversation.atomic-activation",
        turn_id=task["id"],
        request_id=st["run_id"],
        tool_call_id="call.atomic-activation",
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
        deadline_at=time.time() + 120,
        postcondition_type="explicit_postcondition",
        final_response_message_id="assistant.atomic-activation",
        owner_scope="autonomous",
        autonomous_run_id=st["run_id"],
        autonomous_task_id=task["id"],
        autonomous_operation_id="operation.atomic-activation",
        operation_revision=1,
        permission_epoch=262,
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
        reason_code="local_reconciliation_completed",
    )
    state.bind_tool_attempt(
        st,
        attempt_id=running["tool_attempt_id"],
        operation_id="operation.atomic-activation",
        operation_revision=1,
        next_reconciliation_at=None,
    )
    st["run_state"] = "waiting_tool"
    return store, terminal


class _Agent:
    provider = "fixture-provider"
    model = "fixture-model"

    def __init__(self, strategy, resolution, attempt_id):
        self._pending_tool_runtime = {
            "attempt_id": attempt_id,
            "reconciliation_strategy": strategy,
            "reconciliation_resolution": resolution,
        }

    def reset(self):
        return None


def _project_verified_activation(monkeypatch, tmp_path):
    strategy, decision, attempt = _frozen_activation_strategy()
    st, task = _waiting_state(strategy)
    _store, terminal = _terminal_attempt(monkeypatch, tmp_path, st, task)
    resolution = _reconcile_activation(strategy, decision, attempt)
    resolution["durable_attempt_id"] = terminal["tool_attempt_id"]
    emitted = []
    orchestrator = Orchestrator(
        _Agent(strategy, resolution, terminal["tool_attempt_id"]),
        emitted.append,
    )
    orchestrator._state = st
    orchestrator._active_task = task
    orchestrator._active_execution_strategy = strategy
    orchestrator._execution_strategies[id(task)] = strategy
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _state: None)
    assert orchestrator._continue_after_common_tool(
        st,
        0,
        0,
        st["phases"][0],
        task,
        terminal["tool_attempt_id"],
        terminal["status"],
    ) is True
    return st, task, strategy, emitted


def test_01_autonomous_owner_does_not_resume_provider_before_checkpoint():
    from osia import agent as agent_module

    agent = object.__new__(agent_module.OSIAAgent)
    agent._pending_tool_attached_existing = False
    agent.last_pending_tool_attempt_id = ""
    provider_resume = Mock(side_effect=AssertionError("provider resumed first"))
    agent._resume_suspended_tool = provider_resume
    finalized = Mock(return_value=True)
    agent._tool_attempt_metadata = {
        "owner_scope": "autonomous",
        "final_callback": finalized,
    }
    coordinator = Mock()
    record = SimpleNamespace(
        tool_attempt_id="attempt.atomic-owner",
        worker_may_be_active=False,
    )

    with patch(
        "osia.tool_attempts.get_tool_attempt_coordinator",
        return_value=coordinator,
    ):
        marker = agent._suspend_tool_turn(
            "activate_desktop_surface",
            {"surface_ref": WINDOW, "objective": "Calculator"},
            record,
        )

    callbacks = coordinator.register_suspended.call_args.kwargs
    agent._pending_tool_runtime["reconciliation_resolution"] = {
        "durable_attempt_id": "attempt.atomic-owner",
        "resolution": "completed",
    }
    assert marker == "WAITING_TOOL_RESULT"
    assert callbacks["resume"]("attempt.atomic-owner", object()) == ""
    provider_resume.assert_not_called()
    assert callbacks["finalize"]("attempt.atomic-owner", "", None) is True
    finalized.assert_called_once_with("attempt.atomic-owner", "", None)
    agent._pending_tool_runtime.pop("reconciliation_resolution")
    provider_resume.side_effect = None
    provider_resume.return_value = "provider continuation"
    assert callbacks["resume"](
        "attempt.atomic-owner", object(),
    ) == "provider continuation"
    provider_resume.assert_called_once()


def test_02_verified_activation_updates_checkpoint_and_clears_exact_wait(
    monkeypatch, tmp_path,
):
    st, task, _strategy, emitted = _project_verified_activation(
        monkeypatch, tmp_path,
    )
    effects = task["execution_memory"]["completed_effects"]
    assert any(
        item["attempted_effect"] == "application_foreground"
        and item["target_application"] == "calculator"
        and item["target_surface"] == WINDOW
        for item in effects
    )
    assert task["status"] == "in_progress"
    assert task["tool_attempt_id"] == ""
    assert task["tool_durable_state"] == ""
    assert st["run_state"] == "running"
    assert any(
        item.get("reason") == "common_tool_prerequisite_verified"
        for item in emitted
    )


def test_03_activation_completion_keeps_calculation_pending(monkeypatch, tmp_path):
    st, task, strategy, emitted = _project_verified_activation(
        monkeypatch, tmp_path,
    )
    assert st["cursor"] == {"phase": 0, "task": 0}
    assert strategy.succeeded is False
    assert strategy.goal.requested_action in {"calculate", "type"}
    assert strategy.goal.requires_execution is True
    assert not any(item.get("event") == "task_done" for item in emitted)
    assert task["execution_memory"]["effects"][0]["completed"] is False


def test_04_resume_rejects_the_completed_exact_window_activation(
    monkeypatch, tmp_path,
):
    _st, _task, strategy, _emitted = _project_verified_activation(
        monkeypatch, tmp_path,
    )
    duplicate = es.tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": WINDOW, "objective": "Calculator"},
        strategy=strategy,
    )
    assert duplicate is not None and duplicate.allowed is False
    assert duplicate.reason_code == "prerequisite_effect_already_completed"


def test_05_resume_exposes_the_next_uia_primitive(monkeypatch, tmp_path):
    _st, _task, strategy, _emitted = _project_verified_activation(
        monkeypatch, tmp_path,
    )
    primitive = es.tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "calculator-button-3", "objective": "Three"},
        strategy=strategy,
    )
    assert primitive is not None and primitive.allowed is True
    assert primitive.mechanism_id == "native_accessibility"


def test_06_unrelated_terminal_attempt_cannot_clear_wait(monkeypatch, tmp_path):
    strategy, _decision, _attempt = _frozen_activation_strategy()
    st, task = _waiting_state(strategy)
    _store, terminal = _terminal_attempt(monkeypatch, tmp_path, st, task)
    orchestrator = Orchestrator(
        _Agent(strategy, {}, terminal["tool_attempt_id"]),
    )
    orchestrator._state = st
    assert orchestrator._on_common_tool_final(
        "attempt.unrelated", "", None,
    ) is False
    assert task["status"] == "waiting_tool"
    assert task["tool_attempt_id"] == terminal["tool_attempt_id"]


@pytest.mark.parametrize("mismatch", ["wrong_target", "stale_evidence"])
def test_07_wrong_or_stale_evidence_cannot_clear_wait(
    mismatch, monkeypatch, tmp_path,
):
    strategy, decision, attempt = _frozen_activation_strategy()
    st, task = _waiting_state(strategy)
    _store, terminal = _terminal_attempt(monkeypatch, tmp_path, st, task)
    resolution = _reconcile_activation(strategy, decision, attempt)
    resolution["durable_attempt_id"] = terminal["tool_attempt_id"]
    if mismatch == "wrong_target":
        resolution["target_application"] = "notepad"
    else:
        resolution["observed_at"] = attempt.dispatched_at - 1.0
    orchestrator = Orchestrator(
        _Agent(strategy, resolution, terminal["tool_attempt_id"]),
    )
    orchestrator._state = st
    orchestrator._active_execution_strategy = strategy
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _state: None)

    assert orchestrator._continue_after_common_tool(
        st, 0, 0, st["phases"][0], task,
        terminal["tool_attempt_id"], terminal["status"],
    ) is False
    assert task["status"] == "waiting_tool"
    assert task["tool_attempt_id"] == terminal["tool_attempt_id"]
    assert task["last_error"] == "correlated_reconciliation_not_projected"


def test_08_ambiguous_activation_remains_waiting(monkeypatch, tmp_path):
    strategy, _decision, _attempt = _frozen_activation_strategy()
    st, task = _waiting_state(strategy)
    _store, terminal = _terminal_attempt(monkeypatch, tmp_path, st, task)
    orchestrator = Orchestrator(
        _Agent(strategy, {}, terminal["tool_attempt_id"]),
    )
    orchestrator._state = st
    orchestrator._active_execution_strategy = strategy
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _state: None)

    assert orchestrator._continue_after_common_tool(
        st, 0, 0, st["phases"][0], task,
        terminal["tool_attempt_id"], terminal["status"],
    ) is False
    assert task["status"] == "waiting_tool"
    assert strategy.frozen_reason


def test_09_serialization_preserves_completed_activation_and_uia_frontier():
    strategy, decision, attempt = _frozen_activation_strategy()
    _reconcile_activation(strategy, decision, attempt)
    memory = json.loads(json.dumps(es.export_execution_memory(strategy)))
    rebuilt = es.build_execution_strategy(
        ACTION,
        TOOLS,
        postcondition=CRITERION,
        semantic_context=TASK,
    )
    es.rebind_execution_memory(
        rebuilt,
        [memory],
        now=float(memory["desktop_observation"]["observed_at"]) + 1.0,
    )

    assert rebuilt.effect_is_completed(
        "application_foreground",
        target_application="calculator",
        target_surface=WINDOW,
    )
    duplicate = es.tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": WINDOW, "objective": "Calculator"},
        strategy=rebuilt,
    )
    primitive = es.tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "calculator-button-7", "objective": "Seven"},
        strategy=rebuilt,
    )
    assert duplicate is not None and duplicate.allowed is False
    assert primitive is not None and primitive.allowed is True


def test_10_anti_replay_blocks_duplicate_activation_before_reconciliation():
    strategy, _decision, _attempt = _frozen_activation_strategy()
    duplicate = es.tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": WINDOW, "objective": "Calculator"},
        strategy=strategy,
    )
    assert duplicate is not None and duplicate.allowed is False
    assert duplicate.reason_code == "ambiguous_effect_requires_reconciliation"
