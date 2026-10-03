"""WOR-262 deterministic Desktop Calculator stabilization coverage."""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from osia import desktop_scene
from osia.desktop_scene import DesktopScene
from osia.desktop_surface import (
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
    SurfaceResolution,
)
from osia.execution_strategy import (
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    build_execution_strategy,
    execution_strategy_scope,
    normalize_execution_memory,
    record_tool_observation,
    record_tool_outcome,
    rebind_execution_memory,
    select_local_reconciliation_probe,
    tool_preflight,
)
from osia.tools import desktop_control
from osia.turn_terminality import TurnTerminalityController


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
BUTTON_LABELS = (
    "Trois",
    "Quatre",
    "Sept",
    "Multiplier par",
    "Deux",
    "Neuf",
    "Est égal à",
)


def _surface(
    ref_id: str,
    label: str,
    *,
    application: str = "calculator",
    role: str = "ButtonControl",
    kind: SurfaceKind = SurfaceKind.CONTROL,
    patterns: tuple[str, ...] = ("Invoke",),
    focused: bool = False,
) -> DesktopSurfaceRef:
    return DesktopSurfaceRef(
        ref_id=ref_id,
        application_id=application,
        process_ref="proc-calculator",
        window_generation="win-calculator",
        surface_kind=kind,
        role=role,
        accessible_label=label,
        focused=focused,
        enabled=True,
        parent_ref=None if kind is SurfaceKind.WINDOW else "calculator-window",
        bounds=(
            SurfaceBounds(100, 100, 700, 800)
            if kind is SurfaceKind.WINDOW else
            SurfaceBounds(150, 200, 250, 260)
        ),
        runtime_signature="runtime-" + ref_id,
        patterns=patterns,
        structural_epoch=4,
        observed_at=100.0,
    )


def _scene(
    *,
    result: str = "0",
    application: str = "calculator",
    rich: bool = True,
    focus: bool = True,
    observed_at: float | None = None,
) -> DesktopScene:
    window = _surface(
        "calculator-window",
        (
            "Calculator"
            if application in {"calculator", "calculatorapp"}
            else application.title()
        ),
        application=application,
        role="WindowControl",
        kind=SurfaceKind.WINDOW,
        patterns=("Window",),
        focused=True,
    )
    controls = []
    if rich:
        controls.extend(
            _surface(
                "calculator-button-" + str(index),
                label,
                application=application,
                focused=focus and index == 0,
            )
            for index, label in enumerate(BUTTON_LABELS)
        )
        controls.append(_surface(
            "calculator-display",
            "L’affichage est " + result,
            application=application,
            role="TextControl",
            patterns=(),
        ))
    return DesktopScene(
        application_id=application,
        window_ref=window,
        surfaces=(window, *controls),
        structural_epoch=4,
        scene_signature="calculator-scene-" + result,
        accessibility_complete=rich,
        observed_at=(time.time() if observed_at is None else observed_at),
        uia_tree_observed=rich,
    )


def _named_scene(
    application: str,
    title: str,
    *,
    rich: bool,
    foreground: bool,
    key: str,
    host_application: str = "",
    result: str = "0",
    observed_at: float | None = None,
) -> DesktopScene:
    base = _scene(
        result=result,
        application=application,
        rich=rich,
        focus=foreground,
        observed_at=observed_at,
    )
    window_ref = f"{key}-window"
    generation = f"win-{key}"
    process_ref = f"proc-{key}"
    surfaces = []
    for item in base.surfaces:
        is_window = item is base.window_ref
        surfaces.append(replace(
            item,
            ref_id=(window_ref if is_window else f"{key}-{item.ref_id}"),
            application_id=application,
            process_ref=process_ref,
            window_generation=generation,
            accessible_label=(title if is_window else item.accessible_label),
            selected=bool(foreground and is_window),
            focused=bool(foreground and item.focused),
            parent_ref=None if is_window else window_ref,
            runtime_signature=("" if is_window else f"runtime-{key}-{item.ref_id}"),
        ))
    return replace(
        base,
        application_id=application,
        window_ref=surfaces[0],
        surfaces=tuple(surfaces),
        scene_signature=f"scene-{key}-{result}",
        foreground=foreground,
        host_application_id=host_application or application,
        content_application_id=(
            application
            if host_application == "applicationframehost" and rich else ""
        ),
    )


def _target_selection(
    foreground: DesktopScene,
    target: DesktopScene | None,
    *,
    status: str = "exact_target",
    requested_application: str = "Calculator",
    candidate_count: int = 1,
) -> desktop_scene.TargetSurfaceSelection:
    evidence = (
        "uia_content_application:calculatorapp"
        if target is not None and target.host_application_id == "applicationframehost"
        else "executable_application:calculator"
    )
    return desktop_scene.TargetSurfaceSelection(
        requested_application=requested_application,
        status=status,
        foreground_scene=foreground,
        target_scene=target,
        identity_strength=400 if target is not None else 0,
        identity_evidence=evidence if target is not None else "",
        candidate_count=candidate_count,
        top_level_surfaces=(
            {
                "application": foreground.application_id,
                "title": foreground.window_ref.accessible_label,
                "window_class": "foreground",
                "foreground": True,
            },
            *(
                ({
                    "application": target.application_id,
                    "title": target.window_ref.accessible_label,
                    "window_class": "target",
                    "foreground": bool(target.foreground),
                },)
                if target is not None else ()
            ),
        ),
    )


def _strategy():
    return build_execution_strategy(
        TASK,
        {
            "activate_desktop_surface",
            "desktop_click",
            "desktop_press",
            "desktop_type",
            "inspect_desktop_surfaces",
            "open_application",
            "request_visual_observation",
            "run_command",
        },
    )


def _complete_open(strategy) -> None:
    open_node = strategy.mission.action_nodes[0]
    assert strategy.verify_postcondition(
        "exact Calculator foreground observation",
        postcondition=open_node.observable_success_postcondition,
    )


def test_a_launch_only_does_not_complete_calculation():
    strategy = _strategy()
    open_node, calculation_node = strategy.mission.action_nodes

    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record_tool_outcome(
        "open_application",
        decision,
        outcome="success_verified",
        is_error=False,
        reason_code="application_launched",
        result="Calculator running",
        strategy=strategy,
    )

    assert open_node.status == "succeeded"
    assert calculation_node.status == "pending"
    assert calculation_node.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
    assert strategy.succeeded is False


def test_b_window_without_result_is_only_observed():
    strategy = _strategy()
    _complete_open(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=_scene()),
    ):
        result = desktop_scene.inspect_desktop_surfaces("10063")
        proven = record_tool_observation(
            "inspect_desktop_surfaces", result,
            is_error=False, strategy=strategy,
        )

    assert result["receipt"]["outcome"] == "observed"
    assert proven is False
    assert strategy.succeeded is False


def test_c_rich_uia_buttons_progress_then_exact_result_succeeds():
    strategy = _strategy()
    _complete_open(strategy)
    scene = _scene()
    by_ref = {surface.ref_id: surface for surface in scene.surfaces}
    native = NativeSurfaceIdentity(
        pid=17008,
        process_started_at=10.0,
        native_window_id=1234,
        runtime_id=(4, 7, 11),
        class_name="ApplicationFrameWindow",
    )
    invoked = []

    class _Invoke:
        def Invoke(self):
            invoked.append("invoke")

    control = SimpleNamespace(
        GetInvokePattern=lambda: _Invoke(),
        GetSelectionItemPattern=lambda: None,
        GetTogglePattern=lambda: None,
        GetExpandCollapsePattern=lambda: None,
    )
    automation = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _hwnd: object(),
    )

    def _resolve(ref_id, **_kwargs):
        return SurfaceResolution("resolved", by_ref[ref_id], native)

    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.desktop_scene.sys.platform", "win32"),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=native,
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "resolve",
            side_effect=_resolve,
        ),
        patch("osia.tools.desktop_control._load_uiautomation", return_value=automation),
        patch("osia.desktop_scene._find_runtime_control", return_value=control),
    ):
        for index, label in enumerate(BUTTON_LABELS):
            surface = next(
                item for item in scene.surfaces
                if item.accessible_label == label
            )
            observed_result = (
                "3", "34", "347", "347×", "2", "29", "10 063",
            )[index]
            observable_effect = {
                "kind": "exact_surface_text",
                "surface_ref": "calculator-display",
                "expected_text": (
                    observed_result
                    if re.fullmatch(r"[\d\s\u00a0\u202f']+", observed_result)
                    else "L’affichage est " + observed_result
                ),
            }
            arguments = {
                "surface_ref": surface.ref_id,
                "objective": label,
                "action_pattern": "Invoke",
                "observable_effect": observable_effect,
            }
            decision = tool_preflight(
                "activate_desktop_surface", arguments, strategy=strategy,
            )
            assert decision is not None and decision.allowed
            result = desktop_scene.activate_desktop_surface(
                surface.ref_id,
                label,
                observable_effect=observable_effect,
                action_pattern="Invoke",
            )
            assert result["receipt"]["mechanism"] == "invoke"
            record_tool_outcome(
                "activate_desktop_surface",
                decision,
                outcome=result["receipt"]["outcome"],
                is_error=False,
                reason_code=result["receipt"]["reason_code"],
                result=result,
                strategy=strategy,
            )
            observed_target = (
                "10063" if index == len(BUTTON_LABELS) - 1 else "reconcile"
            )
            with patch(
                "osia.desktop_scene.observe_foreground_scene",
                return_value=_scene(result=observed_result),
            ):
                observed = desktop_scene.inspect_desktop_surfaces(observed_target)
            assert record_tool_observation(
                "inspect_desktop_surfaces", observed,
                is_error=False, strategy=strategy,
            )

    assert len(invoked) == len(BUTTON_LABELS)
    native_attempts = [
        item for item in strategy.attempts
        if item.layer == int(StrategyLayer.NATIVE_ACCESSIBILITY)
    ]
    assert len(native_attempts) == len(BUTTON_LABELS)
    assert all(
        item.status == "progressed" for item in native_attempts[:-1]
    )
    assert native_attempts[-1].status == "succeeded"
    assert strategy.succeeded is True


def test_d_verified_calculator_focus_allows_keyboard_fallback():
    strategy = _strategy()
    _complete_open(strategy)
    keyboard = Mock()
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.tools.desktop_control.sys.platform", "win32"),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=_scene()),
        patch("osia.tools.desktop_control._backend", return_value="pyautogui"),
        patch("osia.tools.desktop_control._pyautogui_ready_error", return_value=None),
        patch("osia.tools.desktop_control._load_pyautogui", return_value=keyboard),
    ):
        typed = desktop_control.type_text("347*29")
        submitted = desktop_control.press_key("enter")

    assert not typed.startswith("ERROR:")
    assert not submitted.startswith("ERROR:")
    keyboard.write.assert_called_once_with("347*29")
    keyboard.press.assert_called_once_with("enter")


def test_e_wrong_foreground_rejects_keyboard_before_dispatch():
    strategy = _strategy()
    _complete_open(strategy)
    keyboard = Mock()
    wrong_scene = _scene(application="notepad")
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.tools.desktop_control.sys.platform", "win32"),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=wrong_scene,
        ),
        patch("osia.tools.desktop_control._backend", return_value="pyautogui"),
        patch("osia.tools.desktop_control._load_pyautogui", return_value=keyboard),
    ):
        result = desktop_control.type_text("347*29=")

    assert result == "ERROR: intended application is not in the foreground"
    keyboard.write.assert_not_called()


def test_keyboard_fails_closed_when_foreground_observation_errors():
    strategy = _strategy()
    _complete_open(strategy)
    keyboard = Mock()
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.tools.desktop_control.sys.platform", "win32"),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            side_effect=RuntimeError("UIA unavailable"),
        ),
        patch("osia.tools.desktop_control._backend", return_value="pyautogui"),
        patch("osia.tools.desktop_control._load_pyautogui", return_value=keyboard),
    ):
        result = desktop_control.type_text("347*29=")

    assert result == "ERROR: intended foreground application is not observable"
    keyboard.write.assert_not_called()


def test_windows_focus_guard_does_not_change_other_platform_backends():
    strategy = _strategy()
    _complete_open(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.tools.desktop_control.sys.platform", "linux"),
        patch("osia.desktop_scene.observe_foreground_scene") as observe,
    ):
        assert desktop_control._intended_foreground_error() is None

    observe.assert_not_called()


def test_f_expected_locale_grouped_result_is_exact_typed_proof():
    strategy = _strategy()
    _complete_open(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="10 063"),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("10063")
        proven = record_tool_observation(
            "inspect_desktop_surfaces", result,
            is_error=False, strategy=strategy,
        )

    assert result["receipt"]["dispatch_state"] == "effect_confirmed"
    assert result["receipt"]["outcome"] == "confirmed_success"
    assert result["receipt"]["reason_code"] == "exact_final_state_observation"
    assert proven is True
    assert strategy.succeeded is True


def test_g_wrong_result_never_proves_success():
    strategy = _strategy()
    _complete_open(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="10062"),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("10063")
        proven = record_tool_observation(
            "inspect_desktop_surfaces", result,
            is_error=False, strategy=strategy,
        )

    assert result["receipt"]["outcome"] == "observed"
    assert proven is False
    assert strategy.succeeded is False


def test_decimal_lookalike_never_proves_integer_result():
    strategy = _strategy()
    _complete_open(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="100.63"),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("10063")
        proven = record_tool_observation(
            "inspect_desktop_surfaces", result,
            is_error=False, strategy=strategy,
        )

    assert result["receipt"]["outcome"] == "observed"
    assert proven is False
    assert strategy.succeeded is False


def test_h_no_result_and_no_safe_fallback_exhausts_before_blocking():
    strategy = _strategy()
    _complete_open(strategy)
    calculation = strategy.goal
    scene = _scene(rich=False, focus=False)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        assert not record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )

    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    keyboard = strategy.mechanism_for_layer(
        StrategyLayer.KEYBOARD, node_id=calculation.node_id,
    )
    local = strategy.mechanism_for_layer(
        StrategyLayer.LOCAL_VISION, node_id=calculation.node_id,
    )
    global_vision = strategy.mechanism_for_layer(
        StrategyLayer.GLOBAL_VISION, node_id=calculation.node_id,
    )
    assert all((native, keyboard, local, global_vision))

    native_decision = strategy.select(native, {"target": "Trois"})
    assert native_decision.allowed
    strategy.record(
        native_decision, "failed", failure_reason="uia_tree_unavailable",
    )
    keyboard_decision = strategy.select(keyboard, {"text": "347*29="})
    assert keyboard_decision.allowed
    strategy.record(
        keyboard_decision, "failed", failure_reason="focus_not_observable",
    )
    strategy.mark_inapplicable(
        local, reason_code="effective_model_non_vision",
    )
    strategy.mark_inapplicable(
        global_vision, reason_code="effective_model_non_vision",
    )

    diagnostic = strategy.final_diagnostic()
    assert diagnostic["code"] == "all_semantically_valid_strategies_exhausted"
    assert diagnostic["next_action"] == "precise_failure_or_clarification"
    assert strategy.succeeded is False


def test_i_ambiguous_dispatch_freezes_duplicate_mutation():
    strategy = _strategy()
    _complete_open(strategy)
    calculation = strategy.goal
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    first = strategy.select(native, {"surface_ref": "calculator-button-0"})
    assert first.allowed
    strategy.record(
        first,
        "ambiguous",
        failure_reason="uia_activation_outcome_unknown",
    )

    replay = strategy.select(native, {"surface_ref": "calculator-button-0"})

    assert replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"
    assert sum(item.status == "ambiguous_effect" for item in strategy.attempts) == 1


def test_j_provider_native_shell_receipts_never_prove_desktop_state():
    strategy = _strategy()
    effectful = tool_preflight(
        "run_command",
        {"command": "Start-Process Calculator"},
        strategy=strategy,
    )
    assert effectful is not None
    assert effectful.allowed is True
    record_tool_outcome(
        "run_command",
        effectful,
        outcome="success_verified",
        is_error=False,
        reason_code="provider_typed_completion",
        result={"exit_code": 0},
        strategy=strategy,
    )
    assert strategy.proven_postconditions == {}

    node = strategy.record_external_event(
        "run_command",
        {"command": "Get-Process CalculatorApp"},
        outcome="success_verified",
        evidence="provider_native:read-only:exit-0",
    )
    assert node is not None
    assert node.observable_success_postcondition == ""
    assert strategy.proven_postconditions == {}

    _complete_open(strategy)
    calculation_postcondition = strategy.goal.observable_success_postcondition
    calculation_dispatch = tool_preflight(
        "run_command",
        {"command": "Add-Type; send Calculator keys"},
        strategy=strategy,
    )
    assert calculation_dispatch is not None
    assert calculation_dispatch.allowed is False
    assert calculation_dispatch.reason_code == "mechanism_not_applicable"
    node = strategy.record_external_event(
        "run_command",
        {"command": "Get-Process CalculatorApp"},
        outcome="success_verified",
        evidence="provider_native:read-only:exit-0-again",
    )
    assert node is not None
    assert node.observable_success_postcondition == ""
    assert calculation_postcondition not in strategy.proven_postconditions
    assert strategy.succeeded is False


def test_k_nonvision_model_performs_zero_image_capture():
    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene") as observe,
            patch("osia.desktop_scene._capture_bounds_timed") as capture,
        ):
            result = desktop_scene.request_visual_observation(
                "calculator-window", "incomplete_accessibility", "10063",
            )
    finally:
        desktop_scene.reset_effective_model(route)

    assert result["receipt"]["reason_code"] == "effective_model_non_vision"
    observe.assert_not_called()
    capture.assert_not_called()


def test_l_vision_poor_surface_is_targeted_only_after_native_and_keyboard():
    strategy = _strategy()
    _complete_open(strategy)
    calculation = strategy.goal
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    keyboard = strategy.mechanism_for_layer(
        StrategyLayer.KEYBOARD, node_id=calculation.node_id,
    )
    assert strategy.remaining_mechanisms()[0] is native

    native_decision = strategy.select(native, {"target": "Trois"})
    strategy.record(
        native_decision, "failed", failure_reason="uia_tree_unavailable",
    )
    keyboard_decision = strategy.select(keyboard, {"text": "347*29="})
    strategy.record(
        keyboard_decision, "failed", failure_reason="focus_not_observable",
    )
    assert strategy.remaining_mechanisms()[0].layer is StrategyLayer.LOCAL_VISION

    scene = _scene(rich=False, focus=False)
    turn_tokens = desktop_scene.begin_desktop_turn()
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                return_value=(b"targeted-calculator-window", 2.0, 1.0),
            ) as capture,
        ):
            result = desktop_scene.request_visual_observation(
                scene.window_ref.ref_id,
                "incomplete_accessibility",
                "10063",
                expected_application="calculator",
                expected_window_generation="win-calculator",
            )
    finally:
        desktop_scene.reset_effective_model(route)
        desktop_scene.end_desktop_turn(turn_tokens)

    capture.assert_called_once_with(scene.window_ref.bounds)
    assert result["receipt"]["outcome"] == "observed"
    assert result["receipt"]["surface_ref"] == scene.window_ref.ref_id
    assert result["observation"]["capture_kind"] == "window"


