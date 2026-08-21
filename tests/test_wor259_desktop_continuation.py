from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osia.agent import OSIAAgent, _typed_browser_receipt_outcome
from osia.chatgpt_account.app_server_client import CapabilityEvent
from osia.desktop_scene import DesktopScene
from osia.desktop_surface import (
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
    SurfaceResolution,
)
from osia.desktop_vision_policy import (
    AccessibilityQuality,
    DesktopEvidence,
    classify_accessibility_quality,
    decide_visual_observation,
)
from osia.execution_strategy import (
    AttemptStatus,
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    build_execution_strategy,
)
from osia.tool_contracts import ExecutionOutcome
from osia.turn_terminality import TurnTerminalityController


PNG = b"\x89PNG\r\n\x1a\nwor-259-targeted-window"


def _surface(
    ref_id: str,
    label: str,
    *,
    role: str = "ButtonControl",
    kind: SurfaceKind = SurfaceKind.CONTROL,
    patterns: tuple[str, ...] = ("Invoke",),
    focused: bool = False,
    bounds: SurfaceBounds | None = None,
) -> DesktopSurfaceRef:
    return DesktopSurfaceRef(
        ref_id=ref_id,
        application_id="fixture-app",
        process_ref="proc-fixture",
        window_generation="win-fixture",
        surface_kind=kind,
        role=role,
        accessible_label=label,
        focused=focused,
        parent_ref=None if kind is SurfaceKind.WINDOW else "window-ref",
        bounds=bounds,
        runtime_signature="runtime-" + ref_id,
        patterns=patterns,
        structural_epoch=2,
        observed_at=100.0,
    )


def _scene(*, target: bool, rich: bool = False) -> DesktopScene:
    window = _surface(
        "window-ref",
        "Fixture",
        role="WindowControl",
        kind=SurfaceKind.WINDOW,
        patterns=("Window",),
        focused=True,
        bounds=SurfaceBounds(-900, 40, 100, 740, dpi_scale=1.5),
    )
    controls = [
        _surface(
            f"other-{index}",
            f"Other {index}",
            bounds=SurfaceBounds(-850, 80 + index * 20, -700, 98 + index * 20),
        )
        for index in range(19)
    ]
    if target:
        controls.append(_surface(
            "target-ref",
            "Compose",
            patterns=("Invoke",) if rich else (),
            bounds=SurfaceBounds(-300, 650, 50, 710),
        ))
    return DesktopScene(
        application_id="fixture-app",
        window_ref=window,
        surfaces=(window, *controls),
        structural_epoch=2,
        scene_signature="fixture-scene",
        accessibility_complete=True,
        observed_at=100.0,
        uia_tree_observed=True,
    )


def _native(runtime_id=(7, 8, 9)) -> NativeSurfaceIdentity:
    return NativeSurfaceIdentity(
        pid=123,
        process_started_at=10.0,
        native_window_id=456,
        runtime_id=runtime_id,
        class_name="FixtureWindow",
    )


def test_target_relative_quality_rejects_populated_but_irrelevant_tree():
    evidence = DesktopEvidence(
        candidate_count=0,
        actionable_candidate_count=0,
        accessible_node_count=19,
        target_requested=True,
        actionable_pattern_count=5,
    )

    quality = classify_accessibility_quality(evidence)
    decision = decide_visual_observation(
        evidence,
        model_supports_vision=True,
    )

    assert quality is AccessibilityQuality.TARGET_MISSING
    assert decision.eligible is True
    assert decision.reason_code == "target_missing"


