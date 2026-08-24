"""WOR-262 stateless UIA Invoke observable-effect sentinels."""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from osia import desktop_scene
from osia.desktop_scene import DesktopScene, TargetSurfaceSelection
from osia.desktop_surface import (
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
    SurfaceResolution,
)
from osia.execution_strategy import (
    ExecutionStrategy,
    build_execution_strategy,
    execution_strategy_scope,
    export_execution_memory,
    record_tool_observation,
    record_tool_outcome,
    rebind_execution_memory,
    select_local_reconciliation_probe,
    tool_preflight,
)


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
TOOLS = {
    "activate_desktop_surface",
    "inspect_desktop_surfaces",
    "open_application",
}


def _surface(
    ref_id: str,
    label: str,
    *,
    role: str,
    generation: str = "generation-1",
    parent_ref: str | None = "window-1",
    patterns: tuple[str, ...] = (),
) -> DesktopSurfaceRef:
    kind = SurfaceKind.WINDOW if role == "WindowControl" else SurfaceKind.CONTROL
    return DesktopSurfaceRef(
        ref_id=ref_id,
        application_id="calculator",
        process_ref="proc-calculator",
        window_generation=generation,
        surface_kind=kind,
        role=role,
        accessible_label=label,
        enabled=True,
        parent_ref=None if kind is SurfaceKind.WINDOW else parent_ref,
        bounds=(
            SurfaceBounds(100, 100, 700, 800)
            if kind is SurfaceKind.WINDOW
            else SurfaceBounds(150, 200, 250, 260)
        ),
        runtime_signature="" if kind is SurfaceKind.WINDOW else "runtime-" + ref_id,
        patterns=patterns,
        structural_epoch=7,
        observed_at=time.time() - 1.0,
    )


def _scene(
    display: str,
    *,
    unrelated: str = "stable",
    generation: str = "generation-1",
    window_ref: str = "window-1",
    observed_at: float | None = None,
) -> DesktopScene:
    window = _surface(
        window_ref,
        "Calculator",
        role="WindowControl",
        generation=generation,
        parent_ref=None,
        patterns=("Window",),
    )
    button = _surface(
        "button-three",
        "Three",
        role="ButtonControl",
        generation=generation,
        parent_ref=window_ref,
        patterns=("Invoke",),
    )
    display_surface = _surface(
        "display",
        "Display is " + display,
        role="TextControl",
        generation=generation,
        parent_ref=window_ref,
    )
    unrelated_surface = _surface(
        "unrelated",
        unrelated,
        role="TextControl",
        generation=generation,
        parent_ref=window_ref,
    )
    return DesktopScene(
        application_id="calculator",
        window_ref=window,
        surfaces=(window, button, display_surface, unrelated_surface),
        structural_epoch=7,
        scene_signature=f"scene:{display}:{unrelated}",
        accessibility_complete=True,
        observed_at=time.time() if observed_at is None else observed_at,
        uia_tree_observed=True,
        foreground=True,
    )


def _strategy() -> ExecutionStrategy:
    strategy = build_execution_strategy(TASK, TOOLS)
    open_node = strategy.mission.action_nodes[0]
    assert strategy.verify_postcondition(
        "exact structured open evidence",
        postcondition=open_node.observable_success_postcondition,
    )
    return strategy


def _effect(expected_text: str = "3") -> dict:
    return {
        "kind": "exact_surface_text",
        "surface_ref": "display",
        "expected_text": expected_text,
    }


def _dispatch(
    strategy: ExecutionStrategy,
    *,
    before: str = "0",
    effect: dict | None = None,
    mechanism: str = "invoke",
):
    scene = _scene(before, observed_at=time.time() - 1.0)
    arguments = {
        "surface_ref": "button-three",
        "objective": "Three",
    }
    if effect is not None:
        arguments["observable_effect"] = effect
    decision = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)
    primitive = {
        "kind": "desktop_uia_primitive",
        "mechanism": mechanism,
        "observable_effect": effect or {},
        "dispatched_at": attempt.dispatched_at,
        "before": desktop_scene._primitive_state_projection(
            scene, scene.surfaces[1],
        ),
    }
    result = {
        "text": "UIA Invoke dispatched",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "dispatched",
            "outcome": "success_unverified",
            "reason_code": "native_accessibility_dispatched",
            "surface_ref": "button-three",
            "primitive": primitive,
        },
    }
    record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="success_unverified",
        is_error=False,
        reason_code="native_accessibility_dispatched",
        result=result,
        strategy=strategy,
    )
    return decision, attempt, arguments


def _selection(scene: DesktopScene) -> TargetSurfaceSelection:
    return TargetSurfaceSelection(
        requested_application="calculator",
        status="exact_target",
        foreground_scene=scene,
        target_scene=scene,
        identity_strength=400,
        identity_evidence="executable_application:calculator",
        candidate_count=1,
    )


def _observe(strategy: ExecutionStrategy, scene: DesktopScene):
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(scene),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("calculator")
    reconciled = record_tool_observation(
        "inspect_desktop_surfaces",
        result,
        is_error=False,
        strategy=strategy,
    )
    return result, reconciled