def test_uwp_frame_host_uses_real_content_process_identity():
    controls = (
        {"process_id": 17008},
        {"process_id": 17008},
        {"process_id": 21196},
    )
    with patch(
        "osia.desktop_scene._process_details",
        return_value=("calculatorapp", 10.0),
    ) as process_details:
        application = desktop_scene._semantic_application_id(
            "applicationframehost", 21196, controls,
        )

    assert application == "calculatorapp"
    process_details.assert_called_once_with(17008)
    assert desktop_scene.scene_matches_application(
        _scene(application=application), "calculator",
    )
    assert not desktop_scene.scene_matches_application(
        _scene(application="calculatorhelper"), "calculator",
    )


def _ambiguous_calculation(strategy, mechanism_id="direct:run_command"):
    _complete_open(strategy)
    calculation = strategy.goal
    direct = StrategyMechanism(
        mechanism_id,
        StrategyLayer.DIRECT_CAPABILITY,
        "provider-native semantic calculation",
        application="calculator",
        covers_goal=False,
        mutation=True,
        node_id=calculation.node_id,
    )
    decision = strategy.select(direct, {"command": "calculate 347 x 29"})
    assert decision.allowed
    strategy.record(
        decision,
        "ambiguous",
        failure_reason="provider_failed_after_start",
    )
    return calculation, direct, decision


def _reconcile_neutral_calculation(strategy):
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="0"),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        proven = record_tool_observation(
            "inspect_desktop_surfaces",
            observed,
            is_error=False,
            strategy=strategy,
        )
    assert proven is False
    assert observed["receipt"]["outcome"] == "confirmed_absence"
    assert observed["receipt"]["no_partial_effect"] is True
    return observed


def test_m_unknown_effect_blocks_identical_provider_mutation_replay():
    strategy = _strategy()
    _calculation, direct, _decision = _ambiguous_calculation(strategy)

    blocked = strategy.select(direct, {"command": "calculate 347 x 29"})

    assert blocked.allowed is False
    assert blocked.reason_code == "ambiguous_effect_requires_reconciliation"
    assert sum(item.status == "ambiguous_effect" for item in strategy.attempts) == 1


def test_n_run_command_progress_retires_same_node_mechanism():
    strategy = _strategy()
    first = tool_preflight(
        "run_command", {"command": "Start-Process Calculator"}, strategy=strategy,
    )
    assert first is not None and first.allowed
    record_tool_outcome(
        "run_command",
        first,
        outcome="success_verified",
        is_error=False,
        reason_code="provider_typed_completion",
        result={"exit_code": 0},
        strategy=strategy,
    )

    repeated = tool_preflight(
        "run_command",
        {"command": "Start-Process calc.exe"},
        strategy=strategy,
    )

    assert repeated is not None and repeated.allowed is False
    assert repeated.reason_code == "mechanism_retired_after_unverified_progress"


def test_o_unknown_effect_keeps_read_only_desktop_observation_available():
    strategy = _strategy()
    _ambiguous_calculation(strategy)
    before = len(strategy.attempts)
    assert tool_preflight(
        "inspect_desktop_surfaces", {"target": "10063"}, strategy=strategy,
    ) is None

    record_tool_outcome(
        "inspect_desktop_surfaces",
        None,
        outcome="outcome_unknown",
        is_error=False,
        reason_code="observation_transport_pending",
        result={"receipt": {"outcome": "observed"}},
        strategy=strategy,
    )

    assert len(strategy.attempts) == before
    assert strategy.frozen_reason == "provider_failed_after_start"


def test_p_exact_satisfied_reconciliation_performs_no_fallback_mutation():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="10 063"),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        assert record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )

    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    keyboard = strategy.mechanism_for_layer(
        StrategyLayer.KEYBOARD, node_id=calculation.node_id,
    )
    assert strategy.select(native, {"surface_ref": "digit-3"}).allowed is False
    assert strategy.select(keyboard, {"text": "347*29="}).allowed is False
    assert strategy.succeeded is True


def test_q_exact_absence_retires_old_mechanism_and_allows_safe_fallback():
    strategy = _strategy()
    calculation, direct, _decision = _ambiguous_calculation(strategy)
    _reconcile_neutral_calculation(strategy)

    replay = strategy.select(direct, {"command": "another calculation route"})
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    fallback = strategy.select(native, {"surface_ref": "digit-3"})

    assert replay.allowed is False
    assert replay.reason_code == "mechanism_retired_after_outcome_unknown"
    assert fallback.allowed is True
    assert strategy.snapshot()["reconciliation_events"][-1]["resolution"] == "absent"


def test_r_ambiguous_observation_keeps_all_mutation_blocked():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="347 ×"),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        assert not record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )

    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    blocked = strategy.select(native, {"surface_ref": "digit-3"})
    assert observed["receipt"]["outcome"] == "observed"
    assert blocked.reason_code == "ambiguous_effect_requires_reconciliation"


def test_s_open_unknown_reconciles_only_open_node_then_calculation_executes():
    strategy = _strategy()
    open_node, calculation = strategy.mission.action_nodes
    direct = StrategyMechanism(
        "direct:run_command", StrategyLayer.DIRECT_CAPABILITY,
        "provider-native application launch",
        application="calculator", covers_goal=False, mutation=True,
        node_id=open_node.node_id,
    )
    decision = strategy.select(direct, {"command": "Start-Process Calculator"})
    strategy.record(decision, "ambiguous", failure_reason="provider_failed_after_start")
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=_scene()),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("Calculator")
        assert record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )

    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    assert open_node.observable_success_postcondition in strategy.proven_postconditions
    assert calculation.observable_success_postcondition not in strategy.proven_postconditions
    assert strategy.select(native, {"surface_ref": "digit-3"}).allowed is True


def test_t_neutral_display_allows_one_semantic_uia_dispatch():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    _reconcile_neutral_calculation(strategy)
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    decision = strategy.select(native, {"surface_ref": "digit-3"})
    strategy.record(decision, "progress", observation="digit 3 invoked")

    assert decision.allowed is True
    assert sum(
        item.status == "progressed"
        and item.mechanism_id == native.mechanism_id
        for item in strategy.attempts
    ) == 1


def test_u_partial_expression_never_allows_second_semantic_calculation():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="347"),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )

    assert strategy.select(native, {"surface_ref": "digit-3"}).allowed is False
    assert strategy.frozen_reason


def test_v_uia_rich_reconciled_surface_prioritizes_uia_over_keyboard():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    _reconcile_neutral_calculation(strategy)
    keyboard = strategy.mechanism_for_layer(
        StrategyLayer.KEYBOARD, node_id=calculation.node_id,
    )

    decision = strategy.preview(keyboard, {"text": "347*29="})

    assert decision.allowed is False
    assert decision.reason_code.startswith("higher_strategy_still_applicable:native_accessibility")


def test_w_uia_rich_reconciled_path_never_requests_vision():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    _reconcile_neutral_calculation(strategy)
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    with patch("osia.desktop_scene.request_visual_observation") as vision:
        decision = strategy.select(native, {"surface_ref": "digit-3"})
        strategy.record(decision, "progress", observation="digit 3 invoked")

    vision.assert_not_called()


def test_x_reconciled_uia_sequence_plus_exact_result_is_success_verified():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    _reconcile_neutral_calculation(strategy)
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    for index, label in enumerate(BUTTON_LABELS):
        decision = strategy.select(native, {"surface_ref": f"button-{index}"})
        assert decision.allowed
        strategy.record(decision, "progress", observation=label + " invoked")
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="10063"),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        verified = record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )

    assert verified is True
    assert strategy.succeeded is True
    assert strategy.final_diagnostic()["code"] == "postcondition_verified"


def test_y_launch_only_evidence_never_completes_calculation():
    strategy = _strategy()
    first = tool_preflight(
        "run_command", {"command": "Start-Process Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "run_command", first,
        outcome="success_verified", is_error=False,
        reason_code="provider_typed_completion", result={"exit_code": 0},
        strategy=strategy,
    )
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=_scene()),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("Calculator")
        assert record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )

    assert strategy.mission.action_nodes[1].status == "pending"
    assert strategy.succeeded is False


def test_z_unknown_never_becomes_success_from_provider_exit_or_prose():
    strategy = _strategy()
    calculation, _direct, _decision = _ambiguous_calculation(strategy)
    node = strategy.record_external_event(
        "run_command",
        {"command": "provider says calculation completed"},
        outcome="success_verified",
        evidence="provider exit 0; prose says result 10063",
        semantic_node_id=calculation.node_id,
    )

    assert node is not None and node.observable_success_postcondition == ""
    assert calculation.observable_success_postcondition not in strategy.proven_postconditions
    assert strategy.frozen_reason == "provider_failed_after_start"
    assert strategy.succeeded is False


def test_za_unrelated_read_only_capability_neither_freezes_nor_reconciles_node():
    strategy = _strategy()
    open_node = strategy.mission.action_nodes[0]
    direct = StrategyMechanism(
        "direct:run_command", StrategyLayer.DIRECT_CAPABILITY,
        "provider-native application launch",
        application="calculator", covers_goal=False, mutation=True,
        node_id=open_node.node_id,
    )
    decision = strategy.select(direct, {"command": "Start-Process Calculator"})
    strategy.record(decision, "ambiguous", failure_reason="provider_failed_after_start")
    before = len(strategy.attempts)

    record_tool_outcome(
        "list_files", None,
        outcome="outcome_unknown", is_error=False,
        reason_code="read_only_transport_pending", result="no files found",
        strategy=strategy,
    )

    assert len(strategy.attempts) == before
    assert strategy.frozen_reason == "provider_failed_after_start"
    assert strategy.snapshot()["reconciliation_events"] == []


def test_zb_repeated_provider_plans_allow_only_one_run_command_dispatch():
    strategy = _strategy()
    first = tool_preflight(
        "run_command", {"command": "Start-Process Calculator"}, strategy=strategy,
    )
    assert first is not None and first.allowed
    in_flight = tool_preflight(
        "run_command", {"command": "Start Calculator via shell"}, strategy=strategy,
    )
    assert in_flight is not None and in_flight.allowed is False
    assert in_flight.reason_code == "mechanism_dispatch_in_flight"
    record_tool_outcome(
        "run_command", first,
        outcome="success_verified", is_error=False,
        reason_code="provider_typed_completion", result={"exit_code": 0},
        strategy=strategy,
    )
    decisions = [
        tool_preflight(
            "run_command", {"command": f"alternate launch {index}"},
            strategy=strategy,
        )
        for index in range(16)
    ]

    assert all(item is not None and not item.allowed for item in decisions)
    assert all(
        item.reason_code == "mechanism_retired_after_unverified_progress"
        for item in decisions
    )
    assert sum(item.status == "progressed" for item in strategy.attempts) == 1


def _durable_calculation_unknown(tmp_path, *, attempt_id="provider-calculation"):
    strategy = _strategy()
    calculation, direct, decision = _ambiguous_calculation(strategy)
    controller = TurnTerminalityController(
        turn_id="turn-" + attempt_id,
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"source": "provider_native", "identity": "run_command"},
        postcondition=calculation.observable_success_postcondition,
        attempt_id=attempt_id,
        reason_code="provider_native_completed",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    bound = controller.snapshot()["unknown_effects"][attempt_id]
    assert bound["semantic_node_id"] == calculation.node_id
    assert bound["strategy_attempt_id"] == decision.attempt_id
    assert bound["mechanism_id"] == direct.mechanism_id
    return strategy, controller, calculation, direct, decision


def _reconcile_exact_result(strategy, result="10063"):
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result=result),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        proven = record_tool_observation(
            "inspect_desktop_surfaces",
            observed,
            is_error=False,
            strategy=strategy,
        )
    return observed, proven


def test_bridge_completed_effect_resolves_durable_unknown_without_replay(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    observed, proven = _reconcile_exact_result(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())
    snapshot = controller.snapshot()

    assert proven is True
    assert observed["receipt"]["reason_code"] == "exact_final_state_observation"
    assert snapshot["unknown_effects"] == {}
    assert snapshot["unknown_effect_resolutions"][-1]["resolution"] == "completed"
    assert controller.evaluate().terminal_state == "success_verified"
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    assert strategy.select(native, {"surface_ref": "digit-3"}).allowed is False


def test_bridge_not_applied_unlocks_next_mechanism_and_retires_original(tmp_path):
    strategy, controller, calculation, direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())
    snapshot = controller.snapshot()

    assert snapshot["unknown_effects"] == {}
    assert snapshot["unknown_effect_resolutions"][-1]["resolution"] == "not_applied"
    replay = strategy.select(direct, {"command": "calculate again"})
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    assert replay.reason_code == "mechanism_retired_after_outcome_unknown"
    assert strategy.preview(native, {"surface_ref": "digit-3"}).allowed is True
    assert controller.evaluate().next_strategy.startswith("native_accessibility")