@pytest.mark.parametrize(
    ("evidence", "expected"),
    (
        (DesktopEvidence(tree_observed=False), AccessibilityQuality.NO_TREE),
        (DesktopEvidence(accessible_node_count=0), AccessibilityQuality.WINDOW_ONLY),
        (
            DesktopEvidence(
                accessible_node_count=4,
                candidate_count=2,
                actionable_candidate_count=2,
            ),
            AccessibilityQuality.PARTIAL,
        ),
        (
            DesktopEvidence(
                accessible_node_count=4,
                candidate_count=1,
                actionable_candidate_count=1,
                target_requested=True,
                scene_changed=True,
            ),
            AccessibilityQuality.DELAYED,
        ),
        (
            DesktopEvidence(
                accessible_node_count=4,
                candidate_count=1,
                actionable_candidate_count=0,
                actionable_pattern_count=1,
            ),
            AccessibilityQuality.INACCESSIBLE_SUBTREE,
        ),
        (
            DesktopEvidence(
                accessible_node_count=4,
                custom_rendered=True,
            ),
            AccessibilityQuality.CUSTOM_RENDERED,
        ),
        (
            DesktopEvidence(
                accessible_node_count=4,
                candidate_count=1,
                actionable_candidate_count=1,
            ),
            AccessibilityQuality.RICH_TARGET_RESOLVED,
        ),
    ),
)
def test_accessibility_quality_taxonomy(evidence, expected):
    assert classify_accessibility_quality(evidence) is expected


def test_rich_target_keeps_fast_path_without_visual_observation():
    scene = _scene(target=True, rich=True)
    evidence = scene.target_evidence("Compose")

    assert classify_accessibility_quality(
        evidence
    ) is AccessibilityQuality.RICH_TARGET_RESOLVED
    assert decide_visual_observation(
        evidence,
        model_supports_vision=True,
    ).eligible is False


def test_uia_pre_dispatch_failure_has_canonical_no_effect_receipt():
    from osia import desktop_scene

    scene = _scene(target=True, rich=True)
    target = next(item for item in scene.surfaces if item.ref_id == "target-ref")
    resolution = SurfaceResolution("resolved", target, _native())
    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "native_identity",
                return_value=_native(),
            ),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "resolve",
                return_value=resolution,
            ),
            patch("osia.tools.desktop_control._load_uiautomation", return_value=None),
        ):
            result = desktop_scene.activate_desktop_surface(
                target.ref_id, "Compose",
            )
    finally:
        desktop_scene.reset_effective_model(route)

    receipt = result["receipt"]
    assert receipt["schema_version"] == 1
    assert receipt["dispatch_state"] == "not_dispatched"
    assert receipt["outcome"] == "confirmed_failure_before_effect"
    assert receipt["failure_class"] == "mechanism_failed_continue"
    assert _typed_browser_receipt_outcome(result) == (
        ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
        False,
    )


def test_post_dispatch_uia_exception_remains_outcome_unknown():
    from osia import desktop_scene

    scene = _scene(target=True, rich=True)
    target = next(item for item in scene.surfaces if item.ref_id == "target-ref")
    resolution = SurfaceResolution("resolved", target, _native())

    class _Invoke:
        def Invoke(self):
            raise RuntimeError("effect may already have happened")

    control = SimpleNamespace(
        GetInvokePattern=lambda: _Invoke(),
        GetSelectionItemPattern=lambda: None,
        GetTogglePattern=lambda: None,
        GetExpandCollapsePattern=lambda: None,
    )
    auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _hwnd: object(),
    )
    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "native_identity",
                return_value=_native(),
            ),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "resolve",
                return_value=resolution,
            ),
            patch("osia.tools.desktop_control._load_uiautomation", return_value=auto),
            patch("osia.desktop_scene._find_runtime_control", return_value=control),
        ):
            result = desktop_scene.activate_desktop_surface(
                target.ref_id, "Compose",
            )
    finally:
        desktop_scene.reset_effective_model(route)

    assert result["receipt"]["dispatch_state"] == "effect_unknown"
    assert result["receipt"]["outcome"] == "outcome_unknown"
    assert _typed_browser_receipt_outcome(result) == (
        ExecutionOutcome.OUTCOME_UNKNOWN,
        True,
    )


