"""WOR-262 preparatory attempted-effect identity regressions."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from osia.autonomous.orchestrator import Orchestrator
from osia.execution_strategy import (
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    build_execution_strategy,
    execution_strategy_scope,
    export_execution_memory,
    rebind_execution_memory,
    record_tool_observation,
    record_tool_outcome,
    select_local_reconciliation_probe,
    tool_preflight,
)
from osia.turn_terminality import TurnTerminalityController


TASK = "Open Calculator and calculate 347 x 29. Verify the result is 10063."
STEP = "Open Calculator and enter 347 x 29."
CRITERION = "Calculator displays the multiplication expression or its computed result."
TOOLS = {
    "activate_desktop_surface",
    "desktop_click",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
}


def _physical_prompt() -> str:
    task = {
        "id": "0.0",
        "objective": STEP,
        "success_criterion": CRITERION,
        "requirement_id": "",
        "semantic_next_strategy": "",
    }
    orchestrator = Orchestrator(Mock())
    orchestrator._state = {
        "objective": TASK,
        "phases": [{"name": "Calculate and verify", "tasks": [task]}],
        "browser_checkpoint": {},
        "checklist": [],
        "document_sources": [],
        "artifacts": [],
    }
    return orchestrator._compose(task, "")


def _physical_open_unknown():
    prompt = _physical_prompt()
    strategy = build_execution_strategy(prompt, TOOLS)
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record_tool_outcome(
        "open_application",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="owner_still_running",
        result={"dispatch_started": True},
        strategy=strategy,
    )
    return prompt, strategy, decision


def test_physical_type_node_preserves_preparatory_application_open_effect():
    from osia.tool_contracts import get_tool_spec

    _prompt, strategy, decision = _physical_open_unknown()
    node = strategy.node_by_id(decision.node_id)
    attempt = strategy.attempt_by_id(decision.attempt_id)

    assert node is not None and attempt is not None
    assert node.requested_action == "type"
    assert node.observable_success_postcondition == CRITERION
    assert attempt.node_id == node.node_id
    assert attempt.mechanism_id == "direct:open_application:calculator"
    assert attempt.attempted_effect == "application_open"
    assert attempt.target_application == "calculator"
    assert get_tool_spec("open_application").attempted_effect == (
        "application_open"
    )


def test_physical_type_node_unknown_selects_local_application_observer():
    prompt, strategy, decision = _physical_open_unknown()

    probe = select_local_reconciliation_probe(
        strategy,
        {"inspect_desktop_surfaces"},
        user_request=prompt,
    )

    assert probe is not None
    assert probe["tool"] == "inspect_desktop_surfaces"
    assert probe["arguments"] == {"target": "calculator"}
    assert probe["semantic_effect"] == "application_open"
    assert probe["postcondition"] == "application_open"
    assert probe["strategy_attempt_id"] == decision.attempt_id
    assert strategy.goal.observable_success_postcondition == CRITERION


def _application_open_observation(strategy) -> dict:
    attempt = strategy.attempt_by_id(strategy._frozen_attempt_id)
    assert attempt is not None
    return {
        "text": "exact Calculator application surface observed",
        "scene": {
            "application": "calculatorapp",
            "requested_application": "calculator",
            "window_surface_ref": "surface-calculator-window",
            "window_generation": "calculator-generation-1",
            "structural_epoch": 4,
            "target_resolution": "exact_target",
            "target_foreground": False,
            "foreground_application": "robloxplayerbeta",
            "observed_at": attempt.dispatched_at + 2.0,
        },
        "receipt": {
            "schema_version": 1,
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "dispatch_state": "effect_confirmed",
            "outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "semantic_postcondition": "application_open",
            "postcondition": "application_open",
            "observed_target": "calculator",
            "observed_application": "calculatorapp",
            "observed_at": attempt.dispatched_at + 2.0,
        },
    }


def _resolve_application_open(strategy) -> bool:
    return record_tool_observation(
        "inspect_desktop_surfaces",
        _application_open_observation(strategy),
        is_error=False,
        strategy=strategy,
    )


def test_attempt_effect_survives_snapshot_serialization_and_reconstruction():
    _prompt, strategy, decision = _physical_open_unknown()

    restored = ExecutionStrategy.from_snapshot(
        json.loads(json.dumps(strategy.snapshot()))
    )
    attempt = restored.attempt_by_id(decision.attempt_id)

    assert attempt is not None
    assert attempt.attempted_effect == "application_open"
    assert attempt.target_application == "calculator"
    assert restored.goal.observable_success_postcondition == CRITERION


def test_attempt_effect_survives_autonomous_execution_memory_rebind():
    _prompt, strategy, decision = _physical_open_unknown()
    memory = json.loads(json.dumps(export_execution_memory(strategy)))
    rebuilt = build_execution_strategy(_physical_prompt(), TOOLS)

    rebind_execution_memory(rebuilt, [memory])
    attempt = rebuilt.attempt_by_id(decision.attempt_id)

    assert attempt is not None
    assert attempt.attempted_effect == "application_open"
    assert attempt.target_application == "calculator"


def test_durable_unknown_uses_attempt_effect_not_parent_result(tmp_path):
    from osia.agent import OSIAAgent
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    prompt, strategy, decision = _physical_open_unknown()
    contract = effective_contract(
        get_tool_spec("open_application"), {"name": "Calculator"}, prompt,
    )
    record = ToolExecutionRecord(
        "unknown", True, ExecutionOutcome.OUTCOME_UNKNOWN, 1, True, True,
        contract, "attempt_durable_open", False, "owner_still_running",
    )
    controller = TurnTerminalityController(
        turn_id="wor262-attempt-effect-durable",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    agent = SimpleNamespace(
        _terminality_controller=controller,
        _unfinished_execution=None,
        _emit_execution_trace=lambda *_args, **_kwargs: None,
    )

    with execution_strategy_scope(prompt, strategy=strategy):
        OSIAAgent._record_unfinished_execution(
            agent,
            "open_application",
            {"name": "Calculator"},
            record,
            decision,
        )
    controller.set_strategy_snapshot(
        json.loads(json.dumps(strategy.snapshot()))
    )
    unknown = controller.snapshot()["unknown_effects"]["attempt_durable_open"]

    assert unknown["postcondition"] == "application_open"
    assert unknown["strategy_attempt_id"] == decision.attempt_id
    assert unknown["semantic_node_id"] == decision.node_id
    assert unknown["target_application"] == "calculator"
    assert CRITERION in controller.snapshot()["remaining_work"]


def test_selector_rejects_wrong_attempt_target():
    prompt, strategy, decision = _physical_open_unknown()
    strategy.attempt_by_id(decision.attempt_id).target_application = "paint"

    assert select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=prompt,
    ) is None


def test_selector_rejects_wrong_attempt_effect():
    prompt, strategy, decision = _physical_open_unknown()
    strategy.attempt_by_id(decision.attempt_id).attempted_effect = "text_entered"

    assert select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=prompt,
    ) is None


def test_selector_rejects_observer_when_static_contract_is_not_read_only():
    from osia.tool_contracts import SideEffectClass, get_tool_spec

    prompt, strategy, _decision = _physical_open_unknown()
    original = get_tool_spec("inspect_desktop_surfaces")

    with patch(
        "osia.tool_contracts.get_tool_spec",
        return_value=replace(
            original, side_effect=SideEffectClass.REVERSIBLE_LOCAL,
        ),
    ):
        probe = select_local_reconciliation_probe(
            strategy, {"inspect_desktop_surfaces"}, user_request=prompt,
        )

    assert probe is None


def test_selector_rejects_observer_when_effective_contract_is_not_read_only():
    from osia.tool_contracts import (
        SideEffectClass,
        effective_contract,
        get_tool_spec,
    )

    prompt, strategy, _decision = _physical_open_unknown()
    spec = get_tool_spec("inspect_desktop_surfaces")
    effective = effective_contract(spec, {"target": "calculator"}, prompt)

    with patch(
        "osia.tool_contracts.effective_contract",
        return_value=replace(
            effective, side_effect=SideEffectClass.REVERSIBLE_LOCAL,
        ),
    ):
        probe = select_local_reconciliation_probe(
            strategy, {"inspect_desktop_surfaces"}, user_request=prompt,
        )

    assert probe is None


def test_application_open_observation_resolves_only_preparatory_attempt():
    _prompt, strategy, decision = _physical_open_unknown()
    node = strategy.node_by_id(decision.node_id)

    assert _resolve_application_open(strategy) is True
    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt.status == "progressed"
    assert node.status == "pending"
    assert node.observable_success_postcondition not in strategy.proven_postconditions
    assert strategy.succeeded is False


def test_application_open_receipt_cannot_prove_parent_result_or_10063():
    _prompt, strategy, decision = _physical_open_unknown()
    node = strategy.node_by_id(decision.node_id)

    assert _resolve_application_open(strategy) is True
    assert node.observable_success_postcondition == CRITERION
    assert strategy.proven_postconditions == {}
    assert not any("10063" in item for item in strategy.proven_postconditions)


def test_after_open_reconciliation_next_desktop_mechanism_is_eligible():
    _prompt, strategy, decision = _physical_open_unknown()
    assert _resolve_application_open(strategy) is True
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY,
        node_id=decision.node_id,
    )

    assert native is not None
    assert strategy.preview(
        native,
        {"surface_ref": "calculator-window", "objective": "focus"},
    ).allowed is True


def test_reconciled_open_application_cannot_replay():
    _prompt, strategy, _decision = _physical_open_unknown()
    assert _resolve_application_open(strategy) is True

    replay = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )

    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "mechanism_retired_after_outcome_unknown"


def test_ambiguous_preparatory_effect_blocks_next_mutation():
    _prompt, strategy, _decision = _physical_open_unknown()

    next_mutation = tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "calculator-window", "objective": "focus"},
        strategy=strategy,
    )

    assert next_mutation is not None and next_mutation.allowed is False
    assert next_mutation.reason_code == "ambiguous_effect_requires_reconciliation"


def test_application_foreground_effect_is_independent_from_parent_goal():
    strategy = build_execution_strategy(_physical_prompt(), TOOLS)
    node = strategy.goal
    mechanism = StrategyMechanism(
        "native:foreground:calculator",
        StrategyLayer.NATIVE_ACCESSIBILITY,
        "typed foreground prerequisite",
        application="calculator",
        covers_goal=False,
        mutation=True,
        node_id=node.node_id,
        attempted_effect="application_foreground",
    )
    decision = strategy.select(
        mechanism, {"surface_ref": "calculator-window"},
    )
    attempt = strategy.attempt_by_id(decision.attempt_id)

    assert decision.allowed
    assert attempt.attempted_effect == "application_foreground"
    assert attempt.node_id == node.node_id
    assert node.observable_success_postcondition == CRITERION


def test_uia_primitive_effect_is_independent_from_composite_parent():
    _prompt, strategy, decision = _physical_open_unknown()
    assert _resolve_application_open(strategy) is True

    primitive = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "calculator-button-3",
            "objective": "Three",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "calculator-display",
                "expected_text": "3",
            },
        },
        strategy=strategy,
    )
    attempt = strategy.attempt_by_id(primitive.attempt_id)

    assert primitive is not None and primitive.allowed
    assert attempt.composite_id == decision.node_id
    assert attempt.substep_id
    assert attempt.primitive_postcondition
    assert attempt.attempted_effect == attempt.primitive_postcondition
    assert attempt.node_id == decision.node_id


def test_exact_uia_resume_preserves_strategy_effect_identity():
    from osia.agent import (
        OSIAAgent,
        _strategy_decision_for_unfinished_resume,
    )
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    prompt, strategy, _open_decision = _physical_open_unknown()
    assert _resolve_application_open(strategy) is True
    arguments = {
        "surface_ref": "calculator-button-3",
        "objective": "Three",
        "observable_effect": {
            "kind": "exact_surface_text",
            "surface_ref": "calculator-display",
            "expected_text": "3",
        },
    }
    decision = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    original_effect = decision.attempted_effect
    contract = effective_contract(
        get_tool_spec("activate_desktop_surface"), arguments, prompt,
    )
    failed = ToolExecutionRecord(
        "ERROR: stale semantic reference",
        True,
        ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
        1,
        True,
        False,
        contract,
        "attempt_durable_failed_uia",
        False,
        "stale_semantic_reference",
    )
    agent = SimpleNamespace(
        _terminality_controller=None,
        _unfinished_execution=None,
        _emit_execution_trace=lambda *_args, **_kwargs: None,
    )

    with execution_strategy_scope(prompt, strategy=strategy):
        record_tool_outcome(
            "activate_desktop_surface",
            decision,
            outcome=failed.outcome.value,
            is_error=True,
            reason_code=failed.reason_code,
            result=failed.result,
            strategy=strategy,
        )
        OSIAAgent._record_unfinished_execution(
            agent,
            "activate_desktop_surface",
            arguments,
            failed,
            decision,
        )
        resumed = _strategy_decision_for_unfinished_resume(
            agent._unfinished_execution,
        )
        assert resumed is not None and resumed.allowed
        assert resumed.attempt_id == decision.attempt_id
        assert resumed.node_id == decision.node_id
        assert resumed.attempted_effect == original_effect
        mismatched_state = json.loads(json.dumps(agent._unfinished_execution))
        mismatched_state["strategy_binding"]["attempted_effect"] = "wrong effect"
        assert _strategy_decision_for_unfinished_resume(mismatched_state) is None
        record_tool_outcome(
            "activate_desktop_surface",
            resumed,
            outcome="outcome_unknown",
            is_error=True,
            reason_code="reconciliation_pending",
            result={"dispatch_started": True},
            strategy=strategy,
        )

    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.attempted_effect == original_effect
    assert attempt.failure_or_stagnation_reason == (
        "semantic_dispatch_witness_missing"
    )
    assert strategy._frozen_attempt_id == ""
    assert all(
        item.mechanism_id != "tool:activate_desktop_surface"
        for item in strategy.attempts
    )


def test_final_success_still_requires_exact_parent_postcondition_evidence():
    _prompt, strategy, decision = _physical_open_unknown()
    node = strategy.node_by_id(decision.node_id)
    assert _resolve_application_open(strategy) is True
    assert strategy.succeeded is False

    assert strategy.verify_postcondition(
        "fresh exact Calculator expression/result evidence",
        postcondition=node.observable_success_postcondition,
    ) is True
    assert strategy.succeeded is True


def test_timer_path_reconstructs_attempt_effect_and_resolves_without_provider(
    tmp_path,
):
    from osia import agent as agent_module
    from osia.tool_attempts import (
        ToolAttemptCoordinator,
        ToolAttemptStatus,
        ToolAttemptStore,
    )
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
        persistence_contract,
    )
    from tests.test_wor262_desktop_calculator_stabilization import (
        _named_scene,
        _target_selection,
    )

    prompt, strategy, decision = _physical_open_unknown()
    open_spec = get_tool_spec("open_application")
    open_contract = effective_contract(
        open_spec, {"name": "Calculator"}, prompt,
    )
    policy = persistence_contract(
        open_spec,
        tool_name="open_application",
        arguments={"name": "Calculator"},
        owner_timeout_seconds=30.0,
    )
    store = ToolAttemptStore(tmp_path / "attempts")
    created = store.create_attempt(
        conversation_id="autonomous.attempt.effect",
        turn_id="0.0",
        request_id="attempt_effect",
        tool_call_id="toolcall_attempt_effect",
        tool_name="open_application",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={"language": "en", "source": "autonomous_state"},
        argument_fingerprint=store.fingerprint_arguments(
            {"name": "Calculator"},
        ),
        side_effect="reversible_local",
        idempotence="conditionally_idempotent",
        retry_policy="never_after_dispatch",
        owner_scope="autonomous",
        autonomous_run_id="attempt_effect_run",
        autonomous_task_id="0.0",
        autonomous_operation_id="op_attempt_effect",
        persistence_contract={
            "family": policy.family.value,
            "release_after_seconds": policy.release_after_seconds,
            "inactivity_timeout_seconds": policy.inactivity_timeout_seconds,
            "hard_timeout_seconds": policy.hard_timeout_seconds,
            "check_delays_seconds": list(policy.check_delays_seconds),
            "safe_alternatives": list(policy.safe_alternatives),
            "restart_reconciliation": policy.restart_reconciliation,
        },
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    waiting = store.mark_waiting_verification(
        running["tool_attempt_id"],
        next_check_at=time.time(),
        deadline_at=running["hard_deadline_at"],
        reason_code="owner_still_running",
        owner_settled=True,
    )
    pending = ToolExecutionRecord(
        "pending", True, ExecutionOutcome.PENDING, 1, True, False,
        open_contract, waiting["tool_attempt_id"], False,
        "owner_still_running",
    )
    controller = TurnTerminalityController(
        turn_id="wor262-attempt-effect-timer",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    controller.record_execution_record(
        "open_application", pending, postcondition="application_open",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    agent = agent_module.OSIAAgent(model="wor262-attempt-effect", lang="en")
    agent.provider = "ollama"
    agent.client = Mock(name="provider_client")
    agent._terminality_controller = controller
    agent._terminality_strategy = None
    agent._pending_tool_runtime = {
        "attempt_id": waiting["tool_attempt_id"],
        "tool_name": "open_application",
        "wait_spec": None,
        "worker_may_be_active": False,
        "record": pending,
        "attached_existing": False,
        "strategy_snapshot": json.loads(json.dumps(strategy.snapshot())),
    }
    foreground = _named_scene(
        "robloxplayerbeta", "Roblox", rich=False, foreground=True,
        key="attempt-effect-foreground", observed_at=time.time() + 1.0,
    )
    calculator = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="attempt-effect-calculator", host_application="applicationframehost",
        observed_at=time.time() + 2.0,
    )
    coordinator = ToolAttemptCoordinator(store)
    resumed = []
    finalized = threading.Event()
    coordinator.register_suspended(
        waiting["tool_attempt_id"],
        resume=lambda _attempt_id, payload: resumed.append(payload) or "",
        finalize=lambda *_args: finalized.set() or True,
        verify=agent._verify_suspended_tool,
    )
    coordinator._runtimes[waiting["tool_attempt_id"]].activated = True

    with patch(
        "osia.tool_attempts.get_tool_attempt_store", return_value=store,
    ), patch(
        "osia.desktop_scene._select_desktop_target",
        return_value=_target_selection(foreground, calculator),
    ):
        coordinator._timer_fired(waiting["tool_attempt_id"])

    assert finalized.wait(2.0)
    terminal = store.get_attempt(waiting["tool_attempt_id"])
    restored = agent._pending_tool_runtime["reconciliation_strategy"]
    restored_attempt = restored.attempt_by_id(decision.attempt_id)
    restored_node = restored.node_by_id(decision.node_id)
    assert terminal["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert terminal["attempted_safe_alternatives"] == [
        "local_desktop_application_observation",
    ]
    assert restored_attempt.attempted_effect == "application_open"
    assert restored_attempt.status == "progressed"
    assert restored_node.status == "pending"
    assert CRITERION not in restored.proven_postconditions
    assert controller.snapshot()["unknown_effects"] == {}
    assert CRITERION in controller.snapshot()["remaining_work"]
    assert agent.client.mock_calls == []
    assert resumed


def test_physical_owner_still_running_probe_executes_but_target_is_absent(
    tmp_path,
):
    """Reproduce the sanitized physical boundary without launching an app."""

    from osia import agent as agent_module
    from osia.desktop_scene import TargetSurfaceSelection
    from osia.tool_attempts import ToolAttemptCoordinator, ToolAttemptStore
    from osia.tool_contracts import (
        ExecutionOutcome,
        SideEffectClass,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
        persistence_contract,
    )
    from tests.test_wor262_desktop_calculator_stabilization import _named_scene

    prompt, strategy, decision = _physical_open_unknown()
    open_spec = get_tool_spec("open_application")
    open_contract = effective_contract(
        open_spec, {"name": "Calculator"}, prompt,
    )
    policy = persistence_contract(
        open_spec,
        tool_name="open_application",
        arguments={"name": "Calculator"},
        owner_timeout_seconds=30.0,
    )
    store = ToolAttemptStore(tmp_path / "physical-target-missing")
    created = store.create_attempt(
        conversation_id="autonomous.physical.target-missing",
        turn_id="0.0",
        request_id="physical_target_missing",
        tool_call_id="toolcall_physical_target_missing",
        tool_name="open_application",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={"language": "en", "source": "autonomous_state"},
        argument_fingerprint=store.fingerprint_arguments(
            {"name": "Calculator"},
        ),
        side_effect="reversible_local",
        idempotence="conditionally_idempotent",
        retry_policy="never_after_dispatch",
        owner_scope="autonomous",
        autonomous_run_id="physical_target_missing_run",
        autonomous_task_id="0.0",
        autonomous_operation_id="op_physical_target_missing",
        persistence_contract={
            "family": policy.family.value,
            "release_after_seconds": policy.release_after_seconds,
            "inactivity_timeout_seconds": policy.inactivity_timeout_seconds,
            "hard_timeout_seconds": policy.hard_timeout_seconds,
            "check_delays_seconds": list(policy.check_delays_seconds),
            "safe_alternatives": list(policy.safe_alternatives),
            "restart_reconciliation": policy.restart_reconciliation,
        },
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    waiting = store.mark_waiting_verification(
        running["tool_attempt_id"],
        next_check_at=time.time(),
        deadline_at=running["hard_deadline_at"],
        reason_code="owner_still_running",
        owner_settled=True,
    )
    pending = ToolExecutionRecord(
        "pending", True, ExecutionOutcome.PENDING, 1, True, False,
        open_contract, waiting["tool_attempt_id"], False,
        "owner_still_running",
    )
    controller = TurnTerminalityController(
        turn_id="wor262-physical-target-missing",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "physical-target-missing-terminality",
    )
    controller.record_execution_record(
        "open_application", pending, postcondition="application_open",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    agent = agent_module.OSIAAgent(
        model="wor262-physical-target-missing", lang="en",
    )
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-terra"
    agent.client = Mock(name="provider_client")
    agent._terminality_controller = controller
    agent._terminality_strategy = None
    agent._pending_tool_runtime = {
        "attempt_id": waiting["tool_attempt_id"],
        "tool_name": "open_application",
        "wait_spec": None,
        "worker_may_be_active": False,
        "record": pending,
        "attached_existing": False,
        "strategy_snapshot": json.loads(json.dumps(strategy.snapshot())),
    }
    foreground = _named_scene(
        "chatgpt", "ChatGPT", rich=True, foreground=True,
        key="physical-target-missing-foreground",
        observed_at=time.time() + 1.0,
    )
    selection = TargetSurfaceSelection(
        requested_application="calculator",
        status="target_missing",
        foreground_scene=foreground,
        target_scene=None,
        candidate_count=0,
        top_level_surfaces=({
            "application": "chatgpt",
            "title": "ChatGPT",
            "window_class": "Chrome_WidgetWin_1",
            "foreground": True,
        },),
    )
    coordinator = ToolAttemptCoordinator(store)
    coordinator.register_suspended(
        waiting["tool_attempt_id"],
        resume=lambda *_args: "",
        finalize=lambda *_args: True,
        verify=agent._verify_suspended_tool,
    )
    coordinator._runtimes[waiting["tool_attempt_id"]].activated = True

    counts = {"selected": 0, "preflight": 0, "dispatched": 0, "executed": 0}
    probe_records = []
    selected_probe = agent_module._execution_strategy.select_local_reconciliation_probe
    probe_preflight = agent_module._execution_strategy.tool_preflight
    canonical_dispatch = agent_module._execute_tool_with_policy
    observer = agent_module.TOOL_MAP["inspect_desktop_surfaces"]

    def _select(*args, **kwargs):
        result = selected_probe(*args, **kwargs)
        counts["selected"] += int(isinstance(result, dict))
        return result

    def _preflight(*args, **kwargs):
        counts["preflight"] += 1
        return probe_preflight(*args, **kwargs)

    def _dispatch(*args, **kwargs):
        counts["dispatched"] += 1
        result = canonical_dispatch(*args, **kwargs)
        probe_records.append(result)
        return result

    def _observe(*args, **kwargs):
        counts["executed"] += 1
        return observer(*args, **kwargs)

    try:
        with patch(
            "osia.tool_attempts.get_tool_attempt_store", return_value=store,
        ), patch(
            "osia.desktop_scene._select_desktop_target", return_value=selection,
        ), patch.object(
            agent_module._execution_strategy,
            "select_local_reconciliation_probe",
            side_effect=_select,
        ), patch.object(
            agent_module._execution_strategy,
            "tool_preflight",
            side_effect=_preflight,
        ), patch.object(
            agent_module,
            "_execute_tool_with_policy",
            side_effect=_dispatch,
        ), patch.dict(
            agent_module.TOOL_MAP,
            {"inspect_desktop_surfaces": _observe},
        ):
            coordinator._timer_fired(waiting["tool_attempt_id"])
    finally:
        timer = coordinator._runtimes[waiting["tool_attempt_id"]].timer
        if timer is not None:
            timer.cancel()

    durable = store.get_attempt(waiting["tool_attempt_id"])
    restored = agent._pending_tool_runtime["reconciliation_strategy"]
    restored_attempt = restored.attempt_by_id(decision.attempt_id)
    restored_node = restored.node_by_id(decision.node_id)
    probe_record = probe_records[0]
    result = probe_record.result

    assert counts == {
        "selected": 1, "preflight": 1, "dispatched": 1, "executed": 1,
    }
    assert probe_record.contract.side_effect is SideEffectClass.READ_ONLY
    assert probe_record.is_error is False
    assert result["scene"]["target_resolution"] == "target_missing"
    assert result["scene"]["target_candidate_count"] == 0
    assert result["scene"]["requested_application"] == "calculator"
    assert result["receipt"]["reason_code"] == (
        "semantic_target_surface_not_found"
    )
    assert restored._latest_desktop_observation["target_resolution"] == (
        "target_missing"
    )
    assert restored_attempt.status == "ambiguous_effect"
    assert restored_node.status == "unknown"
    assert restored.frozen_reason == "owner_still_running"
    assert durable["attempted_safe_alternatives"] == [
        "local_desktop_application_observation",
    ]
    assert durable["status"] == "waiting_verification"
    assert controller.snapshot()["unknown_effect_resolutions"] == []
    assert controller.snapshot()["unknown_effects"]
    assert agent.client.mock_calls == []


def test_end_to_end_type_node_open_reconcile_uia_and_exact_10063():
    from tests.test_wor262_desktop_calculator_stabilization import (
        BUTTON_LABELS,
        _dispatch_target_window_focus,
        _observe_stepwise_primitive,
        _reacquisition_world,
        _record_target_observation,
        _start_stepwise_primitive,
        _target_selection,
    )

    final_criterion = "Calculator visibly displays the exact result 10063."
    strategy = build_execution_strategy(
        STEP,
        TOOLS,
        postcondition=final_criterion,
        semantic_context=TASK,
    )
    node = strategy.goal
    assert node.requested_action == "type"
    open_decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert open_decision is not None and open_decision.allowed
    record_tool_outcome(
        "open_application",
        open_decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="owner_still_running",
        result={"dispatch_started": True},
        strategy=strategy,
    )
    assert _resolve_application_open(strategy) is True
    assert node.status == "pending"
    assert final_criterion not in strategy.proven_postconditions

    foreground, calculator = _reacquisition_world()
    _dispatch_target_window_focus(strategy, foreground, calculator)
    _ignored, focused = _reacquisition_world(target_foreground=True)
    focus_result, focused_exact = _record_target_observation(
        strategy,
        _target_selection(focused, focused),
        target="reconcile",
    )
    assert focused_exact is True
    assert focus_result["scene"]["target_foreground"] is True

    prior = "0"
    final_observation = None
    primitive_reconciliations = 0
    observed_results = ("3", "34", "347", "347×", "2", "29", "10 063")
    for index, (label, after) in enumerate(zip(BUTTON_LABELS, observed_results)):
        _start_stepwise_primitive(strategy, label, before_result=prior)
        final_observation, reconciled = _observe_stepwise_primitive(
            strategy,
            after,
            target="10063" if index == len(BUTTON_LABELS) - 1 else "reconcile",
        )
        primitive_reconciliations += int(reconciled)
        prior = after

    assert final_observation["receipt"]["observed_target"] == "10063"
    assert final_observation["receipt"]["postcondition"] == final_criterion
    assert final_criterion in strategy.proven_postconditions
    assert strategy.succeeded is True
    assert primitive_reconciliations == len(BUTTON_LABELS)
    assert sum(
        attempt.mechanism_id == "direct:open_application:calculator"
        for attempt in strategy.attempts
    ) == 1
    assert not any(
        attempt.mechanism_id in {
            "direct:run_command", "keyboard_focus", "local_vision",
            "global_vision",
        }
        and attempt.status == "selected"
        for attempt in strategy.attempts
    )


PHYSICAL_OPEN_RECEIPT = (
    "Launched: Calculator — the app is running (its window did not take the "
    "foreground within 14s; take a screenshot to see the current state before "
    "typing)."
)


def _policy_payload(policy) -> dict:
    return {
        "family": policy.family.value,
        "release_after_seconds": policy.release_after_seconds,
        "inactivity_timeout_seconds": policy.inactivity_timeout_seconds,
        "hard_timeout_seconds": policy.hard_timeout_seconds,
        "check_delays_seconds": list(policy.check_delays_seconds),
        "safe_alternatives": list(policy.safe_alternatives),
        "restart_reconciliation": policy.restart_reconciliation,
    }


def _attempt_context(store, *, suffix: str) -> dict:
    return {
        "enabled": True,
        "store": store,
        "conversation_id": f"autonomous.reconciliation.{suffix}",
        "turn_id": "0.0",
        "request_id": f"request_{suffix}",
        "tool_call_id": f"toolcall_{suffix}",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-terra",
        "owner_scope": "autonomous",
        "autonomous_binding": lambda _tool, _arguments: {
            "run_id": f"run_{suffix}",
            "task_id": "0.0",
            "operation_id": f"operation_{suffix}",
            "operation_revision": 1,
            "permission_epoch": 1,
        },
    }


def _physical_policy_record(store, monkeypatch, *, suffix: str = "physical"):
    from osia import agent as agent_module

    calls = []

    def physical_open(name):
        calls.append(name)
        return PHYSICAL_OPEN_RECEIPT

    monkeypatch.setitem(agent_module.TOOL_MAP, "open_application", physical_open)
    context = _attempt_context(store, suffix=suffix)
    record = agent_module._execute_tool_with_policy(
        "open_application",
        {"name": "Calculator"},
        user_request=_physical_prompt(),
        lang="en",
        enforce_execution_permit=False,
        attempt_context=context,
    )
    return record, context, calls


def test_reconciliation_suspension_01_legacy_contract_terminalizes_exact_receipt(
    tmp_path,
):
    from osia import agent as agent_module
    from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
        persistence_contract,
    )

    arguments = {"name": "Calculator"}
    legacy_spec = replace(
        get_tool_spec("open_application"), postcondition_required=False,
    )
    contract = effective_contract(legacy_spec, arguments, _physical_prompt())
    policy = persistence_contract(
        legacy_spec,
        tool_name="open_application",
        arguments=arguments,
        owner_timeout_seconds=30.0,
    )
    store = ToolAttemptStore(tmp_path / "legacy-terminal")
    created = store.create_attempt(
        conversation_id="autonomous.reconciliation.legacy",
        turn_id="0.0",
        request_id="request_legacy",
        tool_call_id="toolcall_legacy",
        tool_name="open_application",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments(arguments),
        side_effect=contract.side_effect.value,
        idempotence=contract.idempotency.value,
        retry_policy=contract.retry.value,
        owner_scope="autonomous",
        autonomous_run_id="run_legacy",
        autonomous_task_id="0.0",
        autonomous_operation_id="operation_legacy",
        persistence_contract=_policy_payload(policy),
    )
    running = store.mark_running(
        store.mark_accepted(created["tool_attempt_id"])["tool_attempt_id"],
    )
    receipt = ToolExecutionRecord(
        PHYSICAL_OPEN_RECEIPT,
        False,
        ExecutionOutcome.SUCCESS_UNVERIFIED,
        1,
        True,
        False,
        contract,
        running["tool_attempt_id"],
    )

    terminal, created_terminal = agent_module._persist_tool_attempt_terminal(
        store, running["tool_attempt_id"], receipt,
    )

    assert created_terminal is True
    assert terminal["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert terminal["terminal_outcome"] == "success_unverified"


def test_reconciliation_suspension_02_contract_has_typed_observable_effect():
    from osia.tool_contracts import get_tool_spec, persistence_contract

    spec = get_tool_spec("open_application")
    policy = persistence_contract(
        spec,
        tool_name="open_application",
        arguments={"name": "Calculator"},
        owner_timeout_seconds=30.0,
    )

    assert spec.attempted_effect == "application_open"
    assert spec.postcondition_required is True
    assert policy.safe_alternatives == (
        "local_desktop_application_observation",
    )


def test_reconciliation_suspension_03_exact_receipt_enters_waiting_tool(
    tmp_path, monkeypatch,
):
    from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
    from osia.tool_contracts import ExecutionOutcome

    store = ToolAttemptStore(tmp_path / "physical-waiting")
    record, _context, calls = _physical_policy_record(store, monkeypatch)
    durable = store.get_attempt(record.tool_attempt_id)

    assert calls == ["Calculator"]
    assert record.outcome is ExecutionOutcome.PENDING
    assert record.dispatched is True
    assert durable["status"] == ToolAttemptStatus.WAITING_VERIFICATION.value
    assert durable["dispatch_state"] == "owner_settled_unverified"
    assert durable["reason_code"] == "postcondition_required_unverified"
    assert durable["postcondition_type"] == "explicit_postcondition"
    assert durable["persistence_contract"]["safe_alternatives"] == [
        "local_desktop_application_observation",
    ]


def test_reconciliation_suspension_04_snapshot_survives_suspension(
    tmp_path, monkeypatch,
):
    from osia import agent as agent_module
    from osia.tool_attempts import ToolAttemptCoordinator, ToolAttemptStore

    store = ToolAttemptStore(tmp_path / "suspension-snapshot")
    coordinator = ToolAttemptCoordinator(store)
    prompt = _physical_prompt()
    strategy = build_execution_strategy(prompt, TOOLS)
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record, _context, _calls = _physical_policy_record(
        store, monkeypatch, suffix="snapshot",
    )
    record_tool_outcome(
        "open_application",
        decision,
        outcome=record.outcome.value,
        is_error=record.is_error,
        reason_code=record.reason_code,
        result=record.result,
        strategy=strategy,
    )
    agent = agent_module.OSIAAgent(model="wor262-snapshot", lang="en")
    agent._tool_attempt_metadata = {}

    with patch(
        "osia.tool_attempts.get_tool_attempt_coordinator",
        return_value=coordinator,
    ), execution_strategy_scope(prompt, strategy=strategy):
        marker = agent._suspend_tool_turn(
            "open_application", {"name": "Calculator"}, record,
        )

    restored = ExecutionStrategy.from_snapshot(json.loads(json.dumps(
        agent._pending_tool_runtime["strategy_snapshot"],
    )))
    restored_attempt = restored.attempt_by_id(decision.attempt_id)
    probe = select_local_reconciliation_probe(
        restored, {"inspect_desktop_surfaces"}, user_request=prompt,
    )

    assert marker == "WAITING_TOOL_RESULT"
    assert restored_attempt.attempted_effect == "application_open"
    assert restored_attempt.target_application == "calculator"
    assert probe["arguments"] == {"target": "calculator"}
    assert probe["strategy_attempt_id"] == decision.attempt_id


def test_reconciliation_suspension_05_real_coordinator_resolves_and_advances(
    tmp_path, monkeypatch,
):
    from osia import agent as agent_module
    from osia.tool_attempts import (
        ToolAttemptCoordinator,
        ToolAttemptStatus,
        ToolAttemptStore,
    )
    from tests.test_wor262_capability_progression import (
        _calculation_strategy,
        _provider_frontier,
    )
    from tests.test_wor262_desktop_calculator_stabilization import (
        _named_scene,
        _target_selection,
    )

    store = ToolAttemptStore(tmp_path / "real-coordinator")
    prompt = _physical_prompt()
    strategy = build_execution_strategy(prompt, TOOLS)
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    pending, _context, open_calls = _physical_policy_record(
        store, monkeypatch, suffix="real_coordinator",
    )
    record_tool_outcome(
        "open_application",
        decision,
        outcome=pending.outcome.value,
        is_error=pending.is_error,
        reason_code=pending.reason_code,
        result=pending.result,
        strategy=strategy,
    )
    controller = TurnTerminalityController(
        turn_id="wor262-real-coordinator",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "real-coordinator-terminality",
    )
    controller.record_execution_record(
        "open_application", pending, postcondition="application_open",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    agent = agent_module.OSIAAgent(model="wor262-real-coordinator", lang="en")
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-terra"
    agent.client = Mock(name="provider_client")
    agent._terminality_controller = controller
    agent._terminality_strategy = None
    agent._pending_tool_runtime = {
        "attempt_id": pending.tool_attempt_id,
        "tool_name": "open_application",
        "wait_spec": None,
        "worker_may_be_active": False,
        "record": pending,
        "attached_existing": False,
        "strategy_snapshot": json.loads(json.dumps(strategy.snapshot())),
    }
    attempt = strategy.attempt_by_id(decision.attempt_id)
    foreground = _named_scene(
        "chatgpt", "ChatGPT", rich=False, foreground=True,
        key="real-coordinator-foreground", observed_at=attempt.dispatched_at + 1.0,
    )
    calculator = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="real-coordinator-calculator", host_application="applicationframehost",
        observed_at=attempt.dispatched_at + 2.0,
    )
    selection = _target_selection(foreground, calculator)
    observer_calls = []
    original_observer = agent_module.TOOL_MAP["inspect_desktop_surfaces"]

    def observe_once(**arguments):
        observer_calls.append(dict(arguments))
        return original_observer(**arguments)

    monkeypatch.setitem(
        agent_module.TOOL_MAP, "inspect_desktop_surfaces", observe_once,
    )
    coordinator = ToolAttemptCoordinator(store)
    resumed = []
    finalized = threading.Event()
    coordinator.register_suspended(
        pending.tool_attempt_id,
        resume=lambda _attempt_id, payload: resumed.append(payload) or "",
        finalize=lambda *_args: finalized.set() or True,
        verify=agent._verify_suspended_tool,
    )
    coordinator._runtimes[pending.tool_attempt_id].activated = True

    with patch(
        "osia.tool_attempts.get_tool_attempt_store", return_value=store,
    ), patch(
        "osia.desktop_scene._select_desktop_target", return_value=selection,
    ):
        coordinator._timer_fired(pending.tool_attempt_id)

    assert finalized.wait(2.0)
    terminal = store.get_attempt(pending.tool_attempt_id)
    reconciled = agent._pending_tool_runtime["reconciliation_strategy"]
    reconciled_attempt = reconciled.attempt_by_id(decision.attempt_id)
    reconciled_node = reconciled.node_by_id(decision.node_id)
    memory = export_execution_memory(reconciled)
    calculation = _calculation_strategy(memory=memory)
    frontier = _provider_frontier(calculation)

    assert open_calls == ["Calculator"]
    assert observer_calls == [{"target": "calculator"}]
    assert terminal["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert terminal["terminal_outcome"] == "success_verified"
    assert terminal["attempted_safe_alternatives"] == [
        "local_desktop_application_observation",
    ]
    assert resumed[0].reason_code == "local_reconciliation_completed"
    assert reconciled_attempt.status == "progressed"
    assert reconciled_node.status == "pending"
    assert CRITERION not in reconciled.proven_postconditions
    assert "open_application" not in frontier
    assert frontier == {
        "activate_desktop_surface", "inspect_desktop_surfaces",
    }
    assert agent.client.mock_calls == []


def test_reconciliation_suspension_06_unknown_postcondition_has_no_observer(
    tmp_path, monkeypatch,
):
    from osia import agent as agent_module
    from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
    from osia.tool_contracts import ExecutionOutcome, get_tool_spec

    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "open_uri",
        lambda uri: calls.append(uri) or "Opened the allowlisted URI.",
    )
    store = ToolAttemptStore(tmp_path / "unknown-postcondition")
    arguments = {"uri": "https://example.com"}
    first_context = _attempt_context(store, suffix="unknown_postcondition")
    first = agent_module._execute_tool_with_policy(
        "open_uri",
        arguments,
        user_request="Open the allowlisted URI.",
        lang="en",
        enforce_execution_permit=False,
        attempt_context=first_context,
    )
    second_context = _attempt_context(store, suffix="unknown_postcondition")
    second_context["request_id"] = "request_unknown_postcondition_replay"
    second_context["tool_call_id"] = "toolcall_unknown_postcondition_replay"
    second = agent_module._execute_tool_with_policy(
        "open_uri",
        arguments,
        user_request="Open the allowlisted URI.",
        lang="en",
        enforce_execution_permit=False,
        attempt_context=second_context,
    )

    assert get_tool_spec("open_uri").postcondition_required is False
    assert first.outcome is ExecutionOutcome.SUCCESS_UNVERIFIED
    assert store.get_attempt(first.tool_attempt_id)["status"] == (
        ToolAttemptStatus.SUCCEEDED.value
    )
    assert second.outcome is ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert second.dispatched is False
    assert calls == ["https://example.com"]


def test_reconciliation_suspension_07_missing_observer_remains_unresolved():
    prompt, strategy, _decision = _physical_open_unknown()

    probe = select_local_reconciliation_probe(
        strategy,
        {"take_screenshot", "open_application"},
        user_request=prompt,
    )

    assert probe is None
    assert strategy.frozen_reason == "owner_still_running"
    assert strategy.succeeded is False


def test_reconciliation_suspension_08_exhaustion_stalls_fail_closed(
    tmp_path, monkeypatch,
):
    from osia.tool_attempts import (
        ToolAttemptCoordinator,
        ToolAttemptStatus,
        ToolAttemptStore,
    )

    store = ToolAttemptStore(tmp_path / "exhaustion")
    pending, _context, calls = _physical_policy_record(
        store, monkeypatch, suffix="exhaustion",
    )
    durable = store.get_attempt(pending.tool_attempt_id)
    stalled_now = float(durable["inactivity_deadline_at"]) + 0.1
    assert stalled_now < float(durable["hard_deadline_at"])
    coordinator = ToolAttemptCoordinator(store)
    finalized = threading.Event()
    coordinator.register_suspended(
        pending.tool_attempt_id,
        resume=lambda *_args: "",
        finalize=lambda *_args: finalized.set() or True,
        verify=lambda _attempt_id: None,
    )
    coordinator._runtimes[pending.tool_attempt_id].activated = True

    with patch("osia.tool_attempts._now", return_value=stalled_now):
        coordinator._timer_fired(pending.tool_attempt_id)

    assert finalized.wait(2.0)
    terminal = store.get_attempt(pending.tool_attempt_id)
    assert calls == ["Calculator"]
    assert terminal["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert terminal["terminal_outcome"] == "outcome_unknown"
    assert terminal["reason_code"] == "reconciliation_stalled"


def test_reconciliation_suspension_09_mutation_cannot_replay_while_suspended(
    tmp_path, monkeypatch,
):
    from osia import agent as agent_module
    from osia.tool_attempts import ToolAttemptStore
    from osia.tool_contracts import ExecutionOutcome

    store = ToolAttemptStore(tmp_path / "suspended-replay")
    first, _context, calls = _physical_policy_record(
        store, monkeypatch, suffix="suspended_replay",
    )
    replay_context = _attempt_context(store, suffix="suspended_replay")
    replay_context["request_id"] = "request_suspended_replay_second"
    replay_context["tool_call_id"] = "toolcall_suspended_replay_second"
    replay = agent_module._execute_tool_with_policy(
        "open_application",
        {"name": "Calculator"},
        user_request=_physical_prompt(),
        lang="en",
        enforce_execution_permit=False,
        attempt_context=replay_context,
    )

    assert first.outcome is ExecutionOutcome.PENDING
    assert replay.outcome is ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert replay.dispatched is False
    assert replay.attempts == 0
    assert calls == ["Calculator"]


def test_reconciliation_suspension_10_owner_settle_restarts_bounded_inactivity(
    tmp_path, monkeypatch,
):
    from osia import agent as agent_module
    from osia import tool_attempts
    from osia.tool_attempts import ToolAttemptStore

    now = [100.0]
    monkeypatch.setattr(tool_attempts, "_now", lambda: now[0])
    store = ToolAttemptStore(tmp_path / "owner-settled-inactivity")
    created = store.create_attempt(
        conversation_id="autonomous.owner.settled",
        turn_id="0.1",
        request_id="request_owner_settled",
        tool_call_id="toolcall_owner_settled",
        tool_name="activate_desktop_surface",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments({"surface_ref": "s1"}),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        owner_scope="autonomous",
        autonomous_run_id="run_owner_settled",
        autonomous_task_id="0.1",
        autonomous_operation_id="operation_owner_settled",
        deadline_at=145.0,
        persistence_contract={
            "family": "generic",
            "release_after_seconds": 2.0,
            "inactivity_timeout_seconds": 32.0,
            "hard_timeout_seconds": 45.0,
            "check_delays_seconds": [1.0, 2.0, 5.0],
            "safe_alternatives": ["local_desktop_application_observation"],
            "restart_reconciliation": True,
        },
    )
    running = store.mark_running(
        store.mark_accepted(created["tool_attempt_id"])["tool_attempt_id"],
    )
    original_inactivity = running["inactivity_deadline_at"]

    now[0] = 130.0
    agent_module._record_owner_settled_progress(
        store, running["tool_attempt_id"],
    )
    settled = store.get_attempt(running["tool_attempt_id"])
    now[0] = 134.0
    agent_module._record_owner_settled_progress(
        store, running["tool_attempt_id"],
    )
    repeated = store.get_attempt(running["tool_attempt_id"])

    assert original_inactivity == 132.0
    assert settled["last_progress_at"] == 130.0
    assert settled["inactivity_deadline_at"] == 145.0
    assert settled["hard_deadline_at"] == 145.0
    assert repeated["inactivity_deadline_at"] == 145.0
    assert repeated["useful_progress_count"] == 1


def test_reconciliation_suspension_11_pending_owner_receipt_records_settlement(
    tmp_path, monkeypatch,
):
    from osia.tool_attempts import ToolAttemptStore

    store = ToolAttemptStore(tmp_path / "owner-settled-policy")
    pending, _context, calls = _physical_policy_record(
        store, monkeypatch, suffix="owner_settled_policy",
    )
    durable = store.get_attempt(pending.tool_attempt_id)

    assert calls == ["Calculator"]
    assert durable["dispatch_state"] == "owner_settled_unverified"
    assert durable["useful_progress_count"] == 1
    assert len(durable["useful_progress_fingerprints"]) == 1
    assert durable["last_progress_at"] >= durable["started_at"]
    assert durable["inactivity_deadline_at"] <= durable["hard_deadline_at"]
