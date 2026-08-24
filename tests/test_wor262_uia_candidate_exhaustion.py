"""Focused WOR-262 exact UIA candidate-set exhaustion regressions."""

from __future__ import annotations

import time
from types import SimpleNamespace

from osia import desktop_scene
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
WINDOW_REF = "surface-window"
EXPRESSION_REF = "surface-expression"
CANDIDATE_A = "surface-candidate-a"
CANDIDATE_B = "surface-candidate-b"


def _strategy():
    return es.build_execution_strategy(
        "Enter the requested expression in Calculator.",
        TOOLS,
        postcondition=CRITERION,
        semantic_context=TASK,
    )


def _effect(text: str = "347 ×") -> dict:
    return {
        "kind": "exact_surface_text",
        "surface_ref": EXPRESSION_REF,
        "expected_text": text,
    }


def _candidate(
    surface_ref: str,
    *,
    generation: str = "generation-1",
    epoch: int = 1,
    signature: str | None = None,
    label: str = "Multiply",
    patterns: list[str] | None = None,
    automation_id: str = "",
) -> dict:
    supported_patterns = list(patterns or ["Invoke"])
    return {
        "surface_ref": surface_ref,
        "window_generation": generation,
        "structural_epoch": epoch,
        "runtime_signature": signature or "runtime-" + surface_ref,
        "automation_id": automation_id,
        "surface_kind": "control",
        "role": "ButtonControl",
        "label": label,
        "parent_surface_ref": WINDOW_REF,
        "enabled": True,
        "patterns": supported_patterns,
        "actuation": {
            "available": True,
            "attempted_effect": "desktop_uia_primitive",
            "preconditions": [
                "exact_target_exists",
                "fresh_runtime_identity",
            ],
            "background_safe": True,
            "foreground_required": False,
            "patterns": supported_patterns,
        },
    }


def _observe(
    strategy,
    candidates: list[dict],
    *,
    generation: str = "generation-1",
    epoch: int = 1,
    expression: str = "347",
    observed_at: float | None = None,
    additional_surfaces: list[dict] | None = None,
) -> None:
    scene = {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": WINDOW_REF,
        "window_generation": generation,
        "structural_epoch": epoch,
        "accessibility_quality": "rich_target_resolved",
        "target_resolution": "exact_target",
        "target_foreground": True,
        "target_minimized": False,
        "target_identity_evidence": "exact_application_identity",
        "foreground_application": "calculator",
        "focus_observable": True,
        "focused_surface_ref": candidates[0]["surface_ref"] if candidates else WINDOW_REF,
        "observed_at": time.time() if observed_at is None else observed_at,
        "surfaces": [
            {
                "surface_ref": WINDOW_REF,
                "window_generation": generation,
                "structural_epoch": epoch,
                "surface_kind": "window",
                "role": "WindowControl",
                "label": "Calculator",
                "enabled": True,
                "patterns": ["Window"],
                "actuation": {"available": False},
            },
            {
                "surface_ref": EXPRESSION_REF,
                "window_generation": generation,
                "structural_epoch": epoch,
                "runtime_signature": "runtime-expression",
                "surface_kind": "control",
                "role": "TextControl",
                "label": expression,
                "parent_surface_ref": WINDOW_REF,
                "enabled": True,
                "patterns": [],
                "actuation": {"available": False},
            },
            *(additional_surfaces or []),
            *candidates,
        ],
    }
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
                "structural_epoch": epoch,
                "window_surface_ref": WINDOW_REF,
            },
        },
        is_error=False,
        strategy=strategy,
    )


def _arguments(
    surface_ref: str,
    *,
    text: str = "347 ×",
    objective: str = "Multiply",
    action_pattern: str = "",
) -> dict:
    arguments = {
        "surface_ref": surface_ref,
        "objective": objective,
        "observable_effect": _effect(text),
    }
    if action_pattern:
        arguments["action_pattern"] = action_pattern
    return arguments


