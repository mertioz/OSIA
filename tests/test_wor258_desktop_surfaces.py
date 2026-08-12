"""Deterministic WOR-258 semantic-desktop and adaptive-Vision coverage."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osia.agent import OSIAAgent
from osia.desktop_scene import DesktopScene
from osia.desktop_surface import (
    DesktopSurfaceRef,
    DesktopSurfaceRegistry,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
    process_reference,
    window_generation,
)
from osia.desktop_vision_policy import (
    DesktopEvidence,
    decide_visual_observation,
    global_capture_fallback,
)
from osia.execution_strategy import (
    StrategyLayer,
    build_execution_strategy,
    mechanism_for_tool,
    record_tool_observation,
)
from osia.image_inputs import decode_image_data_url
from osia.visual_observation import (
    CaptureKind,
    ObservationReason,
    VisualObservationStore,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"wor-258-scoped-window"


def _public(
    ref_id: str,
    *,
    kind: SurfaceKind,
    role: str,
    label: str,
    parent: str | None = "dsr_window",
    selected: bool = False,
    focused: bool = False,
    bounds: SurfaceBounds | None = None,
    epoch: int = 0,
) -> DesktopSurfaceRef:
    return DesktopSurfaceRef(
        ref_id=ref_id,
        application_id="messenger",
        process_ref="proc_fixture",
        window_generation="win_fixture",
        surface_kind=kind,
        role=role,
        accessible_label=label,
        selected=selected,
        focused=focused,
        enabled=True,
        parent_ref=parent,
        bounds=bounds,
        runtime_signature="runtime_fixture_" + ref_id,
        structural_epoch=epoch,
        observed_at=100.0,
    )


def _scene(*, ambiguous: bool = True, sparse: bool = False, epoch: int = 0) -> DesktopScene:
    window = _public(
        "dsr_window",
        kind=SurfaceKind.WINDOW,
        role="WindowControl",
        label="Messenger",
        parent=None,
        selected=True,
        focused=True,
        bounds=SurfaceBounds(-1200, 80, -200, 880, dpi_scale=1.5),
        epoch=epoch,
    )
    surfaces = [window]
    if not sparse:
        surfaces.extend((
            _public(
                "dsr_tab_home",
                kind=SurfaceKind.TAB,
                role="TabItemControl",
                label="Home",
                selected=True,
                bounds=SurfaceBounds(-1170, 110, -1070, 145),
                epoch=epoch,
            ),
            _public(
                "dsr_candidate_a",
                kind=SurfaceKind.CONVERSATION,
                role="ListItemControl",
                label="Conversation Alpha",
                focused=True,
                bounds=SurfaceBounds(-1150, 190, -930, 225),
                epoch=epoch,
            ),
        ))
        if ambiguous:
            surfaces.append(_public(
                "dsr_candidate_b",
                kind=SurfaceKind.CONVERSATION,
                role="ListItemControl",
                label="Conversation Beta",
                bounds=SurfaceBounds(-1150, 230, -930, 265),
                epoch=epoch,
            ))
    return DesktopScene(
        application_id="messenger",
        window_ref=window,
        surfaces=tuple(surfaces),
        structural_epoch=epoch,
        scene_signature=f"scene-{epoch}",
        accessibility_complete=not sparse,
        observed_at=100.0,
    )


def _register(
    registry: DesktopSurfaceRegistry,
    *,
    hwnd: int = 100,
    pid: int = 20,
    started: float = 10.0,
    scene_signature: str = "scene-a",
    stable_identity: str = "runtime-a",
    runtime_id: tuple[int, ...] = (1, 2, 3),
    bounds: SurfaceBounds | None = None,
) -> DesktopSurfaceRef:
    app = "fixture-app"
    process_ref, generation, epoch = registry.observe_window_scene(
        application_id=app,
        pid=pid,
        process_started_at=started,
        native_window_id=hwnd,
        class_name="FixtureWindow",
        scene_signature=scene_signature,
    )
    native = NativeSurfaceIdentity(
        pid=pid,
        process_started_at=started,
        native_window_id=hwnd,
        runtime_id=runtime_id,
        ancestor_signature="root",
        class_name="FixtureWindow",
    )
    return registry.register(
        application_id=app,
        process_ref=process_ref,
        generation=generation,
        surface_kind=SurfaceKind.CONTROL,
        role="ButtonControl",
        accessible_label="Open",
        selected=False,
        focused=True,
        enabled=True,
        parent_ref=None,
        bounds=bounds or SurfaceBounds(-900, 100, -800, 140, dpi_scale=1.5),
        runtime_signature=stable_identity,
        structural_epoch=epoch,
        scene_signature=scene_signature,
        native=native,
        stable_identity=stable_identity,
    )


def test_opaque_refs_are_stable_but_native_authority_is_private():
    registry = DesktopSurfaceRegistry()
    first = _register(registry)
    second = _register(registry)

    assert first.ref_id == second.ref_id
    safe = first.to_safe_dict()
    assert "native_window_id" not in repr(safe)
    assert "pid" not in safe
    native = registry.native_identity(first.ref_id)
    assert native is not None and native.native_window_id == 100
    assert "pid" not in repr(native)
    assert "native_window_id" not in repr(native)


def test_multiple_windows_same_app_have_exact_distinct_generations():
    registry = DesktopSurfaceRegistry()
    first = _register(registry, hwnd=100, stable_identity="same-control")
    second = _register(registry, hwnd=200, stable_identity="same-control")

    assert first.process_ref == second.process_ref
    assert first.window_generation != second.window_generation
    assert first.ref_id != second.ref_id


@pytest.mark.parametrize(
    ("replacement", "expected_reason"),
    [
        ({"hwnd": 200}, "window_generation_changed"),
        ({"pid": 21, "started": 20.0}, "window_generation_changed"),
    ],
)
def test_window_replacement_and_app_restart_make_ref_stale(replacement, expected_reason):
    registry = DesktopSurfaceRegistry()
    original = _register(registry)
    fresh = _register(registry, **replacement)

    resolved = registry.resolve(
        original.ref_id,
        current_generation=fresh.window_generation,
        current_epoch=fresh.structural_epoch,
    )
    assert resolved.status == "stale"
    assert resolved.reason == expected_reason


def test_structural_epoch_change_invalidates_prior_refs():
    registry = DesktopSurfaceRegistry()
    original = _register(registry, scene_signature="tab-a")
    fresh = _register(registry, scene_signature="tab-b")

    assert fresh.structural_epoch == original.structural_epoch + 1
    resolved = registry.resolve(original.ref_id)
    assert resolved.status == "stale"
    assert resolved.reason == "structural_epoch_changed"


def test_runtime_disappearance_and_bounds_change_fail_closed():
    registry = DesktopSurfaceRegistry()
    runtime_ref = _register(registry)
    missing = registry.resolve(runtime_ref.ref_id, current_runtime_ids={(9, 9)})
    assert missing.status == "stale"
    assert missing.reason == "accessibility_runtime_id_disappeared"

    registry = DesktopSurfaceRegistry()
    bounds_ref = _register(registry)
    moved = registry.resolve(
        bounds_ref.ref_id,
        current_bounds=SurfaceBounds(-700, 100, -600, 140),
    )
    assert moved.status == "stale"
    assert moved.reason == "surface_bounds_changed"


def test_reobservation_after_window_move_never_reuses_old_ref_or_coordinates():
    registry = DesktopSurfaceRegistry()
    original = _register(registry)
    moved = _register(
        registry,
        bounds=SurfaceBounds(-600, 300, -500, 340, dpi_scale=1.5),
    )

    assert moved.ref_id != original.ref_id
    assert registry.resolve(original.ref_id).reason == "surface_bounds_changed"


def test_negative_multimonitor_bounds_and_dpi_are_preserved():
    bounds = SurfaceBounds(-1920, -120, -1280, 480, dpi_scale=1.75)
    assert bounds.valid()
    assert bounds.center == (-1600, 180)
    assert bounds.to_safe_dict()["dpi_scale"] == 1.75


def test_uia_control_bounds_inherit_window_dpi_scale():
    from osia.desktop_scene import _control_bounds

    control = SimpleNamespace(
        BoundingRectangle=SimpleNamespace(
            left=-1800,
            top=20,
            right=-1700,
            bottom=60,
        )
    )
    window = SurfaceBounds(-1920, -120, -1280, 480, dpi_scale=1.75)

    bounds = _control_bounds(control, window)

    assert bounds is not None
    assert bounds.dpi_scale == 1.75
    assert bounds.relative_left == 120
    assert bounds.relative_top == 140


def test_re_resolution_requires_one_semantically_equivalent_candidate():
    registry = DesktopSurfaceRegistry()
    original = _register(registry)
    registry.mark_stale(original.ref_id, "window_generation_changed")
    candidate = _public(
        "dsr_replacement",
        kind=SurfaceKind.CONTROL,
        role="ButtonControl",
        label="Open",
    )
    candidate = DesktopSurfaceRef(
        **{
            **candidate.__dict__,
            "application_id": original.application_id,
        }
    )

    assert registry.prove_equivalent(original.ref_id, [candidate]) == candidate
    assert registry.prove_equivalent(original.ref_id, [candidate, candidate]) is None


def test_scene_reports_selected_tab_focus_and_modal_ownership():
    scene = _scene()
    modal = _public(
        "dsr_modal",
        kind=SurfaceKind.DIALOG,
        role="WindowControl",
        label="Confirmation",
        parent="dsr_window",
        epoch=scene.structural_epoch,
    )
    scene = DesktopScene(**{
        **scene.__dict__,
        "surfaces": (*scene.surfaces, modal),
    })
    safe = scene.safe_dict(target="Conversation")

    assert safe["selected_views"] == ["dsr_tab_home"]
    assert safe["focused_surface_ref"] == "dsr_candidate_a"
    assert safe["modal_surface_refs"] == ["dsr_modal"]
    assert len(safe["candidate_surface_refs"]) == 2


def test_bounded_scene_projection_keeps_ranked_target_and_ancestor_chain():
    window = _public(
        "dsr_window",
        kind=SurfaceKind.WINDOW,
        role="WindowControl",
        label="Messenger",
        parent=None,
    )
    late_parent = _public(
        "dsr_late_parent",
        kind=SurfaceKind.PANE,
        role="PaneControl",
        label="Late pane",
    )
    target = _public(
        "dsr_late_target",
        kind=SurfaceKind.CONTROL,
        role="ButtonControl",
        label="Needle Target",
        parent=late_parent.ref_id,
    )
    fillers = tuple(
        _public(
            f"dsr_filler_{index}",
            kind=SurfaceKind.CONTROL,
            role="TextControl",
            label=f"Filler {index}",
        )
        for index in range(90)
    )
    scene = DesktopScene(
        application_id="messenger",
        window_ref=window,
        surfaces=(window, *fillers, late_parent, target),
        structural_epoch=0,
        scene_signature="large-scene",
        accessibility_complete=True,
        observed_at=100.0,
    )

    safe = scene.safe_dict(target="Needle Target")
    projected = [item["surface_ref"] for item in safe["surfaces"]]

    assert len(projected) == 80
    assert projected[:3] == [window.ref_id, late_parent.ref_id, target.ref_id]
    assert safe["candidate_surface_refs"] == [target.ref_id]


def test_unique_strong_uia_target_needs_no_visual_observation():
    evidence = DesktopEvidence(
        candidate_count=1,
        actionable_candidate_count=1,
        accessible_node_count=20,
    )
    decision = decide_visual_observation(evidence, model_supports_vision=True)
    assert decision.eligible is False
    assert decision.reason_code == "structured_evidence_sufficient"


def test_ambiguity_and_partial_custom_accessibility_trigger_targeted_capture():
    ambiguous = decide_visual_observation(
        DesktopEvidence(
            candidate_count=3,
            actionable_candidate_count=3,
            accessible_node_count=30,
            local_region_available=True,
        ),
        model_supports_vision=True,
    )
    partial = decide_visual_observation(
        DesktopEvidence(
            accessible_node_count=1,
            custom_rendered=True,
            local_region_available=False,
        ),
        model_supports_vision=True,
    )
    assert ambiguous.capture_kind is CaptureKind.REGION
    assert ambiguous.reason is ObservationReason.AMBIGUITY
    assert partial.capture_kind is CaptureKind.WINDOW
    assert partial.reason is ObservationReason.CUSTOM_RENDERED


def test_popup_visual_verification_and_nonvision_gate():
    popup = decide_visual_observation(
        DesktopEvidence(popup_suspected=True, accessible_node_count=8),
        model_supports_vision=True,
    )
    verification = decide_visual_observation(
        DesktopEvidence(
            visual_postcondition=True,
            structured_postcondition_complete=False,
            accessible_node_count=8,
        ),
        model_supports_vision=True,
    )
    nonvision = decide_visual_observation(
        DesktopEvidence(candidate_count=4),
        model_supports_vision=False,
    )
    assert popup.reason is ObservationReason.POPUP_OR_OVERLAY
    assert verification.reason is ObservationReason.VERIFICATION
    assert nonvision.eligible is False
    assert nonvision.reason_code == "effective_model_non_vision"


def test_small_target_refine_is_bounded_and_global_is_last_resort():
    refine = decide_visual_observation(
        DesktopEvidence(small_target=True, accessible_node_count=10),
        model_supports_vision=True,
        for_actuation=True,
    )
    blocked_global = global_capture_fallback(
        model_supports_vision=True,
        local_capture_failed=False,
        global_budget_available=True,
    )
    allowed_global = global_capture_fallback(
        model_supports_vision=True,
        local_capture_failed=True,
        global_budget_available=True,
    )
    assert refine.capture_kind is CaptureKind.ZOOM_REFINE
    assert refine.maximum_passes == 2
    assert blocked_global.eligible is False
    assert allowed_global.capture_kind is CaptureKind.GLOBAL


def test_observation_store_deduplicates_and_has_hard_budgets():
    store = VisualObservationStore(
        maximum_captures=2,
        maximum_global_captures=1,
        maximum_refinements=1,
    )
    first, created = store.add(
        surface_ref="surface-a",
        capture_kind=CaptureKind.WINDOW,
        reason=ObservationReason.AMBIGUITY,
        width=100,
        height=80,
        capture_epoch=0,
        structural_epoch=0,
        surface_fingerprint="window-a",
        png_bytes=PNG,
    )
    duplicate, duplicate_created = store.add(
        surface_ref="surface-a",
        capture_kind=CaptureKind.WINDOW,
        reason=ObservationReason.AMBIGUITY,
        width=100,
        height=80,
        capture_epoch=0,
        structural_epoch=0,
        surface_fingerprint="window-a",
        png_bytes=PNG,
    )
    assert created is True
    assert duplicate_created is False
    assert duplicate.image_sha256 == first.image_sha256
    assert store.stagnation_count == 1
    assert store.current("surface-a", 0) == first

    store.add(
        surface_ref="surface-a",
        capture_kind=CaptureKind.GLOBAL,
        reason=ObservationReason.INCOMPLETE_ACCESSIBILITY,
        width=200,
        height=100,
        capture_epoch=1,
        structural_epoch=1,
        surface_fingerprint="window-b",
        png_bytes=PNG + b"changed",
    )
    assert store.budget_allows(CaptureKind.WINDOW) is False
    assert store.budget_allows(CaptureKind.GLOBAL) is False
    store.clear()
    assert store.image_base64(first.observation_id) is None


def test_durable_strategy_records_only_bounded_safe_visual_receipt():
    strategy = build_execution_strategy(
        "Inspect the desktop app",
        {"inspect_desktop_surfaces", "request_visual_observation"},
    )
    result = {
        "receipt": {
            "outcome": "observed",
            "reason_code": "multiple_semantic_candidates",
            "surface_ref": "dsr_safe_ref",
            "structural_epoch": 3,
        },
        "scene": {"label": "PRIVATE-CONTENT-MUST-NOT-PERSIST"},
        "screenshot": "BASE64-MUST-NOT-PERSIST",
    }

    assert record_tool_observation(
        "request_visual_observation",
        result,
        is_error=False,
        strategy=strategy,
    ) is False
    durable = repr(strategy.snapshot())

    assert "dsr_safe_ref" in durable
    assert "multiple_semantic_candidates" in durable
    assert "PRIVATE-CONTENT-MUST-NOT-PERSIST" not in durable
    assert "BASE64-MUST-NOT-PERSIST" not in durable


def test_visual_evidence_does_not_promote_actuation_above_uia():
    strategy = build_execution_strategy(
        "Select Settings in the desktop app",
        {"activate_desktop_surface", "desktop_click"},
    )
    mechanism = mechanism_for_tool(
        strategy,
        "activate_desktop_surface",
        {"surface_ref": "dsr_settings", "objective": "Settings"},
    )
    assert mechanism is not None
    assert mechanism.layer is StrategyLayer.NATIVE_ACCESSIBILITY
    assert mechanism.mechanism_id == "native_accessibility"


class _Completions:
    def __init__(self, surface_ref: str):
        self.surface_ref = surface_ref
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        if len(self.calls) == 1:
            content = (
                '<tool_call>{"name":"request_visual_observation",'
                '"arguments":{"surface_ref":"' + self.surface_ref + '",'
                '"reason":"ambiguity","target":"Conversation"}}</tool_call>'
            )
            message = SimpleNamespace(content=content, tool_calls=None)
        else:
            message = SimpleNamespace(content="Grounded final.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _run_payload_harness(provider: str, model: str, scene: DesktopScene):
    completions = _Completions(scene.window_ref.ref_id)
    agent = OSIAAgent()
    agent.provider = provider
    agent.model = model
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with (
        patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
        patch(
            "osia.desktop_scene._capture_bounds_timed",
            return_value=(PNG, 4.0, 2.0),
        ) as capture,
        patch("osia.config.get_setting", return_value=False),
    ):
        reply = agent._run_turn("Inspect the ambiguous Conversation controls", history=[])
    return reply, completions.calls, capture


def _image_parts(messages):
    return [
        part
        for message in messages
        for part in (
            message.get("content")
            if isinstance(message.get("content"), list) else []
        )
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def test_vision_route_receives_exact_scoped_png_after_structured_evidence():
    scene = _scene(ambiguous=True)
    reply, calls, capture = _run_payload_harness("openai", "gpt-5.4", scene)

    assert reply == "Grounded final."
    assert capture.call_count == 1
    assert capture.call_args.args[0] == scene.window_ref.bounds
    parts = _image_parts(calls[1])
    assert len(parts) == 1
    content = next(
        message["content"]
        for message in calls[1]
        if isinstance(message.get("content"), list)
    )
    assert [part["type"] for part in content] == ["text", "image_url"]
    decoded = decode_image_data_url(parts[0]["image_url"])
    assert decoded.mime == "image/png"
    assert decoded.data == PNG
    assert hashlib.sha256(decoded.data).hexdigest() == hashlib.sha256(PNG).hexdigest()
    text_part = next(
        message["content"][0]["text"]
        for message in calls[1]
        if isinstance(message.get("content"), list)
    )
    assert scene.window_ref.ref_id in text_part
    assert text_part.index("<tool_result>") < len(text_part)


def test_nonvision_route_captures_and_sends_no_screenshot():
    scene = _scene(ambiguous=True)
    _reply, calls, capture = _run_payload_harness(
        "deepseek", "deepseek-v4-pro", scene,
    )

    assert capture.call_count == 0
    assert len(calls) >= 2
    assert _image_parts(calls[1]) == []
    assert "no screenshot was captured or sent" in repr(calls[1])


def test_unchanged_scene_reuses_observation_without_second_capture():
    from osia import desktop_scene

    scene = _scene(ambiguous=True)
    store_tokens = desktop_scene.begin_desktop_turn()
    route_token = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                return_value=(PNG, 4.0, 2.0),
            ) as capture,
        ):
            first = desktop_scene.request_visual_observation(
                scene.window_ref.ref_id, "ambiguity", "Conversation",
            )
            second = desktop_scene.request_visual_observation(
                scene.window_ref.ref_id, "ambiguity", "Conversation",
            )
        assert capture.call_count == 1
        assert "screenshot" in first
        assert "screenshot" not in second
        assert second["receipt"]["outcome"] == "deduplicated"
        assert first["receipt"]["image_sha256"] == second["receipt"]["image_sha256"]
    finally:
        desktop_scene.reset_effective_model(route_token)
        desktop_scene.end_desktop_turn(store_tokens)


def test_strong_unique_structured_target_does_not_capture():
    from osia import desktop_scene

    scene = _scene(ambiguous=False)
    store_tokens = desktop_scene.begin_desktop_turn()
    route_token = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                return_value=(PNG, 4.0, 2.0),
            ) as capture,
        ):
            result = desktop_scene.request_visual_observation(
                scene.window_ref.ref_id, "ambiguity", "Conversation Alpha",
            )
        assert capture.call_count == 0
        assert result["receipt"]["reason_code"] == "structured_evidence_sufficient"
    finally:
        desktop_scene.reset_effective_model(route_token)
        desktop_scene.end_desktop_turn(store_tokens)


def test_route_switch_to_nonvision_stops_new_visual_observations():
    from osia import desktop_scene

    scene = _scene(ambiguous=True)
    store_tokens = desktop_scene.begin_desktop_turn()
    vision_token = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                return_value=(PNG, 4.0, 2.0),
            ) as capture,
        ):
            first = desktop_scene.request_visual_observation(
                scene.window_ref.ref_id, "ambiguity", "Conversation",
            )
            desktop_scene.reset_effective_model(vision_token)
            nonvision_token = desktop_scene.bind_effective_model(
                "deepseek", "deepseek-v4-pro",
            )
            try:
                second = desktop_scene.request_visual_observation(
                    scene.window_ref.ref_id, "ambiguity", "Conversation",
                )
            finally:
                desktop_scene.reset_effective_model(nonvision_token)
        assert capture.call_count == 1
        assert "screenshot" in first
        assert second["receipt"]["reason_code"] == "effective_model_non_vision"
    finally:
        # vision_token was reset before the route switch.
        desktop_scene.end_desktop_turn(store_tokens)


def test_sensitive_surface_blocks_capture_before_pixels_are_read():
    from osia import desktop_scene

    scene = _scene(ambiguous=True)
    sensitive = _public(
        "dsr_password",
        kind=SurfaceKind.CONTROL,
        role="EditControl",
        label="[sensitive field]",
        epoch=scene.structural_epoch,
    )
    scene = DesktopScene(**{
        **scene.__dict__,
        "surfaces": (*scene.surfaces, sensitive),
    })
    store_tokens = desktop_scene.begin_desktop_turn()
    route_token = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", return_value=scene),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                return_value=(PNG, 4.0, 2.0),
            ) as capture,
        ):
            result = desktop_scene.request_visual_observation(
                scene.window_ref.ref_id, "ambiguity", "Conversation",
            )
        assert capture.call_count == 0
        assert result["receipt"]["reason_code"] == "sensitive_capture_blocked"
    finally:
        desktop_scene.reset_effective_model(route_token)
        desktop_scene.end_desktop_turn(store_tokens)


def test_scene_epoch_change_requires_new_observation():
    from osia import desktop_scene

    scenes = [_scene(ambiguous=True, epoch=0), _scene(ambiguous=True, epoch=1)]
    store_tokens = desktop_scene.begin_desktop_turn()
    route_token = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        with (
            patch("osia.desktop_scene.observe_foreground_scene", side_effect=scenes),
            patch(
                "osia.desktop_scene._capture_bounds_timed",
                side_effect=(
                    (PNG, 4.0, 2.0),
                    (PNG + b"epoch-1", 5.0, 2.5),
                ),
            ) as capture,
        ):
            first = desktop_scene.request_visual_observation(
                "dsr_window", "ambiguity", "Conversation",
            )
            second = desktop_scene.request_visual_observation(
                "dsr_window", "ambiguity", "Conversation",
            )
        assert capture.call_count == 2
        assert first["receipt"]["image_sha256"] != second["receipt"]["image_sha256"]
    finally:
        desktop_scene.reset_effective_model(route_token)
        desktop_scene.end_desktop_turn(store_tokens)