def test_bridge_negative_reconciliation_preserves_unrelated_unknown(tmp_path):
    strategy = _strategy()
    open_node, calculation = strategy.mission.action_nodes
    direct_open = StrategyMechanism(
        "direct:run_command", StrategyLayer.DIRECT_CAPABILITY,
        "provider-native application launch", application="calculator",
        covers_goal=False, mutation=True, node_id=open_node.node_id,
    )
    open_decision = strategy.select(direct_open, {"command": "open calculator"})
    strategy.record(open_decision, "ambiguous", failure_reason="provider_unknown")
    controller = TurnTerminalityController(
        turn_id="turn-unrelated-unknown", objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True, storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown", evidence={"dispatch": "open"},
        postcondition=open_node.observable_success_postcondition,
        attempt_id="provider-open", reason_code="provider_native_completed",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    strategy.reconcile(succeeded=False, evidence="manual-local-test-separation")
    assert strategy.verify_postcondition(
        "exact Calculator foreground observation",
        postcondition=open_node.observable_success_postcondition,
    )
    calculation_direct = StrategyMechanism(
        "direct:calculation-provider", StrategyLayer.DIRECT_CAPABILITY,
        "provider calculation", application="calculator", covers_goal=False,
        mutation=True, node_id=calculation.node_id,
    )
    calculation_decision = strategy.select(
        calculation_direct, {"command": "calculate"},
    )
    strategy.record(
        calculation_decision, "ambiguous", failure_reason="provider_unknown",
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown", evidence={"dispatch": "calculate"},
        postcondition=calculation.observable_success_postcondition,
        attempt_id="provider-calculate", reason_code="provider_native_completed",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    _reconcile_neutral_calculation(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())

    remaining = controller.snapshot()["unknown_effects"]
    assert tuple(remaining) == ("provider-open",)
    assert remaining["provider-open"]["semantic_node_id"] == open_node.node_id


def test_bridge_mismatched_strategy_attempt_cannot_clear_barrier(tmp_path):
    strategy, controller, _calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    snapshot = strategy.snapshot()
    snapshot["reconciliation_events"][-1]["attempt_id"] = "strategy-mismatch"
    controller.set_strategy_snapshot(snapshot)

    assert "provider-calculation" in controller.snapshot()["unknown_effects"]


def test_bridge_mismatched_application_cannot_clear_barrier(tmp_path):
    strategy, controller, _calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    snapshot = strategy.snapshot()
    snapshot["reconciliation_events"][-1]["observed_application"] = "notepad"
    controller.set_strategy_snapshot(snapshot)

    assert "provider-calculation" in controller.snapshot()["unknown_effects"]


def test_bridge_mismatched_target_surface_cannot_clear_barrier(tmp_path):
    strategy = _strategy()
    _complete_open(strategy)
    calculation = strategy.goal
    calculation.required_surface = "calculator-display"
    direct = StrategyMechanism(
        "direct:surface-provider", StrategyLayer.DIRECT_CAPABILITY,
        "provider calculation", application="calculator",
        surface="calculator-display", covers_goal=False, mutation=True,
        node_id=calculation.node_id,
    )
    decision = strategy.select(direct, {"command": "calculate"})
    strategy.record(decision, "ambiguous", failure_reason="provider_unknown")
    controller = TurnTerminalityController(
        turn_id="turn-surface-mismatch", objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True, storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown", evidence={"dispatch": "calculate"},
        postcondition=calculation.observable_success_postcondition,
        attempt_id="provider-surface", reason_code="provider_native_completed",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    _reconcile_neutral_calculation(strategy)
    snapshot = strategy.snapshot()
    snapshot["reconciliation_events"][-1]["target_surface"] = "other-surface"
    controller.set_strategy_snapshot(snapshot)

    assert "provider-surface" in controller.snapshot()["unknown_effects"]


def test_bridge_stale_desktop_evidence_clears_neither_local_nor_durable(tmp_path):
    strategy, controller, _calculation, _direct, decision = (
        _durable_calculation_unknown(tmp_path)
    )
    dispatch = next(
        item for item in strategy.attempts if item.attempt_id == decision.attempt_id
    )
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(observed_at=dispatch.dispatched_at - 1.0),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("10063")
        assert not record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )
    controller.set_strategy_snapshot(strategy.snapshot())

    assert strategy.frozen_reason
    assert strategy.snapshot()["reconciliation_events"] == []
    assert "provider-calculation" in controller.snapshot()["unknown_effects"]


def test_bridge_provider_exit_or_prose_cannot_clear_durable_unknown(tmp_path):
    strategy, controller, _calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    strategy.reconcile(
        succeeded=True, evidence="provider exit 0; prose says result 10063",
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    snapshot = controller.snapshot()
    assert "provider-calculation" in snapshot["unknown_effects"]
    assert snapshot["unknown_effect_resolutions"] == []
    assert controller.evaluate().terminal_state == ""


def test_bridge_partial_state_remains_fail_closed(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    observed, proven = _reconcile_exact_result(strategy, result="347 ×")
    controller.set_strategy_snapshot(strategy.snapshot())
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )

    assert proven is False
    assert observed["receipt"]["outcome"] == "observed"
    assert "provider-calculation" in controller.snapshot()["unknown_effects"]
    assert strategy.preview(native, {"surface_ref": "digit-3"}).allowed is False


def test_bridge_resolution_survives_durable_reconstruction(tmp_path):
    strategy, controller, calculation, direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())
    restored = TurnTerminalityController(
        turn_id=controller.turn_id, objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True, storage_dir=tmp_path,
    )
    restored_strategy = ExecutionStrategy.from_snapshot(
        restored.snapshot()["strategy_snapshot"],
    )
    native = restored_strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )

    assert restored.snapshot()["unknown_effects"] == {}
    assert restored.snapshot()["unknown_effect_resolutions"][-1][
        "resolution"
    ] == "not_applied"
    assert restored_strategy.select(direct, {"command": "replay"}).allowed is False
    assert restored_strategy.preview(native, {"surface_ref": "digit-3"}).allowed


def test_bridge_not_applied_allows_one_uia_dispatch(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    decision = strategy.select(native, {"surface_ref": "digit-3"})
    strategy.record(decision, "progress", observation="digit 3 invoked")

    assert decision.allowed
    assert sum(
        item.mechanism_id == native.mechanism_id and item.status == "progressed"
        for item in strategy.attempts
    ) == 1


def test_bridge_completed_effect_keeps_uia_and_keyboard_counts_zero(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_exact_result(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())
    before = len(strategy.attempts)
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY, node_id=calculation.node_id,
    )
    keyboard = strategy.mechanism_for_layer(
        StrategyLayer.KEYBOARD, node_id=calculation.node_id,
    )

    assert not strategy.select(native, {"surface_ref": "digit-3"}).allowed
    assert not strategy.select(keyboard, {"text": "347*29="}).allowed
    assert len(strategy.attempts) == before + 2
    assert not any(
        item.status == "progressed"
        and item.layer in {
            int(StrategyLayer.NATIVE_ACCESSIBILITY),
            int(StrategyLayer.KEYBOARD),
        }
        for item in strategy.attempts
    )


def test_bridge_launch_only_never_proves_calculation_result(tmp_path):
    strategy = _strategy()
    decision = tool_preflight(
        "run_command", {"command": "Start-Process Calculator"},
        strategy=strategy,
    )
    record_tool_outcome(
        "run_command", decision, outcome="success_verified", is_error=False,
        reason_code="provider_native_completed", result={"exit_code": 0},
        strategy=strategy,
    )
    controller = TurnTerminalityController(
        turn_id="turn-launch-only", objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True, storage_dir=tmp_path,
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    calculation = strategy.mission.action_nodes[1]
    assert calculation.observable_success_postcondition not in (
        controller.snapshot()["proven_postconditions"]
    )
    assert controller.evaluate().terminal_state == ""


def test_bridge_exact_10063_enables_success_only_after_independent_evidence(tmp_path):
    strategy, controller, _calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    assert controller.evaluate().terminal_state == ""
    _reconcile_exact_result(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())

    assert controller.evaluate().terminal_state == "success_verified"
    assert any(
        item["reason_code"] == "exact_final_state_observation"
        for item in controller.snapshot()["typed_evidence"]
    )


def test_bridge_unresolved_effect_forbids_success_even_with_claimed_proof(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    controller.prove_postcondition(
        calculation.observable_success_postcondition,
        {"provider": "claimed-complete"},
    )

    decision = controller.evaluate()
    assert decision.terminal_state == ""
    assert decision.next_strategy == "reconcile_unknown_read_only"
    assert "provider-calculation" in controller.snapshot()["unknown_effects"]


def test_bridge_phase5c_one_of_two_plus_unknown_never_succeeds(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    open_postcondition = strategy.mission.action_nodes[0].observable_success_postcondition
    assert open_postcondition in controller.snapshot()["proven_postconditions"]
    assert calculation.observable_success_postcondition not in (
        controller.snapshot()["proven_postconditions"]
    )

    decision = controller.evaluate()
    assert decision.terminal_state != "success_verified"
    assert decision.next_strategy == "reconcile_unknown_read_only"


def test_bridge_open_node_reconciles_independently_from_calculation(tmp_path):
    strategy = _strategy()
    open_node, calculation = strategy.mission.action_nodes
    direct = StrategyMechanism(
        "direct:run_command", StrategyLayer.DIRECT_CAPABILITY,
        "provider-native application launch", application="calculator",
        covers_goal=False, mutation=True, node_id=open_node.node_id,
    )
    decision = strategy.select(direct, {"command": "open calculator"})
    strategy.record(decision, "ambiguous", failure_reason="provider_unknown")
    controller = TurnTerminalityController(
        turn_id="turn-open-independent", objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True, storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown", evidence={"dispatch": "open"},
        postcondition=open_node.observable_success_postcondition,
        attempt_id="provider-open", reason_code="provider_native_completed",
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown", evidence={"dispatch": "calculation"},
        postcondition=calculation.observable_success_postcondition,
        attempt_id="provider-calculation", reason_code="provider_native_completed",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.desktop_scene.observe_foreground_scene", return_value=_scene()),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("Calculator")
        assert record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )
    controller.set_strategy_snapshot(strategy.snapshot())

    snapshot = controller.snapshot()
    assert tuple(snapshot["unknown_effects"]) == ("provider-calculation",)
    assert snapshot["unknown_effect_resolutions"][-1][
        "durable_attempt_id"
    ] == "provider-open"


def test_bridge_repeated_segments_do_not_restore_cleared_barrier(tmp_path):
    strategy, controller, _calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    snapshot = strategy.snapshot()
    controller.set_strategy_snapshot(snapshot)
    controller.set_strategy_snapshot(snapshot)
    restored = TurnTerminalityController(
        turn_id=controller.turn_id, objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True, storage_dir=tmp_path,
    )
    restored.set_strategy_snapshot(snapshot)

    durable = restored.snapshot()
    assert durable["unknown_effects"] == {}
    assert len(durable["unknown_effect_resolutions"]) == 1


def test_bridge_audit_trail_preserves_original_ambiguous_attempt(tmp_path):
    strategy, controller, calculation, direct, decision = (
        _durable_calculation_unknown(tmp_path)
    )
    _reconcile_neutral_calculation(strategy)
    controller.set_strategy_snapshot(strategy.snapshot())
    resolution = controller.snapshot()["unknown_effect_resolutions"][-1]

    assert resolution["durable_attempt_id"] == "provider-calculation"
    assert resolution["strategy_attempt_id"] == decision.attempt_id
    assert resolution["semantic_node_id"] == calculation.node_id
    assert resolution["mechanism_id"] == direct.mechanism_id
    assert resolution["original_unknown"]["outcome"] == "outcome_unknown"
    assert resolution["original_unknown"]["reason_code"] == (
        "provider_native_completed"
    )


def _unknown_open_execution_record(*, tool_attempt_id=""):
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    arguments = {"name": "Calculator"}
    contract = effective_contract(
        get_tool_spec("open_application"), arguments, TASK,
    )
    return ToolExecutionRecord(
        "ERROR: launch result could not be confirmed",
        True,
        ExecutionOutcome.OUTCOME_UNKNOWN,
        1,
        True,
        False,
        contract,
        tool_attempt_id,
        False,
        "tool_error",
    )


def _record_agent_open_unknown(
    tmp_path,
    *,
    turn_id="wor262-correlation",
    strategy=None,
    decision=None,
):
    from osia.agent import OSIAAgent

    strategy = strategy or _strategy()
    decision = decision or tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record = _unknown_open_execution_record()
    record_tool_outcome(
        "open_application",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="tool_error",
        result=record.result,
        strategy=strategy,
    )
    controller = TurnTerminalityController(
        turn_id=turn_id,
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
    with execution_strategy_scope(TASK, strategy=strategy):
        OSIAAgent._record_unfinished_execution(
            agent,
            "open_application",
            {"name": "Calculator"},
            record,
            decision,
        )
    return strategy, controller, decision, agent


def _typed_is_running(strategy, scene):
    from osia import agent as agent_module

    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.tools.verify.app_running", return_value=True),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_target_selection(
                scene, scene, requested_application="Calculator",
            ),
        ),
    ):
        result = agent_module.tool_is_running("Calculator")
        proven = record_tool_observation(
            "is_running", result, is_error=False, strategy=strategy,
        )
    return result, proven


def test_correlation_projects_strategy_attempt_id_into_terminality_receipt(tmp_path):
    _state, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    unknown = controller.snapshot()["unknown_effects"]

    assert tuple(unknown) == (decision.attempt_id,)
    assert decision.attempt_id
    assert unknown[decision.attempt_id]["attempt_id"] == decision.attempt_id


def test_provider_native_event_segmentation_preserves_exact_attempt_identity(tmp_path):
    from osia.agent import OSIAAgent
    from osia.chatgpt_account.app_server_client import CapabilityEvent

    strategy = _strategy()
    arguments = {"command": "Start-Process Calculator"}
    decision = tool_preflight("run_command", arguments, strategy=strategy)
    assert decision is not None and decision.allowed
    attempt_id = "attempt-provider-segmented"
    open_node = strategy.node_by_id(decision.node_id)
    events = iter((
        (CapabilityEvent(
            source="provider_native",
            identity="commandExecution",
            phase="accepted",
            outcome="dispatch_started",
            item_id="item-provider-segmented",
            tool_attempt_id=attempt_id,
            tool_name="run_command",
            arguments=arguments,
            semantic_node_id=decision.node_id,
            semantic_postcondition=open_node.observable_success_postcondition,
        ),),
        (CapabilityEvent(
            source="provider_native",
            identity="commandExecution",
            phase="completed",
            outcome="outcome_unknown",
            item_id="item-provider-segmented",
            tool_attempt_id=attempt_id,
            tool_name="run_command",
            arguments=arguments,
            semantic_node_id=decision.node_id,
            semantic_postcondition=open_node.observable_success_postcondition,
        ),),
    ))
    controller = TurnTerminalityController(
        turn_id="wor262-provider-segments",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    agent = OSIAAgent(model="fixture", lang="en")
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: next(events),
    )
    agent._terminality_controller = controller
    agent._terminality_strategy = strategy

    agent._consume_provider_capability_events()
    agent._consume_provider_capability_events()
    controller.set_strategy_snapshot(strategy.snapshot())
    unknown = controller.snapshot()["unknown_effects"][attempt_id]

    assert unknown["attempt_id"] == attempt_id
    assert unknown["strategy_attempt_id"] == decision.attempt_id
    assert unknown["semantic_node_id"] == decision.node_id


def test_missing_provable_correlation_remains_empty_and_fail_closed(tmp_path):
    from osia.agent import OSIAAgent

    strategy = _strategy()
    controller = TurnTerminalityController(
        turn_id="wor262-no-correlation",
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
    with execution_strategy_scope(TASK, strategy=strategy):
        OSIAAgent._record_unfinished_execution(
            agent,
            "open_application",
            {"name": "Calculator"},
            _unknown_open_execution_record(),
            None,
        )
    unknown = tuple(controller.snapshot()["unknown_effects"].values())

    assert len(unknown) == 1
    assert unknown[0]["attempt_id"] == ""


def test_correlation_is_scoped_to_the_exact_semantic_node(tmp_path):
    from osia.agent import OSIAAgent

    task = "Open Calculator, then open Paint"
    strategy = build_execution_strategy(task, {"open_application"})
    calculator = strategy.mission.action_nodes[0]
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record = _unknown_open_execution_record()
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="tool_error", result=record.result, strategy=strategy,
    )
    controller = TurnTerminalityController(
        turn_id="wor262-two-nodes",
        objective=task,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    agent = SimpleNamespace(
        _terminality_controller=controller,
        _unfinished_execution=None,
        _emit_execution_trace=lambda *_args, **_kwargs: None,
    )
    with execution_strategy_scope(task, strategy=strategy):
        OSIAAgent._record_unfinished_execution(
            agent, "open_application", {"name": "Calculator"},
            record, decision,
        )
    controller.set_strategy_snapshot(strategy.snapshot())
    unknown = controller.snapshot()["unknown_effects"][decision.attempt_id]

    assert decision.node_id == calculator.node_id
    assert unknown["semantic_node_id"] == calculator.node_id


def test_correlation_never_uses_similar_command_text_as_identity(tmp_path):
    strategy = _strategy()
    arguments = {"name": "Calculator"}
    first = tool_preflight("open_application", arguments, strategy=strategy)
    assert first is not None and first.allowed
    strategy.record(first, "failed", failure_reason="failed_before_effect")
    strategy.observe("fresh deterministic evidence")
    second = tool_preflight("open_application", arguments, strategy=strategy)
    assert second is not None and second.allowed

    _state, controller, correlated, _agent = _record_agent_open_unknown(
        tmp_path,
        turn_id="wor262-similar-command",
        strategy=strategy,
        decision=second,
    )
    unknown = controller.snapshot()["unknown_effects"]

    assert first.attempt_id != second.attempt_id
    assert correlated.attempt_id == second.attempt_id
    assert tuple(unknown) == (second.attempt_id,)


def test_correlated_unknown_binds_exact_durable_strategy_identity(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    unknown = controller.snapshot()["unknown_effects"][decision.attempt_id]

    assert unknown["strategy_attempt_id"] == decision.attempt_id
    assert unknown["mechanism_id"] == "direct:open_application:calculator"
    assert unknown["strategy_dispatched_at"] > 0.0


def test_correlated_identity_survives_durable_reconstruction(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    restored = TurnTerminalityController(
        turn_id=controller.turn_id,
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    unknown = restored.snapshot()["unknown_effects"][decision.attempt_id]

    assert unknown["attempt_id"] == decision.attempt_id
    assert unknown["strategy_attempt_id"] == decision.attempt_id


def test_exact_open_resolution_leaves_unrelated_unknown_untouched(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"source": "unrelated"},
        postcondition=strategy.mission.action_nodes[1].observable_success_postcondition,
        attempt_id="unrelated-calculation",
        reason_code="unrelated_unknown",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    _result, proven = _typed_is_running(strategy, _scene())
    assert proven is True
    controller.set_strategy_snapshot(strategy.snapshot())
    remaining = controller.snapshot()["unknown_effects"]

    assert decision.attempt_id not in remaining
    assert tuple(remaining) == ("unrelated-calculation",)


def test_is_running_emits_canonical_typed_application_open_evidence():
    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="tool_error", result="unknown", strategy=strategy,
    )
    result, proven = _typed_is_running(strategy, _scene())
    receipt = result["receipt"]

    assert proven is True
    assert receipt["evidence_kind"] == "exact_independent_desktop_state"
    assert receipt["evidence_source"] == "inspect_desktop_surfaces"
    assert receipt["semantic_postcondition"] == "application_open"
    assert receipt["observed_application"] == "calculator"
    assert receipt["observed_process_ref"] == "proc-calculator"
    assert receipt["observed_window_surface_ref"] == "calculator-window"


def test_fresh_typed_is_running_resolves_matching_open_unknown(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    result, proven = _typed_is_running(strategy, _scene())
    controller.set_strategy_snapshot(strategy.snapshot())
    snapshot = controller.snapshot()

    assert proven is True
    assert result["receipt"]["observed_at"] > 0.0
    assert decision.attempt_id not in snapshot["unknown_effects"]
    assert snapshot["unknown_effect_resolutions"][-1]["resolution"] == "completed"


def test_application_open_evidence_cannot_resolve_calculation_unknown(tmp_path):
    strategy, controller, calculation, _direct, _decision = (
        _durable_calculation_unknown(tmp_path)
    )
    result, proven = _typed_is_running(strategy, _scene())
    controller.set_strategy_snapshot(strategy.snapshot())

    assert isinstance(result, str)
    assert proven is False
    assert calculation.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
    assert "provider-calculation" in controller.snapshot()["unknown_effects"]


def test_wrong_application_cannot_emit_typed_calculator_open_evidence(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    result, proven = _typed_is_running(
        strategy, _scene(application="notepad"),
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    assert isinstance(result, str)
    assert proven is False
    assert decision.attempt_id in controller.snapshot()["unknown_effects"]


def test_stale_typed_calculator_observation_cannot_reconcile(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    dispatch = next(
        item for item in strategy.attempts
        if item.attempt_id == decision.attempt_id
    )
    result, proven = _typed_is_running(
        strategy, _scene(observed_at=dispatch.dispatched_at - 1.0),
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    assert isinstance(result, dict)
    assert proven is False
    assert decision.attempt_id in controller.snapshot()["unknown_effects"]


def test_ambiguous_uwp_host_process_alone_cannot_reconcile(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    result, proven = _typed_is_running(
        strategy, _scene(application="applicationframehost"),
    )

    assert isinstance(result, str)
    assert proven is False
    assert decision.attempt_id in controller.snapshot()["unknown_effects"]


def test_uwp_content_identity_can_prove_exact_calculator_open(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    result, proven = _typed_is_running(
        strategy, _scene(application="calculatorapp"),
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    assert proven is True
    assert result["receipt"]["observed_application"] == "calculatorapp"
    assert decision.attempt_id not in controller.snapshot()["unknown_effects"]


def test_untyped_is_running_true_is_insufficient_to_clear_unknown(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    proven = record_tool_observation(
        "is_running",
        "running: yes — process for 'Calculator'",
        is_error=False,
        strategy=strategy,
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    assert proven is False
    assert strategy.frozen_reason
    assert decision.attempt_id in controller.snapshot()["unknown_effects"]


def test_open_reconciliation_prevents_open_mechanism_replay(tmp_path):
    strategy, controller, decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    _result, proven = _typed_is_running(strategy, _scene())
    controller.set_strategy_snapshot(strategy.snapshot())
    replay = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )

    assert proven is True
    assert replay is not None and replay.allowed is False
    assert replay.reason_code in {
        "mechanism_retired_after_outcome_unknown",
        "no_pending_action_node",
        "prerequisite_effect_already_completed",
    }
    assert strategy.snapshot()["retired_effect_mechanisms"][
        decision.node_id + "|direct:open_application:calculator"
    ] == "outcome_unknown"
    assert decision.attempt_id not in controller.snapshot()["unknown_effects"]


def test_open_reconciliation_keeps_calculation_node_executable(tmp_path):
    strategy, controller, _decision, _agent = _record_agent_open_unknown(tmp_path)
    controller.set_strategy_snapshot(strategy.snapshot())
    _result, proven = _typed_is_running(strategy, _scene())
    controller.set_strategy_snapshot(strategy.snapshot())
    calculation = strategy.mission.action_nodes[1]
    native = strategy.mechanism_for_layer(
        StrategyLayer.NATIVE_ACCESSIBILITY,
        node_id=calculation.node_id,
    )

    assert proven is True
    assert strategy.preview(native, {"surface_ref": "digit-3"}).allowed is True
    assert controller.evaluate().next_strategy.startswith("native_accessibility")


def test_end_to_end_correlated_open_then_single_uia_expression_and_exact_result(
    tmp_path,
):
    strategy, controller, open_decision, _agent = _record_agent_open_unknown(
        tmp_path,
        turn_id="wor262-end-to-end-correlation",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    _running, open_proven = _typed_is_running(strategy, _scene())
    controller.set_strategy_snapshot(strategy.snapshot())
    assert open_proven is True
    assert controller.evaluate().terminal_state != "success_verified"

    calculation = strategy.mission.action_nodes[1]
    expression_dispatches = []
    selected_layers = []
    for index, label in enumerate(BUTTON_LABELS):
        call = {
            "surface_ref": f"calculator-button-{index}",
            "objective": label,
        }
        decision = tool_preflight(
            "activate_desktop_surface", call, strategy=strategy,
        )
        assert decision is not None and decision.allowed
        selected_layers.append(
            next(
                item.layer for item in strategy.attempts
                if item.attempt_id == decision.attempt_id
            )
        )
        record_tool_outcome(
            "activate_desktop_surface",
            decision,
            outcome="success_verified",
            is_error=False,
            reason_code="uia_invoke_completed",
            result={"surface_ref": call["surface_ref"], "invoked": True},
            strategy=strategy,
        )
    expression_dispatches.append(tuple(BUTTON_LABELS))
    assert controller.evaluate().terminal_state != "success_verified"

    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="10063"),
        ),
    ):
        exact = desktop_scene.inspect_desktop_surfaces("10063")
        result_proven = record_tool_observation(
            "inspect_desktop_surfaces",
            exact,
            is_error=False,
            strategy=strategy,
        )
    controller.set_strategy_snapshot(strategy.snapshot())
    snapshot = controller.snapshot()

    assert result_proven is True
    assert expression_dispatches == [BUTTON_LABELS]
    assert all(
        layer == int(StrategyLayer.NATIVE_ACCESSIBILITY)
        for layer in selected_layers
    )
    assert not any(
        item.layer in {
            int(StrategyLayer.KEYBOARD),
            int(StrategyLayer.LOCAL_VISION),
            int(StrategyLayer.GLOBAL_VISION),
        }
        and item.status == "selected"
        for item in strategy.attempts
    )
    assert sum(
        item.mechanism_id == "direct:run_command" and item.status != "blocked"
        for item in strategy.attempts
    ) <= 1
    assert tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    ).allowed is False
    assert exact["receipt"]["postcondition"] == (
        calculation.observable_success_postcondition
    )
    assert exact["receipt"]["observed_target"] == "10063"
    assert snapshot["unknown_effects"] == {}
    assert snapshot["unknown_effect_resolutions"][-1]["durable_attempt_id"] == (
        open_decision.attempt_id
    )
    assert controller.evaluate().terminal_state == "success_verified"


class _SingleBatchCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def create(self, **_kwargs):
        if self.calls:
            raise AssertionError("unexpected second provider invocation")
        self.calls += 1
        message = SimpleNamespace(content=self.content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call_payload(tool_name: str, arguments: dict) -> str:
    return "<tool_call>" + json.dumps({
        "tool": tool_name,
        **arguments,
    }) + "</tool_call>"


def _run_same_batch_after_open_unknown(
    tmp_path,
    followup_tool: str,
    followup_arguments: dict,
    *,
    observed_scene: DesktopScene | None = None,
    transient_unknown_tool: bool = False,
):
    from osia import agent as agent_module
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    content = "".join((
        _tool_call_payload("open_application", {"name": "Calculator"}),
        _tool_call_payload(followup_tool, followup_arguments),
    ))
    completions = _SingleBatchCompletions(content)
    agent = agent_module.OSIAAgent(model="wor262-gate", lang="en")
    agent.provider = "ollama"
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    agent._disable_hierarchy_for_turn = True
    strategy = _strategy()
    controller = TurnTerminalityController(
        turn_id="wor262-read-only-barrier-" + tmp_path.name,
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    agent._terminality_controller = controller
    agent._terminality_strategy = strategy
    agent._tool_attempt_metadata = {
        "turn_id": controller.turn_id,
        "conversation_id": "wor262-read-only-barrier",
        "provider_id": "ollama",
        "model_id": "wor262-gate",
    }
    executed = []

    def execute(tool_name, kwargs, **_options):
        executed.append(tool_name)
        if tool_name == "open_application":
            return _unknown_open_execution_record()
        if tool_name != "inspect_desktop_surfaces":
            raise AssertionError(
                f"mutating follow-up reached execution: {tool_name}"
            )
        result = desktop_scene.inspect_desktop_surfaces(
            str(kwargs.get("target") or "")
        )
        contract = effective_contract(
            get_tool_spec(tool_name), kwargs, TASK,
        )
        return ToolExecutionRecord(
            result,
            False,
            ExecutionOutcome.SUCCESS_VERIFIED,
            1,
            True,
            False,
            contract,
        )

    if transient_unknown_tool:
        agent_module.TOOL_MAP[followup_tool] = lambda: None
        agent_module.KNOWN_TOOL_NAMES.add(followup_tool)
    try:
        with (
            execution_strategy_scope(TASK, strategy=strategy),
            patch("osia.config.get_setting", return_value=False),
            patch(
                "osia.desktop_scene.observe_foreground_scene",
                side_effect=lambda **_kwargs: observed_scene or _scene(),
            ),
            patch("osia.agent._execute_tool_with_policy", side_effect=execute),
        ):
            final = agent._run_turn(TASK, history=[])
    finally:
        if transient_unknown_tool:
            agent_module.TOOL_MAP.pop(followup_tool, None)
            agent_module.KNOWN_TOOL_NAMES.discard(followup_tool)
    controller.set_strategy_snapshot(strategy.snapshot())
    return agent, strategy, controller, completions, executed, final


def test_same_batch_unknown_dispatches_structured_read_only_reconciliation(tmp_path):
    _agent, strategy, controller, completions, executed, _final = (
        _run_same_batch_after_open_unknown(
            tmp_path,
            "inspect_desktop_surfaces",
            {"target": "Calculator"},
        )
    )
    snapshot = controller.snapshot()

    assert completions.calls == 1
    assert executed == ["open_application", "inspect_desktop_surfaces"]
    assert snapshot["unknown_effects"] == {}
    assert len(snapshot["unknown_effect_resolutions"]) == 1
    assert snapshot["unknown_effect_resolutions"][0]["resolution"] == "completed"
    assert sum(
        item.mechanism_id == "direct:open_application:calculator"
        and item.status == "ambiguous_effect"
        for item in strategy.attempts
    ) == 1


def test_same_batch_read_only_observation_does_not_clear_without_exact_evidence(
    tmp_path,
):
    _agent, _strategy_state, controller, _provider, executed, _final = (
        _run_same_batch_after_open_unknown(
            tmp_path,
            "inspect_desktop_surfaces",
            {"target": "Paint"},
            observed_scene=_scene(),
        )
    )

    assert executed == ["open_application", "inspect_desktop_surfaces"]
    assert len(controller.snapshot()["unknown_effects"]) == 1
    assert controller.snapshot()["unknown_effect_resolutions"] == []


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    (
        (
            "activate_desktop_surface",
            {"surface_ref": "calculator-button-0", "objective": "Trois"},
        ),
        ("desktop_type", {"text": "347*29="}),
        ("run_command", {"command": "Start-Process Calculator"}),
        ("open_application", {"name": "Calculator"}),
    ),
)
def test_same_batch_unknown_blocks_every_canonical_mutation(
    tmp_path, tool_name, arguments,
):
    _agent, _strategy_state, controller, _provider, executed, _final = (
        _run_same_batch_after_open_unknown(
            tmp_path,
            tool_name,
            arguments,
        )
    )

    assert executed == ["open_application"]
    assert len(controller.snapshot()["unknown_effects"]) == 1


def test_same_batch_unknown_effect_classification_fails_closed_without_name_heuristic(
    tmp_path,
):
    from osia.tool_contracts import (
        SideEffectClass,
        compatibility_tool_spec,
        effective_contract,
    )

    tool_name = "inspect_but_semantics_unknown"
    _agent, _strategy_state, controller, _provider, executed, _final = (
        _run_same_batch_after_open_unknown(
            tmp_path,
            tool_name,
            {},
            transient_unknown_tool=True,
        )
    )
    spec = compatibility_tool_spec(tool_name, lambda: None)
    effective = effective_contract(spec, {}, TASK)

    assert spec.side_effect is SideEffectClass.UNKNOWN_HIGH_RISK
    assert effective.side_effect is SideEffectClass.UNKNOWN_HIGH_RISK
    assert effective.commits_fallback_barrier is True
    assert executed == ["open_application"]
    assert len(controller.snapshot()["unknown_effects"]) == 1


def test_same_batch_deadlock_regression_reaches_one_uia_expression_and_exact_result(
    tmp_path,
):
    _agent, strategy, controller, _provider, executed, _final = (
        _run_same_batch_after_open_unknown(
            tmp_path,
            "inspect_desktop_surfaces",
            {"target": "Calculator"},
        )
    )
    calculation = strategy.mission.action_nodes[1]
    uia_dispatches = []
    prior = "0"
    for index, label in enumerate(BUTTON_LABELS):
        current = "10 063" if index == len(BUTTON_LABELS) - 1 else str(index + 3)
        _decision, _attempt, _arguments = _start_stepwise_primitive(
            strategy, label, before_result=prior, expected_result=current,
        )
        target = "10063" if index == len(BUTTON_LABELS) - 1 else "reconcile"
        exact, reconciled = _observe_stepwise_primitive(
            strategy, current, target=target,
        )
        assert reconciled
        uia_dispatches.append(label)
        prior = current
    controller.set_strategy_snapshot(strategy.snapshot())

    assert executed == ["open_application", "inspect_desktop_surfaces"]
    assert uia_dispatches == list(BUTTON_LABELS)
    assert exact["receipt"]["postcondition"] == (
        calculation.observable_success_postcondition
    )
    assert exact["receipt"]["observed_target"] == "10063"
    assert controller.snapshot()["unknown_effects"] == {}
    assert controller.evaluate().terminal_state == "success_verified"
    assert not any(
        item.status == "progressed"
        and item.layer in {
            int(StrategyLayer.KEYBOARD),
            int(StrategyLayer.LOCAL_VISION),
            int(StrategyLayer.GLOBAL_VISION),
        }
        for item in strategy.attempts
    )


class _LocalProbeStore:
    def __init__(self, record: dict) -> None:
        self.record = dict(record)
        self.progress = []
        self.terminals = []

    def get_attempt(self, _attempt_id: str):
        return dict(self.record)

    def record_progress(self, _attempt_id: str, **values):
        self.progress.append(dict(values))
        return dict(self.record)

    def record_safe_alternative(self, _attempt_id: str, _strategy: str):
        attempted = self.record.setdefault("attempted_safe_alternatives", [])
        if _strategy not in attempted:
            attempted.append(_strategy)
        return True

    def record_terminal_result(
        self,
        _attempt_id: str,
        *,
        status,
        terminal_outcome: str,
        dispatch_state: str,
        reason_code: str,
    ):
        self.record.update({
            "status": getattr(status, "value", str(status)),
            "terminal_outcome": terminal_outcome,
            "dispatch_state": dispatch_state,
            "reason_code": reason_code,
        })
        self.terminals.append(dict(self.record))
        return dict(self.record), True


def _local_open_probe_harness(
    tmp_path,
    *,
    target_application: str | None = "calculatorapp",
    observed_at: float | None = None,
    probe_payload: dict | None = None,
    unrelated_unknowns: tuple[str, ...] = (),
    semantic_settle: bool = False,
):
    from osia import agent as agent_module
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    open_contract = effective_contract(
        get_tool_spec("open_application"), {"name": "Calculator"}, TASK,
    )
    attempt_id = "attempt_local_probe_" + tmp_path.name
    pending = ToolExecutionRecord(
        "PENDING: owner settled without a verified postcondition",
        True,
        ExecutionOutcome.PENDING,
        1,
        True,
        False,
        open_contract,
        attempt_id,
        False,
        "postcondition_not_observed",
    )
    record_tool_outcome(
        "open_application",
        decision,
        outcome="pending",
        is_error=True,
        reason_code="postcondition_not_observed",
        result=pending.result,
        strategy=strategy,
    )
    controller = TurnTerminalityController(
        turn_id="wor262-local-probe-" + tmp_path.name,
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_execution_record(
        "open_application",
        pending,
        postcondition=strategy.mission.action_nodes[0].observable_success_postcondition,
    )
    for extra in unrelated_unknowns:
        controller.record_typed_evidence(
            outcome="outcome_unknown",
            evidence={"source": extra},
            postcondition=strategy.mission.action_nodes[1].observable_success_postcondition,
            attempt_id=extra,
            reason_code="unrelated_unknown",
        )
    controller.set_strategy_snapshot(strategy.snapshot())

    agent = agent_module.OSIAAgent(model="wor262-local-probe", lang="en")
    agent.provider = "ollama"
    agent.client = Mock(name="provider_client")
    agent._terminality_controller = controller
    agent._terminality_strategy = strategy
    agent._pending_tool_runtime = {
        "attempt_id": attempt_id,
        "tool_name": "open_application",
        "wait_spec": None,
        "worker_may_be_active": False,
        "record": pending,
        "attached_existing": False,
    }
    durable = {
        "tool_attempt_id": attempt_id,
        "status": "waiting_verification",
        "dispatch_state": "owner_settled_unverified",
        "side_effect": "reversible_local",
        "hard_deadline_at": time.time() + 30.0,
        "inactivity_deadline_at": time.time() + 20.0,
    }
    if semantic_settle:
        durable.update({
            "semantic_settle": {
                "state": "active",
                "checkpoint_id": "substep-local-probe",
            },
            "primitive_journal": [{
                "effect_role": "semantic",
                "application_identity": "calculator",
                "surface_ref": "surface-local-probe",
                "native_window_id": "592338",
                "window_generation": "calculator-generation-1",
            }],
        })
    store = _LocalProbeStore(durable)
    foreground = _named_scene(
        "robloxplayerbeta",
        "Roblox",
        rich=False,
        foreground=True,
        key="local-probe-roblox",
        observed_at=time.time() + 1.0,
    )
    target = None
    if target_application is not None:
        target = _named_scene(
            target_application,
            "Calculator" if "calculator" in target_application else "Paint",
            rich=True,
            foreground=False,
            key="local-probe-target",
            host_application=(
                "applicationframehost"
                if target_application == "calculatorapp" else target_application
            ),
            observed_at=(time.time() + 2.0 if observed_at is None else observed_at),
        )
    selection = _target_selection(
        foreground,
        target,
        status="exact_target" if target is not None else "target_not_found",
    )

    stack = [
        patch("osia.tool_attempts.get_tool_attempt_store", return_value=store),
        patch("osia.desktop_scene._select_desktop_target", return_value=selection),
    ]
    if probe_payload is not None:
        observer_contract = effective_contract(
            get_tool_spec("inspect_desktop_surfaces"),
            {"target": "Calculator"},
            TASK,
        )
        stack.append(patch(
            "osia.agent._execute_tool_with_policy",
            return_value=ToolExecutionRecord(
                probe_payload,
                False,
                ExecutionOutcome.SUCCESS_VERIFIED,
                1,
                True,
                False,
                observer_contract,
            ),
        ))
    with ExitStack() as active_patches:
        for item in stack:
            active_patches.enter_context(item)
        result = agent._verify_suspended_tool(attempt_id)
    return agent, strategy, controller, decision, store, result


def test_local_reconciliation_01_executes_without_provider_request(tmp_path):
    agent, _strategy_state, _controller, _decision, store, result = (
        _local_open_probe_harness(tmp_path)
    )

    assert result is not None and result.outcome.value == "success_verified"
    assert agent.client.mock_calls == []
    assert any(
        item.get("phase") == "local_reconciliation_probe_dispatched"
        for item in store.progress
    )


def test_local_reconciliation_01b_settle_probe_returns_typed_local_receipt(
    tmp_path,
):
    from osia.tool_attempts import (
        SemanticSettleProbeClassification,
        SemanticSettleProbeResult,
    )

    agent, _strategy_state, _controller, _decision, _store, result = (
        _local_open_probe_harness(tmp_path, semantic_settle=True)
    )

    assert isinstance(result, SemanticSettleProbeResult)
    assert result.classification is (
        SemanticSettleProbeClassification.COMPLETED_MATCH
    )
    assert result.observer_mechanism == "inspect_desktop_surfaces"
    assert result.probe_invoked is True
    assert result.observation_completed_at >= result.observation_started_at
    assert result.evidence_ref.startswith("sha256:")
    assert result.terminal_payload.outcome.value == "success_verified"
    assert agent.client.mock_calls == []


def test_local_reconciliation_02_selector_uses_semantic_application_open_relation():
    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="unknown", result="unknown", strategy=strategy,
    )

    probe = select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
    )

    assert probe["semantic_effect"] == "application_open"
    assert probe["arguments"] == {"target": "calculator"}


def test_local_reconciliation_03_records_normal_attributable_observer_receipt(tmp_path):
    _agent, _strategy_state, controller, _decision, _store, _result = (
        _local_open_probe_harness(tmp_path)
    )
    evidence = controller.snapshot()["typed_evidence"]

    assert any(
        item["reason_code"] == "local_reconciliation_probe"
        and item["outcome"] == "success_verified"
        for item in evidence
    )


def test_local_reconciliation_04_preserves_original_unknown_attempt_correlation(tmp_path):
    _agent, _strategy_state, controller, decision, _store, result = (
        _local_open_probe_harness(tmp_path)
    )
    resolution = controller.snapshot()["unknown_effect_resolutions"][-1]

    assert result.tool_attempt_id == resolution["durable_attempt_id"]
    assert resolution["strategy_attempt_id"] == decision.attempt_id


def test_local_reconciliation_05_exact_target_resolves_matching_open_completed(tmp_path):
    _agent, strategy, controller, decision, store, _result = (
        _local_open_probe_harness(tmp_path)
    )

    assert not strategy.frozen_reason
    assert decision.attempt_id == controller.snapshot()[
        "unknown_effect_resolutions"
    ][-1]["strategy_attempt_id"]
    assert store.terminals[-1]["terminal_outcome"] == "success_verified"


def test_local_reconciliation_06_completed_open_never_replays_original_mutation(tmp_path):
    _agent, strategy, _controller, _decision, _store, _result = (
        _local_open_probe_harness(tmp_path)
    )
    replay = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )

    assert replay is not None and replay.allowed is False
    assert sum(
        item.mechanism_id == "direct:open_application:calculator"
        and item.status == "ambiguous_effect"
        for item in strategy.attempts
    ) == 1


def test_local_reconciliation_07_strong_exact_absence_resolves_not_applied(tmp_path):
    strategy = _strategy()
    postcondition = strategy.mission.action_nodes[0].observable_success_postcondition
    observed_at = time.time() + 5.0
    payload = {
        "text": "complete exact Desktop enumeration proves the effect absent",
        "scene": {
            "application": "calculator",
            "requested_application": "calculator",
            "window_surface_ref": "calculator-window",
            "window_generation": "win-calculator",
            "structural_epoch": 9,
            "target_resolution": "exact_target",
            "target_foreground": False,
            "foreground_application": "robloxplayerbeta",
        },
        "receipt": {
            "schema_version": 1,
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "dispatch_state": "effect_absent",
            "outcome": "confirmed_absence",
            "reason_code": "exact_postcondition_absent_no_partial_effect",
            "postcondition": postcondition,
            "observed_target": "Calculator",
            "observed_at": observed_at,
            "no_partial_effect": True,
        },
    }
    _agent, strategy, controller, _decision, store, result = (
        _local_open_probe_harness(tmp_path, probe_payload=payload)
    )

    assert result.outcome.value == "confirmed_failure_before_effect"
    assert controller.snapshot()["unknown_effect_resolutions"][-1][
        "resolution"
    ] == "not_applied"
    assert store.terminals[-1]["reason_code"] == "local_reconciliation_not_applied"
    assert not strategy.frozen_reason


def test_local_reconciliation_08_incomplete_target_observation_remains_unknown(tmp_path):
    _agent, strategy, controller, _decision, store, result = (
        _local_open_probe_harness(tmp_path, target_application=None)
    )

    assert result is None
    assert strategy.frozen_reason
    assert controller.snapshot()["unknown_effects"]
    assert store.terminals == []


def test_local_reconciliation_09_stale_observation_remains_unknown(tmp_path):
    old = time.time() - 60.0
    _agent, strategy, controller, _decision, _store, result = (
        _local_open_probe_harness(tmp_path, observed_at=old)
    )

    assert result is None
    assert strategy.frozen_reason
    assert controller.snapshot()["unknown_effect_resolutions"] == []


def test_local_reconciliation_10_wrong_target_observation_remains_unknown(tmp_path):
    _agent, strategy, controller, _decision, _store, result = (
        _local_open_probe_harness(tmp_path, target_application="paint")
    )

    assert result is None
    assert strategy.frozen_reason
    assert controller.snapshot()["unknown_effect_resolutions"] == []


def test_local_reconciliation_11_read_only_contract_is_eligible():
    from osia.tool_contracts import SideEffectClass, effective_contract, get_tool_spec

    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="unknown", result="unknown", strategy=strategy,
    )
    probe = select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
    )
    contract = effective_contract(
        get_tool_spec(probe["tool"]), probe["arguments"], TASK,
    )

    assert contract.side_effect is SideEffectClass.READ_ONLY


@pytest.mark.parametrize(
    "effect",
    (
        pytest.param("unknown_high_risk", id="unknown-high-risk"),
        pytest.param("dynamically_classified", id="dynamic"),
        pytest.param("reversible_local", id="mutating"),
    ),
)
def test_local_reconciliation_12_13_non_read_only_observer_is_ineligible(effect):
    from osia.tool_contracts import SideEffectClass, get_tool_spec

    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="unknown", result="unknown", strategy=strategy,
    )
    original = get_tool_spec("inspect_desktop_surfaces")
    unsafe = replace(original, side_effect=SideEffectClass(effect))
    with patch("osia.tool_contracts.get_tool_spec", return_value=unsafe):
        probe = select_local_reconciliation_probe(
            strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
        )

    assert probe is None


def test_local_reconciliation_14_similarly_named_unknown_tool_is_not_eligible():
    from osia import execution_strategy as strategy_module

    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="unknown", result="unknown", strategy=strategy,
    )
    with patch.dict(
        strategy_module._RECONCILIATION_OBSERVERS,
        {"application_open": ("inspect_desktop_effect",)},
    ):
        probe = select_local_reconciliation_probe(
            strategy, {"inspect_desktop_effect"}, user_request=TASK,
        )

    assert probe is None


def test_local_reconciliation_15_probe_cannot_complete_calculation_node(tmp_path):
    _agent, strategy, controller, _decision, _store, _result = (
        _local_open_probe_harness(tmp_path)
    )
    calculation = strategy.mission.action_nodes[1]

    assert calculation.status == "pending"
    assert calculation.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
    assert controller.evaluate().terminal_state != "success_verified"


def test_local_reconciliation_16_application_open_evidence_cannot_prove_10063(tmp_path):
    _agent, strategy, controller, _decision, _store, _result = (
        _local_open_probe_harness(tmp_path)
    )

    assert not any(
        "10063" in postcondition
        for postcondition in strategy.proven_postconditions
    )
    assert any("10063" in item for item in controller.snapshot()["remaining_work"])


def test_local_reconciliation_17_unrelated_unknown_effect_remains_untouched(tmp_path):
    _agent, _strategy_state, controller, _decision, _store, _result = (
        _local_open_probe_harness(
            tmp_path,
            unrelated_unknowns=("unrelated-calculation",),
        )
    )

    assert tuple(controller.snapshot()["unknown_effects"]) == (
        "unrelated-calculation",
    )


def test_local_reconciliation_18_multiple_unknowns_correlate_only_exact_effect(tmp_path):
    extras = ("unrelated-one", "unrelated-two")
    _agent, _strategy_state, controller, _decision, _store, _result = (
        _local_open_probe_harness(tmp_path, unrelated_unknowns=extras)
    )
    snapshot = controller.snapshot()

    assert set(snapshot["unknown_effects"]) == set(extras)
    assert len(snapshot["unknown_effect_resolutions"]) == 1


def test_local_reconciliation_19_provider_prose_cannot_replace_local_evidence():
    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="unknown", result="unknown", strategy=strategy,
    )

    strategy.observe("Provider says Calculator opened successfully")

    assert strategy.frozen_reason
    assert strategy.mission.action_nodes[0].status == "unknown"


def test_local_reconciliation_20_provider_exit_success_is_not_exact_desktop_evidence():
    strategy = _strategy()
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    record_tool_outcome(
        "open_application", decision,
        outcome="outcome_unknown", is_error=True,
        reason_code="unknown", result="provider exit code 0", strategy=strategy,
    )

    proven = record_tool_observation(
        "is_running",
        "provider-native process exit code: 0",
        is_error=False,
        strategy=strategy,
    )

    assert proven is False
    assert strategy.frozen_reason


def test_local_reconciliation_full_calculator_sequence_without_provider_inspection(
    tmp_path,
):
    agent, strategy, controller, _decision, _store, open_result = (
        _local_open_probe_harness(tmp_path)
    )
    roblox, calculator = _reacquisition_world()
    _dispatch_target_window_focus(strategy, roblox, calculator)
    _ignored, focused = _reacquisition_world(target_foreground=True)
    _focus_result, focused_exact = _record_target_observation(
        strategy,
        _target_selection(focused, focused),
        target="reconcile",
    )
    prior = "0"
    primitive_count = 0
    primitive_reconciliation_count = 0
    for index, label in enumerate(BUTTON_LABELS):
        after = "10 063" if index == len(BUTTON_LABELS) - 1 else str(index + 3)
        _start_stepwise_primitive(
            strategy,
            label,
            before_result=prior,
            expected_result=after,
        )
        exact, reconciled = _observe_stepwise_primitive(
            strategy,
            after,
            target="10063" if index == len(BUTTON_LABELS) - 1 else "reconcile",
        )
        primitive_count += 1
        primitive_reconciliation_count += int(reconciled)
        prior = after
    controller.set_strategy_snapshot(strategy.snapshot())

    assert agent.client.mock_calls == []
    assert open_result.outcome.value == "success_verified"
    assert focused_exact is True
    assert primitive_count == len(BUTTON_LABELS)
    assert primitive_reconciliation_count == len(BUTTON_LABELS)
    assert exact["receipt"]["observed_target"] == "10063"
    assert controller.snapshot()["unknown_effects"] == {}
    assert controller.evaluate().terminal_state == "success_verified"
    assert not any(
        item.layer in {
            int(StrategyLayer.KEYBOARD),
            int(StrategyLayer.LOCAL_VISION),
            int(StrategyLayer.GLOBAL_VISION),
        }
        for item in strategy.attempts
    )


def _start_stepwise_primitive(
    strategy: ExecutionStrategy,
    label: str,
    *,
    before_result: str = "0",
    expected_result: str | None = None,
):
    scene = _scene(result=before_result, observed_at=time.time() - 1.0)
    with (
        execution_strategy_scope(strategy.mission.objective, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=scene,
        ),
    ):
        fresh = desktop_scene.inspect_desktop_surfaces("reconcile")
        record_tool_observation(
            "inspect_desktop_surfaces",
            fresh,
            is_error=False,
            strategy=strategy,
        )
    target = next(
        item for item in scene.surfaces if item.accessible_label == label
    )
    if expected_result is None:
        if label == BUTTON_LABELS[0]:
            expected_result = "3"
        elif label == BUTTON_LABELS[1]:
            expected_result = before_result + "4"
        elif label == BUTTON_LABELS[2]:
            expected_result = before_result + "7"
        elif label == BUTTON_LABELS[3]:
            expected_result = before_result + "×"
        elif label == BUTTON_LABELS[4]:
            expected_result = "2" if before_result.endswith("×") else before_result + "2"
        elif label == BUTTON_LABELS[5]:
            expected_result = before_result + "9"
        else:
            expected_result = (
                "10 063" if before_result == "347×29" else before_result + "="
            )
    observable_effect = {
        "kind": "exact_surface_text",
        "surface_ref": "calculator-display",
        "expected_text": (
            expected_result
            if re.fullmatch(r"[\d\s\u00a0\u202f']+", expected_result)
            else "L’affichage est " + expected_result
        ),
    }
    arguments = {
        "surface_ref": target.ref_id,
        "objective": label,
        "action_pattern": "Invoke",
        "observable_effect": observable_effect,
    }
    decision = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)
    primitive = {
        "kind": "desktop_uia_primitive",
        "mechanism": "invoke",
        "observable_effect": observable_effect,
        "dispatched_at": attempt.dispatched_at,
        "before": desktop_scene._primitive_state_projection(scene, target),
    }
    result = {
        "text": "Desktop surface activated through native accessibility.",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "dispatched",
            "outcome": "success_unverified",
            "reason_code": "native_accessibility_dispatched",
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


def _observe_stepwise_primitive(
    strategy: ExecutionStrategy,
    after_result: str,
    *,
    target: str = "reconcile",
    application: str = "calculator",
):
    scene = _scene(
        result=after_result,
        application=application,
        observed_at=time.time() + 1.0,
    )
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=scene,
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces(target)
        reconciled = record_tool_observation(
            "inspect_desktop_surfaces",
            result,
            is_error=False,
            strategy=strategy,
        )
    return result, reconciled


def _stepwise_strategy() -> ExecutionStrategy:
    strategy = _strategy()
    _complete_open(strategy)
    return strategy


def test_stepwise_01_composite_only_first_primitive_is_initially_dispatchable():
    strategy = _stepwise_strategy()
    _first, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    second = tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "calculator-button-1", "objective": BUTTON_LABELS[1]},
        strategy=strategy,
    )
    assert attempt.composite_id == strategy.mission.action_nodes[1].node_id
    assert attempt.substep_index == 1
    assert second is not None and not second.allowed


def test_stepwise_02_success_unverified_keeps_second_primitive_blocked():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    blocked = tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "calculator-button-1", "objective": BUTTON_LABELS[1]},
        strategy=strategy,
    )
    assert strategy.frozen_reason
    assert blocked.reason_code == "ambiguous_effect_requires_reconciliation"


def test_stepwise_03_read_only_exact_observation_is_eligible_under_barrier():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    assert tool_preflight(
        "inspect_desktop_surfaces", {"target": "reconcile"}, strategy=strategy,
    ) is None


def test_stepwise_04_exact_observation_resolves_only_first_primitive():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    receipt, reconciled = _observe_stepwise_primitive(strategy, "3")
    calculation = strategy.mission.action_nodes[1]
    assert reconciled is True
    assert receipt["receipt"]["reason_code"] == (
        "exact_desktop_observable_effect_change"
    )
    assert len(strategy._completed_substeps) == 1
    assert calculation.observable_success_postcondition not in (
        strategy.proven_postconditions
    )


def test_stepwise_05_second_primitive_becomes_eligible_after_first_proof():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    _observe_stepwise_primitive(strategy, "3")
    second = tool_preflight(
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
    assert second is not None and second.allowed
    assert strategy.attempt_by_id(second.attempt_id).substep_index == 2


def test_stepwise_06_ambiguous_observation_keeps_next_primitive_blocked():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    receipt, reconciled = _observe_stepwise_primitive(strategy, "0")
    assert receipt["receipt"]["outcome"] == "observed"
    assert reconciled is False
    assert strategy.frozen_reason


def test_stepwise_07_wrong_target_partial_state_never_unlocks_progression():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    _receipt, reconciled = _observe_stepwise_primitive(
        strategy, "3", application="notepad",
    )
    assert reconciled is False
    assert strategy.frozen_reason


def test_stepwise_08_exact_not_applied_reuses_substep_under_retry_rules():
    strategy = _stepwise_strategy()
    _decision, attempt, arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    assert strategy.reconcile(
        succeeded=False,
        evidence="exact_no_effect",
        primitive_only=True,
        evidence_details={
            "evidence_kind": "exact_independent_desktop_state",
            "observed_at": time.time() + 1.0,
            "composite_id": attempt.composite_id,
            "substep_id": attempt.substep_id,
            "postcondition": attempt.primitive_postcondition,
            "observable_effect": attempt.observable_effect,
            "no_partial_effect": True,
        },
    )
    _observe_stepwise_primitive(strategy, "0")
    retry = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    retried = strategy.attempt_by_id(retry.attempt_id)
    assert retry.allowed
    assert retried.substep_id == attempt.substep_id
    assert not strategy._completed_substeps


def test_stepwise_09_two_completed_primitives_make_third_ordered():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0], before_result="0")
    _observe_stepwise_primitive(strategy, "3")
    _start_stepwise_primitive(strategy, BUTTON_LABELS[1], before_result="3")
    _observe_stepwise_primitive(strategy, "34")
    third = tool_preflight(
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
    assert third.allowed
    assert strategy.attempt_by_id(third.attempt_id).substep_index == 3


def test_stepwise_10_completed_primitive_cannot_execute_twice():
    strategy = _stepwise_strategy()
    _first, _attempt, arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    _observe_stepwise_primitive(strategy, "3")
    replay = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert replay is not None and not replay.allowed
    assert replay.reason_code == "primitive_already_completed"


def test_stepwise_11_snapshot_restores_substep_progression_and_barrier():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    _observe_stepwise_primitive(strategy, "3")
    _start_stepwise_primitive(
        strategy, BUTTON_LABELS[1], before_result="3",
    )
    restored = ExecutionStrategy.from_snapshot(strategy.snapshot())
    assert restored.frozen_reason == strategy.frozen_reason
    assert restored._completed_substeps == strategy._completed_substeps
    assert restored._substep_counters == strategy._substep_counters
    assert restored.attempt_by_id(restored._frozen_attempt_id).substep_index == 2


def test_stepwise_12_unrelated_unknown_does_not_corrupt_substep_order(tmp_path):
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[0])
    _observe_stepwise_primitive(strategy, "3")
    controller = TurnTerminalityController(
        turn_id="wor262-unrelated-unknown",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence="foreign",
        postcondition="unrelated external postcondition",
        attempt_id="foreign-attempt",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    second, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[1], before_result="3",
    )
    assert second.allowed and attempt.substep_index == 2
    assert "foreign-attempt" in controller.snapshot()["unknown_effects"]


def test_stepwise_13_success_is_impossible_with_unresolved_primitive(tmp_path):
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    controller = TurnTerminalityController(
        turn_id="wor262-unresolved-primitive",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="success_unverified",
        evidence="uia-dispatched",
        postcondition=attempt.primitive_postcondition,
        attempt_id=attempt.attempt_id,
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    assert strategy.succeeded is False
    assert controller.evaluate().terminal_state != "success_verified"


def test_stepwise_correlated_primitive_resolution_clears_only_its_unknown(tmp_path):
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    controller = TurnTerminalityController(
        turn_id="wor262-correlated-primitive",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="success_unverified",
        evidence="uia-dispatched",
        postcondition=attempt.primitive_postcondition,
        attempt_id=attempt.attempt_id,
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    bound = controller.snapshot()["unknown_effects"][attempt.attempt_id]
    assert bound["substep_id"] == attempt.substep_id

    _observe_stepwise_primitive(strategy, "3")
    controller.set_strategy_snapshot(strategy.snapshot())
    state = controller.snapshot()
    assert state["unknown_effects"] == {}
    assert state["remaining_work"]
    assert state["terminal_state"] == ""


def test_stepwise_same_batch_never_preselects_second_mutation(tmp_path):
    from osia import agent as agent_module
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    calls = (
        _tool_call_payload(
            "activate_desktop_surface",
            {
                "surface_ref": "calculator-button-0",
                "objective": BUTTON_LABELS[0],
                "action_pattern": "Invoke",
                "observable_effect": {
                    "kind": "exact_surface_text",
                    "surface_ref": "calculator-display",
                    "expected_text": "3",
                },
            },
        )
        + _tool_call_payload(
            "inspect_desktop_surfaces", {"target": "reconcile"},
        )
        + _tool_call_payload(
            "activate_desktop_surface",
            {
                "surface_ref": "calculator-button-1",
                "objective": BUTTON_LABELS[1],
                "action_pattern": "Invoke",
                "observable_effect": {
                    "kind": "exact_surface_text",
                    "surface_ref": "calculator-display",
                    "expected_text": "34",
                },
            },
        )
    )
    completions = _SingleBatchCompletions(calls)
    agent = agent_module.OSIAAgent(model="wor262-stepwise", lang="en")
    agent.provider = "ollama"
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    agent._disable_hierarchy_for_turn = True
    strategy = _stepwise_strategy()
    controller = TurnTerminalityController(
        turn_id="wor262-same-batch-stepwise",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    agent._terminality_controller = controller
    agent._terminality_strategy = strategy
    executed = []

    def execute(tool_name, kwargs, **_options):
        executed.append(tool_name)
        contract = effective_contract(get_tool_spec(tool_name), kwargs, TASK)
        if tool_name == "activate_desktop_surface":
            before = _scene(result="0", observed_at=time.time() - 1.0)
            target = next(
                item for item in before.surfaces
                if item.ref_id == kwargs["surface_ref"]
            )
            attempt = strategy.attempt_by_id(strategy._frozen_attempt_id) or (
                strategy.attempts[-1]
            )
            result = {
                "text": "Desktop surface activated through native accessibility.",
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "dispatched",
                    "outcome": "success_unverified",
                    "reason_code": "native_accessibility_dispatched",
                    "primitive": {
                        "kind": "desktop_uia_primitive",
                        "mechanism": "invoke",
                        "observable_effect": kwargs["observable_effect"],
                        "dispatched_at": attempt.dispatched_at,
                        "before": desktop_scene._primitive_state_projection(
                            before, target,
                        ),
                    },
                },
            }
            return ToolExecutionRecord(
                result, False, ExecutionOutcome.SUCCESS_UNVERIFIED,
                1, True, False, contract,
            )
        result = desktop_scene.inspect_desktop_surfaces(
            str(kwargs.get("target") or "")
        )
        return ToolExecutionRecord(
            result, False, ExecutionOutcome.SUCCESS_VERIFIED,
            1, True, False, contract,
        )

    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch("osia.config.get_setting", return_value=False),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(result="3", observed_at=time.time() + 1.0),
        ),
        patch("osia.agent._execute_tool_with_policy", side_effect=execute),
    ):
        agent._run_turn(TASK, history=[])
    controller.set_strategy_snapshot(strategy.snapshot())

    native_attempts = [
        item for item in strategy.attempts
        if item.layer == int(StrategyLayer.NATIVE_ACCESSIBILITY)
    ]
    assert executed == [
        "activate_desktop_surface", "inspect_desktop_surfaces",
    ]
    assert len(native_attempts) == 1
    assert native_attempts[0].status == "progressed"
    assert controller.snapshot()["unknown_effects"] == {}


def test_stepwise_14_final_primitive_without_final_evidence_is_not_success():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[-1], before_result="1006")
    _observe_stepwise_primitive(strategy, "1006=")
    assert strategy._completed_substeps[-1]["substep_index"] == 1
    assert strategy.succeeded is False