def _fail_candidate(
    strategy,
    surface_ref: str = CANDIDATE_A,
    *,
    reason: str = "stale_semantic_reference",
    action_pattern: str = "",
):
    decision = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(surface_ref, action_pattern=action_pattern),
        strategy=strategy,
    )
    assert decision is not None and decision.allowed
    es.record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="confirmed_failure_before_effect",
        is_error=True,
        reason_code=reason,
        result={
            "text": "ERROR: failed before effect",
            "receipt": {
                "outcome": "confirmed_failure_before_effect",
                "reason_code": reason,
                "dispatch_state": "not_dispatched",
            },
        },
        strategy=strategy,
    )
    return decision, strategy.attempt_by_id(decision.attempt_id)


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


def _fresh_after(attempt) -> float:
    return max(time.time(), float(attempt.dispatched_at) + 1.0)


def _reconcile_selected(
    strategy,
    decision,
    *,
    surface_ref: str,
    label: str,
    before_text: str,
    effect: dict,
):
    attempt = strategy.attempt_by_id(decision.attempt_id)
    action_pattern = attempt.action_pattern or "Invoke"
    before = {
        "application": "calculator",
        "window_surface_ref": WINDOW_REF,
        "window_generation": "generation-1",
        "target_foreground": True,
        "structural_epoch": 1,
        "observed_at": attempt.dispatched_at - 0.1,
        "state_signature": "before-" + before_text,
        "state_rows": [{
            "surface_ref": EXPRESSION_REF,
            "identity": "runtime-expression",
            "role": "TextControl",
            "label": before_text,
            "selected": False,
            "focused": False,
        }],
        "target": {
            "surface_ref": surface_ref,
            "application": "calculator",
            "process_ref": "process-calculator",
            "window_generation": "generation-1",
            "surface_kind": "control",
            "runtime_signature": "runtime-" + surface_ref,
            "role": "ButtonControl",
            "label": label,
            "parent_surface_ref": WINDOW_REF,
            "selected": False,
            "focused": True,
            "enabled": True,
            "patterns": [action_pattern],
        },
    }
    strategy.record(
        decision,
        "ambiguous",
        failure_reason="desktop_primitive_requires_exact_reconciliation",
        primitive_receipt={
            "kind": "desktop_uia_primitive",
            "mechanism": action_pattern.casefold(),
            "observable_effect": effect,
            "dispatched_at": attempt.dispatched_at,
            "before": before,
        },
    )
    assert strategy.reconcile(
        succeeded=True,
        evidence="exact replacement witness",
        primitive_only=True,
        evidence_details={
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "evidence_outcome": "confirmed_success",
            "reason_code": "exact_desktop_observable_effect_change",
            "postcondition": attempt.primitive_postcondition,
            "strategy_attempt_id": attempt.attempt_id,
            "semantic_node_id": attempt.node_id,
            "mechanism_id": attempt.mechanism_id,
            "attempted_effect": attempt.attempted_effect,
            "primitive_postcondition": attempt.primitive_postcondition,
            "observable_effect": effect,
            "composite_id": attempt.composite_id,
            "substep_id": attempt.substep_id,
            "substep_index": attempt.substep_index,
            "observed_application": "calculator",
            "observed_surface": WINDOW_REF,
            "window_generation": "generation-1",
            "structural_epoch": 1,
            "observed_at": attempt.dispatched_at + 1.0,
            "surface_continuity": "exact_same_generation",
            "previous_window_surface_ref": WINDOW_REF,
            "previous_window_generation": "generation-1",
            "no_partial_effect": False,
        },
    )
    return attempt


def test_01_one_failed_candidate_keeps_native_tier_active_when_another_exists():
    strategy = _strategy()
    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates)
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, candidates, observed_at=_fresh_after(failed))

    diagnostic = es._uia_candidate_diagnostic(strategy, strategy.goal)

    assert diagnostic["tier_state"] == "actionable"
    assert diagnostic["retired_candidate_count"] == 1
    assert diagnostic["remaining_candidate_count"] == 1
    assert diagnostic["remaining_candidates"][0]["surface_ref"] == CANDIDATE_B
    assert "activate_desktop_surface" in _frontier(strategy)