def test_pre_dispatch_failure_attaches_targeted_observation_only_for_poor_uia():
    from osia import desktop_scene

    scene = _scene(target=False)
    stale_target = _surface("old-target", "Compose", patterns=())
    resolution = SurfaceResolution("resolved", stale_target, _native())
    tokens = desktop_scene.begin_desktop_turn()
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "native_identity",
                return_value=_native(),
            ),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "resolve",
                return_value=resolution,
            ),
            patch("osia.tools.desktop_control._load_uiautomation", return_value=None),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                return_value=(PNG, 3.0, 1.0),
            ) as capture,
        ):
            result = desktop_scene.activate_desktop_surface(
                stale_target.ref_id, "Compose",
            )
    finally:
        desktop_scene.reset_effective_model(route)
        desktop_scene.end_desktop_turn(tokens)

    assert capture.call_count == 1
    assert capture.call_args.args[0] == scene.window_ref.bounds
    assert result["receipt"]["accessibility_quality"] == "target_missing"
    assert result["receipt"]["adaptive_visual_observation"] == "observed"
    assert "screenshot" in result


def test_delayed_tree_is_reobserved_without_unnecessary_capture():
    from osia import desktop_scene

    initial = _scene(target=False)
    hydrated = _scene(target=True, rich=True)
    stale_target = _surface("old-target", "Compose", patterns=())
    resolution = SurfaceResolution("resolved", stale_target, _native())
    tokens = desktop_scene.begin_desktop_turn()
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch(
                "osia.desktop_scene.observe_foreground_scene",
                side_effect=(initial, hydrated),
            ) as observe,
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "native_identity",
                return_value=_native(),
            ),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "resolve",
                return_value=resolution,
            ),
            patch("osia.tools.desktop_control._load_uiautomation", return_value=None),
            patch("osia.desktop_scene._capture_bounds_timed") as capture,
        ):
            result = desktop_scene.activate_desktop_surface(
                stale_target.ref_id, "Compose",
            )
    finally:
        desktop_scene.reset_effective_model(route)
        desktop_scene.end_desktop_turn(tokens)

    assert observe.call_count == 2
    capture.assert_not_called()
    assert result["receipt"]["adaptive_visual_observation"] == (
        "structured_evidence_sufficient"
    )


def test_stale_target_is_reresolved_only_to_one_equivalent_surface():
    from osia import desktop_scene

    scene = _scene(target=True, rich=True)
    replacement = next(
        item for item in scene.surfaces if item.ref_id == "target-ref"
    )
    old = DesktopSurfaceRef(**{
        **replacement.__dict__,
        "ref_id": "old-target-ref",
        "window_generation": "old-generation",
    })
    stale = SurfaceResolution("stale", old, None, "structural_epoch_changed")
    resolved = SurfaceResolution("resolved", replacement, _native())

    class _Invoke:
        def Invoke(self):
            return None

    control = SimpleNamespace(
        GetInvokePattern=lambda: _Invoke(),
        GetSelectionItemPattern=lambda: None,
        GetTogglePattern=lambda: None,
        GetExpandCollapsePattern=lambda: None,
    )
    auto = SimpleNamespace(
        UIAutomationInitializerInThread=lambda: nullcontext(),
        ControlFromHandle=lambda _hwnd: object(),
    )
    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "native_identity",
                return_value=_native(),
            ),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "resolve",
                side_effect=(stale, resolved),
            ),
            patch.object(
                desktop_scene.DESKTOP_SURFACE_REGISTRY,
                "prove_equivalent",
                return_value=replacement,
            ) as prove,
            patch("osia.tools.desktop_control._load_uiautomation", return_value=auto),
            patch("osia.desktop_scene._find_runtime_control", return_value=control),
        ):
            result = desktop_scene.activate_desktop_surface(
                old.ref_id, "Compose",
            )
    finally:
        desktop_scene.reset_effective_model(route)

    prove.assert_called_once()
    assert result["receipt"]["reason_code"] == "native_accessibility_dispatched"
    assert result["receipt"]["reresolved_from"] == old.ref_id


