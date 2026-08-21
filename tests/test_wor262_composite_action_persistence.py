"""Focused WOR-262 composite-action progression regressions."""

from __future__ import annotations

import json
import time

from osia import execution_strategy as es
from osia import tool_attempts
from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
from osia.tool_contracts import (
    effective_static_tool_specs,
    effective_tool_surface_scope,
)


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
COMPOSITE = "Open Calculator and enter 347 × 29."
FINAL_CRITERION = "Calculator displays the exact result 10063."
TOOLS = {
    "activate_desktop_surface",
    "desktop_click",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
    "query_document",
    "request_visual_observation",
    "run_command",
    "take_screenshot",
}


def _observation():
    return {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": "surface-calculator-window",
        "window_generation": "calculator-generation-1",
        "accessibility_quality": "rich_target_resolved",
        "target_resolution": "exact_target",
        "target_foreground": False,
        "target_minimized": False,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "chatgpt",
        "observed_at": time.time(),
    }


def _pending_composite_strategy():
    strategy = es.build_execution_strategy(
        COMPOSITE,
        TOOLS,
        postcondition=FINAL_CRITERION,
        semantic_context=TASK,
    )
    decision = es.tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    es.record_tool_outcome(
        "open_application",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="owner_still_running",
        result={"dispatch_started": True},
        strategy=strategy,
    )
    observed_at = time.time() + 0.01
    assert strategy.reconcile(
        succeeded=True,
        evidence="fresh exact Calculator application observation",
        evidence_details={
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "evidence_outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "postcondition": "application_open",
            "observed_application": "calculator",
            "observed_surface": "surface-calculator-window",
            "window_generation": "calculator-generation-1",
            "structural_epoch": 1,
            "observed_at": observed_at,
        },
    )
    strategy._latest_desktop_observation = {
        **_observation(),
        "observed_at": observed_at,
    }
    assert strategy.succeeded is False
    return strategy


class _Agent:
    provider = "fixture-provider"
    model = "fixture-model"

    def __init__(self, strategy, attempt_id):
        self._pending_tool_runtime = {
            "attempt_id": attempt_id,
            "reconciliation_strategy": strategy,
        }

    def reset(self):
        return None


def _continue_verified_open(monkeypatch, tmp_path):
    strategy = _pending_composite_strategy()
    st = state.new_task_state(
        TASK,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Calculate and verify",
            "tasks": [{
                "objective": COMPOSITE,
                "success_criterion": FINAL_CRITERION,
            }],
        }],
        run_id="run.composite",
        language="en",
    )
    st["status"] = "in_progress"
    st["run_state"] = "running"
    st["source_permission_epoch"] = 262
    task = st["phases"][0]["tasks"][0]
    task["execution_memory"] = es.export_execution_memory(strategy)

    store = ToolAttemptStore(tmp_path)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    created = store.create_attempt(
        conversation_id="conversation.composite",
        turn_id=task["id"],
        request_id=st["run_id"],
        tool_call_id="call.open",
        tool_name="open_application",
        provider_id=st["provider"],
        model_id=st["model"],
        turn_context={"language": "en"},
        argument_fingerprint=store.fingerprint_arguments({"name": "Calculator"}),
        side_effect="reversible_local",
        idempotence="conditionally_idempotent",
        retry_policy="never_after_dispatch",
        deadline_at=time.time() + 120,
        postcondition_type="explicit_postcondition",
        final_response_message_id="assistant.open",
        owner_scope="autonomous",
        autonomous_run_id=st["run_id"],
        autonomous_task_id=task["id"],
        autonomous_operation_id="operation.open",
        operation_revision=1,
        permission_epoch=262,
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    terminal, _ = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
        reason_code="local_reconciliation_completed",
    )
    state.bind_tool_attempt(
        st,
        attempt_id=running["tool_attempt_id"],
        operation_id="operation.open",
        operation_revision=1,
        next_reconciliation_at=None,
    )

    emitted = []
    orchestrator = Orchestrator(
        _Agent(strategy, running["tool_attempt_id"]), emitted.append,
    )
    orchestrator._state = st
    orchestrator._active_task = task
    orchestrator._active_execution_strategy = strategy
    orchestrator._execution_strategies[id(task)] = strategy
    monkeypatch.setattr(orchestrator, "_safe_save", lambda _state: None)
    orchestrator._pending_tool_completion = (
        running["tool_attempt_id"], terminal["status"],
    )
    assert orchestrator._step(st) is True
    return st, task, strategy, emitted