def test_02_failed_candidate_is_retired_without_retiring_native_tier():
    strategy = _strategy()
    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates)
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, candidates, observed_at=_fresh_after(failed))
    attempts_before = len(strategy.attempts)

    retry = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A),
        strategy=strategy,
    )

    assert retry is not None and retry.allowed is False
    assert retry.reason_code == "uia_candidate_retired_before_effect"
    assert len(strategy.attempts) == attempts_before
    assert es._structured_uia_frontier_state(strategy, strategy.goal) == "actionable"


def test_03_fresh_reresolution_can_replace_a_stale_candidate_identity():
    strategy = _strategy()
    _observe(strategy, [_candidate(CANDIDATE_A)])
    _decision, failed = _fail_candidate(strategy)
    replacement = _candidate(
        CANDIDATE_A,
        generation="generation-2",
        epoch=2,
        signature="runtime-replacement",
    )
    _observe(
        strategy,
        [replacement],
        generation="generation-2",
        epoch=2,
        observed_at=_fresh_after(failed),
    )

    decision = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A),
        strategy=strategy,
    )

    assert decision is not None and decision.allowed
    assert strategy.attempt_by_id(decision.attempt_id).substep_id == failed.substep_id


def test_04_fresh_viable_replacement_keeps_all_lower_tiers_absent():
    strategy = _strategy()
    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates)
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, candidates, observed_at=_fresh_after(failed))

    frontier = _provider_frontier(strategy)

    assert "activate_desktop_surface" in frontier
    assert {
        "desktop_type",
        "desktop_press",
        "desktop_click",
        "take_screenshot",
        "request_visual_observation",
    }.isdisjoint(frontier)


def test_05_all_fresh_exact_candidates_exhausted_exposes_keyboard_only():
    strategy = _strategy()
    candidate = _candidate(CANDIDATE_A)
    _observe(strategy, [candidate])
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, [candidate], observed_at=_fresh_after(failed))

    frontier = _provider_frontier(strategy)

    assert es._structured_uia_frontier_state(strategy, strategy.goal) == "exhausted"
    assert {"desktop_type", "desktop_press"}.issubset(frontier)
    assert "activate_desktop_surface" not in frontier
    assert "desktop_click" not in frontier
    assert "take_screenshot" not in frontier


def test_06_possible_effect_still_blocks_every_alternate_mutation():
    strategy = _strategy()
    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates)
    decision = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A),
        strategy=strategy,
    )
    assert decision is not None and decision.allowed
    strategy.record(
        decision,
        "ambiguous",
        failure_reason="effect_possible_postcondition_unknown",
    )

    alternate = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_B),
        strategy=strategy,
    )

    assert alternate is not None and alternate.allowed is False
    assert alternate.reason_code == "ambiguous_effect_requires_reconciliation"


def test_07_keyboard_fallback_keeps_the_exact_pending_substep_identity():
    strategy = _strategy()
    candidate = _candidate(CANDIDATE_A)
    _observe(strategy, [candidate])
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, [candidate], observed_at=_fresh_after(failed))

    keyboard = es.tool_preflight(
        "desktop_type", {"text": "*"}, strategy=strategy,
    )

    assert keyboard is not None and keyboard.allowed
    attempt = strategy.attempt_by_id(keyboard.attempt_id)
    assert attempt.substep_id == failed.substep_id
    assert attempt.substep_index == failed.substep_index
    assert attempt.attempted_effect == failed.attempted_effect
    es.record_tool_outcome(
        "desktop_type",
        keyboard,
        outcome="success_unverified",
        is_error=False,
        reason_code="tool_receipt",
        result="typed into focused target",
        strategy=strategy,
    )
    unobserved = es.tool_preflight(
        "desktop_press", {"key": "2"}, strategy=strategy,
    )
    assert unobserved is not None and unobserved.allowed is False
    assert unobserved.reason_code == "keyboard_substep_observation_required"
    waiting_frontier = _provider_frontier(strategy)
    assert "inspect_desktop_surfaces" in waiting_frontier
    assert {
        "activate_desktop_surface",
        "desktop_type",
        "desktop_press",
        "desktop_click",
        "take_screenshot",
        "request_visual_observation",
    }.isdisjoint(waiting_frontier)
    _observe(
        strategy,
        [candidate],
        expression="347 ×",
        observed_at=attempt.dispatched_at + 1.0,
    )
    assert es._pending_uia_substep(strategy, strategy.goal.node_id) == {}
    assert strategy._completed_substeps[-1]["substep_id"] == failed.substep_id