def _dispatch_through_real_uia_path(strategy: ExecutionStrategy):
    before = _scene("0", observed_at=time.time() - 1.0)
    effect = _effect()
    arguments = {
        "surface_ref": "button-three",
        "objective": "Three",
        "observable_effect": effect,
    }
    native = NativeSurfaceIdentity(
        pid=4242,
        process_started_at=100.0,
        native_window_id=9090,
        runtime_id=(42, 3),
        class_name="ApplicationFrameWindow",
    )
    invocations = []

    class InvokePattern:
        def Invoke(self):
            invocations.append("invoke")

    control = SimpleNamespace(GetInvokePattern=lambda: InvokePattern())
    auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _hwnd: object(),
    )

    def resolve(ref_id: str, **_kwargs):
        surface = next(
            (item for item in before.surfaces if item.ref_id == ref_id),
            None,
        )
        return SurfaceResolution(
            "resolved" if surface is not None else "unknown",
            surface,
            native if surface is not None else None,
        )

    with (
        patch.object(desktop_scene.sys, "platform", "win32"),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "resolve",
            side_effect=resolve,
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=native,
        ),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(before),
        ),
        patch("osia.desktop_scene._find_runtime_control", return_value=control),
        patch("osia.tools.desktop_control._load_uiautomation", return_value=auto),
    ):
        decision = tool_preflight(
            "activate_desktop_surface", arguments, strategy=strategy,
        )
        assert decision is not None and decision.allowed
        result = desktop_scene.activate_desktop_surface(**arguments)
        record_tool_outcome(
            "activate_desktop_surface",
            decision,
            outcome="success_unverified",
            is_error=False,
            reason_code=result["receipt"]["reason_code"],
            result=result,
            strategy=strategy,
        )
    return decision, strategy.attempt_by_id(decision.attempt_id), arguments, invocations


def test_uia_provider_stall_returns_typed_unknown_before_common_release():
    invocations = []

    class InvokePattern:
        def Invoke(self):
            invocations.append("invoke")
            time.sleep(0.4)

    control = SimpleNamespace(GetInvokePattern=lambda: InvokePattern())
    auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _hwnd: object(),
    )
    started = time.monotonic()
    with (
        patch.object(
            desktop_scene,
            "_UIA_ACTION_DISPATCH_TIMEOUT_SECONDS",
            0.02,
        ),
        patch("osia.desktop_scene._find_runtime_control", return_value=control),
    ):
        dispatch = desktop_scene._dispatch_fresh_uia_action_bounded(
            auto,
            native_window_id=9090,
            runtime_id=(42, 3),
            action_pattern="Invoke",
            observed_patterns=("Invoke",),
        )

    assert time.monotonic() - started < 0.3
    assert invocations == ["invoke"]
    assert dispatch["dispatch_started"] is True
    assert dispatch["completed"] is False
    assert dispatch["dispatched_at"] > 0.0
    assert dispatch["reason_code"] == "uia_action_dispatch_timed_out"
    assert dispatch["worker_may_be_active"] is True


def test_uia_pre_dispatch_stall_cannot_invoke_after_timeout():
    invocations = []

    class InvokePattern:
        def Invoke(self):
            invocations.append("invoke")

    control = SimpleNamespace(GetInvokePattern=lambda: InvokePattern())

    def delayed_root(_hwnd):
        time.sleep(0.08)
        return object()

    auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=delayed_root,
    )
    with (
        patch.object(
            desktop_scene,
            "_UIA_ACTION_DISPATCH_TIMEOUT_SECONDS",
            0.02,
        ),
        patch("osia.desktop_scene._find_runtime_control", return_value=control),
    ):
        dispatch = desktop_scene._dispatch_fresh_uia_action_bounded(
            auto,
            native_window_id=9090,
            runtime_id=(42, 3),
            action_pattern="Invoke",
            observed_patterns=("Invoke",),
        )
        time.sleep(0.1)

    assert dispatch["dispatch_started"] is False
    assert dispatch["completed"] is False
    assert dispatch["reason_code"] == (
        "uia_action_dispatch_timed_out_before_effect"
    )
    assert invocations == []


def test_timed_out_uia_dispatch_preserves_typed_primitive_receipt():
    strategy = _strategy()
    with patch(
        "osia.desktop_scene._dispatch_fresh_uia_action_bounded",
        return_value={
            "dispatch_started": True,
            "dispatched_at": time.time(),
            "completed": False,
            "reason_code": "uia_action_dispatch_timed_out",
            "worker_may_be_active": True,
            "selected_pattern": "Invoke",
            "selected_pattern_available": True,
            "fresh_supported_patterns": ["Invoke"],
        },
    ):
        _decision, attempt, _arguments, invocations = (
            _dispatch_through_real_uia_path(strategy)
        )

    assert invocations == []
    assert attempt.primitive_receipt["kind"] == "desktop_uia_primitive"
    assert attempt.primitive_receipt["mechanism"] == "invoke"
    assert attempt.primitive_receipt["observable_effect"] == _effect()
    assert attempt.primitive_receipt["worker_may_be_active"] is True
    assert attempt.actionability_evidence["dispatch_preflight_result"] == (
        "effect_unknown"
    )


