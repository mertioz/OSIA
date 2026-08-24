"""Focused WOR-265 Phase 2A durable continuation and terminality regressions."""

from __future__ import annotations

import copy
import json
import time
from unittest.mock import Mock, patch

from osia import agent as agent_module
from osia import chat_routing
from osia import desktop_scene
from osia import execution_strategy as es
from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator, _VerificationOutcome
from osia.server import server
from osia.tool_attempts import WAITING_TOOL_RESULT
from osia.tool_contracts import (
    ExecutionOutcome,
    ToolExecutionRecord,
    effective_contract,
    get_tool_spec,
)
from tests.test_wor262_desktop_calculator_stabilization import (
    BUTTON_LABELS,
    _observe_stepwise_primitive,
    _start_stepwise_primitive,
    _stepwise_strategy,
)
from tests.test_wor264_normal_chat_desktop import TASK, TOOLS
from tests.test_wor262_stateless_uia_effect_proof import (
    TASK as UIA_TASK,
    _dispatch as _dispatch_uia_primitive,
    _effect as _uia_effect,
    _scene as _uia_scene,
    _selection as _uia_selection,
    _strategy as _uia_strategy,
)


class _Agent:
    provider = "fixture-provider"
    model = "fixture-model"

    def __init__(self):
        self._pending_tool_runtime = None
        self.last_pending_tool_attempt_id = ""
        self._autonomous_resume_exact_call = None
        self.activated = 0
        self.cleared = 0

    def reset(self):
        return None

    def clear_tool_attempts(self):
        self.cleared += 1

    def activate_pending_tool_attempt(self):
        self.activated += 1
        return True


class _StopOrchestrator:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def _durable_state(*, run_id="run.wor265.phase2a"):
    st = state.new_task_state(
        TASK,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Calculate",
            "tasks": [{
                "objective": "Enter and evaluate 5 + 23.",
                "success_criterion": "The Calculator display shows 28.",
            }],
        }],
        run_id=run_id,
        language="fr",
    )
    st.update({
        "status": "in_progress",
        "run_state": "running",
        "source_conversation_id": "conversation.wor265",
        "source_request_id": "request.wor265",
        "source_turn_id": "turn.wor265",
    })
    return st


def _five_checkpoint():
    st = _durable_state()
    task = st["phases"][0]["tasks"][0]
    task["execution_memory"] = {
        "schema_version": 1,
        "effects": [{
            "effect_key": "calculation-5-plus-23",
            "target_application": "calculator",
            "requested_action": "type",
            "requested_content": "5+23",
            "requires_execution": True,
            "completed": False,
            "required_postconditions": ["The Calculator display shows 28."],
            "completed_primitive_signatures": ["calculation|five"],
            "completed_substeps": [{
                "composite_id": "calculation",
                "substep_id": "five",
                "substep_index": 1,
                "attempt_id": "strategy-five",
                "action_signature": "five",
                "attempted_effect": "L’affichage est 5",
                "evidence": "evidence-five",
            }],
        }],
        "completed_effects": [],
        "evidence_revision": 1,
    }
    task["semantic_frontier_remaining"] = ["+", "2", "3", "="]
    return st


def _park_verifier_wait(orchestrator, st):
    task = st["phases"][0]["tasks"][0]
    calls = []

    def _run(_message, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return "The display is 5."
        task["tool_attempt_id"] = "attempt.plus"
        st["run_state"] = "waiting_tool"
        orchestrator.agent.last_pending_tool_attempt_id = "attempt.plus"
        orchestrator.agent._pending_tool_runtime = {
            "attempt_id": "attempt.plus",
        }
        return WAITING_TOOL_RESULT

    orchestrator._agent_run = _run
    return calls


def _isolated_state_file(monkeypatch, tmp_path):
    path = tmp_path / "task_state_mobile.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", path)
    monkeypatch.setattr(state.document_artifacts, "cleanup_state_artifacts", lambda _st: None)
    return path


def _terminalize_cancel(monkeypatch, tmp_path, st=None):
    _isolated_state_file(monkeypatch, tmp_path)
    st = st or _durable_state()
    st.update({
        "abandon_origin_status": "in_progress",
        "abandon_origin_run_state": "running",
        "abandon_state": "abandon_requested",
    })
    orchestrator = _StopOrchestrator()
    monkeypatch.setitem(server._autonomous, "agent", None)
    assert server._terminalize_autonomous_abandon(st, orchestrator)
    return st, orchestrator