def test_08_keyboard_cannot_skip_to_future_composite_substeps():
    strategy = _strategy()
    candidate = _candidate(CANDIDATE_A)
    _observe(strategy, [candidate])
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, [candidate], observed_at=_fresh_after(failed))
    attempts_before = len(strategy.attempts)

    skipped = es.tool_preflight(
        "desktop_type", {"text": "29"}, strategy=strategy,
    )

    assert skipped is not None and skipped.allowed is False
    assert skipped.reason_code == "keyboard_action_skips_pending_substep"
    assert len(strategy.attempts) == attempts_before
    shortcut = es.tool_preflight(
        "desktop_press", {"key": "ctrl+v"}, strategy=strategy,
    )
    assert shortcut is not None and shortcut.allowed is False
    assert shortcut.reason_code == "keyboard_action_skips_pending_substep"
    assert len(strategy.attempts) == attempts_before


def test_09_provider_emitted_keyboard_is_rejected_while_replacement_is_viable():
    strategy = _strategy()
    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates)
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, candidates, observed_at=_fresh_after(failed))
    attempts_before = len(strategy.attempts)

    rejected = es.tool_preflight(
        "desktop_press", {"key": "2"}, strategy=strategy,
    )

    assert rejected is not None and rejected.allowed is False
    assert rejected.reason_code == "stronger_exact_uia_frontier_available"
    assert len(strategy.attempts) == attempts_before


def test_10_identity_mismatched_uia_continuation_cannot_skip_pending_effect():
    strategy = _strategy()
    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates)
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, candidates, observed_at=_fresh_after(failed))
    attempts_before = len(strategy.attempts)

    rejected = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_B, text="347 × 2"),
        strategy=strategy,
    )

    assert rejected is not None and rejected.allowed is False
    assert rejected.reason_code == "uia_action_skips_pending_substep"
    assert len(strategy.attempts) == attempts_before


def test_11_real_path_replacement_completes_once_then_exposes_next_substep():
    strategy = _strategy()
    prefix = [
        ("surface-three", "Three", "0", "3"),
        ("surface-four", "Four", "3", "34"),
        ("surface-seven", "Seven", "34", "347"),
    ]
    for surface_ref, label, before_text, expected_text in prefix:
        _observe(
            strategy,
            [_candidate(surface_ref, label=label)],
            expression=before_text,
        )
        prefix_decision = es.tool_preflight(
            "activate_desktop_surface",
            _arguments(
                surface_ref,
                text=expected_text,
                objective=label,
            ),
            strategy=strategy,
        )
        assert prefix_decision is not None and prefix_decision.allowed
        _reconcile_selected(
            strategy,
            prefix_decision,
            surface_ref=surface_ref,
            label=label,
            before_text=before_text,
            effect=_effect(expected_text),
        )
    assert [
        strategy.attempt_by_id(item["attempt_id"]).observable_effect[
            "expected_text"
        ]
        for item in strategy._completed_substeps
    ] == ["3", "34", "347"]

    candidates = [_candidate(CANDIDATE_A), _candidate(CANDIDATE_B)]
    _observe(strategy, candidates, expression="347")
    _decision, failed = _fail_candidate(strategy)
    _observe(strategy, candidates, observed_at=_fresh_after(failed))
    decision = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_B),
        strategy=strategy,
    )
    assert decision is not None and decision.allowed
    attempt = _reconcile_selected(
        strategy,
        decision,
        surface_ref=CANDIDATE_B,
        label="Multiply",
        before_text="347",
        effect=_effect(),
    )
    assert strategy.attempt_by_id(decision.attempt_id).status == "progressed"
    assert es._pending_uia_substep(strategy, strategy.goal.node_id) == {}
    _observe(
        strategy,
        [_candidate("surface-next")],
        expression="347 ×",
        observed_at=attempt.dispatched_at + 2.0,
    )
    next_decision = es.tool_preflight(
        "activate_desktop_surface",
        _arguments("surface-next", text="347 × 2"),
        strategy=strategy,
    )
    assert next_decision is not None and next_decision.allowed
    next_attempt = strategy.attempt_by_id(next_decision.attempt_id)
    assert next_attempt.substep_index == failed.substep_index + 1