def test_provider_invocation_does_not_consume_strategy_without_typed_attempt(
    tmp_path,
):
    strategy = build_execution_strategy(
        "Click Compose in Signal", {"desktop_click"},
    )
    controller = TurnTerminalityController(
        turn_id="wor259-provider-noop",
        objective="Click Compose in Signal",
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    before = tuple(controller.snapshot()["strategy_frontier"])
    decision = controller.evaluate(provider_route="p:m")
    invocation = controller.begin_invocation(
        provider_route="p:m",
        strategy_id=decision.next_strategy,
        postcondition=decision.postcondition,
    )
    controller.record_provider_invocation_completed(invocation)
    controller.set_strategy_snapshot(strategy.snapshot())

    after = controller.evaluate(provider_route="p:m")
    assert not after.terminal
    assert tuple(controller.snapshot()["strategy_frontier"]) == before
    assert controller.snapshot()["strategy_attempt_evidence"] == []


def test_only_typed_strategy_attempt_retires_one_frontier_item(tmp_path):
    strategy = build_execution_strategy(
        "Click Compose in Signal", {"desktop_click"},
    )
    controller = TurnTerminalityController(
        turn_id="wor259-typed-attempt",
        objective="Click Compose in Signal",
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    mechanism = strategy.mechanism_for_layer(StrategyLayer.NATIVE_ACCESSIBILITY)
    selected = strategy.select(mechanism, {"target": "Compose"})
    strategy.record(
        selected,
        "failed",
        failure_reason="confirmed_failure_before_effect",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    state = controller.snapshot()

    assert mechanism.mechanism_id not in state["strategy_frontier"]
    assert state["strategy_attempt_evidence"][0]["status"] == (
        AttemptStatus.FAILED.value
    )
    assert "keyboard_focus" in state["strategy_frontier"]


def _provider_event(attempt_id: str, command: str) -> CapabilityEvent:
    return CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="completed",
        outcome="success_verified",
        item_id="item-" + attempt_id,
        tool_attempt_id=attempt_id,
        tool_name="run_command",
        arguments={"command": command},
    )


def test_duplicate_and_late_provider_event_cannot_prove_second_node():
    strategy = build_execution_strategy(
        "Create file alpha, then create file beta", {"run_command"},
    )
    event = _provider_event("attempt-alpha", "New-Item alpha")
    batches = iter(((event,), (event,)))
    agent = OSIAAgent(model="fixture", lang="en")
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: next(batches),
    )
    agent._terminality_controller = None
    agent._terminality_strategy = strategy

    agent._consume_provider_capability_events()
    agent._consume_provider_capability_events()

    assert len(strategy.proven_postconditions) == 1
    assert len(strategy.pending_nodes()) == 1
    assert "beta" in strategy.pending_nodes()[0].target_object


def test_late_provider_event_keeps_original_attempt_node_binding():
    strategy = build_execution_strategy(
        "Create file alpha, then create file beta", {"run_command"},
    )
    alpha = strategy.mission.action_nodes[0]
    beta = strategy.mission.action_nodes[1]
    accepted = CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="accepted",
        outcome="dispatch_started",
        item_id="item-stable",
        tool_attempt_id="attempt-stable",
        tool_name="run_command",
        arguments={"command": "New-Item alpha"},
        semantic_node_id=alpha.node_id,
    )
    late = CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="completed",
        outcome="success_verified",
        item_id="item-stable",
        tool_attempt_id="attempt-stable",
        tool_name="run_command",
        arguments={"command": "New-Item alpha"},
        semantic_node_id=beta.node_id,
    )
    batches = iter(((accepted,), (late,)))
    agent = OSIAAgent(model="fixture", lang="en")
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: next(batches),
    )
    agent._terminality_controller = None
    agent._terminality_strategy = strategy

    agent._consume_provider_capability_events()
    agent._consume_provider_capability_events()

    assert alpha.status == "succeeded"
    assert beta.status != "succeeded"
    assert len(strategy.proven_postconditions) == 1