def test_01_verified_primitive_does_not_complete_parent_composite():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    _observe_stepwise_primitive(strategy, "3")

    assert len(strategy._completed_substeps) == 1
    assert strategy.mission.action_nodes[1].status != "succeeded"
    assert strategy.succeeded is False


def test_02_completed_primitive_cannot_replay_or_skip_the_next_one():
    strategy = _stepwise_strategy()
    _first, _attempt, first_arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    _observe_stepwise_primitive(strategy, "3")

    replay = es.tool_preflight(
        "activate_desktop_surface", first_arguments, strategy=strategy,
    )
    second = es.tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "calculator-button-1",
            "objective": BUTTON_LABELS[1],
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "calculator-display",
                "expected_text": "34",
            },
        },
        strategy=strategy,
    )
    third = es.tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "calculator-button-2",
            "objective": BUTTON_LABELS[2],
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "calculator-display",
                "expected_text": "347",
            },
        },
        strategy=strategy,
    )

    assert replay is not None and replay.reason_code == "primitive_already_completed"
    assert second is not None and second.allowed
    assert third is not None and not third.allowed


def test_03_phase2a_progress_projection_is_observational_after_five():
    st = _five_checkpoint()
    before = copy.deepcopy(st)

    projected = chat_routing.durable_progress_from_state(st)

    assert projected["terminal"] is False
    assert st == before
    assert st["phases"][0]["tasks"][0]["semantic_frontier_remaining"] == [
        "+", "2", "3", "=",
    ]


def test_04_checkpoint_reload_after_five_keeps_plus_as_exact_next_substep(
    monkeypatch, tmp_path,
):
    _isolated_state_file(monkeypatch, tmp_path)
    st = _five_checkpoint()
    state.save_task_state(st)

    loaded = state.load_task_state()
    task = loaded["phases"][0]["tasks"][0]

    assert task["execution_memory"]["effects"][0]["completed"] is False
    assert task["execution_memory"]["effects"][0]["completed_substeps"][0][
        "attempted_effect"
    ] == "L’affichage est 5"
    assert task["semantic_frontier_remaining"] == ["+", "2", "3", "="]


def test_05_verifier_wait_has_a_durable_reason_not_an_ambiguous_reply():
    agent = _Agent()
    orchestrator = Orchestrator(agent)
    orchestrator._state = _durable_state()
    orchestrator._active_task = orchestrator._state["phases"][0]["tasks"][0]
    orchestrator._agent_run = lambda _message, **_kwargs: WAITING_TOOL_RESULT

    outcome = orchestrator._llm_judge_outcome(
        "The Calculator display shows 28.",
    )

    assert outcome.reason_code == "verification_waiting_tool"
    assert outcome.ok is False


def test_06_mutation_started_by_verifier_parks_the_exact_receipt():
    st = _five_checkpoint()
    agent = _Agent()
    events = []
    orchestrator = Orchestrator(agent, events.append)
    orchestrator._safe_save = lambda _st: None
    calls = _park_verifier_wait(orchestrator, st)

    assert orchestrator._step(st) is False

    task = st["phases"][0]["tasks"][0]
    assert calls == [1, 2]
    assert st["run_state"] == "waiting_tool"
    assert task["status"] == "waiting_tool"
    assert task["tool_attempt_id"] == "attempt.plus"
    assert agent.activated == 1
    assert events[-1]["event"] == "waiting_tool"


def test_07_verifier_wait_restores_budget_and_never_records_retry():
    st = _five_checkpoint()
    agent = _Agent()
    orchestrator = Orchestrator(agent)
    orchestrator._safe_save = lambda _st: None
    _park_verifier_wait(orchestrator, st)

    assert orchestrator._step(st) is False

    task = st["phases"][0]["tasks"][0]
    assert task["attempts"] == 0
    assert not any(item.get("event") == "retry" for item in st["log"])
    assert task["execution_memory"]["effects"][0]["completed"] is False