def test_12_legacy_candidate_identity_remains_retired_fail_closed():
    strategy = _strategy()
    candidate = _candidate(CANDIDATE_A)
    _observe(strategy, [candidate])
    _decision, failed = _fail_candidate(strategy)
    snapshot = strategy.snapshot()
    raw_attempt = next(
        item for item in snapshot["attempts"]
        if item["attempt_id"] == failed.attempt_id
    )
    raw_attempt["target_surface_identity"] = {
        "surface_ref": CANDIDATE_A,
        "window_generation": "generation-1",
    }
    restored = es.ExecutionStrategy.from_snapshot(snapshot)
    _observe(
        restored,
        [candidate],
        observed_at=_fresh_after(failed),
    )

    rejected = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A),
        strategy=restored,
    )

    assert rejected is not None and rejected.allowed is False
    assert rejected.reason_code == "uia_candidate_retired_before_effect"


def test_13_verified_single_character_candidate_cannot_claim_another_delta():
    strategy = _strategy()
    prefix = [
        (CANDIDATE_A, "Three", "0", "3"),
        (CANDIDATE_B, "Four", "3", "34"),
    ]
    for surface_ref, label, before_text, expected_text in prefix:
        candidates = [
            _candidate(CANDIDATE_A, label="Three"),
            _candidate(CANDIDATE_B, label="Four"),
        ]
        _observe(strategy, candidates, expression=before_text)
        decision = es.tool_preflight(
            "activate_desktop_surface",
            _arguments(
                surface_ref,
                text=expected_text,
                objective=label,
            ),
            strategy=strategy,
        )
        assert decision is not None and decision.allowed
        _reconcile_selected(
            strategy,
            decision,
            surface_ref=surface_ref,
            label=label,
            before_text=before_text,
            effect=_effect(expected_text),
        )

    seven = _candidate("surface-seven", label="Seven")
    candidates = [
        _candidate(CANDIDATE_A, label="Three"),
        _candidate(CANDIDATE_B, label="Four"),
        seven,
    ]
    _observe(strategy, candidates, expression="34")
    attempts_before = len(strategy.attempts)

    wrong = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, text="347", objective="Seven"),
        strategy=strategy,
    )

    assert wrong is not None and wrong.allowed is False
    assert wrong.reason_code == "uia_candidate_conflicts_with_verified_text_delta"
    assert len(strategy.attempts) == attempts_before
    correct = es.tool_preflight(
        "activate_desktop_surface",
        _arguments("surface-seven", text="347", objective="Seven"),
        strategy=strategy,
    )
    assert correct is not None and correct.allowed


def test_14_semantic_candidate_with_callable_invoke_is_actionable():
    control = SimpleNamespace(
        GetInvokePattern=lambda: SimpleNamespace(Invoke=lambda: None),
    )
    assert "Invoke" in desktop_scene._supported_patterns(control)

    strategy = _strategy()
    _observe(strategy, [_candidate(CANDIDATE_A)])
    arguments = _arguments(CANDIDATE_A)
    decision = es.tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )

    assert decision is not None and decision.allowed
    assert arguments["action_pattern"] == "Invoke"
    assert strategy.attempt_by_id(decision.attempt_id).action_pattern == "Invoke"


def test_15_semantic_candidate_without_callable_invoke_is_not_actionable():
    control = SimpleNamespace(
        GetInvokePattern=lambda: SimpleNamespace(),
    )

    assert "Invoke" not in desktop_scene._supported_patterns(control)

    strategy = _strategy()
    _observe(
        strategy,
        [_candidate(CANDIDATE_A, patterns=["SelectionItem"])],
    )
    rejected = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, action_pattern="Invoke"),
        strategy=strategy,
    )

    assert rejected is not None and rejected.allowed is False
    assert rejected.reason_code == "uia_action_pattern_unavailable"