def test_stepwise_15_fresh_exact_final_evidence_enables_success():
    strategy = _stepwise_strategy()
    _start_stepwise_primitive(strategy, BUTTON_LABELS[-1], before_result="347×29")
    receipt, reconciled = _observe_stepwise_primitive(
        strategy, "10 063", target="10063",
    )
    assert reconciled is True
    assert receipt["receipt"]["reason_code"] == "exact_final_state_observation"
    assert strategy.succeeded is True


def test_relevance_16_document_query_is_ineligible_for_desktop_calculation():
    strategy = _stepwise_strategy()
    decision = tool_preflight(
        "query_document",
        {"document_id": "doc_" + "a" * 32, "operation": "summary"},
        strategy=strategy,
    )
    assert decision is not None and not decision.allowed
    assert decision.reason_code == "tool_capability_not_applicable_to_semantic_node"


def test_relevance_17_inspect_name_without_capability_is_not_eligible():
    strategy = _stepwise_strategy()
    decision = tool_preflight(
        "inspect_something", {}, strategy=strategy,
    )
    assert decision is not None and not decision.allowed


def test_relevance_18_exact_desktop_observation_is_eligible():
    strategy = _stepwise_strategy()
    assert tool_preflight(
        "inspect_desktop_surfaces", {"target": "Calculator"}, strategy=strategy,
    ) is None