def test_08_reload_recheck_parks_plus_without_replaying_or_spending_budget():
    st = _five_checkpoint()
    task = st["phases"][0]["tasks"][0]
    task.update({"status": "in_progress", "attempts": 2})
    agent = _Agent()
    orchestrator = Orchestrator(agent)
    orchestrator._safe_save = lambda _st: None
    orchestrator._resume_recheck = True

    def _verify(_task, _evidence):
        task["tool_attempt_id"] = "attempt.plus"
        st["run_state"] = "waiting_tool"
        agent.last_pending_tool_attempt_id = "attempt.plus"
        agent._pending_tool_runtime = {"attempt_id": "attempt.plus"}
        return _VerificationOutcome(False, None, "verification_waiting_tool")

    orchestrator._verify_outcome = _verify

    assert orchestrator._step(st) is False
    assert task["attempts"] == 2
    assert task["semantic_frontier_remaining"][0] == "+"
    assert agent.activated == 1


def test_09_correlated_cancel_establishes_one_authoritative_terminal(
    monkeypatch, tmp_path,
):
    st, orchestrator = _terminalize_cancel(monkeypatch, tmp_path)

    assert st["status"] == "aborted"
    assert st["run_state"] == "cancelled"
    assert st["terminal_authority"]["kind"] == "user_cancelled"
    assert st["terminal_authority"]["run_id"] == st["run_id"]
    assert orchestrator.stopped == 1
    assert chat_routing.terminal_result_from_state(st, "fr")["status"] == (
        "cancelled"
    )


def test_10_late_blocked_unknown_effect_cannot_overwrite_cancel_terminal(
    monkeypatch, tmp_path,
):
    st, _orchestrator = _terminalize_cancel(monkeypatch, tmp_path)
    st["status"] = "failed"
    st["run_state"] = "blocked_unknown_effect"
    state.save_task_state(st)

    loaded = state.load_task_state()
    assert loaded["status"] == "aborted"
    assert loaded["run_state"] == "cancelled"
    assert loaded["abandon_state"] == "abandoned_proven"


def test_11_cancel_authority_preserves_late_unknown_effect_evidence(
    monkeypatch, tmp_path,
):
    st, _orchestrator = _terminalize_cancel(monkeypatch, tmp_path)
    st["operation_records"] = [{
        "operation_id": "operation.plus",
        "status": "outcome_unknown",
        "execution_outcome": "outcome_unknown",
        "tool_attempt_id": "attempt.plus",
    }]
    st["ambiguity_barriers"] = [{
        "operation_id": "operation.plus",
        "status": "outcome_unknown",
    }]
    st["run_state"] = "blocked_unknown_effect"
    state.save_task_state(st)

    loaded = state.load_task_state()
    assert loaded["run_state"] == "cancelled"
    assert loaded["operation_records"][0]["status"] == "outcome_unknown"
    assert loaded["ambiguity_barriers"][0]["operation_id"] == "operation.plus"


def test_12_stale_callback_copy_cannot_rewrite_newer_persisted_terminal(
    monkeypatch, tmp_path,
):
    _isolated_state_file(monkeypatch, tmp_path)
    st = _durable_state()
    state.save_task_state(st)
    stale = copy.deepcopy(st)
    st.update({
        "abandon_origin_status": "in_progress",
        "abandon_origin_run_state": "running",
        "abandon_state": "abandon_requested",
    })
    monkeypatch.setitem(server._autonomous, "agent", None)
    assert server._terminalize_autonomous_abandon(st, _StopOrchestrator())

    stale["status"] = "failed"
    stale["run_state"] = "blocked_unknown_effect"
    stale["failure_diagnostic"] = None
    state.save_task_state(stale)

    loaded = state.load_task_state()
    assert stale["run_state"] == "cancelled"
    assert loaded["terminal_authority"]["revision"] == 1
    assert loaded["failure_diagnostic"]["reason_code"] == "user_aborted"


def test_13_non_cancelled_unknown_effect_behavior_is_unchanged(
    monkeypatch, tmp_path,
):
    _isolated_state_file(monkeypatch, tmp_path)
    st = _durable_state(run_id="run.non.cancelled")
    st.update({"status": "failed", "run_state": "blocked_unknown_effect"})
    st["operation_records"] = [{
        "operation_id": "operation.real-unknown",
        "status": "outcome_unknown",
    }]

    state.save_task_state(st)
    loaded = state.load_task_state()

    assert loaded["status"] == "failed"
    assert loaded["run_state"] == "blocked_unknown_effect"
    assert "terminal_authority" not in loaded
    assert chat_routing.durable_progress_from_state(loaded)["status"] == "blocked"