def test_stateless_invoke_without_observable_postcondition_remains_unresolved():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy)

    assert attempt.primitive_postcondition == ""
    assert select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
    ) is None
    assert strategy._completed_substeps == []


def test_fresh_structured_invoke_without_observable_postcondition_is_rejected():
    strategy = _strategy()
    _result, reconciled = _observe(
        strategy,
        _scene("0", observed_at=time.time()),
    )
    decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "button-three",
            "objective": "Three",
            "action_pattern": "Invoke",
        },
        strategy=strategy,
    )

    assert reconciled is False
    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "uia_invoke_observable_effect_required"


def test_exact_observable_surface_postcondition_reconciles_stateless_invoke():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    result, reconciled = _observe(
        strategy,
        _scene("3", observed_at=attempt.dispatched_at + 1.0),
    )

    assert reconciled is True
    assert result["receipt"]["reason_code"] == (
        "exact_desktop_observable_effect_change"
    )
    assert result["receipt"]["observable_effect"] == _effect()


def test_settled_exact_unchanged_state_reconciles_primitive_not_applied():
    from osia.turn_terminality import TurnTerminalityController

    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(
        strategy,
        effect=_effect(),
    )
    attempt_id = attempt.attempt_id
    strategy = ExecutionStrategy.from_snapshot(json.loads(json.dumps(
        strategy.snapshot()
    )))
    attempt = strategy.attempt_by_id(attempt_id)
    assert attempt is not None
    result, reconciled = _observe(
        strategy,
        _scene("0", observed_at=attempt.dispatched_at + 6.0),
    )
    event = strategy.snapshot()["reconciliation_events"][-1]

    assert reconciled is True
    assert result["receipt"]["outcome"] == "confirmed_absence"
    assert result["receipt"]["reason_code"] == (
        "exact_desktop_primitive_absent_no_partial_effect"
    )
    assert result["receipt"]["no_partial_effect"] is True
    assert event["resolution"] == "primitive_not_applied"
    assert TurnTerminalityController._desktop_resolution_kind(event) == (
        "primitive_not_applied"
    )
    assert strategy.attempt_by_id(attempt.attempt_id).status == "blocked"
    assert strategy.frozen_reason == ""


def test_active_uia_worker_cannot_be_reconciled_as_not_applied():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(
        strategy,
        effect=_effect(),
    )
    attempt.primitive_receipt["worker_may_be_active"] = True
    result, reconciled = _observe(
        strategy,
        _scene("0", observed_at=attempt.dispatched_at + 6.0),
    )

    assert reconciled is False
    assert result["receipt"].get("reason_code") != (
        "exact_desktop_primitive_absent_no_partial_effect"
    )
    assert strategy._frozen_attempt_id == attempt.attempt_id


