"""WOR-262 exact UIA target identity checkpoint/reconciliation sentinels."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import patch

from osia import desktop_scene
from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator
from osia.desktop_scene import DesktopScene, TargetSurfaceSelection
from osia.desktop_surface import (
    DESKTOP_SURFACE_REGISTRY,
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
    SurfaceResolution,
)
from osia.execution_strategy import (
    build_execution_strategy,
    execution_strategy_scope,
    export_execution_memory,
    record_tool_observation,
    record_tool_outcome,
    rebind_execution_memory,
    tool_preflight,
)


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
TOOLS = {
    "activate_desktop_surface",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
}


def _surface(
    ref_id: str,
    label: str,
    *,
    role: str,
    kind: SurfaceKind,
    generation: str = "win-generation-1",
    parent: str | None = "window-1",
    patterns: tuple[str, ...] = (),
    structural_epoch: int = 4,
) -> DesktopSurfaceRef:
    return DesktopSurfaceRef(
        ref_id=ref_id,
        application_id="calculator",
        process_ref="proc-calculator-1",
        window_generation=generation,
        surface_kind=kind,
        role=role,
        accessible_label=label,
        enabled=True,
        parent_ref=None if kind is SurfaceKind.WINDOW else parent,
        bounds=(
            SurfaceBounds(100, 100, 700, 800)
            if kind is SurfaceKind.WINDOW else
            SurfaceBounds(150, 200, 250, 260)
        ),
        runtime_signature=(
            "" if kind is SurfaceKind.WINDOW else "runtime-" + ref_id.split("-")[-1]
        ),
        patterns=patterns,
        structural_epoch=structural_epoch,
        observed_at=time.time() - 2.0,
    )


def _scene(
    result: str,
    *,
    generation: str = "win-generation-1",
    window_ref: str = "window-1",
    control_ref: str = "button-three",
    structural_epoch: int = 4,
    observed_at: float | None = None,
) -> DesktopScene:
    window = _surface(
        window_ref,
        "Localized title is not identity",
        role="WindowControl",
        kind=SurfaceKind.WINDOW,
        generation=generation,
        parent=None,
        patterns=("Window",),
        structural_epoch=structural_epoch,
    )
    button = _surface(
        control_ref,
        "Localized button label",
        role="ButtonControl",
        kind=SurfaceKind.CONTROL,
        generation=generation,
        parent=window_ref,
        patterns=("Invoke",),
        structural_epoch=structural_epoch,
    )
    display = _surface(
        "display",
        result,
        role="TextControl",
        kind=SurfaceKind.CONTROL,
        generation=generation,
        parent=window_ref,
        structural_epoch=structural_epoch,
    )
    return DesktopScene(
        application_id="calculator",
        window_ref=window,
        surfaces=(window, button, display),
        structural_epoch=structural_epoch,
        scene_signature="scene-" + result,
        accessibility_complete=True,
        observed_at=time.time() if observed_at is None else observed_at,
        uia_tree_observed=True,
        foreground=True,
    )


def _strategy():
    strategy = build_execution_strategy(TASK, TOOLS)
    open_node = strategy.mission.action_nodes[0]
    assert strategy.verify_postcondition(
        "exact structured open evidence",
        postcondition=open_node.observable_success_postcondition,
    )
    return strategy


def _dispatch(strategy, before: DesktopScene, *, native_hwnd: int | None = None):
    target = before.surfaces[1]
    arguments = {
        "surface_ref": target.ref_id,
        "objective": target.accessible_label,
        "observable_effect": {
            "kind": "exact_surface_text",
            "surface_ref": before.surfaces[2].ref_id,
            "expected_text": "3",
        },
    }
    native = NativeSurfaceIdentity(
        pid=4242,
        process_started_at=100.0,
        native_window_id=native_hwnd or 9090,
        runtime_id=(42, 7),
        class_name="ApplicationFrameWindow",
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
            "" if surface is not None else "unknown_surface_ref",
        )

    with patch.object(DESKTOP_SURFACE_REGISTRY, "resolve", side_effect=resolve):
        decision = tool_preflight(
            "activate_desktop_surface", arguments, strategy=strategy,
        )
        assert decision is not None and decision.allowed
        attempt = strategy.attempt_by_id(decision.attempt_id)
        primitive = {
            "kind": "desktop_uia_primitive",
            "mechanism": "invoke",
            "observable_effect": arguments["observable_effect"],
            "dispatched_at": attempt.dispatched_at,
            "before": desktop_scene._primitive_state_projection(before, target),
        }
        result = {
            "text": "UIA dispatch uncertain",
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "dispatched",
                "outcome": "success_unverified",
                "reason_code": "native_accessibility_dispatched",
                "surface_ref": target.ref_id,
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
    return decision, strategy.attempt_by_id(decision.attempt_id), arguments


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


def _observation(strategy, scene: DesktopScene) -> tuple[dict, bool]:
    with (
        execution_strategy_scope(TASK, strategy=strategy),
        patch(
            "osia.desktop_scene._select_desktop_target",
            return_value=_selection(scene),
        ),
    ):
        result = desktop_scene.inspect_desktop_surfaces("calculator")
    resolved = record_tool_observation(
        "inspect_desktop_surfaces",
        result,
        is_error=False,
        strategy=strategy,
    )
    return result, resolved


def _rebuild(memory: dict):
    rebuilt = build_execution_strategy(TASK, TOOLS)
    rebind_execution_memory(rebuilt, [memory], now=time.time())
    return rebuilt


def test_exact_surface_hwnd_and_generation_are_bound_at_dispatch():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(
        strategy, _scene("0"), native_hwnd=7340034,
    )

    assert attempt.target_surface == "button-three"
    assert attempt.target_surface_identity["surface_ref"] == "button-three"
    assert attempt.target_surface_identity["window_surface_ref"] == "window-1"
    assert attempt.target_surface_identity["window_generation"] == (
        "win-generation-1"
    )
    assert attempt.target_surface_identity["native_window_id"] == "7340034"


def test_exact_surface_identity_survives_checkpoint_serialization():
    strategy = _strategy()
    decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    memory = json.loads(json.dumps(export_execution_memory(strategy)))
    persisted = memory["effects"][1]["attempts"][-1]

    assert persisted["attempt_id"] == decision.attempt_id
    assert persisted["target_surface"] == attempt.target_surface
    assert persisted["target_surface_identity"] == attempt.target_surface_identity
    before = persisted["primitive_receipt"]["before"]
    assert before["window_surface_ref"] == "window-1"
    assert before["window_generation"] == "win-generation-1"
    assert before["target"]["surface_ref"] == "button-three"
    assert before["target"]["process_ref"] == "proc-calculator-1"
    assert before["target"]["runtime_signature"] == "runtime-three"
    assert before["state_rows"] == attempt.primitive_receipt["before"][
        "state_rows"
    ]


def test_reconstructed_waiting_tool_retains_correlated_target_identity():
    strategy = _strategy()
    decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    normalized_task = state._normalize_task(0, {
        "objective": TASK,
        "success_criterion": "10063 is independently observed",
        "status": "waiting_tool",
        "tool_attempt_id": "attempt_checkpoint_262",
        "execution_memory": export_execution_memory(strategy),
    })
    rebuilt = _rebuild(normalized_task["execution_memory"])
    restored = rebuilt.attempt_by_id(decision.attempt_id)

    assert normalized_task["status"] == "waiting_tool"
    assert restored is not None
    assert restored.target_surface_identity == attempt.target_surface_identity
    assert restored.primitive_receipt["before"]["window_surface_ref"] == (
        attempt.primitive_receipt["before"]["window_surface_ref"]
    )
    assert restored.primitive_receipt["before"]["window_generation"] == (
        attempt.primitive_receipt["before"]["window_generation"]
    )
    assert restored.primitive_receipt["before"]["target"][
        "runtime_signature"
    ] == attempt.primitive_receipt["before"]["target"]["runtime_signature"]
    assert restored.primitive_receipt["before"]["state_rows"] == (
        attempt.primitive_receipt["before"]["state_rows"]
    )
    assert rebuilt._frozen_attempt_id == decision.attempt_id


def test_fresh_same_surface_evidence_resolves_exact_primitive():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    result, resolved = _observation(
        strategy,
        _scene("3", observed_at=attempt.dispatched_at + 1.0),
    )

    assert resolved is True
    assert result["receipt"]["surface_continuity"] == "exact_same_generation"
    assert strategy._completed_substeps[-1]["attempt_id"] == attempt.attempt_id


def test_wrong_surface_does_not_resolve_exact_primitive():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    wrong = _scene(
        "3",
        window_ref="wrong-window",
        control_ref="wrong-button",
        observed_at=attempt.dispatched_at + 1.0,
    )
    _result, resolved = _observation(strategy, wrong)

    assert resolved is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_stale_generation_without_proven_continuity_does_not_resolve():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    changed = _scene(
        "3",
        generation="win-generation-2",
        window_ref="window-2",
        control_ref="button-three-2",
        observed_at=attempt.dispatched_at + 1.0,
    )
    with execution_strategy_scope(TASK, strategy=strategy):
        evidence = desktop_scene._exact_primitive_reconciliation(changed)

    assert evidence == {}
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_stale_generation_with_structured_semantic_continuity_rebinds_safely():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    changed = _scene(
        "3",
        generation="win-generation-2",
        window_ref="window-2",
        control_ref="button-three-2",
        observed_at=attempt.dispatched_at + 1.0,
    )
    # Re-resolution uses the same runtime signature, role, process and patterns;
    # neither localized title nor application name alone can prove continuity.
    changed_button = replace(
        changed.surfaces[1],
        runtime_signature=attempt.target_surface_identity["runtime_signature"],
    )
    changed = replace(
        changed,
        surfaces=(changed.window_ref, changed_button, changed.surfaces[2]),
    )
    result, resolved = _observation(strategy, changed)

    assert resolved is True
    assert result["receipt"]["surface_continuity"] == (
        "semantic_reresolution_proven"
    )
    assert result["receipt"]["previous_window_generation"] == (
        "win-generation-1"
    )
    assert result["receipt"]["rebound_window_generation"] == (
        "win-generation-2"
    )


def test_same_generation_new_epoch_rebinds_exact_runtime_identity():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    changed = _scene(
        "3",
        window_ref="window-epoch-2",
        control_ref="button-three-epoch-2",
        structural_epoch=5,
        observed_at=attempt.dispatched_at + 1.0,
    )
    changed_button = replace(
        changed.surfaces[1],
        runtime_signature=attempt.target_surface_identity["runtime_signature"],
    )
    changed = replace(
        changed,
        surfaces=(changed.window_ref, changed_button, changed.surfaces[2]),
    )
    result, resolved = _observation(strategy, changed)

    assert resolved is True
    assert result["receipt"]["surface_continuity"] == (
        "semantic_reresolution_proven"
    )
    assert result["receipt"]["previous_window_generation"] == (
        "win-generation-1"
    )
    assert result["receipt"]["rebound_window_generation"] == (
        "win-generation-1"
    )
    assert result["receipt"]["previous_structural_epoch"] == 4
    assert result["receipt"]["rebound_structural_epoch"] == 5


def test_same_generation_new_refs_without_new_epoch_remain_ambiguous():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    changed = _scene(
        "3",
        window_ref="window-rebound-without-epoch",
        control_ref="button-three-rebound-without-epoch",
        observed_at=attempt.dispatched_at + 1.0,
    )
    changed_button = replace(
        changed.surfaces[1],
        runtime_signature=attempt.target_surface_identity["runtime_signature"],
    )
    changed = replace(
        changed,
        surfaces=(changed.window_ref, changed_button, changed.surfaces[2]),
    )
    _result, resolved = _observation(strategy, changed)

    assert resolved is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_same_generation_rebind_requires_persisted_prior_epoch():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
    attempt.primitive_receipt["before"].pop("structural_epoch")
    changed = _scene(
        "3",
        window_ref="window-epoch-without-prior",
        control_ref="button-three-epoch-without-prior",
        structural_epoch=5,
        observed_at=attempt.dispatched_at + 1.0,
    )
    changed_button = replace(
        changed.surfaces[1],
        runtime_signature=attempt.target_surface_identity["runtime_signature"],
    )
    changed = replace(
        changed,
        surfaces=(changed.window_ref, changed_button, changed.surfaces[2]),
    )
    _result, resolved = _observation(strategy, changed)

    assert resolved is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_evidence_for_another_substep_cannot_resolve_this_attempt():
    strategy = _strategy()
    _decision, attempt, _arguments = _dispatch(strategy, _scene("0"))
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
    result["receipt"]["substep_id"] = "substep_other_composite"

    assert record_tool_observation(
        "inspect_desktop_surfaces", result, is_error=False, strategy=strategy,
    ) is False
    assert strategy._frozen_attempt_id == attempt.attempt_id


def test_checkpoint_recovery_never_replays_and_exposes_next_uia_substep():
    strategy = _strategy()
    decision, attempt, arguments = _dispatch(strategy, _scene("0"))
    memory = json.loads(json.dumps(export_execution_memory(strategy)))
    checkpoint = state.new_task_state(
        TASK,
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Exact UIA checkpoint",
            "tasks": [{
                "objective": TASK,
                "success_criterion": "10063 is independently observed",
                "status": "waiting_tool",
                "tool_attempt_id": "attempt_real_path_262",
                "execution_memory": memory,
            }],
        }],
        run_id="run.target-surface-real-path",
        language="en",
    )
    task = checkpoint["phases"][0]["tasks"][0]
    rebuilt = Orchestrator(object())._strategy_for_task(checkpoint, task)
    replay = tool_preflight(
        "activate_desktop_surface", arguments, strategy=rebuilt,
    )
    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"

    restored = rebuilt.attempt_by_id(decision.attempt_id)
    _result, resolved = _observation(
        rebuilt,
        _scene("3", observed_at=restored.dispatched_at + 1.0),
    )
    next_step = tool_preflight(
        "activate_desktop_surface",
        {"surface_ref": "button-four", "objective": "Four"},
        strategy=rebuilt,
    )

    assert resolved is True
    assert rebuilt.frozen_reason == ""
    assert next_step is not None and next_step.allowed
    assert rebuilt.attempt_by_id(next_step.attempt_id).substep_index == (
        attempt.substep_index + 1
    )
    assert sum(
        item.action_signature == attempt.action_signature
        and item.status in {"selected", "ambiguous_effect", "progressed"}
        for item in rebuilt.attempts
    ) == 1