def test_16_another_callable_canonical_pattern_remains_actionable():
    control = SimpleNamespace(
        GetInvokePattern=lambda: SimpleNamespace(),
        GetSelectionItemPattern=lambda: SimpleNamespace(Select=lambda: None),
    )
    supported = desktop_scene._supported_patterns(control)

    assert "Invoke" not in supported
    assert "SelectionItem" in supported

    strategy = _strategy()
    _observe(
        strategy,
        [_candidate(CANDIDATE_A, patterns=["SelectionItem"])],
    )
    arguments = _arguments(
        CANDIDATE_A, action_pattern="SelectionItem",
    )
    selected = es.tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )

    assert selected is not None and selected.allowed
    assert strategy.attempt_by_id(selected.attempt_id).action_pattern == (
        "SelectionItem"
    )


def test_17_one_candidate_pattern_failure_does_not_retire_alternate_pair():
    strategy = _strategy()
    candidate = _candidate(
        CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
    )
    _observe(strategy, [candidate])
    _decision, failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="Invoke",
    )
    _observe(strategy, [candidate], observed_at=_fresh_after(failed))

    diagnostic = es._uia_candidate_diagnostic(strategy, strategy.goal)
    remaining_patterns = {
        item["action_pattern"] for item in diagnostic["remaining_candidates"]
    }

    assert diagnostic["candidate_pattern_pair_count"] == 2
    assert diagnostic["retired_candidate_count"] == 1
    assert remaining_patterns == {"SelectionItem"}
    alternate = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, action_pattern="SelectionItem"),
        strategy=strategy,
    )
    assert alternate is not None and alternate.allowed
    assert strategy.attempt_by_id(alternate.attempt_id).substep_id == (
        failed.substep_id
    )


def test_18_unavailable_pattern_receipt_is_retired_failed_before_effect():
    strategy = _strategy()
    candidate = _candidate(
        CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
    )
    _observe(strategy, [candidate])
    arguments = _arguments(CANDIDATE_A, action_pattern="Invoke")
    decision = es.tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    es.record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="confirmed_failure_before_effect",
        is_error=True,
        reason_code="tool_failed",
        result={
            "text": "ERROR: selected pattern unavailable",
            "receipt": {
                "dispatch_state": "not_dispatched",
                "outcome": "confirmed_failure_before_effect",
                "reason_code": "uia_action_pattern_unavailable",
                "actionability": {
                    "semantic_match": True,
                    "actionable_match": False,
                    "selected_pattern": "Invoke",
                    "selected_pattern_available": False,
                    "fresh_supported_patterns": ["SelectionItem"],
                    "dispatch_preflight_result": "failed_before_effect",
                    "failed_before_effect_reason": (
                        "uia_action_pattern_unavailable"
                    ),
                    "stale_rebind_status": "fresh_exact_reference",
                    "retirement_state": (
                        "eligible_for_exact_pair_retirement"
                    ),
                    "automation_id": "opaque-action-id",
                },
            },
        },
        strategy=strategy,
    )

    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt.status == "failed"
    assert attempt.failure_or_stagnation_reason == (
        "uia_action_pattern_unavailable"
    )
    assert attempt.action_pattern == "Invoke"
    assert attempt.actionability_evidence == {
        "semantic_match": True,
        "actionable_match": False,
        "selected_pattern_available": False,
        "dispatch_preflight_result": "failed_before_effect",
        "failed_before_effect_reason": "uia_action_pattern_unavailable",
        "stale_rebind_status": "fresh_exact_reference",
        "retirement_state": "retired_failed_before_effect",
        "automation_id": "opaque-action-id",
        "fresh_supported_patterns": ["SelectionItem"],
    }
    assert strategy.frozen_reason == ""


def test_19_fresh_replacement_with_valid_pattern_becomes_eligible():
    strategy = _strategy()
    original = _candidate(CANDIDATE_A, patterns=["Invoke"])
    _observe(strategy, [original])
    _decision, failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="Invoke",
    )
    replacement = _candidate(
        CANDIDATE_A,
        generation="generation-2",
        epoch=2,
        signature="runtime-replacement",
        patterns=["SelectionItem"],
    )
    _observe(
        strategy,
        [replacement],
        generation="generation-2",
        epoch=2,
        observed_at=_fresh_after(failed),
    )

    selected = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, action_pattern="SelectionItem"),
        strategy=strategy,
    )

    assert selected is not None and selected.allowed
    attempt = strategy.attempt_by_id(selected.attempt_id)
    assert attempt.substep_id == failed.substep_id
    assert attempt.action_pattern == "SelectionItem"