@pytest.mark.parametrize("case", ("settling", "structural_epoch"))
def test_unsettled_or_structurally_changed_state_cannot_prove_not_applied(
    case: str,
):
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(
        strategy,
        effect=_effect(),
    )
    scene = _scene(
        "0",
        observed_at=(
            attempt.dispatched_at + 1.0
            if case == "settling" else
            attempt.dispatched_at + 6.0
        ),
    )
    if case == "structural_epoch":
        scene = replace(scene, structural_epoch=scene.structural_epoch + 1)
    result, reconciled = _observe(strategy, scene)

    assert reconciled is False
    assert result["receipt"].get("reason_code") != (
        "exact_desktop_primitive_absent_no_partial_effect"
    )
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_durable_probe_preserves_primitive_completion_for_projection():
    from osia.agent import OSIAAgent
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    strategy = _strategy()
    _decision, attempt, arguments = _dispatch(
        strategy, effect=_effect(),
    )
    after = _scene("3", observed_at=attempt.dispatched_at + 1.0)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(after),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("calculator")
    probe_contract = effective_contract(
        get_tool_spec("inspect_desktop_surfaces"),
        {"target": "calculator"},
        TASK,
    )
    probe_record = ToolExecutionRecord(
        result,
        False,
        ExecutionOutcome.SUCCESS_VERIFIED,
        1,
        True,
        False,
        probe_contract,
        reason_code="success_verified",
    )
    original_contract = effective_contract(
        get_tool_spec("activate_desktop_surface"), arguments, TASK,
    )
    original_record = ToolExecutionRecord(
        {},
        False,
        ExecutionOutcome.PENDING,
        1,
        True,
        False,
        original_contract,
        "attempt-durable-primitive",
    )
    runtime = {
        "attempt_id": "attempt-durable-primitive",
        "local_reconciliation_probe_attempted": False,
        "reconciliation_strategy": strategy,
        "record": original_record,
    }
    durable = {
        "hard_deadline_at": time.time() + 30.0,
        "inactivity_deadline_at": time.time() + 30.0,
    }
    store = Mock()
    agent = object.__new__(OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-terra"
    agent.lang = "en"
    agent._terminality_controller = None

    with (
        patch(
            "osia.agent._execute_tool_with_policy",
            return_value=probe_record,
        ),
        patch(
            "osia.agent._persist_tool_attempt_terminal",
        ),
        patch("osia.action_policy.guard_tool_call", return_value=None),
    ):
        reconciled = OSIAAgent._verify_local_reconciliation_probe(
            agent,
            "attempt-durable-primitive",
            runtime,
            durable,
            store,
        )

    assert reconciled is not None
    assert reconciled.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    assert runtime["reconciliation_resolution"]["resolution"] == (
        "primitive_completed"
    )


def test_exact_pre_post_semantic_delta_verifies_only_declared_surface():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    result, reconciled = _observe(
        strategy,
        _scene(
            "3",
            unrelated="also changed",
            observed_at=attempt.dispatched_at + 1.0,
        ),
    )

    assert reconciled is True
    assert result["receipt"]["postcondition"] == attempt.primitive_postcondition
    assert result["receipt"]["substep_id"] == attempt.substep_id


def test_unrelated_ui_delta_does_not_verify_declared_effect():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    result, reconciled = _observe(
        strategy,
        _scene(
            "0",
            unrelated="changed elsewhere",
            observed_at=attempt.dispatched_at + 1.0,
        ),
    )

    assert reconciled is False
    assert result["receipt"]["outcome"] == "observed"
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_stateful_control_delta_cannot_bypass_declared_surface_effect():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(
        strategy,
        effect=_effect(),
        mechanism="toggle",
    )
    after = _scene("0", observed_at=attempt.dispatched_at + 1.0)
    after = replace(
        after,
        surfaces=(
            after.window_ref,
            replace(after.surfaces[1], selected=True),
            *after.surfaces[2:],
        ),
    )

    _result, reconciled = _observe(strategy, after)

    assert reconciled is False
    assert strategy._completed_substeps == []


@pytest.mark.parametrize("mismatch", ("stale", "wrong_generation"))
def test_stale_or_wrong_surface_evidence_does_not_verify(mismatch: str):
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    scene = _scene("3", observed_at=attempt.dispatched_at + 1.0)
    if mismatch == "stale":
        scene = replace(scene, observed_at=attempt.dispatched_at - 1.0)
    else:
        scene = _scene(
            "3",
            generation="generation-2",
            window_ref="window-2",
            observed_at=attempt.dispatched_at + 1.0,
        )
        scene = replace(
            scene,
            surfaces=(
                scene.window_ref,
                replace(
                    scene.surfaces[1],
                    runtime_signature="runtime-wrong-button",
                ),
                *scene.surfaces[2:],
            ),
        )

    _result, reconciled = _observe(strategy, scene)

    assert reconciled is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_evidence_for_another_substep_does_not_verify():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(
                _scene("3", observed_at=attempt.dispatched_at + 1.0),
            ),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("calculator")
    result["receipt"]["substep_id"] = "substep-other"

    assert record_tool_observation(
        "inspect_desktop_surfaces",
        result,
        is_error=False,
        strategy=strategy,
    ) is False
    assert strategy._completed_substeps == []


def test_verified_primitive_advances_exactly_one_substep_not_composite():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    _result, reconciled = _observe(
        strategy,
        _scene("3", observed_at=attempt.dispatched_at + 1.0),
    )
    next_decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "button-four",
            "objective": "Four",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "display",
                "expected_text": "34",
            },
        },
        strategy=strategy,
    )

    assert reconciled is True
    assert [item["substep_index"] for item in strategy._completed_substeps] == [1]
    assert next_decision is not None and next_decision.allowed
    assert strategy.attempt_by_id(next_decision.attempt_id).substep_index == 2
    assert strategy.succeeded is False


def test_unresolved_primitive_cannot_replay():
    strategy = _strategy()
    _decision, attempt, arguments = _dispatch(strategy, effect=_effect())
    replay = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )

    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"
    assert sum(item.attempt_id == attempt.attempt_id for item in strategy.attempts) == 1


def test_intermediate_effect_never_proves_exact_final_result():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    _result, reconciled = _observe(
        strategy,
        _scene("3", observed_at=attempt.dispatched_at + 1.0),
    )
    calculation = strategy.goal.observable_success_postcondition

    assert reconciled is True
    assert calculation not in strategy.proven_postconditions
    assert strategy.succeeded is False


def test_observable_effect_and_target_identity_survive_checkpoint_rebind():
    strategy = _strategy()
    decision, attempt, _arguments = _dispatch(strategy, effect=_effect())
    snapshot = json.loads(json.dumps(strategy.snapshot()))
    restored = ExecutionStrategy.from_snapshot(snapshot)
    memory = json.loads(json.dumps(export_execution_memory(strategy)))
    rebound = build_execution_strategy(TASK, TOOLS)
    rebind_execution_memory(rebound, [memory], now=time.time())

    restored_attempt = restored.attempt_by_id(decision.attempt_id)
    rebound_attempt = rebound.attempt_by_id(decision.attempt_id)
    assert restored_attempt.observable_effect == _effect()
    assert rebound_attempt.observable_effect == _effect()
    assert rebound_attempt.target_surface_identity == attempt.target_surface_identity
    assert rebound_attempt.primitive_receipt["before"]["state_rows"] == (
        attempt.primitive_receipt["before"]["state_rows"]
    )