def test_relevance_19_uia_actuation_is_eligible_for_desktop_node():
    strategy = _stepwise_strategy()
    decision = tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "calculator-button-0", "objective": BUTTON_LABELS[0]},
        strategy=strategy,
    )
    assert decision is not None and decision.allowed


def test_relevance_20_unrelated_read_only_capability_remains_ineligible():
    strategy = _stepwise_strategy()
    decision = tool_preflight(
        "read_file", {"path": "bounded.txt"}, strategy=strategy,
    )
    assert decision is not None and not decision.allowed


def test_relevance_21_valid_document_goal_keeps_document_query_eligible():
    strategy = build_execution_strategy(
        "Query the document summary.", {"query_document"},
    )
    assert tool_preflight(
        "query_document",
        {"document_id": "doc_" + "b" * 32, "operation": "summary"},
        strategy=strategy,
    ) is None


def test_vision_22_rich_uia_blocks_screenshot_selection():
    strategy = _stepwise_strategy()
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces(BUTTON_LABELS[0])
        record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )
    decision = tool_preflight("take_screenshot", {}, strategy=strategy)
    assert decision is not None and not decision.allowed
    assert decision.reason_code == "structured_uia_evidence_sufficient"


def test_vision_23_poor_uia_preserves_targeted_visual_observation():
    strategy = _stepwise_strategy()
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=_scene(rich=False),
        ),
    ):
        observed = desktop_scene.inspect_desktop_surfaces("target")
        record_tool_observation(
            "inspect_desktop_surfaces", observed,
            is_error=False, strategy=strategy,
        )
    blocked = tool_preflight(
        "request_visual_observation",
        {
            "surface_ref": "calculator-window",
            "reason": "incomplete_accessibility",
            "target": "target",
        },
        strategy=strategy,
    )
    assert blocked is not None and blocked.allowed is False
    assert blocked.reason_code == "keyboard_frontier_not_exhausted"