def test_14_phase1_calculator_contract_still_reaches_verified_28(tmp_path):
    strategy = es.build_execution_strategy(TASK, TOOLS)
    open_node, calculation = strategy.mission.action_nodes
    assert strategy.verify_postcondition(
        "exact Calculator open evidence",
        postcondition=open_node.observable_success_postcondition,
    )
    exact_result = {
        "scene": {
            "application": "calculatorapp",
            "requested_application": "calculator",
            "window_surface_ref": "surface-calculator-window",
            "target_resolution": "exact_target",
            "target_foreground": True,
            "foreground_application": "calculatorapp",
        },
        "receipt": {
            "outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "postcondition": calculation.observable_success_postcondition,
            "observed_target": "28",
        },
    }

    assert es.record_tool_observation(
        "inspect_desktop_surfaces",
        exact_result,
        is_error=False,
        strategy=strategy,
    )
    assert strategy.succeeded is True
    assert calculation.observable_success_postcondition.endswith("exact result 28")


def test_15_autonomous_boundary_retires_only_proven_no_effect_continuation():
    agent = _Agent()
    agent._unfinished_execution = {
        "call": {
            "tool": "activate_desktop_surface",
            "surface_ref": "stale-surface",
        },
        "outcome": "failed_before_effect",
        "reason_code": "semantic_target_surface_unavailable",
    }
    agent._continuation_resolution_pending = True
    agent._continuation_resume_selected = True
    orchestrator = Orchestrator(agent)

    assert orchestrator._retire_failed_before_effect_continuation() is True
    assert agent._unfinished_execution is None
    assert agent._continuation_resolution_pending is False
    assert agent._continuation_resume_selected is False

    unknown = {
        "call": {"tool": "external_mutation"},
        "outcome": "outcome_unknown",
        "reason_code": "outcome_unknown_requires_reconciliation",
    }
    agent._unfinished_execution = unknown
    assert orchestrator._retire_failed_before_effect_continuation() is False
    assert agent._unfinished_execution is unknown


