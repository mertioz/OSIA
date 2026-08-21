"""Focused WOR-262 prerequisite-effect and background UIA regressions."""

from __future__ import annotations

import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from osia import execution_strategy as es
from osia import desktop_scene
from osia.autonomous.orchestrator import Orchestrator
from osia.desktop_scene import DesktopScene, _surface_actuation_contract
from osia.desktop_surface import (
    DesktopSurfaceRef,
    DesktopSurfaceRegistry,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
)
from osia.tool_contracts import (
    effective_static_tool_specs,
    effective_tool_surface_scope,
)


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
OPEN_CRITERION = "The Calculator application window is visible."
CALCULATION = "Enter and evaluate 347 × 29."
CALC_CRITERION = "Calculator displays the result 10063."
TOOLS = {
    "activate_desktop_surface",
    "desktop_click",
    "desktop_press",
    "desktop_scroll",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
    "open_uri",
    "query_document",
    "request_visual_observation",
    "run_command",
    "take_screenshot",
}


def _observation(*, foreground: bool = False, observed_at: float | None = None):
    return {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": "surface-calculator-window",
        "window_generation": "calculator-generation-1",
        "accessibility_quality": "rich_target_resolved",
        "target_resolution": "exact_target",
        "target_foreground": foreground,
        "target_minimized": False,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "calculator" if foreground else "chatgpt",
        "observed_at": time.time() if observed_at is None else observed_at,
    }


def _open_strategy():
    return es.build_execution_strategy(
        "Open Calculator.",
        TOOLS,
        postcondition=OPEN_CRITERION,
        semantic_context=TASK,
    )


def _completed_open_memory(*, foreground: bool = False):
    strategy = _open_strategy()
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
    observed_at = time.time() + 1.0
    assert strategy.reconcile(
        succeeded=True,
        evidence="exact Calculator application observation",
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
    strategy._latest_desktop_observation = _observation(
        foreground=foreground,
        observed_at=observed_at,
    )
    return es.export_execution_memory(strategy)


def _calculation_strategy(*, memory=None, foreground: bool = False):
    strategy = es.build_execution_strategy(
        CALCULATION,
        TOOLS,
        postcondition=CALC_CRITERION,
        semantic_context=TASK,
    )
    if memory is not None:
        es.rebind_execution_memory(
            strategy,
            [memory],
            now=float(
                (memory.get("desktop_observation") or {}).get("observed_at")
                or time.time()
            ),
        )
    elif foreground:
        strategy._latest_desktop_observation = _observation(foreground=True)
    return strategy


def _provider_frontier(strategy):
    with es.execution_strategy_scope(
        strategy.mission.objective,
        strategy=strategy,
    ), effective_tool_surface_scope("autonomous"):
        return {spec.name for spec in effective_static_tool_specs()}


def _surface(
    *,
    kind: SurfaceKind = SurfaceKind.CONTROL,
    patterns=("Invoke",),
    enabled: bool = True,
):
    return DesktopSurfaceRef(
        ref_id="surface-control",
        application_id="generic-app",
        process_ref="process-ref",
        window_generation="window-generation",
        surface_kind=kind,
        role="ButtonControl" if kind is SurfaceKind.CONTROL else "WindowControl",
        accessible_label="Exact control",
        enabled=enabled,
        runtime_signature="runtime-signature",
        patterns=tuple(patterns),
    )


def test_01_pending_open_prerequisite_keeps_open_effect_eligible():
    strategy = _calculation_strategy()
    assert es.tool_applicability(
        strategy, "open_application", {"name": "Calculator"},
    ) == (True, "")


def test_02_exact_open_reconciliation_persists_completed_effect():
    memory = _completed_open_memory()
    assert memory["effects"][0]["completed"] is True
    assert memory["completed_effects"][0]["attempted_effect"] == "application_open"

    reconciled = _open_strategy()
    es.rebind_execution_memory(reconciled, [memory])
    stale = _open_strategy()
    task = {}
    agent = SimpleNamespace(_pending_tool_runtime={
        "attempt_id": "attempt-open",
        "reconciliation_strategy": reconciled,
    })
    orchestrator = Orchestrator(agent)
    orchestrator._active_task = task
    orchestrator._active_execution_strategy = stale
    selected = orchestrator._strategy_reconciled_for_attempt(
        "attempt-open", task,
    )
    orchestrator._persist_execution_memory(task, selected)
    assert task["execution_memory"]["completed_effects"][0][
        "attempted_effect"
    ] == "application_open"


def test_03_completed_open_is_absent_from_calculation_frontier():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    assert es.tool_applicability(
        strategy, "open_application", {"name": "Calculator"},
    ) == (False, "prerequisite_effect_already_completed")
    frontier = _provider_frontier(strategy)
    assert frontier == {
        "activate_desktop_surface",
        "inspect_desktop_surfaces",
    }
    assert {
        "open_application", "query_document", "run_command",
        "take_screenshot", "request_visual_observation", "desktop_click",
    }.isdisjoint(frontier)


def test_04_same_strategy_duplicate_remains_retired_by_anti_replay():
    strategy = _open_strategy()
    decision = es.tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    es.record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="owner_still_running", result={}, strategy=strategy,
    )
    assert strategy.reconcile(
        succeeded=True,
        evidence="exact observation",
        evidence_details={
            "postcondition": "application_open",
            "observed_at": time.time() + 1.0,
        },
    )
    replay = es.tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "mechanism_retired_after_outcome_unknown"