def test_vision_24_non_vision_model_never_starts_image_capture():
    scene = _scene(rich=False)
    token = desktop_scene.bind_effective_model("groq", "llama-3.3-70b")
    try:
        with patch(
            "osia.desktop_scene.request_visual_observation",
        ) as request_visual:
            result = desktop_scene._pre_dispatch_failure(
                "ERROR: unavailable",
                "uia_action_pattern_unavailable",
                scene=scene,
                target="target",
            )
        request_visual.assert_not_called()
    finally:
        desktop_scene.reset_effective_model(token)
    assert result["receipt"]["adaptive_visual_observation"] == (
        "effective_model_non_vision"
    )


def _reacquisition_world(*, target_foreground: bool = False, rich: bool = True):
    roblox = _named_scene(
        "robloxplayerbeta", "Roblox", rich=False, foreground=not target_foreground,
        key="roblox", observed_at=time.time() + 1.0,
    )
    roblox = replace(roblox, uia_tree_observed=True)
    calculator = _named_scene(
        "calculatorapp", "Calculator", rich=rich, foreground=target_foreground,
        key="calculator-target", host_application="applicationframehost",
        observed_at=time.time() + 2.0,
    )
    return roblox, calculator


def _dispatch_target_window_focus(
    strategy: ExecutionStrategy,
    roblox: DesktopScene,
    calculator: DesktopScene,
):
    native = NativeSurfaceIdentity(
        pid=4242,
        process_started_at=10.0,
        native_window_id=10101,
        class_name="ApplicationFrameWindow",
    )
    resolved = SurfaceResolution(
        "resolved", calculator.window_ref, native,
    )
    arguments = {
        "surface_ref": calculator.window_ref.ref_id,
        "objective": calculator.window_ref.accessible_label,
    }
    decision = tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    with (
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "resolve",
            return_value=resolved,
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=native,
        ),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_target_selection(roblox, calculator),
        ),
        patch(
            "osia.tools.desktop_control.focus_app_window",
            return_value=("ready", "Calculator"),
        ),
    ):
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
    return decision, result


def _record_target_observation(
    strategy: ExecutionStrategy,
    selection: desktop_scene.TargetSurfaceSelection,
    *,
    target: str,
):
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=selection,
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces(target)
        reconciled = record_tool_observation(
            "inspect_desktop_surfaces", result,
            is_error=False, strategy=strategy,
        )
    return result, reconciled


def test_reacquisition_01_background_calculator_beats_foreground_roblox():
    roblox, calculator = _reacquisition_world()
    selected = desktop_scene._resolve_target_scene(
        (roblox, calculator),
        "Calculator",
        foreground_scene=roblox,
    )
    assert selected.status == "exact_target"
    assert selected.target_scene is calculator
    assert selected.identity_evidence == "uia_content_application:calculatorapp"


def test_reacquisition_01b_target_first_enumeration_observes_background_uwp():
    roblox, calculator = _reacquisition_world()
    with (
        patch(
            "osia.desktop_scene.observe_foreground_scene",
            return_value=roblox,
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=None,
        ),
        patch(
            "osia.tools.desktop_control._enum_app_windows_win32",
            return_value=[
                (20202, "Calculator", 5252, "ApplicationFrameWindow"),
            ],
        ),
        patch(
            "osia.desktop_scene._process_details",
            return_value=("applicationframehost", 20.0),
        ),
        patch(
            "osia.desktop_scene._observe_window_scene",
            return_value=calculator,
        ) as observe_window,
    ):
        selected = desktop_scene._select_desktop_target("Calculator")
    assert selected.status == "exact_target"
    assert selected.target_scene is calculator
    observe_window.assert_called_once()


def test_reacquisition_02_non_target_quality_does_not_contaminate_target():
    roblox, calculator = _reacquisition_world()
    strategy = _strategy()
    observed, _ = _record_target_observation(
        strategy,
        _target_selection(roblox, calculator),
        target=BUTTON_LABELS[0],
    )
    assert roblox.accessibility_quality("Calculator").value == "window_only"
    assert observed["scene"]["accessibility_quality"] == "rich_target_resolved"
    assert observed["scene"]["foreground_application"] == "robloxplayerbeta"


def test_reacquisition_03_roblox_window_only_never_unlocks_vision():
    roblox, calculator = _reacquisition_world()
    strategy = _strategy()
    _record_target_observation(
        strategy, _target_selection(roblox, calculator), target="Calculator",
    )
    decision = tool_preflight("take_screenshot", {}, strategy=strategy)
    assert decision is not None and not decision.allowed
    assert decision.reason_code == "target_focus_verification_required_before_vision"


def test_reacquisition_04_background_target_proves_application_open():
    roblox, calculator = _reacquisition_world()
    strategy = _strategy()
    observed, _ = _record_target_observation(
        strategy, _target_selection(roblox, calculator), target="Calculator",
    )
    assert observed["receipt"]["semantic_postcondition"] == "application_open"
    assert observed["receipt"]["outcome"] == "confirmed_success"
    assert strategy.mission.action_nodes[0].status == "succeeded"


def test_reacquisition_05_background_target_is_not_foreground_until_activated():
    roblox, calculator = _reacquisition_world()
    strategy = _strategy()
    observed, _ = _record_target_observation(
        strategy, _target_selection(roblox, calculator), target="Calculator",
    )
    assert observed["scene"]["target_foreground"] is False
    assert observed["scene"]["application"] == "calculatorapp"


def test_reacquisition_06_focus_dispatch_requires_read_only_reobservation():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    _decision, result = _dispatch_target_window_focus(
        strategy, roblox, calculator,
    )
    assert result["receipt"]["outcome"] == "success_unverified"
    assert result["receipt"]["primitive"]["kind"] == "desktop_window_focus"
    assert strategy.frozen_reason


def test_reacquisition_07_verified_focus_makes_uia_eligible():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    _dispatch_target_window_focus(strategy, roblox, calculator)
    _ignored, focused = _reacquisition_world(target_foreground=True)
    observed, reconciled = _record_target_observation(
        strategy, _target_selection(focused, focused), target="reconcile",
    )
    assert reconciled is True
    assert observed["receipt"]["reason_code"] == (
        "exact_desktop_target_focus_observed"
    )
    decision = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": focused.surfaces[1].ref_id,
            "objective": focused.surfaces[1].accessible_label,
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "calculator-target-calculator-display",
                "expected_text": "3",
            },
        },
        strategy=strategy,
    )
    assert decision is not None and decision.allowed


def test_reacquisition_08_activation_receipt_alone_never_proves_focus():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    _dispatch_target_window_focus(strategy, roblox, calculator)
    assert strategy._completed_substeps == []
    assert strategy._latest_desktop_observation == {}
    assert strategy.frozen_reason


def test_reacquisition_09_wrong_foreground_keeps_uia_blocked_after_activation():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    _dispatch_target_window_focus(strategy, roblox, calculator)
    later_background = replace(calculator, observed_at=time.time() + 10.0)
    _record_target_observation(
        strategy,
        _target_selection(roblox, later_background),
        target="reconcile",
    )
    blocked = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": later_background.surfaces[1].ref_id,
            "objective": later_background.surfaces[1].accessible_label,
        },
        strategy=strategy,
    )
    assert blocked is not None and not blocked.allowed
    assert strategy.frozen_reason


def test_reacquisition_10_command_exit_and_absent_target_do_not_prove_open():
    roblox, _calculator = _reacquisition_world()
    strategy = _strategy()
    decision = tool_preflight(
        "run_command", {"command": "start Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record_tool_outcome(
        "run_command", decision,
        outcome="success_verified", is_error=False,
        reason_code="process_exit_zero", result="exit code 0",
        strategy=strategy,
    )
    missing = _target_selection(
        roblox, None, status="target_missing", candidate_count=0,
    )
    _record_target_observation(strategy, missing, target="Calculator")
    open_postcondition = strategy.mission.action_nodes[0].observable_success_postcondition
    assert open_postcondition not in strategy.proven_postconditions


def test_reacquisition_11_application_frame_host_alone_is_not_calculator():
    host = _named_scene(
        "applicationframehost", "Calculator", rich=False, foreground=False,
        key="frame-only", host_application="applicationframehost",
    )
    assert desktop_scene.scene_matches_application(host, "Calculator") is False


def test_reacquisition_12_exact_uwp_content_identity_is_calculator():
    _roblox, calculator = _reacquisition_world()
    rank, evidence = desktop_scene._scene_application_identity(
        calculator, "Calculator",
    )
    assert rank == 400
    assert evidence == "uia_content_application:calculatorapp"


def test_reacquisition_13_two_equal_target_windows_fail_closed():
    roblox, calculator = _reacquisition_world()
    other = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="calculator-other", host_application="applicationframehost",
    )
    selected = desktop_scene._resolve_target_scene(
        (roblox, calculator, other),
        "Calculator",
        foreground_scene=roblox,
    )
    assert selected.status == "target_ambiguous"
    assert selected.target_scene is None
    assert selected.candidate_count == 2


def test_reacquisition_14_target_resolution_survives_generation_refresh():
    roblox, calculator = _reacquisition_world()
    refreshed = replace(
        calculator,
        window_ref=replace(
            calculator.window_ref,
            ref_id="calculator-refreshed-window",
            window_generation="win-calculator-refreshed",
        ),
        surfaces=tuple(
            replace(
                item,
                window_generation="win-calculator-refreshed",
                parent_ref=(
                    None if item is calculator.window_ref
                    else "calculator-refreshed-window"
                ),
                ref_id=(
                    "calculator-refreshed-window"
                    if item is calculator.window_ref else item.ref_id
                ),
            )
            for item in calculator.surfaces
        ),
    )
    refreshed = replace(refreshed, window_ref=refreshed.surfaces[0])
    selected = desktop_scene._resolve_target_scene(
        (roblox, refreshed), "Calculator", foreground_scene=roblox,
    )
    assert selected.status == "exact_target"
    assert selected.target_scene.window_ref.window_generation == (
        "win-calculator-refreshed"
    )


def test_reacquisition_15_stale_window_ref_is_reresolved_before_focus():
    roblox, calculator = _reacquisition_world()
    old = replace(
        calculator.window_ref,
        ref_id="stale-calculator-window",
    )
    native = NativeSurfaceIdentity(
        pid=4242, process_started_at=10.0, native_window_id=10101,
    )
    stale = SurfaceResolution("stale", old, None, "window_generation_changed")
    resolved = SurfaceResolution("resolved", calculator.window_ref, native)
    with (
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "resolve",
            side_effect=(stale, resolved),
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=native,
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "prove_equivalent",
            return_value=calculator.window_ref,
        ),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_target_selection(roblox, calculator),
        ),
        patch(
            "osia.tools.desktop_control.focus_app_window",
            return_value=("ready", "Calculator"),
        ),
    ):
        result = desktop_scene.activate_desktop_surface(
            old.ref_id, "Calculator",
        )
    assert result["receipt"]["reresolved_from"] == old.ref_id
    assert result["receipt"]["surface_ref"] == calculator.window_ref.ref_id


def test_reacquisition_16_rich_target_keeps_uia_ahead_of_keyboard():
    _roblox, calculator = _reacquisition_world(target_foreground=True)
    strategy = _stepwise_strategy()
    _record_target_observation(
        strategy,
        _target_selection(calculator, calculator),
        target=BUTTON_LABELS[0],
    )
    keyboard = tool_preflight(
        "desktop_press", {"key": "3"}, strategy=strategy,
    )
    assert keyboard is not None and not keyboard.allowed
    assert keyboard.reason_code == "stronger_exact_uia_frontier_available"


def test_reacquisition_17_rich_target_keeps_vision_count_zero():
    _roblox, calculator = _reacquisition_world(target_foreground=True)
    strategy = _stepwise_strategy()
    _record_target_observation(
        strategy,
        _target_selection(calculator, calculator),
        target=BUTTON_LABELS[0],
    )
    visual = tool_preflight(
        "request_visual_observation",
        {
            "surface_ref": calculator.window_ref.ref_id,
            "reason": "incomplete_accessibility",
            "target": "target",
        },
        strategy=strategy,
    )
    assert visual is not None and not visual.allowed
    assert visual.reason_code == "structured_uia_evidence_sufficient"