def test_mismatched_late_event_does_not_poison_legitimate_completion():
    strategy = build_execution_strategy(
        "Create file alpha, then create file beta", {"run_command"},
    )
    alpha = strategy.mission.action_nodes[0]
    accepted = CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="accepted",
        outcome="dispatch_started",
        item_id="item-stable",
        tool_attempt_id="attempt-stable",
        tool_name="run_command",
        arguments={"command": "New-Item alpha"},
        semantic_node_id=alpha.node_id,
    )
    mismatched = CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="completed",
        outcome="success_verified",
        item_id="item-stable",
        tool_attempt_id="attempt-stable",
        tool_name="run_command",
        arguments={"command": "New-Item beta"},
    )
    completed = CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="completed",
        outcome="success_verified",
        item_id="item-stable",
        tool_attempt_id="attempt-stable",
        tool_name="run_command",
        arguments={"command": "New-Item alpha"},
    )
    batches = iter(((accepted,), (mismatched,), (completed,)))
    agent = OSIAAgent(model="fixture", lang="en")
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: next(batches),
    )
    agent._terminality_controller = None
    agent._terminality_strategy = strategy

    agent._consume_provider_capability_events()
    agent._consume_provider_capability_events()
    agent._consume_provider_capability_events()

    assert alpha.status == "succeeded"
    assert len(strategy.proven_postconditions) == 1
    assert "beta" in strategy.pending_nodes()[0].target_object


def test_two_semantic_nodes_need_two_distinct_provider_events():
    strategy = build_execution_strategy(
        "Create file alpha, then create file beta", {"run_command"},
    )
    events = (
        _provider_event("attempt-alpha", "New-Item alpha"),
        _provider_event("attempt-beta", "New-Item beta"),
    )
    agent = OSIAAgent(model="fixture", lang="en")
    agent.client = SimpleNamespace(consume_capability_events=lambda: events)
    agent._terminality_controller = None
    agent._terminality_strategy = strategy

    agent._consume_provider_capability_events()

    assert strategy.pending_nodes() == []
    assert len(strategy.proven_postconditions) == 2


def test_missing_bound_node_cannot_fall_back_to_another_semantic_node():
    strategy = build_execution_strategy(
        "Open Calculator, then open Paint", {"run_command"},
    )

    node = strategy.record_external_event(
        "run_command",
        {"command": "Start-Process Paint"},
        outcome="success_verified",
        evidence="provider_native:late",
        semantic_node_id="retired-original-node",
    )

    assert node is None
    assert strategy.proven_postconditions == {}
    assert len(strategy.pending_nodes()) == 2


def test_local_capture_refuses_foreground_window_identity_change(tmp_path):
    from osia import agent as agent_module
    from osia import desktop_scene

    scene = _scene(target=False)
    output = tmp_path / "local-window.png"
    with (
        patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
        patch.object(
            desktop_scene.DESKTOP_SURFACE_REGISTRY,
            "native_identity",
            return_value=_native(),
        ),
        patch(
            "osia.desktop_scene._capture_bounds_timed",
            return_value=(b"png", 1.0, 1.0),
        ),
        patch(
            "osia.tools.desktop_control._foreground_window_win32",
            return_value=(999, "different", 123),
        ),
    ):
        bounds = agent_module._capture_exact_foreground_window(str(output))

    assert bounds is None
    assert not output.exists()


def test_local_vision_uses_exact_window_before_global_coordinates():
    from osia import desktop_scene
    from osia import agent as agent_module

    bounds = SurfaceBounds(-100, 50, 900, 650, dpi_scale=1.5)
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.agent._capture_screen", return_value=None),
            patch("osia.tools.desktop_control.resolve_element_rect", return_value=None),
            patch("osia.tools.desktop_control.activate_by_keyboard", return_value=None),
            patch("osia.agent._capture_exact_foreground_window", return_value=bounds),
            patch(
                "osia.agent._vision_locate",
                return_value={"found": True, "x": 100, "y": 100},
            ) as locate,
            patch(
                "osia.agent._refine_click_target",
                return_value=(0, 150, True),
            ),
            patch("osia.tools.desktop_control.click_at", return_value="clicked") as click,
            patch("osia.agent._screens_changed", return_value=True),
        ):
            result = agent_module.tool_desktop_click("Compose")
    finally:
        desktop_scene.reset_effective_model(route)

    assert not result.startswith("ERROR:")
    assert locate.call_count == 1
    assert locate.call_args.args[0].endswith("osia_desktop_window_local.png")
    click.assert_called_once_with(0, 150)


