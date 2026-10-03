"""Focused WOR-262 exact UIA-before-coordinate frontier regressions."""

from __future__ import annotations

import time

from osia import execution_strategy as es
from osia.capability_federation import federated_capabilities
from osia.tool_contracts import (
    effective_static_tool_specs,
    effective_tool_surface_scope,
)


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
CRITERION = "Calculator visibly displays the exact result 10063."
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
WINDOW_REF = "surface-calculator-window"
CONTROL_REF = "surface-calculator-three"
DISPLAY_REF = "surface-calculator-display"


def _strategy(objective: str = "Enter 347 × 29 and evaluate it."):
    return es.build_execution_strategy(
        objective,
        TOOLS,
        postcondition=CRITERION,
        semantic_context=TASK,
    )


def _scene(
    *,
    actionable: bool = True,
    background_safe: bool = True,
    foreground_required: bool = False,
    foreground: bool = True,
    quality: str = "rich_target_resolved",
    observed_at: float | None = None,
) -> dict:
    observed_at = time.time() if observed_at is None else observed_at
    surfaces = [
        {
            "surface_ref": WINDOW_REF,
            "surface_kind": "window",
            "role": "WindowControl",
            "label": "Calculator",
            "enabled": True,
            "patterns": ["Window"],
            "actuation": {
                "available": True,
                "attempted_effect": "application_foreground",
                "preconditions": ["exact_target_exists", "target_restorable"],
                "background_safe": False,
                "foreground_required": False,
            },
        },
        {
            "surface_ref": DISPLAY_REF,
            "surface_kind": "control",
            "role": "TextControl",
            "label": "The display is 0",
            "enabled": True,
            "patterns": [],
            "actuation": {
                "available": False,
                "reason_code": "safe_uia_pattern_unavailable",
            },
        },
    ]
    if actionable:
        surfaces.append({
            "surface_ref": CONTROL_REF,
            "surface_kind": "control",
            "role": "ButtonControl",
            "label": "Three",
            "enabled": True,
            "patterns": ["Invoke"],
            "actuation": {
                "available": True,
                "attempted_effect": "desktop_uia_primitive",
                "preconditions": [
                    "exact_target_exists",
                    "fresh_runtime_identity",
                    *(["target_foreground"] if foreground_required else []),
                ],
                "background_safe": background_safe,
                "foreground_required": foreground_required,
                "patterns": ["Invoke"],
            },
        })
    return {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": WINDOW_REF,
        "window_generation": "calculator-generation-1",
        "accessibility_quality": quality,
        "target_resolution": "exact_target",
        "target_foreground": foreground,
        "target_minimized": False,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "calculator" if foreground else "chatgpt",
        "focus_observable": foreground,
        "focused_surface_ref": CONTROL_REF if foreground else "",
        "observed_at": observed_at,
        "surfaces": surfaces,
    }


def _observe(strategy, **scene_kwargs) -> dict:
    scene = _scene(**scene_kwargs)
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )
    return scene


def _static_frontier(strategy) -> set[str]:
    with es.execution_strategy_scope(
        strategy.mission.objective,
        strategy=strategy,
    ), effective_tool_surface_scope("chat"):
        return {spec.name for spec in effective_static_tool_specs()}


def _complete_visible_surface() -> set[str]:
    """WOR-300: every runtime capability of the active surface stays visible."""

    with effective_tool_surface_scope("chat"):
        return {spec.name for spec in effective_static_tool_specs()}


def test_01_exact_verifiable_uia_is_visible_and_coordinate_fallback_is_absent():
    strategy = _strategy()
    _observe(strategy)

    frontier = _static_frontier(strategy)

    primitive = strategy._latest_desktop_observation[
        "uia_actionable_frontier"
    ][0]
    assert primitive["surface_ref"] == CONTROL_REF
    assert primitive["verifier"] == "exact_surface_text"
    assert primitive["observable_effect_surface_refs"] == [DISPLAY_REF]
    assert frontier == _complete_visible_surface()
    assert "activate_desktop_surface" in frontier
    # Execution integrity: the exact-UIA frontier still refuses the lower
    # coordinate/vision tiers before dispatch...
    assert es.tool_applicability(strategy, "desktop_click", {}) == (
        False, "fresh_authoritative_visual_target_required",
    )
    assert es.tool_applicability(strategy, "take_screenshot", {}) == (
        False, "structured_uia_evidence_sufficient",
    )
    assert es.tool_applicability(strategy, "request_visual_observation", {}) == (
        False, "structured_uia_evidence_sufficient",
    )
    # ...while WOR-300/WOR-301 keep every runtime capability request-visible:
    # cross-domain capabilities are never relevance refused.
    for tool in ("query_document", "run_command"):
        assert es.tool_applicability(strategy, tool, {}) == (True, ""), tool


def test_02_excluded_lower_tier_is_never_refused():
    strategy = _strategy()
    _observe(strategy)
    attempts_before = len(strategy.attempts)

    decision = es.tool_preflight(
        "desktop_click",
        {"description": "Three"},
        strategy=strategy,
    )

    # WOR-301: frontier ranking never refuses an available call; the engine
    # still records the attempt for effect correlation.
    assert decision is None or decision.allowed is True
    assert len(strategy.attempts) >= attempts_before
    assert strategy.frozen_reason == ""