def test_reacquisition_18_exact_poor_target_preserves_targeted_vision():
    _roblox, calculator = _reacquisition_world(
        target_foreground=True, rich=False,
    )
    strategy = _stepwise_strategy()
    _record_target_observation(
        strategy, _target_selection(calculator, calculator), target="target",
    )
    blocked = tool_preflight(
        "request_visual_observation",
        {
            "surface_ref": calculator.window_ref.ref_id,
            "reason": "incomplete_accessibility",
            "target": "target",
        },
        strategy=strategy,
    )
    assert blocked is not None and blocked.allowed is False
    assert blocked.reason_code == "keyboard_frontier_not_exhausted"


def test_reacquisition_19_non_target_poor_target_rich_still_blocks_vision():
    roblox, calculator = _reacquisition_world()
    focused = replace(
        calculator,
        foreground=True,
        window_ref=replace(calculator.window_ref, focused=True, selected=True),
    )
    focused = replace(
        focused,
        surfaces=(focused.window_ref, *calculator.surfaces[1:]),
    )
    strategy = _stepwise_strategy()
    _record_target_observation(
        strategy,
        _target_selection(focused, focused),
        target=BUTTON_LABELS[0],
    )
    visual = tool_preflight("take_screenshot", {}, strategy=strategy)
    assert roblox.accessibility_quality("Calculator").value == "window_only"
    assert visual is not None and not visual.allowed
    assert visual.reason_code == "structured_uia_evidence_sufficient"


def test_reacquisition_20_unrelated_foreground_never_satisfies_target_open():
    roblox, _calculator = _reacquisition_world()
    strategy = _strategy()
    with execution_strategy_scope(TASK, strategy=strategy):
        postcondition, semantic = desktop_scene._exact_observed_postcondition(
            roblox, "Calculator",
        )
    assert postcondition == ""
    assert semantic == ""


def test_reacquisition_21_end_to_end_target_focus_then_verified_uia():
    roblox, calculator = _reacquisition_world()
    strategy = _strategy()
    opening, _ = _record_target_observation(
        strategy, _target_selection(roblox, calculator), target="Calculator",
    )
    assert opening["scene"]["target_foreground"] is False
    assert strategy.mission.action_nodes[0].status == "succeeded"

    _dispatch_target_window_focus(strategy, roblox, calculator)
    _ignored, focused = _reacquisition_world(target_foreground=True)
    focus_observation, focus_reconciled = _record_target_observation(
        strategy, _target_selection(focused, focused), target="reconcile",
    )
    assert focus_reconciled is True
    assert focus_observation["scene"]["target_foreground"] is True

    results = ("3", "34", "347", "347×", "347×2", "347×29", "10063")
    prior = "0"
    for label, result in zip(BUTTON_LABELS, results):
        _start_stepwise_primitive(
            strategy,
            label,
            before_result=prior,
            expected_result=result,
        )
        observed, _reconciled = _observe_stepwise_primitive(
            strategy,
            result,
            target="10063" if result == "10063" else "reconcile",
        )
        prior = result
    assert observed["receipt"]["reason_code"] == "exact_final_state_observation"
    assert observed["receipt"]["observed_target"] == "10063"
    assert strategy.succeeded is True
    assert not any(
        item.layer in {
            int(StrategyLayer.KEYBOARD),
            int(StrategyLayer.LOCAL_VISION),
            int(StrategyLayer.GLOBAL_VISION),
        }
        for item in strategy.attempts
    )


def test_stepwise_end_to_end_verified_order_has_no_blind_fallbacks():
    strategy = _stepwise_strategy()
    mutation_count = 0
    observation_count = 0
    reconciliation_count = 0
    prior = "0"
    max_unresolved = 0
    for index, label in enumerate(BUTTON_LABELS):
        after = "10 063" if index == len(BUTTON_LABELS) - 1 else str(index + 3)
        _decision, attempt, _arguments = _start_stepwise_primitive(
            strategy, label, before_result=prior, expected_result=after,
        )
        mutation_count += 1
        max_unresolved = max(max_unresolved, int(bool(strategy.frozen_reason)))
        target = "10063" if index == len(BUTTON_LABELS) - 1 else "reconcile"
        receipt, reconciled = _observe_stepwise_primitive(
            strategy, after, target=target,
        )
        observation_count += 1
        reconciliation_count += int(reconciled)
        assert not strategy.frozen_reason
        assert attempt.substep_index == index + 1
        assert receipt["receipt"]["outcome"] == "confirmed_success"
        prior = after

    irrelevant = tool_preflight(
        "query_document",
        {"document_id": "doc_" + "c" * 32, "operation": "summary"},
        strategy=strategy,
    )
    assert mutation_count == len(BUTTON_LABELS)
    assert observation_count == len(BUTTON_LABELS)
    assert reconciliation_count == len(BUTTON_LABELS)
    assert max_unresolved == 1
    assert irrelevant is not None and not irrelevant.allowed
    assert strategy.succeeded is True
    assert len({item["substep_id"] for item in strategy._completed_substeps}) == (
        len(BUTTON_LABELS)
    )
    assert not any(
        item.layer in {
            int(StrategyLayer.KEYBOARD),
            int(StrategyLayer.LOCAL_VISION),
            int(StrategyLayer.GLOBAL_VISION),
        }
        and item.status in {"progressed", "succeeded"}
        for item in strategy.attempts
    )








def _sanitized_physical_chat_open_prompt() -> str:
    """Canonical Chat objective used by the captured physical run (WOR-303)."""

    return "Open Calculator."


def _sanitized_physical_frozen_strategy():
    prompt = _sanitized_physical_chat_open_prompt()
    strategy = build_execution_strategy(
        prompt,
        {"desktop_click", "inspect_desktop_surfaces", "open_application"},
    )
    decision = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record_tool_outcome(
        "open_application",
        decision,
        outcome="pending",
        is_error=True,
        reason_code="postcondition_not_observed",
        result="pending",
        strategy=strategy,
    )
    return prompt, strategy, decision


def test_physical_shape_serialize_reconstruct_remains_probe_eligible():
    prompt, strategy, decision = _sanitized_physical_frozen_strategy()
    serialized = json.loads(json.dumps(strategy.snapshot()))
    restored = ExecutionStrategy.from_snapshot(serialized)

    probe = select_local_reconciliation_probe(
        restored,
        {"inspect_desktop_surfaces"},
        user_request=prompt,
    )

    assert probe is not None
    assert probe["strategy_attempt_id"] == decision.attempt_id
    assert probe["semantic_node_id"] == decision.node_id
    assert probe["mechanism_id"] == "direct:open_application:calculator"


def test_physical_shape_preserves_open_desktop_calculator_metadata():
    from osia.execution_strategy import _semantic_domain

    _prompt, strategy, decision = _sanitized_physical_frozen_strategy()
    node = strategy.node_by_id(decision.node_id)
    attempt = strategy.attempt_by_id(decision.attempt_id)

    assert node is not None and attempt is not None
    assert _semantic_domain(node) == "desktop"
    assert node.requested_action == "open"
    assert node.required_application == "calculator"
    assert node.required_surface == ""
    assert node.observable_success_postcondition == (
        "A Calculator window is visible."
    )
    assert attempt.node_id == node.node_id
    assert attempt.mechanism_id == "direct:open_application:calculator"


def test_physical_shape_missing_calculator_identity_still_fails_closed():
    prompt, strategy, _decision = _sanitized_physical_frozen_strategy()
    snapshot = strategy.snapshot()
    for node in snapshot["mission"]["action_nodes"]:
        node["required_application"] = ""
    snapshot["goal"]["required_application"] = ""
    restored = ExecutionStrategy.from_snapshot(snapshot)

    assert select_local_reconciliation_probe(
        restored,
        {"inspect_desktop_surfaces"},
        user_request=prompt,
    ) is None


def test_physical_registry_contract_and_persistence_allow_exact_local_probe():
    from osia.agent import TOOL_MAP
    from osia.tool_contracts import (
        SideEffectClass,
        effective_contract,
        get_tool_spec,
        persistence_contract,
    )

    observer = get_tool_spec("inspect_desktop_surfaces")
    effective = effective_contract(
        observer,
        {"target": "calculator"},
        "Open Calculator.",
    )
    persistence = persistence_contract(
        get_tool_spec("open_application"),
        tool_name="open_application",
        arguments={"name": "Calculator"},
        owner_timeout_seconds=30.0,
    )

    assert "inspect_desktop_surfaces" in TOOL_MAP
    assert observer.side_effect is SideEffectClass.READ_ONLY
    assert effective.side_effect is SideEffectClass.READ_ONLY
    assert persistence.safe_alternatives == (
        "local_desktop_application_observation",
    )


def test_suspension_serializes_the_exact_frozen_strategy():
    from osia import agent as agent_module
    from osia.tool_contracts import (
        ExecutionOutcome,
        ToolExecutionRecord,
        effective_contract,
        get_tool_spec,
    )

    prompt, strategy, decision = _sanitized_physical_frozen_strategy()
    contract = effective_contract(
        get_tool_spec("open_application"), {"name": "Calculator"}, prompt,
    )
    pending = ToolExecutionRecord(
        "pending", True, ExecutionOutcome.PENDING, 1, True, False,
        contract, "attempt_physical_shape", False,
        "postcondition_not_observed",
    )
    agent = agent_module.OSIAAgent(model="wor262-suspend", lang="en")
    agent._tool_attempt_metadata = {}
    coordinator = Mock()

    with patch(
        "osia.tool_attempts.get_tool_attempt_coordinator",
        return_value=coordinator,
    ), execution_strategy_scope(prompt, strategy=strategy):
        marker = agent._suspend_tool_turn(
            "open_application", {"name": "Calculator"}, pending,
        )

    serialized = json.loads(json.dumps(
        agent._pending_tool_runtime["strategy_snapshot"],
    ))
    restored = ExecutionStrategy.from_snapshot(serialized)
    probe = select_local_reconciliation_probe(
        restored, {"inspect_desktop_surfaces"}, user_request=prompt,
    )
    assert marker == "WAITING_TOOL_RESULT"
    assert probe is not None
    assert probe["strategy_attempt_id"] == decision.attempt_id
    coordinator.register_suspended.assert_called_once()




def test_fast_close_01_minimized_packaged_window_remains_exact_target():
    app_id = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    with (
        patch("osia.desktop_scene._window_is_minimized", return_value=True),
        patch("osia.desktop_scene._window_bounds", return_value=None),
        patch(
            "osia.desktop_scene._process_details",
            return_value=("applicationframehost", 10.0),
        ),
        patch(
            "osia.desktop_scene._window_packaged_application_id",
            return_value=app_id,
        ),
        patch("osia.desktop_scene._owner_window_id", return_value=0),
        patch("osia.tools.desktop_control._load_uiautomation", return_value=None),
    ):
        scene = desktop_scene._observe_window_scene(
            (4065764, "Calculatrice", 21196),
            target="Calculator",
            foreground=False,
            class_name_hint="ApplicationFrameWindow",
        )

    assert scene is not None
    assert scene.application_id == "calculator"
    assert scene.packaged_application_id == app_id
    assert scene.minimized is True
    assert scene.window_ref.bounds is None
    rank, evidence = desktop_scene._scene_application_identity(scene, "Calculator")
    assert rank == 350
    assert evidence == "packaged_application_id:" + app_id


def test_fast_close_02_minimized_exact_target_requires_semantic_activation():
    foreground, calculator = _reacquisition_world()
    minimized_window = replace(calculator.window_ref, bounds=None)
    minimized = replace(
        calculator,
        application_id="calculator",
        window_ref=minimized_window,
        surfaces=(minimized_window,),
        accessibility_complete=False,
        uia_tree_observed=False,
        packaged_application_id=(
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        ),
        minimized=True,
    )
    strategy = _stepwise_strategy()
    observed, _ = _record_target_observation(
        strategy,
        _target_selection(foreground, minimized),
        target="Calculator",
    )

    assert observed["scene"]["target_minimized"] is True
    activation = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": minimized.window_ref.ref_id,
            "objective": minimized.window_ref.accessible_label,
        },
        strategy=strategy,
    )
    click = tool_preflight(
        "desktop_click",
        {"description": "bring Calculator to the foreground"},
        strategy=strategy,
    )
    assert activation is not None and activation.allowed
    assert click is not None and not click.allowed
    assert click.reason_code == "exact_target_activation_required"


def test_fast_close_03_exact_coordinate_goal_keeps_desktop_click_available():
    strategy = build_execution_strategy(
        "Click screen coordinates (500, 400) in Calculator.",
        {"activate_desktop_surface", "desktop_click", "inspect_desktop_surfaces"},
    )
    _ignored, calculator = _reacquisition_world(target_foreground=True)
    _record_target_observation(
        strategy,
        _target_selection(calculator, calculator),
        target="Calculator",
    )

    decision = tool_preflight(
        "desktop_click",
        {"description": "screen coordinates (500, 400)"},
        strategy=strategy,
    )
    assert strategy._latest_desktop_observation["target_foreground"] is True
    assert decision is not None and decision.allowed


def test_fast_close_04_replan_preserves_fresh_minimized_state_but_not_stale_ref():
    observed_at = time.time()
    memory = normalize_execution_memory({
        "desktop_observation": {
            "application": "calculator",
            "requested_application": "calculator",
            "window_surface_ref": "surface-calculator-window",
            "window_generation": "win-calculator",
            "accessibility_quality": "window_only",
            "target_resolution": "exact_target",
            "target_foreground": False,
            "target_minimized": True,
            "target_identity_evidence": "packaged_application_id:stable",
            "foreground_application": "brave",
            "observed_at": observed_at,
        },
    })
    fresh = build_execution_strategy(TASK, {"desktop_click"})
    rebind_execution_memory(fresh, (memory,), now=observed_at + 1.0)
    stale = build_execution_strategy(TASK, {"desktop_click"})
    rebind_execution_memory(stale, (memory,), now=observed_at + 31.0)

    assert fresh._latest_desktop_observation["target_minimized"] is True
    assert fresh._latest_desktop_observation["window_surface_ref"] == (
        "surface-calculator-window"
    )
    assert stale._latest_desktop_observation["target_resolution"] == (
        "stale_target_requires_refresh"
    )
    assert stale._latest_desktop_observation["target_minimized"] is False
    assert stale._latest_desktop_observation["window_surface_ref"] == ""


def test_duplicate_target_01_same_window_generation_collapses_to_content_record():
    foreground, content = _reacquisition_world()
    duplicate = replace(
        content,
        content_application_id="",
        packaged_application_id="Microsoft.Example_123!App",
        accessibility_complete=False,
        uia_tree_observed=False,
        surfaces=(content.window_ref,),
        observed_at=content.observed_at - 1.0,
    )
    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (foreground, duplicate, content),
            "Calculator",
            foreground_scene=foreground,
        )

    assert selected.status == "exact_target"
    assert selected.target_scene is content
    assert selected.identity_evidence == "uia_content_application:calculatorapp"


def test_duplicate_target_02_same_aumid_different_real_windows_remain_distinct():
    foreground, _content = _reacquisition_world()
    app_id = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    first = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=False, foreground=False,
            key="package-first", host_application="applicationframehost",
        ),
        packaged_application_id=app_id,
    )
    second = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=False, foreground=False,
            key="package-second", host_application="applicationframehost",
        ),
        packaged_application_id=app_id,
    )
    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (foreground, first, second),
            "Calculator",
            foreground_scene=foreground,
        )

    assert first.window_ref.window_generation != second.window_ref.window_generation
    assert selected.status == "target_ambiguous"
    assert selected.candidate_count == 2


def test_duplicate_target_03_explicit_stale_record_is_excluded():
    foreground, _content = _reacquisition_world()
    first = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="stale-window", host_application="applicationframehost",
    )
    current = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="current-window", host_application="applicationframehost",
    )

    def resolve(ref_id):
        scene = first if ref_id == first.window_ref.ref_id else current
        return SurfaceResolution(
            "stale" if scene is first else "resolved",
            scene.window_ref,
            None,
            "window_generation_changed" if scene is first else "",
        )

    with (
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "resolve",
            side_effect=resolve,
        ),
        patch("osia.desktop_scene._current_launch_target", return_value=None),
    ):
        selected = desktop_scene._resolve_target_scene(
            (foreground, first, current),
            "Calculator",
            foreground_scene=foreground,
        )

    assert selected.status == "exact_target"
    assert selected.target_scene is current


def test_duplicate_target_04_current_open_correlation_selects_exact_generation():
    foreground, first = _reacquisition_world()
    second = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="launch-correlated", host_application="applicationframehost",
    )
    correlation = desktop_scene._LaunchTargetCorrelation(
        application_id="calculator",
        process_ref=second.window_ref.process_ref,
        window_generation=second.window_ref.window_generation,
        correlated_at=time.time(),
    )
    with patch(
        "osia.desktop_scene._current_launch_target",
        return_value=correlation,
    ):
        selected = desktop_scene._resolve_target_scene(
            (foreground, first, second),
            "Calculator",
            foreground_scene=foreground,
        )

    assert selected.status == "exact_target"
    assert selected.target_scene is second
    assert selected.identity_evidence.startswith("current_open_attempt:")


def test_duplicate_target_05_equal_real_windows_without_correlation_fail_closed():
    foreground, first = _reacquisition_world()
    second = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=False,
        key="equal-real-window", host_application="applicationframehost",
    )
    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (foreground, first, second),
            "Calculator",
            foreground_scene=foreground,
        )

    assert selected.status == "target_ambiguous"
    assert selected.target_scene is None


def test_duplicate_target_06_exact_content_beats_restored_package_only_frame():
    foreground, content = _reacquisition_world()
    content = replace(content, minimized=True)
    package_only = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=False, foreground=False,
            key="restored-package", host_application="applicationframehost",
        ),
        packaged_application_id="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
        minimized=False,
    )
    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (foreground, package_only, content),
            "Calculator",
            foreground_scene=foreground,
        )

    assert selected.status == "exact_target"
    assert selected.target_scene is content
    assert selected.target_scene.minimized is True


def test_duplicate_target_07_aumid_and_restored_state_are_not_a_tie_breaker():
    foreground, _content = _reacquisition_world()
    app_id = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    minimized = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=False, foreground=False,
            key="minimized-package", host_application="applicationframehost",
        ),
        packaged_application_id=app_id,
        minimized=True,
    )
    restored = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=False, foreground=False,
            key="restored-package-two", host_application="applicationframehost",
        ),
        packaged_application_id=app_id,
        minimized=False,
    )
    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (foreground, minimized, restored),
            "Calculator",
            foreground_scene=foreground,
        )

    assert selected.status == "target_ambiguous"
    assert selected.target_scene is None