def _reconstructed_verification_strategy():
    memory = json.loads(json.dumps(es.export_execution_memory(
        _pending_composite_strategy()
    )))
    st = state.new_task_state(
        TASK,
        "fixture-provider",
        "fixture-model",
        [
            {
                "name": "Prior composite",
                "tasks": [{
                    "objective": COMPOSITE,
                    "success_criterion": FINAL_CRITERION,
                    "status": "failed",
                    "execution_memory": memory,
                }],
            },
            {
                "name": "Replanned verification",
                "tasks": [{
                    "objective": "Verify the Calculator display is 10063.",
                    "success_criterion": FINAL_CRITERION,
                }],
            },
        ],
        run_id="run.replanned-composite",
        language="en",
    )
    task = st["phases"][1]["tasks"][0]
    orchestrator = Orchestrator(type("Agent", (), {
        "provider": "fixture-provider",
        "model": "fixture-model",
    })())
    orchestrator._state = st
    return orchestrator._strategy_for_task(st, task)


def _provider_frontier(strategy):
    with (
        es.execution_strategy_scope(TASK, strategy=strategy),
        effective_tool_surface_scope("autonomous"),
    ):
        return {spec.name for spec in effective_static_tool_specs()}


def test_01_open_completion_does_not_complete_composite_task(monkeypatch, tmp_path):
    st, task, _strategy, emitted = _continue_verified_open(monkeypatch, tmp_path)
    assert task["status"] == "in_progress"
    assert st["cursor"] == {"phase": 0, "task": 0}
    assert not any(item.get("event") == "task_done" for item in emitted)


def test_02_remaining_calculation_stays_executable(monkeypatch, tmp_path):
    _st, task, strategy, _emitted = _continue_verified_open(monkeypatch, tmp_path)
    assert strategy.goal.requires_execution is True
    assert task["execution_memory"]["effects"][0]["requires_execution"] is True


def test_03_progression_retains_exact_calculator_target(monkeypatch, tmp_path):
    _st, task, strategy, _emitted = _continue_verified_open(monkeypatch, tmp_path)
    assert strategy.goal.required_application == "calculator"
    assert task["execution_memory"]["effects"][0]["target_application"] == "calculator"


def test_04_replan_restores_remaining_semantic_action_from_memory():
    strategy = _reconstructed_verification_strategy()
    assert strategy.goal.requested_action == "type"
    assert strategy.goal.requires_execution is True
    assert strategy.goal.target_object == "347 × 29"


def test_05_serialization_preserves_remaining_action_contract():
    memory = es.export_execution_memory(_pending_composite_strategy())
    restored = es.normalize_execution_memory(json.loads(json.dumps(memory)))
    effect = restored["effects"][0]
    assert effect["requested_action"] == "type"
    assert effect["requires_execution"] is True
    assert effect["target_object"] == "347 × 29"
    assert effect["required_postconditions"] == [FINAL_CRITERION]


def test_06_completed_open_is_preserved_and_not_replayed():
    strategy = _reconstructed_verification_strategy()
    assert strategy.effect_is_completed(
        "application_open", target_application="calculator",
    )
    assert es.tool_applicability(
        strategy, "open_application", {"name": "Calculator"},
    ) == (False, "prerequisite_effect_already_completed")