def test_real_path_dispatches_once_observes_effect_and_exposes_next_primitive():
    strategy = _strategy()
    _decision, attempt, _arguments, invocations = _dispatch_through_real_uia_path(
        strategy,
    )
    _result, reconciled = _observe(
        strategy,
        _scene("3", observed_at=attempt.dispatched_at + 1.0),
    )
    next_decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "button-four",
            "objective": "Four",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "display",
                "expected_text": "34",
            },
        },
        strategy=strategy,
    )

    assert invocations == ["invoke"]
    assert reconciled is True
    assert next_decision is not None and next_decision.allowed
    assert strategy.attempt_by_id(next_decision.attempt_id).substep_index == 2


def test_real_path_without_declared_delta_remains_unresolved_without_replay():
    strategy = _strategy()
    _decision, attempt, arguments, invocations = _dispatch_through_real_uia_path(
        strategy,
    )
    _result, reconciled = _observe(
        strategy,
        _scene("0", observed_at=attempt.dispatched_at + 1.0),
    )
    replay = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )

    assert invocations == ["invoke"]
    assert reconciled is False
    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"


def _dense_witness_scene(
    expression: str,
    *,
    display: str = "347",
    status: str = "Ready",
    mode_selected: bool = False,
    changed_decoy: bool = False,
    observed_at: float | None = None,
) -> DesktopScene:
    """Reproduce the physical Calculator shape with a late expression node."""

    generation = "dense-generation"
    window_ref = "dense-window"
    window = _surface(
        window_ref,
        "Calculator",
        role="WindowControl",
        generation=generation,
        parent_ref=None,
        patterns=("Window",),
    )
    multiply = _surface(
        "multiply-button",
        "Multiply by",
        role="ButtonControl",
        generation=generation,
        parent_ref=window_ref,
        patterns=("Invoke",),
    )
    mode = replace(
        _surface(
            "mode-option",
            "Scientific mode",
            role="ListItemControl",
            generation=generation,
            parent_ref=window_ref,
            patterns=("SelectionItem",),
        ),
        selected=mode_selected,
    )
    next_button = _surface(
        "next-button",
        "Two",
        role="ButtonControl",
        generation=generation,
        parent_ref=window_ref,
        patterns=("Invoke",),
    )
    display_surface = _surface(
        "dense-display",
        "Display is " + display,
        role="TextControl",
        generation=generation,
        parent_ref=window_ref,
    )
    expression_surface = _surface(
        "zz-expression-witness",
        expression,
        role="TextControl",
        generation=generation,
        parent_ref=window_ref,
    )
    status_surface = _surface(
        "yy-status-witness",
        status,
        role="TextControl",
        generation=generation,
        parent_ref=window_ref,
    )
    decoys = tuple(
        _surface(
            f"a-decoy-{index:02d}",
            (
                "changed elsewhere"
                if changed_decoy and index == 0 else
                f"stable row {index:02d}"
            ),
            role="TextControl",
            generation=generation,
            parent_ref=window_ref,
        )
        for index in range(40)
    )
    return DesktopScene(
        application_id="calculator",
        window_ref=window,
        surfaces=(
            window,
            multiply,
            mode,
            next_button,
            display_surface,
            expression_surface,
            status_surface,
            *decoys,
        ),
        structural_epoch=7,
        scene_signature=f"dense:{expression}:{display}:{status}:{mode_selected}",
        accessibility_complete=True,
        observed_at=(time.time() if observed_at is None else observed_at),
        uia_tree_observed=True,
        foreground=True,
    )


def _surface_by_ref(scene: DesktopScene, surface_ref: str) -> DesktopSurfaceRef:
    return next(item for item in scene.surfaces if item.ref_id == surface_ref)


def _expression_effect(expected_text: str = "Expression is 347 ×") -> dict:
    return {
        "kind": "exact_surface_text",
        "surface_ref": "zz-expression-witness",
        "expected_text": expected_text,
    }


def _dispatch_scene_primitive(
    strategy: ExecutionStrategy,
    scene: DesktopScene,
    *,
    surface_ref: str,
    objective: str,
    effect: dict | None = None,
    mechanism: str = "invoke",
):
    arguments = {"surface_ref": surface_ref, "objective": objective}
    if effect is not None:
        arguments["observable_effect"] = effect
    decision = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)
    primitive = {
        "kind": "desktop_uia_primitive",
        "mechanism": mechanism,
        "observable_effect": effect or {},
        "dispatched_at": attempt.dispatched_at,
        "before": desktop_scene._primitive_state_projection(
            scene,
            _surface_by_ref(scene, surface_ref),
            witness_surface_refs=(
                str((effect or {}).get("surface_ref") or ""),
            ),
        ),
    }
    result = {
        "text": "UIA primitive dispatched",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "dispatched",
            "outcome": "success_unverified",
            "reason_code": "native_accessibility_dispatched",
            "surface_ref": surface_ref,
            "primitive": primitive,
        },
    }
    record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="success_unverified",
        is_error=False,
        reason_code="native_accessibility_dispatched",
        result=result,
        strategy=strategy,
    )
    return decision, attempt, arguments