def test_03_uia_exhaustion_before_effect_exposes_keyboard_before_lower_tiers():
    strategy = _strategy()
    _observe(strategy)
    native = strategy.mechanism_for_layer(
        es.StrategyLayer.NATIVE_ACCESSIBILITY,
        node_id=strategy.goal.node_id,
    )
    assert native is not None
    strategy.mark_inapplicable(
        native,
        reason_code="exact_uia_unavailable_before_effect",
    )

    frontier = _static_frontier(strategy)

    assert frontier == _complete_visible_surface()
    assert {"desktop_type", "desktop_press"}.issubset(frontier)
    assert es.tool_applicability(strategy, "desktop_click", {})[0] is False
    assert es.tool_applicability(strategy, "take_screenshot", {})[0] is False
    assert es.tool_applicability(
        strategy, "activate_desktop_surface", {},
    ) == (False, "uia_frontier_exhausted_keyboard_ready")


def test_04_explicit_coordinate_goal_keeps_desktop_click_valid():
    strategy = _strategy(
        "Click screen coordinates (500, 400) in Calculator.",
    )
    _observe(strategy)

    assert es.tool_applicability(
        strategy,
        "desktop_click",
        {"description": "screen coordinates (500, 400)"},
    ) == (True, "")
    # WOR-289/WOR-300: V3 desktop_click is dormant and thus genuinely
    # unavailable on every surface, even when the semantic strategy accepts
    # the explicit coordinate intent.
    assert "desktop_click" not in _static_frontier(strategy)


def test_05_inaccessible_uia_progresses_to_focused_keyboard_first():
    strategy = _strategy()
    _observe(
        strategy,
        actionable=False,
        quality="poor_target_resolved",
    )

    frontier = _static_frontier(strategy)

    assert frontier == _complete_visible_surface()
    assert {"desktop_type", "desktop_press"}.issubset(frontier)
    assert es.tool_applicability(strategy, "desktop_click", {})[0] is False
    assert es.tool_applicability(strategy, "take_screenshot", {})[0] is False


def test_06_background_safe_uia_does_not_require_window_activation():
    strategy = _strategy()
    _observe(strategy, foreground=False, background_safe=True)

    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {
            "surface_ref": CONTROL_REF,
            "objective": "Three",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": DISPLAY_REF,
                "expected_text": "3",
            },
        },
    ) == (True, "")
    assert strategy.effect_is_completed(
        "application_foreground",
        target_application="calculator",
    ) is False
    assert "desktop_click" not in _static_frontier(strategy)


def test_07_foreground_required_uia_waits_for_fresh_verified_activation():
    strategy = _strategy()
    first = _observe(
        strategy,
        foreground=False,
        background_safe=False,
        foreground_required=True,
    )

    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {
            "surface_ref": CONTROL_REF,
            "objective": "Three",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": DISPLAY_REF,
                "expected_text": "3",
            },
        },
    ) == (False, "exact_target_activation_required")
    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {"surface_ref": WINDOW_REF, "objective": "Calculator"},
    ) == (True, "")

    second = _observe(
        strategy,
        foreground=True,
        background_safe=False,
        foreground_required=True,
        observed_at=float(first["observed_at"]) + 1.0,
    )
    strategy.remember_completed_effect(
        "application_foreground",
        target_application="calculator",
        target_surface=WINDOW_REF,
        evidence="fresh exact foreground observation",
        observed_at=float(second["observed_at"]),
    )
    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {
            "surface_ref": CONTROL_REF,
            "objective": "Three",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": DISPLAY_REF,
                "expected_text": "3",
            },
        },
    ) == (True, "")


def test_08_completed_window_focus_does_not_hide_control_activation_tool():
    strategy = _strategy()
    scene = _observe(strategy)
    strategy.remember_completed_effect(
        "application_foreground",
        target_application="calculator",
        target_surface=WINDOW_REF,
        evidence="fresh exact foreground observation",
        observed_at=float(scene["observed_at"]),
    )

    assert "activate_desktop_surface" in _static_frontier(strategy)
    assert es.tool_applicability(
        strategy,
        "activate_desktop_surface",
        {"surface_ref": WINDOW_REF, "objective": "Calculator"},
    ) == (False, "prerequisite_effect_already_completed")


def test_09_uia_frontier_survives_checkpoint_reconstruction():
    first = _strategy()
    scene = _observe(first)
    memory = es.export_execution_memory(first)
    second = _strategy()
    es.rebind_execution_memory(
        second,
        [memory],
        now=float(scene["observed_at"]) + 1.0,
    )

    assert second._latest_desktop_observation[
        "uia_actionable_frontier"
    ][0]["surface_ref"] == CONTROL_REF
    assert "desktop_click" not in _static_frontier(second)
    assert "activate_desktop_surface" in _static_frontier(second)


def test_10_real_autonomous_provider_frontier_matches_canonical_hierarchy():
    strategy = _strategy()
    _observe(strategy)

    with es.execution_strategy_scope(
        strategy.mission.objective,
        strategy=strategy,
    ), effective_tool_surface_scope("chat"):
        provider_frontier = {
            item.identity
            for item in federated_capabilities("chatgpt_account")
            if item.available and item.source == "osia"
        }

    assert "activate_desktop_surface" in provider_frontier
    assert provider_frontier == _complete_visible_surface()
    # WOR-300: request relevance never removes a runtime capability from the
    # provider frontier; only execution-integrity gates refuse a call.
    assert es.tool_applicability(strategy, "desktop_click", {}) == (
        False, "fresh_authoritative_visual_target_required",
    )
    assert es.tool_applicability(strategy, "take_screenshot", {}) == (
        False, "structured_uia_evidence_sufficient",
    )
    assert es.tool_applicability(strategy, "request_visual_observation", {}) == (
        False, "structured_uia_evidence_sufficient",
    )
    for tool in ("query_document", "run_command"):
        assert es.tool_applicability(strategy, tool, {}) == (True, ""), tool