def test_global_vision_is_used_only_after_local_failure():
    from osia import desktop_scene
    from osia import agent as agent_module

    bounds = SurfaceBounds(100, 50, 900, 650)
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.agent._capture_screen", return_value=None),
            patch("osia.tools.desktop_control.resolve_element_rect", return_value=None),
            patch("osia.tools.desktop_control.activate_by_keyboard", return_value=None),
            patch("osia.agent._capture_exact_foreground_window", return_value=bounds),
            patch(
                "osia.agent._vision_locate",
                side_effect=(
                    {"found": False, "reason": "not in local window"},
                    {"found": True, "x": 400, "y": 300},
                ),
            ) as locate,
            patch("osia.tools.desktop_control.click_at", return_value="clicked") as click,
            patch("osia.agent._screens_changed", return_value=True),
        ):
            result = agent_module.tool_desktop_click("Compose")
    finally:
        desktop_scene.reset_effective_model(route)

    assert not result.startswith("ERROR:")
    assert locate.call_count == 2
    assert locate.call_args_list[0].args[0].endswith(
        "osia_desktop_window_local.png"
    )
    assert locate.call_args_list[1].args[0].endswith("osia_desktop_click.png")
    click.assert_called_once_with(400, 300)


def test_nonvision_legacy_path_never_invokes_coordinate_vision():
    from osia import desktop_scene
    from osia import agent as agent_module

    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        with (
            patch("osia.agent._capture_screen", return_value=None),
            patch("osia.tools.desktop_control.resolve_element_rect", return_value=None),
            patch("osia.tools.desktop_control.activate_by_keyboard", return_value=None),
            patch("osia.agent._capture_exact_foreground_window") as local_capture,
            patch("osia.agent._vision_locate") as locate,
            patch("osia.tools.desktop_control.click_at") as click,
        ):
            result = agent_module.tool_desktop_click("Compose")
    finally:
        desktop_scene.reset_effective_model(route)

    assert result.startswith("ERROR:")
    assert "no model screenshot" in result
    local_capture.assert_not_called()
    locate.assert_not_called()
    click.assert_not_called()


def test_send_confirmation_boundary_is_unchanged():
    from osia.tool_contracts import (
        ConfirmationPolicy,
        effective_contract,
        get_tool_spec,
    )

    locate = effective_contract(
        get_tool_spec("activate_desktop_surface"),
        {"surface_ref": "safe-ref", "objective": "Compose"},
    )
    send = effective_contract(
        get_tool_spec("activate_desktop_surface"),
        {"surface_ref": "safe-ref", "objective": "Send"},
    )
    enter = effective_contract(
        get_tool_spec("desktop_press"),
        {"key": "Enter"},
    )

    assert locate.confirmation is ConfirmationPolicy.NO_CONFIRMATION
    assert send.confirmation is ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION
    assert enter.confirmation is ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION


def test_outcome_unknown_freezes_alternate_mechanisms_until_reconciled():
    strategy = build_execution_strategy(
        "Click Compose in Signal", {"desktop_click"},
    )
    native = strategy.mechanism_for_layer(StrategyLayer.NATIVE_ACCESSIBILITY)
    keyboard = strategy.mechanism_for_layer(StrategyLayer.KEYBOARD)
    selected = strategy.select(native, {"target": "Compose"})
    strategy.record(
        selected,
        "ambiguous",
        failure_reason="uia_activation_outcome_unknown",
    )

    blocked = strategy.select(keyboard, {"target": "Compose"})
    assert blocked.allowed is False
    assert blocked.reason_code == "ambiguous_effect_requires_reconciliation"


def test_definitively_inapplicable_mechanism_is_typed_and_not_remaining():
    strategy = ExecutionStrategy(
        build_execution_strategy("Open Calculator", set()).mission,
        (
            StrategyMechanism(
                "local_vision",
                StrategyLayer.LOCAL_VISION,
                "fixture",
                applicable=True,
                mutation=True,
            ),
        ),
    )
    mechanism = strategy.mechanism_for_layer(StrategyLayer.LOCAL_VISION)

    attempt = strategy.mark_inapplicable(
        mechanism,
        reason_code="effective_model_non_vision",
    )

    assert attempt.status == AttemptStatus.INAPPLICABLE.value
    assert strategy.remaining_mechanisms() == []