def _complete_dense_display_step(
    strategy: ExecutionStrategy,
    *,
    before: str,
    after: str,
    surface_ref: str,
    objective: str,
):
    effect = {
        "kind": "exact_surface_text",
        "surface_ref": "dense-display",
        "expected_text": after,
    }
    _decision, attempt, _arguments = _dispatch_scene_primitive(
        strategy,
        _dense_witness_scene(
            "Expression is stable",
            display=before,
            observed_at=time.time() - 1.0,
        ),
        surface_ref=surface_ref,
        objective=objective,
        effect=effect,
    )
    _result, reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is stable",
            display=after,
            observed_at=attempt.dispatched_at + 1.0,
        ),
    )
    assert reconciled is True


def _dispatch_dense_real_path(strategy: ExecutionStrategy):
    before = _dense_witness_scene(
        "Expression is 347",
        observed_at=time.time() - 1.0,
    )
    effect = _expression_effect()
    arguments = {
        "surface_ref": "multiply-button",
        "objective": "Multiply by",
        "observable_effect": effect,
    }
    native = NativeSurfaceIdentity(
        pid=4347,
        process_started_at=100.0,
        native_window_id=9029,
        runtime_id=(34, 7, 29),
        class_name="ApplicationFrameWindow",
    )
    invocations = []

    class InvokePattern:
        def Invoke(self):
            invocations.append("invoke")

    control = SimpleNamespace(GetInvokePattern=lambda: InvokePattern())
    auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _hwnd: object(),
    )

    def resolve(ref_id: str, **_kwargs):
        surface = next(
            (item for item in before.surfaces if item.ref_id == ref_id),
            None,
        )
        return SurfaceResolution(
            "resolved" if surface is not None else "unknown",
            surface,
            native if surface is not None else None,
        )

    with (
        patch.object(desktop_scene.sys, "platform", "win32"),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "resolve",
            side_effect=resolve,
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=native,
        ),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(before),
        ),
        patch("osia.desktop_scene._find_runtime_control", return_value=control),
        patch("osia.tools.desktop_control._load_uiautomation", return_value=auto),
    ):
        decision = tool_preflight(
            "activate_desktop_surface", arguments, strategy=strategy,
        )
        assert decision is not None and decision.allowed
        result = desktop_scene.activate_desktop_surface(**arguments)
        record_tool_outcome(
            "activate_desktop_surface",
            decision,
            outcome="success_unverified",
            is_error=False,
            reason_code=result["receipt"]["reason_code"],
            result=result,
            strategy=strategy,
        )
    return (
        decision,
        strategy.attempt_by_id(decision.attempt_id),
        arguments,
        invocations,
        result,
    )


def test_dense_physical_shape_keeps_declared_expression_witness_bounded():
    scene = _dense_witness_scene("Expression is 347")
    target = _surface_by_ref(scene, "multiply-button")
    unprioritized = desktop_scene._primitive_state_projection(scene, target)
    protected = desktop_scene._primitive_state_projection(
        scene,
        target,
        witness_surface_refs=("zz-expression-witness",),
    )

    assert len(unprioritized["state_rows"]) == 32
    assert not any(
        row["surface_ref"] == "zz-expression-witness"
        for row in unprioritized["state_rows"]
    )
    assert len(protected["state_rows"]) == 32
    assert any(
        row["surface_ref"] == "zz-expression-witness"
        and row["label"] == "Expression is 347"
        for row in protected["state_rows"]
    )


def test_dense_real_path_expression_witness_advances_one_substep_once():
    strategy = _strategy()
    _decision, attempt, _arguments, invocations, result = (
        _dispatch_dense_real_path(strategy)
    )
    observed, reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347 ×",
            observed_at=attempt.dispatched_at + 1.0,
        ),
    )
    next_decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "next-button",
            "objective": "Two",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "yy-status-witness",
                "expected_text": "Operand accepted",
            },
        },
        strategy=strategy,
    )

    before_rows = result["receipt"]["primitive"]["before"]["state_rows"]
    assert invocations == ["invoke"]
    assert any(
        row["surface_ref"] == "zz-expression-witness"
        for row in before_rows
    )
    assert reconciled is True
    assert observed["receipt"]["reason_code"] == (
        "exact_desktop_observable_effect_change"
    )
    assert [item["substep_index"] for item in strategy._completed_substeps] == [1]
    assert strategy.frozen_reason == ""
    assert next_decision is not None and next_decision.allowed
    assert strategy.attempt_by_id(next_decision.attempt_id).substep_index == 2
    assert strategy.succeeded is False


def test_completed_exact_witness_state_cannot_regress_within_composite():
    strategy = _strategy()
    _complete_dense_display_step(
        strategy,
        before="3",
        after="0",
        surface_ref="multiply-button",
        objective="Clear",
    )
    _complete_dense_display_step(
        strategy,
        before="0",
        after="3",
        surface_ref="next-button",
        objective="Three",
    )

    blocked = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "mode-option",
            "objective": "Backspace",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "dense-display",
                "expected_text": "0",
            },
        },
        strategy=strategy,
    )

    assert blocked is not None and blocked.allowed is False
    assert blocked.reason_code == "primitive_observable_witness_regression"
    assert len(strategy.attempts) == 2