def test_05_fresh_exact_absence_reenables_open_prerequisite():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": {
                "application": "",
                "requested_application": "calculator",
                "window_surface_ref": "",
                "target_resolution": "target_missing",
                "target_candidate_count": 0,
                "target_foreground": False,
                "foreground_application": "chatgpt",
                "observed_at": time.time() + 2.0,
            },
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_target_surface_not_found",
            },
        },
        is_error=False,
        strategy=strategy,
    )
    assert es.tool_applicability(
        strategy, "open_application", {"name": "Calculator"},
    ) == (True, "")


def test_06_completed_open_survives_execution_memory_reconstruction():
    first = _calculation_strategy(memory=_completed_open_memory())
    second = _calculation_strategy(memory=es.export_execution_memory(first))
    assert second.effect_is_completed(
        "application_open", target_application="calculator",
    )


def test_07_reconstruction_retains_exact_target_application():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    assert strategy.goal.required_application == "calculator"
    assert strategy._latest_desktop_observation["requested_application"] == "calculator"


def test_08_calculation_node_exposes_structured_uia_mechanism():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    assert strategy.goal.requires_execution is True
    assert "native_accessibility" in {
        item.mechanism_id for item in strategy.remaining_mechanisms()
    }
    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {"surface_ref": "surface-control", "objective": "Exact control"},
    ) == (True, "")