def test_duplicate_target_08_existing_exact_window_focus_records_correlation():
    foreground, content = _reacquisition_world()
    native = NativeSurfaceIdentity(
        pid=20260,
        process_started_at=time.time() - 10.0,
        native_window_id=1838062,
        class_name="ApplicationFrameWindow",
    )
    with (
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_target_selection(foreground, content),
        ),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=native,
        ),
        patch(
            "osia.tools.desktop_control.focus_app_window",
            return_value=("ready", "Calculator"),
        ) as focus,
        patch("osia.desktop_scene._remember_launch_target") as remember,
    ):
        status, detail = desktop_scene.focus_existing_application_window(
            "Calculator",
        )

    assert (status, detail) == ("ready", "Calculator")
    focus.assert_called_once_with((1838062, "Calculator", 20260))
    remember.assert_called_once_with(content)


def test_duplicate_target_09_ambiguous_existing_windows_never_focus_or_launch():
    foreground, _content = _reacquisition_world()
    selection = _target_selection(
        foreground,
        None,
        status="target_ambiguous",
        candidate_count=2,
    )
    with (
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=selection,
        ),
        patch("osia.tools.desktop_control.focus_app_window") as focus,
    ):
        status, detail = desktop_scene.focus_existing_application_window(
            "Calculator",
        )

    assert (status, detail) == ("target_ambiguous", "")
    focus.assert_not_called()


def test_duplicate_target_10_open_application_reuses_semantic_window_without_spawn():
    from osia import agent as agent_module

    with (
        patch(
            "osia.desktop_scene.focus_existing_application_window",
            return_value=("ready", "Calculatrice"),
        ),
        patch.object(agent_module.subprocess, "Popen") as spawn,
    ):
        result = agent_module.tool_open_application("Calculator")

    assert "did not launch a second instance" in result
    assert "Calculatrice" in result
    spawn.assert_not_called()


def test_duplicate_target_11_uwp_host_and_inset_content_are_one_window():
    content = replace(
        _named_scene(
            "calculatorapp", "Calculatrice", rich=True, foreground=True,
            key="content-core", host_application="calculatorapp",
        ),
        window_ref=replace(
            _named_scene(
                "calculatorapp", "Calculatrice", rich=True, foreground=True,
                key="content-bounds", host_application="calculatorapp",
            ).window_ref,
            bounds=SurfaceBounds(819, 124, 1219, 789),
        ),
    )
    content = replace(content, surfaces=(content.window_ref, *content.surfaces[1:]))
    frame = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=True, foreground=False,
            key="packaged-frame", host_application="applicationframehost",
        ),
        content_application_id="calculatorapp",
        packaged_application_id=(
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        ),
    )
    frame = replace(
        frame,
        window_ref=replace(
            frame.window_ref,
            bounds=SurfaceBounds(810, 123, 1228, 798),
        ),
    )
    frame = replace(frame, surfaces=(frame.window_ref, *frame.surfaces[1:]))

    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (content, frame),
            "Calculator",
            foreground_scene=content,
        )

    assert selected.status == "exact_target"
    assert selected.candidate_count == 1
    assert selected.target_scene is frame
    assert selected.identity_evidence == "uia_content_application:calculatorapp"


def test_duplicate_target_12_separate_host_and_content_bounds_stay_ambiguous():
    content = replace(
        _named_scene(
            "calculatorapp", "Calculatrice", rich=True, foreground=True,
            key="separate-content", host_application="calculatorapp",
        ),
        window_ref=replace(
            _named_scene(
                "calculatorapp", "Calculatrice", rich=True, foreground=True,
                key="separate-content-bounds", host_application="calculatorapp",
            ).window_ref,
            bounds=SurfaceBounds(100, 100, 500, 700),
        ),
    )
    content = replace(content, surfaces=(content.window_ref, *content.surfaces[1:]))
    frame = replace(
        _named_scene(
            "calculator", "Calculatrice", rich=True, foreground=False,
            key="separate-frame", host_application="applicationframehost",
        ),
        content_application_id="calculatorapp",
        packaged_application_id=(
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        ),
    )
    frame = replace(
        frame,
        window_ref=replace(
            frame.window_ref,
            bounds=SurfaceBounds(900, 100, 1300, 700),
        ),
    )
    frame = replace(frame, surfaces=(frame.window_ref, *frame.surfaces[1:]))

    with patch("osia.desktop_scene._current_launch_target", return_value=None):
        selected = desktop_scene._resolve_target_scene(
            (content, frame),
            "Calculator",
            foreground_scene=content,
        )

    assert selected.status == "target_ambiguous"
    assert selected.candidate_count == 2


def test_final_activation_01_uncertain_window_focus_selects_local_observer():
    from osia.tool_contracts import get_tool_spec, persistence_contract

    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    decision, _result = _dispatch_target_window_focus(
        strategy, roblox, calculator,
    )
    attempt = strategy.attempt_by_id(decision.attempt_id)
    probe = select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
    )
    spec = get_tool_spec("activate_desktop_surface")
    persistence = persistence_contract(
        spec,
        tool_name=spec.name,
        arguments={
            "surface_ref": calculator.window_ref.ref_id,
            "objective": calculator.window_ref.accessible_label,
        },
        owner_timeout_seconds=30.0,
    )

    assert attempt is not None
    assert attempt.attempted_effect == "application_foreground"
    assert attempt.target_surface == calculator.window_ref.ref_id
    assert spec.safe_reconciliation_descriptors == (
        "local_desktop_application_observation",
    )
    assert persistence.safe_alternatives == (
        "local_desktop_application_observation",
    )
    assert probe is not None
    assert probe["semantic_effect"] == "application_foreground"
    assert probe["arguments"] == {"target": "calculator"}


def test_final_activation_02_fresh_exact_focus_resolves_only_activation(tmp_path):
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    decision, _result = _dispatch_target_window_focus(
        strategy, roblox, calculator,
    )
    strategy = ExecutionStrategy.from_snapshot(json.loads(json.dumps(
        strategy.snapshot(),
    )))
    restored_attempt = strategy.attempt_by_id(decision.attempt_id)
    assert restored_attempt is not None
    assert restored_attempt.target_surface == calculator.window_ref.ref_id
    assert restored_attempt.primitive_receipt["before"][
        "window_generation"
    ] == calculator.window_ref.window_generation
    controller = TurnTerminalityController(
        turn_id="wor262-final-activation",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"source": "activation_dispatch"},
        postcondition="application_foreground",
        attempt_id="attempt_activation_probe",
        reason_code="postcondition_required_unverified",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    _ignored, focused = _reacquisition_world(target_foreground=True)

    observed, reconciled = _record_target_observation(
        strategy, _target_selection(focused, focused), target="reconcile",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    resolution = controller.snapshot()["unknown_effect_resolutions"][-1]

    assert reconciled is True
    assert observed["receipt"]["postcondition"] == "application_foreground"
    assert strategy.snapshot()["reconciliation_events"][-1]["resolution"] == (
        "completed"
    )
    assert strategy.goal.status == "pending"
    assert strategy.succeeded is False
    assert resolution["strategy_attempt_id"] == decision.attempt_id
    assert resolution["postcondition"] == "application_foreground"
    assert resolution["resolution"] == "completed"




def test_final_activation_04_wrong_foreground_keeps_activation_unresolved():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    decision, _result = _dispatch_target_window_focus(
        strategy, roblox, calculator,
    )
    later = replace(calculator, observed_at=time.time() + 10.0)

    observed, reconciled = _record_target_observation(
        strategy, _target_selection(roblox, later), target="reconcile",
    )

    assert reconciled is False
    assert observed["scene"]["target_foreground"] is False
    assert strategy._frozen_attempt_id == decision.attempt_id


def test_final_activation_05_different_generation_never_reconciles_focus():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    decision, _result = _dispatch_target_window_focus(
        strategy, roblox, calculator,
    )
    changed = _named_scene(
        "calculatorapp", "Calculator", rich=True, foreground=True,
        key="replacement-generation", host_application="applicationframehost",
        observed_at=time.time() + 10.0,
    )

    _observed, reconciled = _record_target_observation(
        strategy, _target_selection(changed, changed), target="reconcile",
    )

    assert reconciled is False
    assert strategy._frozen_attempt_id == decision.attempt_id


def test_final_activation_06_activation_never_replays_while_unresolved():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    decision, _result = _dispatch_target_window_focus(
        strategy, roblox, calculator,
    )

    replay = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": calculator.window_ref.ref_id,
            "objective": calculator.window_ref.accessible_label,
        },
        strategy=strategy,
    )

    assert replay is not None and not replay.allowed
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"
    assert strategy._frozen_attempt_id == decision.attempt_id


def test_final_activation_07_focus_evidence_cannot_prove_calculation_result():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    _dispatch_target_window_focus(strategy, roblox, calculator)
    _ignored, focused = _reacquisition_world(target_foreground=True)
    _record_target_observation(
        strategy, _target_selection(focused, focused), target="reconcile",
    )
    calculation = strategy.goal.observable_success_postcondition

    assert calculation not in strategy.proven_postconditions
    assert strategy.goal.status == "pending"
    assert strategy.succeeded is False


def test_final_activation_08_rich_focus_keeps_uia_ahead_of_all_fallbacks():
    roblox, calculator = _reacquisition_world()
    strategy = _stepwise_strategy()
    _dispatch_target_window_focus(strategy, roblox, calculator)
    _ignored, focused = _reacquisition_world(target_foreground=True)
    _record_target_observation(
        strategy,
        _target_selection(focused, focused),
        target=focused.surfaces[1].accessible_label,
    )
    keyboard = tool_preflight(
        "desktop_press", {"key": "3"}, strategy=strategy,
    )
    visual = tool_preflight(
        "request_visual_observation",
        {
            "surface_ref": focused.window_ref.ref_id,
            "reason": "incomplete_accessibility",
            "target": focused.surfaces[1].accessible_label,
        },
        strategy=strategy,
    )
    uia = tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": focused.surfaces[1].ref_id,
            "objective": focused.surfaces[1].accessible_label,
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": "calculator-target-calculator-display",
                "expected_text": "3",
            },
        },
        strategy=strategy,
    )

    assert uia is not None and uia.allowed
    assert keyboard is not None and not keyboard.allowed
    assert visual is not None and not visual.allowed
    assert not any(
        attempt.layer in {
            int(StrategyLayer.KEYBOARD),
            int(StrategyLayer.LOCAL_VISION),
            int(StrategyLayer.GLOBAL_VISION),
        }
        and attempt.status in {"selected", "progressed", "succeeded"}
        for attempt in strategy.attempts
    )


def _uia_reconciliation_observation(
    strategy: ExecutionStrategy,
    result: str,
    *,
    observed_at: float | None = None,
) -> dict:
    scene = _scene(
        result=result,
        observed_at=(time.time() + 1.0 if observed_at is None else observed_at),
    )
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_target_selection(scene, scene),
        ),
    ):
        return desktop_scene.inspect_desktop_surfaces("calculator")


def test_uia_reconciliation_01_physical_shape_selects_persisted_observer():
    from osia import execution_strategy as strategy_module
    from osia.tool_contracts import get_tool_spec, persistence_contract

    strategy = _stepwise_strategy()
    _decision, attempt, arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    persistence = persistence_contract(
        get_tool_spec("activate_desktop_surface"),
        tool_name="activate_desktop_surface",
        arguments=arguments,
        owner_timeout_seconds=30.0,
    )
    probe = select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
    )

    assert attempt.attempted_effect == attempt.primitive_postcondition
    assert attempt.attempted_effect not in strategy_module._RECONCILIATION_OBSERVERS
    assert persistence.safe_alternatives == (
        "local_desktop_application_observation",
    )
    assert probe is not None
    assert probe["tool"] == "inspect_desktop_surfaces"
    assert probe["observer_relation"] == "desktop_uia_primitive"
    assert probe["strategy_attempt_id"] == attempt.attempt_id
    assert probe["substep_id"] == attempt.substep_id


def test_uia_reconciliation_02_unobservable_primitive_remains_fail_closed():
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    receipt = json.loads(json.dumps(attempt.primitive_receipt))
    receipt["before"]["state_rows"] = []
    receipt["before"]["target"] = {}
    attempt.primitive_receipt = receipt

    assert select_local_reconciliation_probe(
        strategy, {"inspect_desktop_surfaces"}, user_request=TASK,
    ) is None


def test_uia_reconciliation_03_identity_survives_strategy_persistence():
    strategy = _stepwise_strategy()
    decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    restored = ExecutionStrategy.from_snapshot(json.loads(json.dumps(
        strategy.snapshot()
    )))
    persisted = restored.attempt_by_id(decision.attempt_id)
    probe = select_local_reconciliation_probe(
        restored, {"inspect_desktop_surfaces"}, user_request=TASK,
    )

    assert persisted is not None
    assert persisted.attempt_id == attempt.attempt_id
    assert persisted.node_id == attempt.node_id
    assert persisted.composite_id == attempt.composite_id
    assert persisted.substep_id == attempt.substep_id
    assert persisted.substep_index == attempt.substep_index
    assert persisted.attempted_effect == attempt.attempted_effect
    assert persisted.primitive_postcondition == attempt.primitive_postcondition
    assert persisted.primitive_receipt == attempt.primitive_receipt
    assert probe is not None and probe["substep_index"] == 1


def test_uia_reconciliation_04_fresh_exact_evidence_resolves_only_primitive():
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    result = _uia_reconciliation_observation(strategy, "3")
    receipt = result["receipt"]
    resolved = record_tool_observation(
        "inspect_desktop_surfaces", result, is_error=False, strategy=strategy,
    )

    assert resolved is True
    assert receipt["strategy_attempt_id"] == attempt.attempt_id
    assert receipt["semantic_node_id"] == attempt.node_id
    assert receipt["composite_id"] == attempt.composite_id
    assert receipt["substep_id"] == attempt.substep_id
    assert receipt["substep_index"] == attempt.substep_index
    assert receipt["attempted_effect"] == attempt.attempted_effect
    assert receipt["primitive_postcondition"] == attempt.primitive_postcondition
    assert strategy.attempt_by_id(attempt.attempt_id).status == "progressed"
    assert strategy.goal.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
    assert strategy.succeeded is False


def test_uia_reconciliation_04b_exact_no_op_retries_the_same_substep_safely():
    strategy = _stepwise_strategy()
    _decision, attempt, arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    result = _uia_reconciliation_observation(
        strategy,
        "0",
        observed_at=attempt.dispatched_at + 6.0,
    )

    assert record_tool_observation(
        "inspect_desktop_surfaces",
        result,
        is_error=False,
        strategy=strategy,
    ) is True
    retry = tool_preflight(
        "activate_desktop_surface",
        arguments,
        strategy=strategy,
    )

    assert attempt.status == "blocked"
    assert retry is not None and retry.allowed
    retry_attempt = strategy.attempt_by_id(retry.attempt_id)
    assert retry_attempt is not None
    assert retry_attempt.substep_id == attempt.substep_id
    assert retry_attempt.substep_index == attempt.substep_index
    assert retry_attempt.attempt_id != attempt.attempt_id


def test_uia_reconciliation_05_wrong_substep_cannot_resolve_primitive():
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    result = _uia_reconciliation_observation(strategy, "3")
    result["receipt"]["substep_id"] = "substep_other"

    assert record_tool_observation(
        "inspect_desktop_surfaces", result, is_error=False, strategy=strategy,
    ) is False
    assert strategy._frozen_attempt_id == attempt.attempt_id
    assert strategy._completed_substeps == []


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("strategy_attempt_id", "strategy_other"),
        ("semantic_node_id", "node_other"),
        ("mechanism_id", "mechanism_other"),
        ("substep_index", 99),
        ("attempted_effect", "different effect"),
        ("primitive_postcondition", "different postcondition"),
    ),
)
def test_uia_reconciliation_06_other_identity_mismatch_cannot_resolve(
    field: str,
    wrong_value: object,
):
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    result = _uia_reconciliation_observation(strategy, "3")
    result["receipt"][field] = wrong_value

    assert record_tool_observation(
        "inspect_desktop_surfaces", result, is_error=False, strategy=strategy,
    ) is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


@pytest.mark.parametrize("mismatch", ("stale", "surface", "generation"))
def test_uia_reconciliation_07_stale_or_wrong_window_cannot_prove_primitive(
    mismatch: str,
):
    strategy = _stepwise_strategy()
    _decision, attempt, _arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    scene = _scene(result="3", observed_at=time.time() + 1.0)
    if mismatch == "stale":
        scene = replace(scene, observed_at=attempt.dispatched_at - 1.0)
    else:
        window = replace(
            scene.window_ref,
            ref_id=("replacement-window" if mismatch == "surface" else scene.window_ref.ref_id),
            window_generation=(
                "replacement-generation"
                if mismatch == "generation" else scene.window_ref.window_generation
            ),
        )
        scene = replace(scene, window_ref=window, surfaces=(window, *scene.surfaces[1:]))
    with execution_strategy_scope(TASK, strategy=strategy):
        evidence = desktop_scene._exact_primitive_reconciliation(scene)

    assert evidence == {}
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_uia_reconciliation_08_no_replay_and_repeated_composite_progression():
    strategy = _stepwise_strategy()
    _first, first_attempt, first_arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[0],
    )
    replay = tool_preflight(
        "activate_desktop_surface", first_arguments, strategy=strategy,
    )
    assert replay is not None and not replay.allowed
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"

    first_result = _uia_reconciliation_observation(strategy, "3")
    assert record_tool_observation(
        "inspect_desktop_surfaces",
        first_result,
        is_error=False,
        strategy=strategy,
    ) is True
    _second, second_attempt, _second_arguments = _start_stepwise_primitive(
        strategy, BUTTON_LABELS[1], before_result="3",
    )
    second_result = _uia_reconciliation_observation(strategy, "34")
    assert record_tool_observation(
        "inspect_desktop_surfaces",
        second_result,
        is_error=False,
        strategy=strategy,
    ) is True

    assert first_attempt.substep_index == 1
    assert second_attempt.substep_index == 2
    assert [item["substep_index"] for item in strategy._completed_substeps] == [
        1, 2,
    ]
    third = tool_preflight(
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
    assert third is not None and third.allowed
    assert strategy.goal.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