def test_new_exact_witness_state_remains_eligible_after_progress():
    strategy = _strategy()
    _complete_dense_display_step(
        strategy,
        before="3",
        after="0",
        surface_ref="multiply-button",
        objective="Clear",
    )
    _complete_dense_display_step(
        strategy,
        before="0",
        after="3",
        surface_ref="next-button",
        objective="Three",
    )

    decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "mode-option",
            "objective": "Four",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "dense-display",
                "expected_text": "34",
            },
        },
        strategy=strategy,
    )

    assert decision is not None and decision.allowed is True
    assert strategy.attempt_by_id(decision.attempt_id).substep_index == 3


def test_same_text_on_different_witness_surface_remains_eligible():
    strategy = _strategy()
    _complete_dense_display_step(
        strategy,
        before="3",
        after="0",
        surface_ref="multiply-button",
        objective="Clear",
    )
    _complete_dense_display_step(
        strategy,
        before="0",
        after="3",
        surface_ref="next-button",
        objective="Three",
    )

    decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "mode-option",
            "objective": "Update status",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "yy-status-witness",
                "expected_text": "0",
            },
        },
        strategy=strategy,
    )

    assert decision is not None and decision.allowed is True


def test_stateful_target_transition_remains_eligible_without_text_witness():
    strategy = _strategy()
    _complete_dense_display_step(
        strategy,
        before="3",
        after="0",
        surface_ref="multiply-button",
        objective="Clear",
    )
    _complete_dense_display_step(
        strategy,
        before="0",
        after="3",
        surface_ref="next-button",
        objective="Three",
    )

    decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "mode-option",
            "objective": "Select scientific mode",
        },
        strategy=strategy,
    )

    assert decision is not None and decision.allowed is True
    assert strategy.attempt_by_id(decision.attempt_id).observable_effect == {}


def test_witness_regression_guard_survives_checkpoint_restore():
    strategy = _strategy()
    _complete_dense_display_step(
        strategy,
        before="3",
        after="0",
        surface_ref="multiply-button",
        objective="Clear",
    )
    _complete_dense_display_step(
        strategy,
        before="0",
        after="3",
        surface_ref="next-button",
        objective="Three",
    )
    restored = ExecutionStrategy.from_snapshot(
        json.loads(json.dumps(strategy.snapshot())),
    )

    blocked = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": "mode-option",
            "objective": "Backspace",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "dense-display",
                "expected_text": "0",
            },
        },
        strategy=restored,
    )

    assert blocked is not None and blocked.allowed is False
    assert blocked.reason_code == "primitive_observable_witness_regression"


def test_dense_unrelated_ui_delta_does_not_prove_expression_witness():
    strategy = _strategy()
    before = _dense_witness_scene(
        "Expression is 347", observed_at=time.time() - 1.0,
    )
    _decision, attempt, _arguments = _dispatch_scene_primitive(
        strategy,
        before,
        surface_ref="multiply-button",
        objective="Multiply by",
        effect=_expression_effect(),
    )
    result, reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347",
            status="changed elsewhere",
            changed_decoy=True,
            observed_at=attempt.dispatched_at + 1.0,
        ),
    )

    assert reconciled is False
    assert result["receipt"]["outcome"] == "observed"
    assert strategy._completed_substeps == []


def test_same_text_on_wrong_surface_does_not_prove_declared_witness():
    strategy = _strategy()
    before = _dense_witness_scene(
        "Expression is 347", observed_at=time.time() - 1.0,
    )
    _decision, attempt, _arguments = _dispatch_scene_primitive(
        strategy,
        before,
        surface_ref="multiply-button",
        objective="Multiply by",
        effect=_expression_effect(),
    )
    _result, reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347",
            status="Expression is 347 ×",
            observed_at=attempt.dispatched_at + 1.0,
        ),
    )

    assert reconciled is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_stale_expression_evidence_does_not_prove_substep():
    strategy = _strategy()
    before = _dense_witness_scene(
        "Expression is 347", observed_at=time.time() - 1.0,
    )
    _decision, attempt, _arguments = _dispatch_scene_primitive(
        strategy,
        before,
        surface_ref="multiply-button",
        objective="Multiply by",
        effect=_expression_effect(),
    )
    _result, reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347 ×",
            observed_at=attempt.dispatched_at - 1.0,
        ),
    )

    assert reconciled is False
    assert strategy._completed_substeps == []