def test_20_all_exact_candidate_pattern_pairs_exhaust_before_keyboard():
    strategy = _strategy()
    candidate = _candidate(
        CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
    )
    _observe(strategy, [candidate])
    _decision, invoke_failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="Invoke",
    )
    _observe(
        strategy, [candidate], observed_at=_fresh_after(invoke_failed),
    )
    _decision, selection_failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="SelectionItem",
    )
    _observe(
        strategy, [candidate], observed_at=_fresh_after(selection_failed),
    )

    assert es._structured_uia_frontier_state(strategy, strategy.goal) == (
        "exhausted"
    )
    keyboard = es.tool_preflight(
        "desktop_type", {"text": "*"}, strategy=strategy,
    )
    assert keyboard is not None and keyboard.allowed
    assert strategy.attempt_by_id(keyboard.attempt_id).substep_id == (
        invoke_failed.substep_id
    )


def test_21_unavailable_pair_can_continue_safely_inside_uia():
    strategy = _strategy()
    candidates = [
        _candidate(CANDIDATE_A, patterns=["Invoke"]),
        _candidate(CANDIDATE_B, patterns=["SelectionItem"]),
    ]
    _observe(strategy, candidates)
    _decision, failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="Invoke",
    )
    _observe(strategy, candidates, observed_at=_fresh_after(failed))

    alternate = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_B, action_pattern="SelectionItem"),
        strategy=strategy,
    )

    assert alternate is not None and alternate.allowed
    assert strategy.frozen_reason == ""
    assert strategy.attempt_by_id(alternate.attempt_id).substep_id == (
        failed.substep_id
    )


def test_22_possible_effect_blocks_another_pattern_on_same_candidate():
    strategy = _strategy()
    candidate = _candidate(
        CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
    )
    _observe(strategy, [candidate])
    invoke = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, action_pattern="Invoke"),
        strategy=strategy,
    )
    assert invoke is not None and invoke.allowed
    strategy.record(
        invoke,
        "ambiguous",
        failure_reason="effect_possible_postcondition_unknown",
    )

    alternate = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, action_pattern="SelectionItem"),
        strategy=strategy,
    )

    assert alternate is not None and alternate.allowed is False
    assert alternate.reason_code == "ambiguous_effect_requires_reconciliation"


def test_23_structured_frontier_contains_only_actionable_pattern_pairs():
    scene = {
        "window_surface_ref": WINDOW_REF,
        "window_generation": "generation-1",
        "structural_epoch": 1,
        "surfaces": [
            _candidate(
                CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
            ),
            {
                **_candidate(CANDIDATE_B, patterns=["Invoke"]),
                "actuation": {
                    "available": False,
                    "attempted_effect": "",
                    "reason_code": "safe_uia_pattern_unavailable",
                },
            },
            {
                "surface_ref": EXPRESSION_REF,
                "role": "TextControl",
                "label": "before",
                "patterns": [],
                "actuation": {"available": False},
            },
        ],
    }

    frontier = es._desktop_uia_frontier_from_scene(scene)

    assert {
        (item["surface_ref"], item["action_pattern"])
        for item in frontier
    } == {
        (CANDIDATE_A, "Invoke"),
        (CANDIDATE_A, "SelectionItem"),
    }


