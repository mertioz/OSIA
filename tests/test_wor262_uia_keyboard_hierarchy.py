"""Focused WOR-262 exact UIA-before-keyboard frontier regressions."""

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


def _strategy():
    return es.build_execution_strategy(
        "Enter 347 × 29 and evaluate it.",
        TOOLS,
        postcondition=CRITERION,
        semantic_context=TASK,
    )


def _observe(
    strategy,
    *,
    actionable: bool = True,
    foreground: bool = True,
    focus: bool = True,
    foreground_application: str = "calculator",
    observed_at: float | None = None,
) -> None:
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
                "preconditions": ["exact_target_exists"],
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
            "actuation": {"available": False},
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
                ],
                "background_safe": True,
                "foreground_required": False,
                "patterns": ["Invoke"],
            },
        })
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": {
                "application": "calculator",
                "requested_application": "calculator",
                "window_surface_ref": WINDOW_REF,
                "window_generation": "calculator-generation-1",
                "accessibility_quality": (
                    "rich_target_resolved" if actionable
                    else "poor_target_resolved"
                ),
                "target_resolution": "exact_target",
                "target_foreground": foreground,
                "target_minimized": False,
                "target_identity_evidence": (
                    "uia_content_application:calculatorapp"
                ),
                "foreground_application": foreground_application,
                "focus_observable": focus,
                "focused_surface_ref": CONTROL_REF if focus else "",
                "observed_at": (
                    time.time() if observed_at is None else observed_at
                ),
                "surfaces": surfaces,
            },
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )


def _frontier(strategy) -> set[str]:
    with es.execution_strategy_scope(
        strategy.mission.objective,
        strategy=strategy,
    ), effective_tool_surface_scope("autonomous"):
        return {spec.name for spec in effective_static_tool_specs()}


def _provider_frontier(strategy) -> set[str]:
    with es.execution_strategy_scope(
        strategy.mission.objective,
        strategy=strategy,
    ), effective_tool_surface_scope("autonomous"):
        return {
            item.identity
            for item in federated_capabilities("chatgpt_account")
            if item.available and item.source == "osia"
        }


def _retire_uia(strategy, reason: str = "exact_uia_unavailable_before_effect"):
    native = strategy.mechanism_for_layer(
        es.StrategyLayer.NATIVE_ACCESSIBILITY,
        node_id=strategy.goal.node_id,
    )
    assert native is not None
    strategy.mark_inapplicable(native, reason_code=reason)


def _retire_keyboard(strategy):
    keyboard = strategy.mechanism_for_layer(
        es.StrategyLayer.KEYBOARD,
        node_id=strategy.goal.node_id,
    )
    assert keyboard is not None
    strategy.mark_inapplicable(
        keyboard,
        reason_code="keyboard_focus_unavailable_before_effect",
    )


def test_01_viable_exact_uia_hides_keyboard_but_keeps_uia_visible():
    strategy = _strategy()
    _observe(strategy)

    frontier = _frontier(strategy)

    assert "activate_desktop_surface" in frontier
    assert "desktop_type" not in frontier
    assert "desktop_press" not in frontier


def test_02_provider_frontier_matches_viable_uia_hierarchy():
    strategy = _strategy()
    _observe(strategy)

    frontier = _provider_frontier(strategy)

    assert "activate_desktop_surface" in frontier
    assert {
        "desktop_type",
        "desktop_press",
        "desktop_click",
        "take_screenshot",
        "request_visual_observation",
        "run_command",
        "query_document",
    }.isdisjoint(frontier)


def test_03_provider_emitted_excluded_keyboard_is_rejected_before_effect():
    strategy = _strategy()
    _observe(strategy)
    attempts_before = len(strategy.attempts)

    decision = es.tool_preflight(
        "desktop_type", {"text": "347*29="}, strategy=strategy,
    )

    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "stronger_exact_uia_frontier_available"
    assert len(strategy.attempts) == attempts_before
    assert strategy.frozen_reason == ""


def test_04_uia_exhaustion_before_effect_exposes_focused_keyboard_only():
    strategy = _strategy()
    _observe(strategy)
    _retire_uia(strategy)

    frontier = _provider_frontier(strategy)

    assert {"desktop_type", "desktop_press"}.issubset(frontier)
    assert "activate_desktop_surface" not in frontier
    assert "desktop_click" not in frontier
    assert "take_screenshot" not in frontier


def test_05_inaccessible_uia_can_expose_focused_keyboard():
    strategy = _strategy()
    _observe(strategy, actionable=False)

    assert {"desktop_type", "desktop_press"}.issubset(_frontier(strategy))
    decision = es.tool_preflight(
        "desktop_type", {"text": "347*29="}, strategy=strategy,
    )
    assert decision is not None and decision.allowed is True


def test_06_keyboard_fallback_requires_fresh_exact_focus():
    strategy = _strategy()
    _observe(strategy, actionable=False, focus=False)

    assert es.tool_applicability(
        strategy, "desktop_type", {"text": "347*29="},
    ) == (False, "fresh_exact_keyboard_focus_required")


def test_07_stale_or_wrong_focus_blocks_keyboard():
    stale = _strategy()
    _observe(
        stale,
        actionable=False,
        observed_at=time.time() - 31.0,
    )
    wrong = _strategy()
    _observe(
        wrong,
        actionable=False,
        foreground_application="notepad",
    )

    assert es.tool_applicability(
        stale, "desktop_press", {"key": "3"},
    ) == (False, "stale_keyboard_focus_observation")
    assert es.tool_applicability(
        wrong, "desktop_press", {"key": "3"},
    ) == (False, "exact_keyboard_focus_target_mismatch")


def test_08_background_safe_uia_does_not_need_focus_or_activation():
    strategy = _strategy()
    _observe(
        strategy,
        foreground=False,
        focus=False,
        foreground_application="notepad",
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
    assert "desktop_type" not in _frontier(strategy)


def test_09_coordinate_and_vision_remain_below_focused_keyboard():
    strategy = _strategy()
    _observe(strategy, actionable=False)

    frontier = _frontier(strategy)

    assert {"desktop_type", "desktop_press"}.issubset(frontier)
    assert "desktop_click" not in frontier
    assert "take_screenshot" not in frontier
    assert "request_visual_observation" not in frontier


def test_10_lower_tiers_open_only_after_keyboard_exhaustion():
    strategy = _strategy()
    _observe(strategy, actionable=False)
    _retire_keyboard(strategy)

    frontier = _frontier(strategy)

    assert "desktop_type" not in frontier
    assert "desktop_press" not in frontier
    assert "desktop_click" in frontier
    assert "take_screenshot" in frontier


def test_11_focus_evidence_survives_fresh_checkpoint_reconstruction():
    first = _strategy()
    _observe(first, actionable=False)
    memory = es.export_execution_memory(first)
    second = _strategy()
    es.rebind_execution_memory(second, [memory], now=time.time() + 1.0)

    assert second._latest_desktop_observation["focus_observable"] is True
    assert second._latest_desktop_observation["focused_surface_ref"] == CONTROL_REF
    assert "desktop_type" in _frontier(second)


def test_12_stateless_uia_frontier_still_carries_independent_verifier():
    strategy = _strategy()
    _observe(strategy)

    primitive = strategy._latest_desktop_observation[
        "uia_actionable_frontier"
    ][0]
    assert primitive["verifier"] == "exact_surface_text"
    assert primitive["observable_effect_surface_refs"] == [DISPLAY_REF]