def test_07_remaining_frontier_excludes_open_application():
    assert "open_application" not in _provider_frontier(
        _reconstructed_verification_strategy()
    )


def test_08_remaining_frontier_excludes_generic_run_command():
    strategy = _reconstructed_verification_strategy()
    assert "run_command" not in _provider_frontier(strategy)
    decision = es.tool_preflight(
        "run_command",
        {"command": "generic desktop automation"},
        strategy=strategy,
    )
    assert decision is not None and decision.allowed is False


def test_09_remaining_frontier_exposes_canonical_uia_path():
    strategy = _reconstructed_verification_strategy()
    assert "native_accessibility" in {
        item.mechanism_id for item in strategy.remaining_mechanisms()
    }
    assert {"inspect_desktop_surfaces", "activate_desktop_surface"}.issubset(
        _provider_frontier(strategy)
    )


def test_10_informational_verification_cannot_replace_pending_mutation():
    strategy = _reconstructed_verification_strategy()
    assert strategy.mission.objective == "Verify the Calculator display is 10063."
    assert strategy.goal.requires_execution is True
    assert strategy.succeeded is False


def test_11_final_completion_requires_exact_result_postcondition():
    strategy = _pending_composite_strategy()
    assert strategy.succeeded is False
    assert strategy.verify_postcondition(
        "fresh but unrelated Calculator evidence",
        postcondition="Calculator is open.",
    ) is False
    assert strategy.succeeded is False
    assert strategy.verify_postcondition(
        "fresh exact Calculator display evidence for 10063",
        postcondition=FINAL_CRITERION,
    ) is True
    assert strategy.succeeded is True


def test_12_real_autonomous_three_step_progression_keeps_calculation_frontier():
    open_criterion = "The Calculator application window is visible."
    opened = es.build_execution_strategy(
        "Open Calculator.",
        TOOLS,
        postcondition=open_criterion,
        semantic_context=TASK,
    )
    decision = es.tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=opened,
    )
    assert decision is not None and decision.allowed
    es.record_tool_outcome(
        "open_application",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="owner_still_running",
        result={"dispatch_started": True},
        strategy=opened,
    )
    assert opened.reconcile(
        succeeded=True,
        evidence="fresh exact Calculator window evidence",
        evidence_details={
            "postcondition": "application_open",
            "observed_at": time.time() + 0.01,
        },
    )
    opened._latest_desktop_observation = _observation()
    assert opened.succeeded is True

    st = state.new_task_state(
        TASK,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Open, calculate, verify",
            "tasks": [
                {
                    "objective": "Open Calculator.",
                    "success_criterion": open_criterion,
                    "status": "done",
                    "execution_memory": es.export_execution_memory(opened),
                },
                {
                    "objective": "Enter and evaluate 347 × 29 in Calculator.",
                    "success_criterion": FINAL_CRITERION,
                },
                {
                    "objective": "Verify the Calculator display is 10063.",
                    "success_criterion": FINAL_CRITERION,
                },
            ],
        }],
        run_id="run.real-autonomous-progression",
        language="en",
    )
    st = json.loads(json.dumps(st))
    st["cursor"] = {"phase": 0, "task": 1}
    calculation_task = st["phases"][0]["tasks"][1]
    orchestrator = Orchestrator(type("Agent", (), {
        "provider": "fixture-provider",
        "model": "fixture-model",
    })())
    orchestrator._state = st
    calculation = orchestrator._strategy_for_task(st, calculation_task)
    frontier = _provider_frontier(calculation)

    assert st["phases"][0]["tasks"][0]["status"] == "done"
    assert calculation_task["status"] == "pending"
    assert calculation.goal.requested_action == "type"
    assert calculation.goal.requires_execution is True
    assert calculation.goal.required_application == "calculator"
    assert "open_application" not in frontier
    assert "run_command" not in frontier
    assert {"inspect_desktop_surfaces", "activate_desktop_surface"}.issubset(frontier)