def test_24_verified_prefix_survives_pattern_rebinding_and_progresses():
    strategy = _strategy()
    for surface_ref, label, before_text, expected_text in (
        ("surface-three", "Three", "0", "3"),
        ("surface-four", "Four", "3", "34"),
        ("surface-seven", "Seven", "34", "347"),
    ):
        _observe(
            strategy,
            [_candidate(surface_ref, label=label)],
            expression=before_text,
        )
        decision = es.tool_preflight(
            "activate_desktop_surface",
            _arguments(
                surface_ref,
                text=expected_text,
                objective=label,
                action_pattern="Invoke",
            ),
            strategy=strategy,
        )
        assert decision is not None and decision.allowed
        _reconcile_selected(
            strategy,
            decision,
            surface_ref=surface_ref,
            label=label,
            before_text=before_text,
            effect=_effect(expected_text),
        )

    operator = _candidate(
        CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
    )
    _observe(strategy, [operator], expression="347")
    _decision, failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="Invoke",
    )
    _observe(strategy, [operator], observed_at=_fresh_after(failed))
    alternate = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(CANDIDATE_A, action_pattern="SelectionItem"),
        strategy=strategy,
    )
    assert alternate is not None and alternate.allowed
    progressed = _reconcile_selected(
        strategy,
        alternate,
        surface_ref=CANDIDATE_A,
        label="Multiply",
        before_text="347",
        effect=_effect(),
    )

    assert [
        strategy.attempt_by_id(item["attempt_id"]).observable_effect[
            "expected_text"
        ]
        for item in strategy._completed_substeps
    ] == ["3", "34", "347", "347 ×"]
    _observe(
        strategy,
        [_candidate("surface-next", label="Two")],
        expression="347 ×",
        observed_at=progressed.dispatched_at + 2.0,
    )
    next_step = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(
            "surface-next", text="347 × 2", objective="Two",
            action_pattern="Invoke",
        ),
        strategy=strategy,
    )
    assert next_step is not None and next_step.allowed
    assert strategy.attempt_by_id(next_step.attempt_id).substep_index == (
        failed.substep_index + 1
    )


def test_25_lower_tiers_stay_hidden_while_actionable_pair_remains():
    strategy = _strategy()
    candidate = _candidate(
        CANDIDATE_A, patterns=["Invoke", "SelectionItem"],
    )
    _observe(strategy, [candidate])
    _decision, failed = _fail_candidate(
        strategy,
        reason="uia_action_pattern_unavailable",
        action_pattern="Invoke",
    )
    _observe(strategy, [candidate], observed_at=_fresh_after(failed))

    frontier = _provider_frontier(strategy)

    assert "activate_desktop_surface" in frontier
    assert {
        "desktop_type",
        "desktop_press",
        "desktop_click",
        "take_screenshot",
        "request_visual_observation",
    }.isdisjoint(frontier)


def test_26_repeated_native_literal_family_keeps_verified_witness_after_restore():
    strategy = _strategy()
    first = _candidate(
        "surface-five",
        label="Five",
        automation_id="num5Button",
    )
    _observe(strategy, [first], expression="0")
    first_decision = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(
            first["surface_ref"],
            text="5",
            objective="Five",
            action_pattern="Invoke",
        ),
        strategy=strategy,
    )
    assert first_decision is not None and first_decision.allowed
    _reconcile_selected(
        strategy,
        first_decision,
        surface_ref=first["surface_ref"],
        label="Five",
        before_text="0",
        effect=_effect("5"),
    )

    second = _candidate(
        "surface-two",
        label="Two",
        automation_id="num2Button",
    )
    history = {
        "surface_ref": "surface-history",
        "window_generation": "generation-1",
        "structural_epoch": 1,
        "runtime_signature": "runtime-history",
        "surface_kind": "control",
        "role": "TextControl",
        "label": "5 +",
        "parent_surface_ref": WINDOW_REF,
        "enabled": True,
        "patterns": [],
        "actuation": {"available": False},
    }
    _observe(
        strategy,
        [second],
        expression="5",
        additional_surfaces=[history],
    )
    restored = es.ExecutionStrategy.from_snapshot(strategy.snapshot())

    rejected = es.tool_preflight(
        "activate_desktop_surface",
        {
            "surface_ref": second["surface_ref"],
            "objective": "Two",
            "action_pattern": "Invoke",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": history["surface_ref"],
                "expected_text": "5 + 2",
            },
        },
        strategy=restored,
    )
    accepted = es.tool_preflight(
        "activate_desktop_surface",
        _arguments(
            second["surface_ref"],
            text="2",
            objective="Two",
            action_pattern="Invoke",
        ),
        strategy=restored,
    )

    assert rejected is not None and rejected.allowed is False
    assert rejected.reason_code == "uia_literal_family_witness_conflict"
    assert accepted is not None and accepted.allowed is True