def test_16_ambiguous_read_only_probe_is_retried_then_resolves_exact_effect():
    strategy = _uia_strategy()
    _decision, attempt, arguments = _dispatch_uia_primitive(
        strategy,
        effect=_uia_effect(),
    )
    probe_contract = effective_contract(
        get_tool_spec("inspect_desktop_surfaces"),
        {"target": "calculator"},
        UIA_TASK,
    )
    original_contract = effective_contract(
        get_tool_spec("activate_desktop_surface"),
        arguments,
        UIA_TASK,
    )
    original_record = ToolExecutionRecord(
        {},
        False,
        ExecutionOutcome.PENDING,
        1,
        True,
        False,
        original_contract,
        "attempt-wor265-progressive-probe",
    )
    runtime = {
        "attempt_id": "attempt-wor265-progressive-probe",
        "tool_name": "activate_desktop_surface",
        "wait_spec": None,
        "record": original_record,
        "reconciliation_strategy": strategy,
        # A persisted safe-alternative audit entry is history, not an
        # exhaustion marker for later bounded read-only observations.
        "local_reconciliation_probe_attempted": True,
    }
    durable = {
        "dispatch_state": "owner_settled_unverified",
        "hard_deadline_at": time.time() + 30.0,
        "inactivity_deadline_at": time.time() + 30.0,
        "attempted_safe_alternatives": [
            "local_desktop_application_observation",
        ],
    }

    probe_results = []
    for display in ("0", "3"):
        scene = _uia_scene(
            display,
            observed_at=attempt.dispatched_at + 1.0 + len(probe_results),
        )
        with (
            es.execution_strategy_scope(UIA_TASK, strategy=strategy),
            patch(
                "osia.desktop_scene._select_desktop_target",
                return_value=_uia_selection(scene),
            ),
        ):
            result = desktop_scene.inspect_desktop_surfaces("calculator")
        probe_results.append(ToolExecutionRecord(
            result,
            False,
            ExecutionOutcome.SUCCESS_VERIFIED,
            1,
            True,
            False,
            probe_contract,
        ))

    store = Mock()
    store.get_attempt.return_value = durable
    agent = object.__new__(agent_module.OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-terra"
    agent.lang = "en"
    agent._pending_tool_runtime = runtime
    agent._terminality_controller = None
    agent._terminality_strategy = strategy

    with (
        patch(
            "osia.tool_attempts.get_tool_attempt_store",
            return_value=store,
        ),
        patch(
            "osia.agent._execute_tool_with_policy",
            side_effect=probe_results,
        ) as execute_probe,
        patch("osia.agent._persist_tool_attempt_terminal"),
        patch("osia.action_policy.guard_tool_call", return_value=None),
    ):
        first = agent._verify_suspended_tool(runtime["attempt_id"])
        assert runtime["local_reconciliation_probe_in_progress"] is False
        second = agent._verify_suspended_tool(runtime["attempt_id"])

    assert first is None
    assert second is not None
    assert second.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    assert execute_probe.call_count == 2
    assert all(
        call.args[0] == "inspect_desktop_surfaces"
        for call in execute_probe.call_args_list
    )
    assert runtime["reconciliation_resolution"]["resolution"] == (
        "primitive_completed"
    )
    assert runtime["local_reconciliation_probe_in_progress"] is False


def test_17_settled_no_op_probe_resolves_before_effect_and_allows_continuation():
    strategy = _uia_strategy()
    _decision, attempt, arguments = _dispatch_uia_primitive(
        strategy,
        effect=_uia_effect(),
    )
    probe_contract = effective_contract(
        get_tool_spec("inspect_desktop_surfaces"),
        {"target": "calculator"},
        UIA_TASK,
    )
    original_contract = effective_contract(
        get_tool_spec("activate_desktop_surface"),
        arguments,
        UIA_TASK,
    )
    original_record = ToolExecutionRecord(
        {},
        False,
        ExecutionOutcome.PENDING,
        1,
        True,
        False,
        original_contract,
        "attempt-wor265-settled-no-op",
    )
    runtime = {
        "attempt_id": "attempt-wor265-settled-no-op",
        "tool_name": "activate_desktop_surface",
        "wait_spec": None,
        "record": original_record,
        "reconciliation_strategy": strategy,
    }
    durable = {
        "dispatch_state": "owner_settled_unverified",
        "hard_deadline_at": time.time() + 30.0,
        "inactivity_deadline_at": time.time() + 30.0,
        "attempted_safe_alternatives": [],
    }
    scene = _uia_scene(
        "0",
        observed_at=attempt.dispatched_at + 6.0,
    )
    with (
        es.execution_strategy_scope(UIA_TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_uia_selection(scene),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("calculator")
    probe_record = ToolExecutionRecord(
        result,
        False,
        ExecutionOutcome.SUCCESS_VERIFIED,
        1,
        True,
        False,
        probe_contract,
    )

    store = Mock()
    store.get_attempt.return_value = durable
    agent = object.__new__(agent_module.OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-terra"
    agent.lang = "en"
    agent._pending_tool_runtime = runtime
    agent._terminality_controller = None
    agent._terminality_strategy = strategy

    with (
        patch(
            "osia.tool_attempts.get_tool_attempt_store",
            return_value=store,
        ),
        patch(
            "osia.agent._execute_tool_with_policy",
            return_value=probe_record,
        ),
        patch("osia.agent._persist_tool_attempt_terminal") as persist,
        patch("osia.action_policy.guard_tool_call", return_value=None),
    ):
        resolved = agent._verify_suspended_tool(runtime["attempt_id"])

    assert resolved is not None
    assert resolved.outcome is ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert resolved.reason_code == "local_reconciliation_not_applied"
    assert runtime["reconciliation_resolution"]["resolution"] == (
        "primitive_not_applied"
    )
    assert strategy.attempt_by_id(attempt.attempt_id).status == "blocked"
    assert strategy.frozen_reason == ""
    store.record_safe_alternative.assert_called_once_with(
        runtime["attempt_id"],
        "local_desktop_application_observation",
    )
    persist.assert_called_once()