@pytest.mark.parametrize("mismatch", ("substep", "target"))
def test_wrong_substep_or_target_cannot_claim_exact_expression_evidence(
    mismatch: str,
):
    strategy = _strategy()
    before = _dense_witness_scene(
        "Expression is 347", observed_at=time.time() - 1.0,
    )
    _decision, attempt, _arguments = _dispatch_scene_primitive(
        strategy,
        before,
        surface_ref="multiply-button",
        objective="Multiply by",
        effect=_expression_effect(),
    )
    after = _dense_witness_scene(
        "Expression is 347 ×",
        observed_at=attempt.dispatched_at + 1.0,
    )
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(after),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("calculator")
    if mismatch == "substep":
        result["receipt"]["substep_id"] = "substep-other"
    else:
        result["scene"]["application"] = "notepad"

    assert record_tool_observation(
        "inspect_desktop_surfaces",
        result,
        is_error=False,
        strategy=strategy,
    ) is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_composite_progresses_through_expression_selection_and_status_witnesses():
    strategy = _strategy()

    first_before = _dense_witness_scene(
        "Expression is 347", observed_at=time.time() - 1.0,
    )
    _first, first_attempt, _first_args = _dispatch_scene_primitive(
        strategy,
        first_before,
        surface_ref="multiply-button",
        objective="Multiply by",
        effect=_expression_effect(),
    )
    _first_result, first_reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347 ×",
            observed_at=first_attempt.dispatched_at + 1.0,
        ),
    )

    second_before = _dense_witness_scene(
        "Expression is 347 ×",
        observed_at=time.time() - 1.0,
    )
    _second, second_attempt, _second_args = _dispatch_scene_primitive(
        strategy,
        second_before,
        surface_ref="mode-option",
        objective="Scientific mode",
        mechanism="selectionitem",
    )
    second_result, second_reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347 ×",
            mode_selected=True,
            observed_at=second_attempt.dispatched_at + 1.0,
        ),
    )

    third_before = _dense_witness_scene(
        "Expression is 347 ×",
        mode_selected=True,
        observed_at=time.time() - 1.0,
    )
    status_effect = {
        "kind": "exact_surface_text",
        "surface_ref": "yy-status-witness",
        "expected_text": "Operand accepted",
    }
    _third, third_attempt, _third_args = _dispatch_scene_primitive(
        strategy,
        third_before,
        surface_ref="next-button",
        objective="Two",
        effect=status_effect,
    )
    _third_result, third_reconciled = _observe(
        strategy,
        _dense_witness_scene(
            "Expression is 347 ×",
            status="Operand accepted",
            mode_selected=True,
            observed_at=third_attempt.dispatched_at + 1.0,
        ),
    )

    assert first_reconciled is True
    assert second_reconciled is True
    assert third_reconciled is True
    assert second_result["receipt"]["reason_code"] == (
        "exact_desktop_primitive_state_change"
    )
    assert [item["substep_index"] for item in strategy._completed_substeps] == [
        1, 2, 3,
    ]
    assert all(
        strategy.attempt_by_id(item["attempt_id"]).status == "progressed"
        for item in strategy._completed_substeps
    )
    assert strategy.frozen_reason == ""
    assert strategy.succeeded is False


def test_unresolved_expression_cannot_replay():
    strategy = _strategy()
    before = _dense_witness_scene(
        "Expression is 347", observed_at=time.time() - 1.0,
    )
    _observe(strategy, before)
    _decision, attempt, arguments = _dispatch_scene_primitive(
        strategy,
        before,
        surface_ref="multiply-button",
        objective="Multiply by",
        effect=_expression_effect(),
    )
    replay = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )

    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"
    dispatched_primitives = [
        item for item in strategy.attempts if item.primitive_receipt
    ]
    assert [item.attempt_id for item in dispatched_primitives] == [
        attempt.attempt_id,
    ]
    assert strategy._frozen_attempt_id == attempt.attempt_id
    assert strategy._completed_substeps == []


def test_expression_witness_checkpoint_preserves_exact_pending_identity():
    strategy = _strategy()
    _decision, attempt, _arguments, _invocations, result = (
        _dispatch_dense_real_path(strategy)
    )
    memory = json.loads(json.dumps(export_execution_memory(strategy)))
    rebound = build_execution_strategy(TASK, TOOLS)
    rebind_execution_memory(rebound, [memory], now=time.time())
    restored = rebound.attempt_by_id(attempt.attempt_id)
    probe = select_local_reconciliation_probe(
        rebound, {"inspect_desktop_surfaces"}, user_request=TASK,
    )

    assert restored is not None
    assert restored.composite_id == attempt.composite_id
    assert restored.substep_id == attempt.substep_id
    assert restored.observable_effect == _expression_effect()
    assert restored.primitive_receipt["kind"] == "desktop_uia_primitive"
    assert restored.primitive_receipt["mechanism"] == "invoke"
    assert restored.primitive_receipt["observable_effect"] == (
        result["receipt"]["primitive"]["observable_effect"]
    )
    assert restored.primitive_receipt["before"]["state_rows"] == (
        result["receipt"]["primitive"]["before"]["state_rows"]
    )
    assert restored.primitive_receipt["before"]["target"]["surface_ref"] == (
        "multiply-button"
    )
    assert any(
        row["surface_ref"] == "zz-expression-witness"
        for row in restored.primitive_receipt["before"]["state_rows"]
    )
    assert probe is not None
    assert probe["strategy_attempt_id"] == attempt.attempt_id
    assert probe["substep_id"] == attempt.substep_id