def test_09_invoke_pattern_is_background_safe_without_foreground():
    contract = _surface_actuation_contract(
        _surface(patterns=("Invoke",)),
        target_foreground=False,
        target_minimized=False,
    )
    assert contract["available"] is True
    assert contract["background_safe"] is True
    assert "target_foreground" not in contract["preconditions"]

    registry = DesktopSurfaceRegistry()
    native_window = NativeSurfaceIdentity(
        pid=42,
        process_started_at=1.0,
        native_window_id=4242,
        class_name="GenericWindow",
    )
    window = registry.register(
        application_id="generic-app",
        process_ref="generic-process",
        generation="generic-generation",
        surface_kind=SurfaceKind.WINDOW,
        role="WindowControl",
        accessible_label="Generic app",
        selected=False,
        focused=False,
        enabled=True,
        parent_ref=None,
        bounds=SurfaceBounds(10, 10, 410, 410),
        runtime_signature="window-runtime",
        structural_epoch=1,
        scene_signature="scene-signature",
        native=native_window,
        stable_identity="window",
        patterns=("Window",),
    )
    control_native = NativeSurfaceIdentity(
        pid=42,
        process_started_at=1.0,
        native_window_id=4242,
        class_name="ButtonControl",
        runtime_id=(1, 2, 3),
    )
    control = registry.register(
        application_id="generic-app",
        process_ref="generic-process",
        generation="generic-generation",
        surface_kind=SurfaceKind.CONTROL,
        role="ButtonControl",
        accessible_label="Exact control",
        selected=False,
        focused=False,
        enabled=True,
        parent_ref=window.ref_id,
        bounds=SurfaceBounds(20, 20, 80, 60),
        runtime_signature="control-runtime",
        structural_epoch=1,
        scene_signature="scene-signature",
        native=control_native,
        stable_identity="control",
        patterns=("Invoke",),
    )
    scene = DesktopScene(
        application_id="generic-app",
        window_ref=window,
        surfaces=(window, control),
        structural_epoch=1,
        scene_signature="scene-signature",
        accessibility_complete=True,
        observed_at=time.time(),
        foreground=False,
    )
    invoked = []
    fake_pattern = SimpleNamespace(Invoke=lambda: invoked.append("invoke"))
    fake_control = SimpleNamespace(GetInvokePattern=lambda: fake_pattern)
    fake_auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _handle: object(),
    )
    with patch.object(desktop_scene.sys, "platform", "win32"), patch.object(
        desktop_scene, "DESKTOP_SURFACE_REGISTRY", registry,
    ), patch.object(
        desktop_scene,
        "_select_desktop_target",
        return_value=SimpleNamespace(target_scene=scene),
    ), patch(
        "osia.tools.desktop_control._load_uiautomation",
        return_value=fake_auto,
    ), patch.object(
        desktop_scene, "_find_runtime_control", return_value=fake_control,
    ):
        result = desktop_scene.activate_desktop_surface(
            control.ref_id, "Exact control",
        )

    assert invoked == ["invoke"]
    assert result["receipt"]["dispatch_state"] == "dispatched"
    assert result["receipt"]["background_safe"] is True


def test_10_foreground_required_mutation_exposes_activation_not_keyboard():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    assert es.tool_applicability(strategy, "desktop_type", {}) == (
        False, "fresh_target_foreground_required",
    )
    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {"surface_ref": "surface-calculator-window", "objective": "Calculator"},
    ) == (True, "")


def test_11_unrelated_foreground_does_not_force_focus_for_safe_uia():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    assert strategy._latest_desktop_observation["foreground_application"] == "chatgpt"
    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {"surface_ref": "surface-control", "objective": "Exact control"},
    ) == (True, "")
    assert es.tool_applicability(strategy, "desktop_click", {}) == (
        False, "background_safe_uia_required",
    )


def test_12_keyboard_rechecks_live_foreground_before_effect():
    from osia.tools import desktop_control

    strategy = _calculation_strategy(foreground=True)
    with es.execution_strategy_scope(TASK, strategy=strategy), patch(
        "osia.desktop_scene.observe_foreground_scene",
        return_value=object(),
    ), patch(
        "osia.desktop_scene.scene_matches_application",
        return_value=False,
    ):
        assert desktop_control._intended_foreground_error() == (
            "ERROR: intended application is not in the foreground"
        )


def test_13_excluded_open_cannot_pass_canonical_preflight():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    decision = es.tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "prerequisite_effect_already_completed"


def test_14_open_prerequisite_does_not_prove_calculation_goal():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    assert strategy.effect_is_completed(
        "application_open", target_application="calculator",
    )
    assert CALC_CRITERION not in strategy.proven_postconditions
    assert strategy.succeeded is False


def test_15_exact_result_receipt_is_still_required_for_success():
    strategy = _calculation_strategy(memory=_completed_open_memory())
    observed = _observation()
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {"scene": observed, "receipt": {"outcome": "observed"}},
        is_error=False,
        strategy=strategy,
    )
    assert strategy.succeeded is False
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": observed,
            "receipt": {
                "outcome": "confirmed_success",
                "reason_code": "exact_final_state_observation",
                "postcondition": CALC_CRITERION,
                "observed_target": "10063",
            },
        },
        is_error=False,
        strategy=strategy,
    )
    assert strategy.succeeded is True
