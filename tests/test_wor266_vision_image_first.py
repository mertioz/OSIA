"""WOR-266 final same-model multimodal Vision architecture coverage."""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.skip(
    "Legacy WOR-266 V2/V3 suite is superseded by the canonical Chat and "
    "Computer Use V4 coverage in WOR-289/WOR-305; its local probe/audit "
    "helpers are intentionally excluded from release checkouts.",
    allow_module_level=True,
)
from PIL import Image

from osia import agent as agent_module
from osia import desktop_scene
from osia.config import DEFAULT_SETTINGS
from osia.desktop_scene import DesktopScene
from osia.desktop_surface import (
    DesktopSurfaceRef,
    DesktopSurfaceRegistry,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
)
from osia.image_inputs import (
    adapt_messages_for_provider,
    image_input_decision,
    resolve_image_artifact,
)
from osia.tool_contracts import get_tool_spec
from osia.vision_runtime import (
    CaptureFailure,
    CoordinateTransform,
    ImageInputTransport,
    NormalizedBBox,
    VisionCaptureError,
    VisionDomain,
    VisionFailure,
    VisionRuntime,
    VisionStage,
    VisionTrace,
    VisionTransportError,
    VisualFrame,
    VisualStatus,
    VisualTarget,
    WindowsCaptureDriver,
    crop_capture,
    prepare_capture,
    visual_frame_pixels_unchanged,
)
from tools.wor266_prompt_audit import build_prompt_quality_report


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ROOT = Path(__file__).resolve().parents[1]


def _scene(*, foreground: bool = True) -> tuple[DesktopScene, DesktopSurfaceRef]:
    bounds = SurfaceBounds(10, 20, 110, 120)
    window = DesktopSurfaceRef(
        ref_id="dsr_fixture",
        application_id="fixture",
        process_ref="proc_fixture",
        window_generation="win_fixture",
        surface_kind=SurfaceKind.WINDOW,
        role="WindowControl",
        accessible_label="Fixture",
        bounds=bounds,
        structural_epoch=0,
    )
    return DesktopScene(
        application_id="fixture",
        window_ref=window,
        surfaces=(window,),
        structural_epoch=0,
        scene_signature="scene_fixture",
        accessibility_complete=False,
        observed_at=time.time(),
        foreground=foreground,
    ), window


def _registered_scene(
    registry: DesktopSurfaceRegistry,
    *,
    scene_signature: str = "scene-a",
    bounds: SurfaceBounds | None = None,
    foreground: bool = True,
    pid: int = 41,
    started_at: float = 1000.0,
    hwnd: int = 101,
    class_name: str = "FixtureClass",
) -> DesktopScene:
    process_ref, generation, epoch = registry.observe_window_scene(
        application_id="fixture",
        pid=pid,
        process_started_at=started_at,
        native_window_id=hwnd,
        class_name=class_name,
        scene_signature=scene_signature,
    )
    window = registry.register(
        application_id="fixture",
        process_ref=process_ref,
        generation=generation,
        surface_kind=SurfaceKind.WINDOW,
        role="WindowControl",
        accessible_label="Fixture",
        selected=foreground,
        focused=foreground,
        enabled=True,
        parent_ref=None,
        bounds=bounds or SurfaceBounds(10, 20, 110, 120),
        runtime_signature="",
        structural_epoch=epoch,
        scene_signature=scene_signature,
        native=NativeSurfaceIdentity(
            pid, started_at, hwnd, class_name=class_name,
        ),
        stable_identity="window",
        patterns=("Window",),
    )
    return DesktopScene(
        application_id="fixture",
        window_ref=window,
        surfaces=(window,),
        structural_epoch=epoch,
        scene_signature=scene_signature,
        accessibility_complete=False,
        observed_at=time.time(),
        foreground=foreground,
    )


def _frame(
    *,
    provider: str = "openai",
    model: str = "gpt-5.4",
    foreground: bool = True,
) -> VisualFrame:
    return VisualFrame(
        capture_id="vf_fixture",
        provider=provider,
        model=model,
        application_id="fixture",
        surface_ref="dsr_fixture",
        window_generation="win_fixture",
        structural_epoch=0,
        captured_at=time.time(),
        image_width=1,
        image_height=1,
        mime="image/png",
        image_bytes=PNG,
        transform=CoordinateTransform(
            source_width=1,
            source_height=1,
            source_box=(0.0, 0.0, 1.0, 1.0),
            screen_bounds=(10, 20, 110, 120),
            client_origin_screen=(10, 20),
            window_bounds=(10, 20, 110, 120),
            client_bounds=(0, 0, 100, 100),
        ),
        native_window_token="native_fixture",
        capture_method="win32_windows_graphics_capture",
        exact_surface_pixels=True,
        foreground=foreground,
        domain=VisionDomain.DESKTOP,
        window_bounds=(10, 20, 110, 120),
        client_bounds=(0, 0, 100, 100),
        screen_origin=(10, 20),
    )


def _provenance_frame(
    *,
    captured_at: float | None = None,
    expires_at: float | None = None,
    parent_image_id: str = "",
) -> VisualFrame:
    captured = float(captured_at or time.time())
    output = io.BytesIO()
    Image.new("RGB", (400, 300), (12, 34, 56)).save(output, format="PNG")
    target = VisualTarget(
        application_id="fixture",
        domain=VisionDomain.DESKTOP,
        surface_id="dsr_fixture",
        surface_generation="win_fixture",
        bounds=(100, 80, 900, 680),
        native_window_id="101",
        dpi_scale=1.5,
    )
    return prepare_capture(
        output.getvalue(),
        provider="openai",
        model="gpt-5.4",
        application_id="fixture",
        surface_ref="dsr_fixture",
        window_generation="win_fixture",
        structural_epoch=0,
        transform=CoordinateTransform(
            source_width=800,
            source_height=600,
            source_box=(0.0, 0.0, 800.0, 600.0),
            screen_bounds=(100, 80, 900, 680),
            client_origin_screen=(100, 80),
            dpi_scale=1.5,
            monitor_id="monitor-1",
            window_bounds=(100, 80, 900, 680),
            client_bounds=(0, 0, 800, 600),
        ),
        native_window_token="native_fixture",
        capture_method="win32_windows_graphics_capture",
        exact_surface_pixels=True,
        target_identity=target.identity_key,
        monitor_id="monitor-1",
        window_bounds=(100, 80, 900, 680),
        client_bounds=(0, 0, 800, 600),
        screen_origin=(100, 80),
        captured_at=captured,
        expires_at=float(expires_at or (captured + 8.0)),
        parent_image_id=parent_image_id,
        source_pixel_format="BGRA8",
        pixel_format_conversion="BGRA8_TO_RGB8",
        alpha_handling="OPAQUE_COMPOSITOR_ALPHA_DROPPED",
        source_color_space="DWM_SDR",
        color_space_conversion="DWM_SDR_TO_SRGB_PNG",
    )


def _target() -> VisualTarget:
    return VisualTarget(
        application_id="fixture",
        domain=VisionDomain.DESKTOP,
        surface_id="dsr_fixture",
        surface_generation="win_fixture",
        bounds=(10, 20, 110, 120),
        native_window_id="101",
    )


def test_windows_capture_driver_accepts_isotropic_dpi_scaled_client_frame(
    monkeypatch,
):
    monkeypatch.setattr("osia.vision_runtime.sys.platform", "win32")
    frame = replace(
        _provenance_frame(),
        transform=CoordinateTransform(
            source_width=900,
            source_height=600,
            source_box=(0.0, 0.0, 900.0, 600.0),
            screen_bounds=(160, 164, 1285, 914),
            client_origin_screen=(160, 164),
            window_bounds=(150, 125, 1295, 924),
            client_bounds=(0, 0, 1125, 750),
        ),
        window_bounds=(150, 125, 1295, 924),
        client_bounds=(0, 0, 1125, 750),
    )
    assert WindowsCaptureDriver(lambda _target: frame)(_target()) is frame


def test_windows_capture_driver_rejects_anisotropic_capture_transform(
    monkeypatch,
):
    monkeypatch.setattr("osia.vision_runtime.sys.platform", "win32")
    frame = replace(
        _provenance_frame(),
        transform=CoordinateTransform(
            source_width=900,
            source_height=600,
            source_box=(0.0, 0.0, 900.0, 600.0),
            screen_bounds=(160, 164, 1285, 864),
            client_origin_screen=(160, 164),
            window_bounds=(150, 125, 1295, 924),
            client_bounds=(0, 0, 1125, 700),
        ),
        window_bounds=(150, 125, 1295, 924),
        client_bounds=(0, 0, 1125, 700),
    )
    with pytest.raises(VisionCaptureError) as caught:
        WindowsCaptureDriver(lambda _target: frame)(_target())
    assert caught.value.failure is CaptureFailure.WRONG_DIMENSIONS


def test_pixel_freshness_is_scoped_to_the_model_selected_bbox():
    source = _provenance_frame()
    changed = Image.new("RGB", (400, 300), (12, 34, 56))
    for x in range(320, 400):
        for y in range(240, 300):
            changed.putpixel((x, y), (255, 0, 0))
    output = io.BytesIO()
    changed.save(output, format="PNG")
    fresh = replace(
        source,
        capture_id="vf_outside_bbox_changed",
        image_bytes=output.getvalue(),
        image_sha256="",
    )
    bbox = NormalizedBBox(left=0.1, top=0.1, right=0.2, bottom=0.2)

    assert visual_frame_pixels_unchanged(source, fresh, bbox)
    assert not visual_frame_pixels_unchanged(source, fresh)


def _fake_win32(monkeypatch, *, exists=True, visible=True):
    from osia.tools import desktop_control

    gui = SimpleNamespace(
        IsWindow=lambda _hwnd: exists,
        IsWindowVisible=lambda _hwnd: visible,
    )
    monkeypatch.setattr(desktop_control, "_load_win32", lambda: (gui, object()))
    return desktop_control


def _probe_agent_type(completion):
    class FakeAgent:
        def __init__(self, model, lang):
            self.model, self.provider = model, "openai"
            self.messages = [{"role": "system", "content": "OSIA"}]
            self.client = SimpleNamespace(chat=SimpleNamespace(
                completions=completion,
            ))
            self._chatgpt_conversation_id = None

        def set_model(self, model, provider=None):
            self.model, self.provider = model, provider

        def _wire_messages(self):
            return self.messages

        def _reasoning_request(self):
            return {"kwargs": {}}

    return FakeAgent


def _successful_request(monkeypatch) -> dict:
    scene, window = _scene()
    frame = _frame()
    native = NativeSurfaceIdentity(41, 1000.0, 101, class_name="FixtureClass")
    monkeypatch.setattr(
        desktop_scene,
        "_observe_exact_bound_visual_scene",
        lambda *args, **kwargs: (scene, window, ""),
    )
    monkeypatch.setattr(
        desktop_scene,
        "_visual_target_for_scene",
        lambda *_args: _target(),
    )
    monkeypatch.setattr(
        desktop_scene.DESKTOP_SURFACE_REGISTRY,
        "native_identity",
        lambda _ref: native,
    )

    class Runtime:
        def __init__(self, provider, model):
            assert (provider, model) == ("openai", "gpt-5.4")

        def resolve_target(self, _semantic, resolver, *, trace):
            target = resolver("")
            trace.record(VisionStage.TARGET_RESOLVED, "fixture_target")
            return target, trace

        def capture(self, _target_value, _driver, *, trace):
            trace.record(VisionStage.CAPTURE_REQUESTED, "fixture_capture")
            trace.record(VisionStage.CAPTURE_COMPLETED, "fixture_captured")
            return frame, trace

    monkeypatch.setattr(desktop_scene, "VisionRuntime", Runtime)
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    turn = desktop_scene.begin_desktop_turn()
    try:
        return desktop_scene.request_visual_observation(
            window.ref_id, "custom_rendered", "pixel target",
        )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)


def test_01_real_hwnd_uses_direct_production_refresh_without_enumeration(monkeypatch):
    scene, _window = _scene()
    native = NativeSurfaceIdentity(41, 1000.0, 101, class_name="FixtureClass")
    dc = _fake_win32(monkeypatch)
    monkeypatch.setattr(
        dc, "_enum_app_windows_win32",
        lambda: (_ for _ in ()).throw(AssertionError("must not enumerate")),
    )
    monkeypatch.setattr(
        desktop_scene, "_window_metadata",
        lambda _hwnd: ("Fixture", 41, "FixtureClass", SurfaceBounds(10, 20, 110, 120)),
    )
    monkeypatch.setattr(desktop_scene, "_process_details", lambda _pid: ("fixture", 1000.0))
    monkeypatch.setattr(dc, "_foreground_window_win32", lambda: (999, "Other", 9))
    monkeypatch.setattr(desktop_scene, "_observe_window_scene", lambda *args, **kwargs: scene)
    monkeypatch.setattr(desktop_scene.DESKTOP_SURFACE_REGISTRY, "native_identity", lambda _ref: native)
    refreshed, reason = desktop_scene._refresh_windows_native_scene(
        101, expected_native=native,
        expected_application="fixture", expected_window_generation="win_fixture",
    )
    assert refreshed is scene
    assert reason == ""


def test_02_new_window_missing_from_old_snapshot_can_refresh_register(monkeypatch):
    scene, window = _scene(foreground=False)
    monkeypatch.setattr(
        desktop_scene.DESKTOP_SURFACE_REGISTRY,
        "resolve",
        lambda _ref, **_kwargs: SimpleNamespace(
            resolved=False, surface=None, reason="unknown_surface_ref",
        ),
    )
    monkeypatch.setattr(
        desktop_scene, "_refresh_windows_native_scene",
        lambda *args, **kwargs: (scene, ""),
    )
    current, requested, reason = desktop_scene._observe_exact_bound_visual_scene(
        "not_yet_discovered",
        expected_native_window_id="101",
        expected_application="fixture",
        expected_window_generation="win_fixture",
    )
    assert (current, requested, reason) == (scene, window, "")


def test_03_destroyed_hwnd_is_rejected_with_typed_reason(monkeypatch):
    _fake_win32(monkeypatch, exists=False)
    scene, reason = desktop_scene._refresh_windows_native_scene(101)
    assert scene is None
    assert reason == "SURFACE_STALE_HWND_DESTROYED"


def test_03b_untrusted_process_identity_is_rejected(monkeypatch):
    _fake_win32(monkeypatch)
    monkeypatch.setattr(
        desktop_scene,
        "_window_metadata",
        lambda _hwnd: (
            "Fixture", 41, "FixtureClass", SurfaceBounds(10, 20, 110, 120),
        ),
    )
    monkeypatch.setattr(
        desktop_scene,
        "_process_details",
        lambda _pid: ("process-41", 1000.0),
    )
    scene, reason = desktop_scene._refresh_windows_native_scene(101)
    assert scene is None
    assert reason == "PROCESS_IDENTITY_FAILED"


def test_04_recycled_hwnd_cannot_inherit_old_generation(monkeypatch):
    _fake_win32(monkeypatch)
    monkeypatch.setattr(
        desktop_scene, "_window_metadata",
        lambda _hwnd: ("Fixture", 42, "FixtureClass", SurfaceBounds(10, 20, 110, 120)),
    )
    monkeypatch.setattr(desktop_scene, "_process_details", lambda _pid: ("fixture", 2000.0))
    old = NativeSurfaceIdentity(41, 1000.0, 101, class_name="FixtureClass")
    scene, reason = desktop_scene._refresh_windows_native_scene(101, expected_native=old)
    assert scene is None
    assert reason == "SURFACE_STALE_HWND_RECYCLED"


def test_05_nonforeground_exact_target_resolves_read_only(monkeypatch):
    scene, _window = _scene(foreground=False)
    native = NativeSurfaceIdentity(41, 1000.0, 101, class_name="FixtureClass")
    dc = _fake_win32(monkeypatch)
    monkeypatch.setattr(desktop_scene, "_window_metadata", lambda _hwnd: (
        "Fixture", 41, "FixtureClass", SurfaceBounds(10, 20, 110, 120),
    ))
    monkeypatch.setattr(desktop_scene, "_process_details", lambda _pid: ("fixture", 1000.0))
    monkeypatch.setattr(dc, "_foreground_window_win32", lambda: (999, "Other", 9))
    monkeypatch.setattr(desktop_scene, "_observe_window_scene", lambda *args, **kwargs: scene)
    monkeypatch.setattr(desktop_scene.DESKTOP_SURFACE_REGISTRY, "native_identity", lambda _ref: native)
    refreshed, reason = desktop_scene._refresh_windows_native_scene(101, expected_native=native)
    assert refreshed is scene and refreshed.foreground is False and not reason


def test_06_active_vision_model_receives_direct_image_part(monkeypatch):
    result = _successful_request(monkeypatch)
    assert result["receipt"]["image_part_count"] == 1
    assert result["image_ref"]["type"] == "image_ref"
    assert resolve_image_artifact(
        result["image_ref"]["image_id"]
    ).image_bytes == PNG
    assert "screenshot" not in result


def test_07_image_part_provider_identity_is_unchanged():
    request = ImageInputTransport().build(_frame(), "observe", {})
    assert request.provider == "openai"


def test_08_image_part_model_identity_is_unchanged():
    request = ImageInputTransport().build(_frame(), "observe", {})
    assert request.model == "gpt-5.4"


def test_09_turn_request_identity_survives_same_model_image_turn(monkeypatch):
    from osia import wor266_live_probe as probe

    response_json = json.dumps({
        "status": "match",
        "expected_postcondition": "visible",
        "observed_state": "ABC34679",
        "confidence": 0.99,
        "target_bbox": None,
        "need_refine": False,
        "ambiguity": "",
    })

    class Completion:
        def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=response_json),
            )])

    monkeypatch.setattr(agent_module, "OSIAAgent", _probe_agent_type(Completion()))
    verdict, diagnostics = probe._same_model_image_turn(
        provider="openai", model="gpt-5.4",
        screenshot_base64=base64.b64encode(PNG).decode("ascii"),
        frame_id="vf_fixture", target="pixel code",
        expected_postcondition="visible",
    )
    assert verdict.observed_state == "ABC34679"
    assert diagnostics["turn_identity_unchanged"] is True


def test_10_image_response_stays_in_normal_agent_reasoning_context():
    source = (ROOT / "osia" / "agent.py").read_text(encoding="utf-8")
    assert 'self.messages.append({"role": "user", "content": content_parts})' in source
    assert "analyze_browser_screenshot" not in source


def test_11_nonvision_active_model_does_not_capture_or_handoff():
    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        result = desktop_scene.request_visual_observation(
            "dsr_missing", "custom_rendered", "target",
        )
    finally:
        desktop_scene.reset_effective_model(route)
    assert result["receipt"]["reason_code"] == "IMAGE_INPUT_UNAVAILABLE"
    assert result["receipt"]["dispatch_state"] == "not_dispatched"


def test_12_old_dedicated_provider_configuration_is_ignored():
    assert "vision_provider" not in DEFAULT_SETTINGS
    assert "vision_model" not in DEFAULT_SETTINGS


def test_13_no_dedicated_provider_selector_remains_in_active_ui_config():
    text = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "osia/gui/main_popup.py", "osia/config.py",
            "config/settings.example.json", "osia/server/server.py",
        )
    )
    assert "vision_provider" not in text
    assert "vision_model" not in text


def test_14_no_secondary_model_callback_path_remains():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "osia").rglob("*.py")
    )
    for forbidden in (
        "_provider_completion", "build_vision_client",
        "analyze_browser_screenshot", "ImageProviderTransport",
    ):
        assert forbidden not in text


def test_15_unknown_or_nonvision_route_never_silently_hands_off():
    assert image_input_decision("deepseek", "deepseek-v4-pro", ("image/png",)).usable is False
    assert image_input_decision("unknown", "unknown", ("image/png",)).usable is False


def test_16_exact_bound_capture_metadata_is_preserved(monkeypatch):
    result = _successful_request(monkeypatch)
    receipt = result["receipt"]
    assert receipt["capture_method"] == "win32_windows_graphics_capture"
    assert receipt["exact_surface_pixels"] is True
    assert receipt["surface_ref"] == "dsr_fixture"


def test_17_capture_protection_is_typed_and_sends_no_image(monkeypatch):
    scene, window = _scene()
    monkeypatch.setattr(desktop_scene, "_observe_exact_bound_visual_scene", lambda *a, **k: (scene, window, ""))
    monkeypatch.setattr(desktop_scene, "_visual_target_for_scene", lambda *_a: _target())

    class Runtime:
        def __init__(self, *_args):
            pass

        def resolve_target(self, _semantic, resolver, *, trace):
            trace.record(VisionStage.TARGET_RESOLVED, "target")
            return resolver(""), trace

        def capture(self, *_args, **_kwargs):
            raise VisionCaptureError(CaptureFailure.PROTECTED_CONTENT)

    monkeypatch.setattr(desktop_scene, "VisionRuntime", Runtime)
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    turn = desktop_scene.begin_desktop_turn()
    try:
        result = desktop_scene.request_visual_observation(
            window.ref_id, "custom_rendered", "target",
        )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)
    assert result["receipt"]["reason_code"] == "CAPTURE_PROTECTED"
    assert "screenshot" not in result


def test_18_image_part_is_real_and_nonempty():
    request = ImageInputTransport().build(_frame(), "observe", {})
    image_ref = request.messages[0]["content"][1]
    artifact = resolve_image_artifact(image_ref["image_id"])
    assert image_ref["type"] == "image_ref"
    assert artifact.source_type == "desktop_capture"
    assert artifact.image_bytes == PNG


def test_19_provider_adapter_cannot_silently_drop_image_part():
    request = ImageInputTransport().build(_frame(), "observe", {})
    adapted = adapt_messages_for_provider(request.messages, "openai")
    parts = adapted[0]["content"]
    assert sum(part.get("type") == "image_url" for part in parts) == 1


def test_20_pixel_only_token_is_recognized_by_the_active_model_path(monkeypatch):
    test_09_turn_request_identity_survives_same_model_image_turn(monkeypatch)


def test_21_exact_api_evidence_does_not_force_vision():
    prompt = agent_module.get_system_prompt("en", "gpt-5.4", "openai").casefold()
    assert "vision first" not in prompt
    assert "always use vision" not in prompt


def test_22_exact_dom_or_uia_evidence_does_not_force_vision():
    prompt = agent_module.get_system_prompt("en", "gpt-5.4", "openai").casefold()
    assert "uia first" not in prompt
    assert "in this order" not in prompt


def test_23_custom_rendered_ui_keeps_visual_observation_available():
    spec = get_tool_spec("request_visual_observation")
    reasons = spec.input_schema["properties"]["reason"]["enum"]
    assert "custom_rendered" in reasons


def test_24_model_may_choose_visual_observation_when_useful():
    description = get_tool_spec("request_visual_observation").capability
    assert "current model" in description
    assert "first" not in description.casefold()


def test_25_vision_failure_is_read_only_not_mutation_uncertainty():
    route = desktop_scene.bind_effective_model("deepseek", "deepseek-v4-pro")
    try:
        result = desktop_scene.request_visual_observation(
            "dsr_missing", "custom_rendered", "target",
        )
    finally:
        desktop_scene.reset_effective_model(route)
    assert result["receipt"]["dispatch_state"] == "not_dispatched"
    assert result["receipt"]["outcome"] == "confirmed_failure_before_effect"


def test_26_visual_click_gets_exact_actiontruth_identity(monkeypatch):
    class Store:
        def get_attempt(self, _attempt):
            return {"action_truth": {
                "action_id": "act_1", "semantic_effect_id": "effect_1",
            }}

        def bind_visual_action_receipt(self, _attempt, _receipt):
            return None

    import osia.tool_attempts as attempts
    monkeypatch.setattr(attempts, "current_tool_attempt_id", lambda: "attempt_1")
    monkeypatch.setattr(attempts, "current_tool_attempt_store", lambda: Store())
    result = agent_module._bind_visual_action_identity({
        "receipt": {"proposal_id": "proposal_1"},
    })
    receipt = result["receipt"]
    assert receipt["action_id"] == "act_1"
    assert receipt["attempt_id"] == "attempt_1"
    assert receipt["semantic_effect_id"] == "effect_1"


def test_27_uncertain_visual_click_cannot_cross_a_second_dispatch_boundary(monkeypatch):
    scene, window = _scene()
    frame = _frame()
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    turn = desktop_scene.begin_desktop_turn()
    dispatched = []
    try:
        desktop_scene._VISUAL_FRAMES.get()[frame.frame_id] = frame
        desktop_scene.register_visual_actuation_frame(frame)
        desktop_scene.acknowledge_visual_actuation_frame(
            frame.frame_id, provider="openai", model="gpt-5.4",
        )
        monkeypatch.setattr(desktop_scene, "_observe_exact_bound_visual_scene", lambda *a, **k: (scene, window, ""))
        monkeypatch.setattr(
            desktop_scene,
            "_capture_bound_visual_surface",
            lambda *_a, **_k: replace(frame, capture_id="vf_fresh"),
        )
        with pytest.raises(RuntimeError, match="replay blocked"):
            desktop_scene.vision_guided_desktop_click(
                "target", lambda x, y: dispatched.append((x, y)),
                visual_frame_id=frame.frame_id,
                visual_bbox={"left": .2, "top": .2, "right": .4, "bottom": .4},
                before_dispatch=lambda: (_ for _ in ()).throw(RuntimeError("replay blocked")),
            )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)
    assert dispatched == []


def test_28_fresh_read_only_evidence_is_required_after_visual_click(monkeypatch):
    scene, window = _scene()
    frame = _frame()
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    turn = desktop_scene.begin_desktop_turn()
    try:
        desktop_scene._VISUAL_FRAMES.get()[frame.frame_id] = frame
        desktop_scene.register_visual_actuation_frame(frame)
        desktop_scene.acknowledge_visual_actuation_frame(
            frame.frame_id, provider="openai", model="gpt-5.4",
        )
        monkeypatch.setattr(desktop_scene, "_observe_exact_bound_visual_scene", lambda *a, **k: (scene, window, ""))
        monkeypatch.setattr(
            desktop_scene,
            "_capture_bound_visual_surface",
            lambda *_a, **_k: replace(frame, capture_id="vf_fresh"),
        )
        result = desktop_scene.vision_guided_desktop_click(
            "target", lambda _x, _y: "clicked",
            visual_frame_id=frame.frame_id,
            visual_bbox={"left": .2, "top": .2, "right": .4, "bottom": .4},
            before_dispatch=lambda: None,
        )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)
    assert result["receipt"]["dispatch_state"] == "effect_unknown"
    assert "request_visual_observation" in result["receipt"]["available_read_only_observers"]


def test_28b_visual_click_rejects_same_geometry_when_pixels_changed(monkeypatch):
    scene, window = _scene()
    frame = _frame()
    changed_png = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(changed_png, format="PNG")
    fresh = replace(
        frame,
        capture_id="vf_changed",
        image_bytes=changed_png.getvalue(),
        image_sha256="",
    )
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    turn = desktop_scene.begin_desktop_turn()
    dispatched = []
    try:
        desktop_scene._VISUAL_FRAMES.get()[frame.frame_id] = frame
        desktop_scene.register_visual_actuation_frame(frame)
        desktop_scene.acknowledge_visual_actuation_frame(
            frame.frame_id, provider="openai", model="gpt-5.4",
        )
        monkeypatch.setattr(
            desktop_scene,
            "_observe_exact_bound_visual_scene",
            lambda *a, **k: (scene, window, ""),
        )
        monkeypatch.setattr(
            desktop_scene,
            "_capture_bound_visual_surface",
            lambda *_a, **_k: fresh,
        )
        result = desktop_scene.vision_guided_desktop_click(
            "target", lambda x, y: dispatched.append((x, y)),
            visual_frame_id=frame.frame_id,
            visual_bbox={"left": .2, "top": .2, "right": .4, "bottom": .4},
            before_dispatch=lambda: None,
        )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)

    assert result["dispatched"] is False
    assert result["reason_code"] == "SURFACE_STALE_PIXELS_CHANGED"
    assert dispatched == []


def test_29_prompt_has_no_dedicated_image_provider_instruction():
    prompt = agent_module.get_system_prompt("en", "gpt-5.4", "openai").casefold()
    assert "vision provider" not in prompt
    assert "paired vision" not in prompt


def test_30_prompt_has_no_vision_first_rule():
    assert "vision first" not in agent_module.get_system_prompt("en", "gpt-5.4", "openai").casefold()


def test_31_prompt_has_no_rigid_uia_first_rule():
    assert "uia first" not in agent_module.get_system_prompt("en", "gpt-5.4", "openai").casefold()


def test_33_essential_prompt_safety_rules_remain():
    report = build_prompt_quality_report()
    assert report["acceptance"]["safety_preserved"] is True


def test_34_tool_descriptions_expose_capabilities_not_recipes():
    visual = get_tool_spec("request_visual_observation").capability.casefold()
    click = get_tool_spec("desktop_click").capability.casefold()
    assert "current model" in visual
    assert "another model" not in visual
    assert "first" not in visual
    assert "visual_frame_id" in click


def test_35_stage_names_describe_same_model_image_input():
    assert VisionStage.IMAGE_INPUT_SENT.value == "IMAGE_INPUT_SENT"
    assert VisionStage.MODEL_RESPONSE_RECEIVED.value == "MODEL_RESPONSE_RECEIVED"
    assert not hasattr(VisionStage, "PROVIDER_REQUEST_SENT")


def test_36_visual_frame_repr_and_safe_metadata_never_include_pixels():
    frame = _frame()
    assert "iVBOR" not in repr(frame)
    assert "image_bytes" not in frame.safe_dict()


def test_37_live_probe_types_provider_timeout_at_image_send(monkeypatch):
    from osia import wor266_live_probe as probe

    class Completion:
        def create(self, **_kwargs):
            raise TimeoutError("private provider detail")

    monkeypatch.setattr(
        agent_module, "OSIAAgent", _probe_agent_type(Completion()),
    )
    with pytest.raises(probe._ProbeStageFailure) as raised:
        probe._same_model_image_turn(
            provider="openai",
            model="gpt-5.4",
            screenshot_base64=base64.b64encode(PNG).decode("ascii"),
            frame_id="vf_fixture",
            target="pixel code",
            expected_postcondition="visible",
        )
    assert raised.value.projection["failed_stage"] == "IMAGE_INPUT_SENT"
    assert raised.value.projection["failure_class"] == "PROVIDER_TIMEOUT"
    assert "private provider detail" not in repr(raised.value.projection)


def test_38_live_probe_types_invalid_model_output_at_verdict_parse(monkeypatch):
    from osia import wor266_live_probe as probe

    class Completion:
        def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="not structured output"),
            )])

    monkeypatch.setattr(
        agent_module, "OSIAAgent", _probe_agent_type(Completion()),
    )
    with pytest.raises(probe._ProbeStageFailure) as raised:
        probe._same_model_image_turn(
            provider="openai",
            model="gpt-5.4",
            screenshot_base64=base64.b64encode(PNG).decode("ascii"),
            frame_id="vf_fixture",
            target="pixel code",
            expected_postcondition="visible",
        )
    assert raised.value.projection["failed_stage"] == "VISUAL_VERDICT_PARSED"
    assert raised.value.projection["failure_class"] == "MODEL_OUTPUT_INVALID"


def test_39_new_surface_survives_immediate_freshness_validation(monkeypatch):
    registry = DesktopSurfaceRegistry()
    scene = _registered_scene(registry)
    diagnostics = {}
    monkeypatch.setattr(desktop_scene, "DESKTOP_SURFACE_REGISTRY", registry)
    monkeypatch.setattr(
        desktop_scene, "_refresh_windows_native_scene",
        lambda *_args, **_kwargs: (scene, ""),
    )

    current, requested, reason = desktop_scene._observe_exact_bound_visual_scene(
        scene.window_ref.ref_id,
        expected_application="fixture",
        expected_window_generation=scene.window_ref.window_generation,
        diagnostics=diagnostics,
    )

    assert current is scene
    assert requested == scene.window_ref
    assert reason == ""
    assert diagnostics["events"][-2]["first_mismatch"] == ""
    assert diagnostics["events"][-2]["continuity"] == "EXACT_CURRENT_TARGET"


def test_40_same_native_refresh_rebinds_to_current_canonical_generation():
    registry = DesktopSurfaceRegistry()
    old_scene = _registered_scene(registry, scene_signature="scene-a")
    current_scene = _registered_scene(registry, scene_signature="scene-b")

    rebound = registry.rebind_same_target(
        old_scene.window_ref.ref_id, current_scene.surfaces,
    )

    assert old_scene.window_ref.window_generation == current_scene.window_ref.window_generation
    assert current_scene.structural_epoch == old_scene.structural_epoch + 1
    assert rebound.resolved
    assert rebound.surface == current_scene.window_ref
    assert rebound.reason == "same_target_refreshed"


def test_41_scene_epoch_change_alone_does_not_invalidate_native_target():
    registry = DesktopSurfaceRegistry()
    old_scene = _registered_scene(registry, scene_signature="scene-a")
    current_scene = _registered_scene(registry, scene_signature="scene-b")

    assert registry.resolve(old_scene.window_ref.ref_id).status == "stale"
    anchor = registry.refresh_anchor(old_scene.window_ref.ref_id)

    assert anchor.status == "refreshable"
    assert anchor.native is not None
    assert anchor.native.native_window_id == 101
    assert current_scene.window_ref.window_generation == old_scene.window_ref.window_generation


def test_42_old_copied_ref_safely_resolves_to_current_same_target(monkeypatch):
    registry = DesktopSurfaceRegistry()
    old_scene = _registered_scene(registry, scene_signature="scene-a")
    copied_ref = old_scene.window_ref
    current_scene = _registered_scene(registry, scene_signature="scene-b")
    diagnostics = {}
    monkeypatch.setattr(desktop_scene, "DESKTOP_SURFACE_REGISTRY", registry)
    monkeypatch.setattr(
        desktop_scene, "_refresh_windows_native_scene",
        lambda *_args, **_kwargs: (current_scene, ""),
    )

    scene, requested, reason = desktop_scene._observe_exact_bound_visual_scene(
        copied_ref.ref_id, diagnostics=diagnostics,
    )

    assert scene is current_scene
    assert requested == current_scene.window_ref
    assert requested.ref_id != copied_ref.ref_id
    assert reason == ""
    assert diagnostics["events"][-2]["continuity"] == "SAME_TARGET_REFRESHED"


def test_43_same_hwnd_with_changed_window_class_remains_stale():
    registry = DesktopSurfaceRegistry()
    old_scene = _registered_scene(registry, class_name="FixtureClass")
    replacement = _registered_scene(registry, class_name="OtherClass")

    rebound = registry.rebind_same_target(
        old_scene.window_ref.ref_id, replacement.surfaces,
    )

    assert not rebound.resolved
    assert rebound.reason == "native_target_changed"


def test_44_moved_window_refreshes_for_read_only_observation(monkeypatch):
    registry = DesktopSurfaceRegistry()
    old_scene = _registered_scene(
        registry, bounds=SurfaceBounds(10, 20, 110, 120),
    )
    moved_scene = _registered_scene(
        registry, bounds=SurfaceBounds(210, 220, 310, 320),
    )
    monkeypatch.setattr(desktop_scene, "DESKTOP_SURFACE_REGISTRY", registry)
    monkeypatch.setattr(
        desktop_scene, "_refresh_windows_native_scene",
        lambda *_args, **_kwargs: (moved_scene, ""),
    )

    scene, requested, reason = desktop_scene._observe_exact_bound_visual_scene(
        old_scene.window_ref.ref_id,
    )

    assert scene is moved_scene
    assert requested == moved_scene.window_ref
    assert requested.bounds == SurfaceBounds(210, 220, 310, 320)
    assert reason == ""


def test_45_moved_window_invalidates_old_visual_action_bbox(monkeypatch):
    registry = DesktopSurfaceRegistry()
    moved_scene = _registered_scene(
        registry, bounds=SurfaceBounds(210, 220, 310, 320),
    )
    frame = replace(
        _frame(),
        window_generation=moved_scene.window_ref.window_generation,
        surface_ref=moved_scene.window_ref.ref_id,
    )
    dispatched = []
    monkeypatch.setattr(
        desktop_scene, "_observe_exact_bound_visual_scene",
        lambda *_args, **_kwargs: (moved_scene, moved_scene.window_ref, ""),
    )
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    turn = desktop_scene.begin_desktop_turn()
    try:
        desktop_scene._VISUAL_FRAMES.get()[frame.frame_id] = frame
        desktop_scene.register_visual_actuation_frame(frame)
        desktop_scene.acknowledge_visual_actuation_frame(
            frame.frame_id, provider="openai", model="gpt-5.4",
        )
        result = desktop_scene.vision_guided_desktop_click(
            "target", lambda x, y: dispatched.append((x, y)),
            visual_frame_id=frame.frame_id,
            visual_bbox={"left": .2, "top": .2, "right": .4, "bottom": .4},
            before_dispatch=lambda: None,
        )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)

    assert result["reason_code"] == "SURFACE_STALE_GEOMETRY_CHANGED"
    assert dispatched == []


def test_46_nonforeground_same_target_is_read_only_refreshable(monkeypatch):
    registry = DesktopSurfaceRegistry()
    scene = _registered_scene(registry, foreground=False)
    monkeypatch.setattr(desktop_scene, "DESKTOP_SURFACE_REGISTRY", registry)
    monkeypatch.setattr(
        desktop_scene, "_refresh_windows_native_scene",
        lambda *_args, **_kwargs: (scene, ""),
    )

    current, requested, reason = desktop_scene._observe_exact_bound_visual_scene(
        scene.window_ref.ref_id,
    )

    assert current is scene and requested == scene.window_ref
    assert current.foreground is False
    assert reason == ""


def test_47_read_only_rebinding_does_not_bypass_strict_stale_guard():
    registry = DesktopSurfaceRegistry()
    old_scene = _registered_scene(registry, scene_signature="scene-a")
    current_scene = _registered_scene(registry, scene_signature="scene-b")

    rebound = registry.rebind_same_target(
        old_scene.window_ref.ref_id, current_scene.surfaces,
    )

    assert rebound.resolved
    assert registry.resolve(old_scene.window_ref.ref_id).status == "stale"
    assert registry.native_identity(old_scene.window_ref.ref_id) is None


def test_48_live_fixture_keeps_production_registry_and_resolver_path():
    source = (ROOT / "osia" / "wor266_live_probe.py").read_text(encoding="utf-8")

    assert "inspect_desktop_surfaces(_FIXTURE_TITLE)" in source
    assert "desktop_scene.request_visual_observation(" in source
    assert "DESKTOP_SURFACE_REGISTRY" not in source
    assert "mark_stale(" not in source
    assert "_surface_diagnostics=True" in source


def test_48b_live_stale_gate_captures_then_reuses_after_state_change(monkeypatch):
    from osia import config as config_module
    from osia import image_inputs
    from osia import wor266_live_probe as probe

    events = []
    scene = {
        "window_surface_ref": "surface-fixture",
        "application": "fixture",
        "window_generation": "generation-fixture",
    }

    class Fixture:
        hwnd = 101

        def __init__(self, scenario=""):
            assert scenario == "safe-click"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def change_target_for_stale_gate(self):
            events.append("state_changed")

        def status(self):
            return {"fixture_dispatch_count": 0}

    monkeypatch.setattr(
        config_module,
        "load_settings",
        lambda: {
            "selected_model_provider": "chatgpt_account",
            "selected_model": "gpt-5.6-terra",
        },
    )
    monkeypatch.setattr(
        image_inputs,
        "image_input_decision",
        lambda *_args, **_kwargs: SimpleNamespace(usable=True),
    )
    monkeypatch.setattr(probe, "LiveWindowsVisionFixtureProcess", Fixture)
    monkeypatch.setattr(
        probe, "_open_bound_scene", lambda *_a, **_k: ({}, scene),
    )

    def capture(*_args, **_kwargs):
        events.append("captured")
        return {
            "text": "{}",
            "receipt": {"frame_id": "frame-before-change"},
        }

    def stale_reuse(*_args, **_kwargs):
        events.append("reused")
        return {
            "dispatched": False,
            "reason_code": "SURFACE_STALE_GEOMETRY_CHANGED",
        }

    monkeypatch.setattr(probe, "_capture_for_model", capture)
    monkeypatch.setattr(
        probe,
        "_same_model_image_turn",
        lambda **_kwargs: (
            SimpleNamespace(target_bbox=NormalizedBBox(.2, .2, .4, .4)),
            {"image_input_sent": True, "image_part_count": 1},
        ),
    )
    monkeypatch.setattr(
        desktop_scene, "vision_guided_desktop_click", stale_reuse,
    )

    report = probe.run_live_probe("stale")

    assert events == ["captured", "state_changed", "reused"]
    assert report["passed"] is True
    assert report["dispatched"] is False
    assert report["frame_id"] == "frame-before-change"


def test_48c_live_report_does_not_export_artifact_hash_prefixes():
    source = (ROOT / "osia" / "wor266_live_probe.py").read_text(
        encoding="utf-8",
    )

    assert '"artifact_hash_prefix"' not in source
    assert '"delivered_image_hash_prefix"' not in source


def test_48d_observation_report_strips_internal_artifact_hash(monkeypatch):
    from osia import wor266_live_probe as probe

    token = "ACFHJKMP"
    colors = [item[0] for item in probe._COLOR_SWATCHES]
    private_hash = "f" * 64
    scene = {
        "target_resolution": "exact_target",
        "target_actionable_candidate_count": 0,
        "window_surface_ref": "surface-fixture",
        "window_generation": "generation-fixture",
        "structural_epoch": 1,
        "surfaces": [],
    }
    result = {
        "receipt": {
            "frame_id": "frame-fixture",
            "native_window_id": "101",
            "exact_surface_pixels": True,
            "image_part_count": 1,
            "capture_method": "win32_windows_graphics_capture",
        },
        "diagnostics": {"receipts": [
            {"stage": stage} for stage in (
                "TARGET_RESOLVED", "CAPTURE_REQUESTED",
                "CAPTURE_COMPLETED", "IMAGE_PART_BUILT",
            )
        ]},
        "observation": {
            "dimensions": {"width": 900, "height": 600},
            "image_sha256": private_hash,
        },
    }
    model_diagnostics = {
        "image_input_sent": True,
        "image_part_count": 1,
        "native_status": "completed",
        "native_thread_id_present": True,
        "native_turn_id_present": True,
        "model_rerouted": False,
        "effective_model_id": "gpt-5.6-terra",
        "capability_events": [],
        "response_item_types": [],
        "capture_dispatch_count": 1,
        "local_fidelity_error": "",
        "_raw_response_text": json.dumps({"token": token, "colors": colors}),
        "native_tool_names": ["request_visual_observation"],
        "delivered_frame_id": "frame-fixture",
        "delivered_artifact_verified": True,
    }
    local_fidelity = {
        "passed": True,
        "checks": {
            "artifact_hash_matches_frame": True,
            "serialized_hash_matches_artifact": True,
            "serialized_dimensions_match": True,
        },
        "maximum_channel_delta": 0,
        "dimensions": [900, 600],
    }

    class Fixture:
        hwnd = 101
        private_token = token

        def creation_diagnostics(self):
            return {"phase": "WINDOW_CREATION", "hwnd": "101"}

    monkeypatch.setattr(
        probe,
        "_dynamic_same_model_color_turn",
        lambda **_kwargs: (
            {"token": token, "colors": colors},
            model_diagnostics,
            result,
            local_fidelity,
        ),
    )

    report, _result, _payload = probe._run_observation_gate(
        Fixture(),
        {},
        scene,
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        conversation_id="conversation-fixture",
        turn_id="turn-fixture",
    )

    serialized = json.dumps(report, sort_keys=True)
    assert report["checks"]["same_artifact_delivered"] is True
    assert private_hash not in serialized
    assert "artifact_content_hash" not in serialized


def test_49_native_lifecycle_diagnostics_do_not_leak_from_normal_tool_use(
    monkeypatch,
):
    result = _successful_request(monkeypatch)

    assert "surface_lifecycle" not in result["receipt"]
    assert "surface_lifecycle" not in result["diagnostics"]


def test_50_typed_stale_reason_and_same_model_identity_reach_probe_receipt(
    monkeypatch,
):
    monkeypatch.setattr(
        desktop_scene, "_observe_exact_bound_visual_scene",
        lambda *_args, **_kwargs: (
            None, None, "SURFACE_STALE_HWND_DESTROYED",
        ),
    )
    route = desktop_scene.bind_effective_model("openai", "gpt-5.4")
    try:
        result = desktop_scene.request_visual_observation(
            "dsr_old", "custom_rendered", "target",
            _surface_diagnostics=True,
        )
    finally:
        desktop_scene.reset_effective_model(route)

    assert result["receipt"]["failure_class"] == "TARGET_STALE"
    assert result["receipt"]["reason_code"] == "SURFACE_STALE_HWND_DESTROYED"
    assert result["receipt"]["provider_id"] == "openai"
    assert result["receipt"]["model_id"] == "gpt-5.4"


def test_51_image_transport_rejects_expired_frame_before_artifact_build():
    now = time.time()
    stale = replace(
        _frame(),
        captured_at=now - 20.0,
        expires_at=now - 1.0,
    )

    with pytest.raises(VisionTransportError) as raised:
        ImageInputTransport().build(stale, "observe", {})

    assert raised.value.status is VisualStatus.STALE_FRAME
    assert raised.value.failure is VisionFailure.TARGET_STALE
    assert raised.value.stage is VisionStage.IMAGE_PART_BUILT
    assert raised.value.reason_code == "image_part_frame_stale"


def test_52_frame_artifact_preserves_lifetime_geometry_dpi_and_lineage():
    captured_at = time.time() - 1.0
    expires_at = captured_at + 8.0
    frame = _provenance_frame(
        captured_at=captured_at,
        expires_at=expires_at,
        parent_image_id="img_parent_exact",
    )

    artifact = frame.to_image_artifact()

    assert artifact.created_at == captured_at
    assert artifact.expires_at == expires_at
    assert artifact.parent_image_id == "img_parent_exact"
    assert artifact.source_dimensions == (800, 600)
    assert artifact.provider_dimensions == (400, 300)
    assert artifact.dpi == 144
    rebound = replace(frame, registered_image_id=artifact.image_id)
    assert rebound.to_image_artifact().image_id == artifact.image_id
    assert rebound.safe_dict()["registered_image_id"] == artifact.image_id
    assert rebound.safe_dict()["pixel_format_conversion"] == "BGRA8_TO_RGB8"
    assert rebound.safe_dict()["alpha_handling"] == (
        "OPAQUE_COMPOSITOR_ALPHA_DROPPED"
    )
    assert rebound.safe_dict()["color_space_conversion"] == (
        "DWM_SDR_TO_SRGB_PNG"
    )


def test_53_crop_preserves_pixel_age_expiry_provenance_and_exact_parent():
    source = _provenance_frame(parent_image_id="img_root")
    source_artifact = source.to_image_artifact()
    source = replace(
        source,
        registered_image_id=source_artifact.image_id,
    )

    refined = crop_capture(
        source,
        NormalizedBBox(left=0.4, top=0.35, right=0.6, bottom=0.65),
    )

    assert refined is not source
    assert refined.captured_at == source.captured_at
    assert refined.expires_at == source.expires_at
    assert refined.parent_image_id == source_artifact.image_id
    assert refined.registered_image_id == ""
    assert refined.source_pixel_format == source.source_pixel_format
    assert refined.pixel_format_conversion == source.pixel_format_conversion
    assert refined.alpha_handling == source.alpha_handling
    assert refined.source_color_space == source.source_color_space
    assert refined.color_space_conversion == source.color_space_conversion
    assert refined.to_image_artifact().parent_image_id == source_artifact.image_id
    assert visual_frame_pixels_unchanged(refined, _provenance_frame())

    refined_artifact = refined.to_image_artifact()
    second_refinement = crop_capture(
        refined,
        NormalizedBBox(left=0.4, top=0.4, right=0.6, bottom=0.6),
    )
    assert second_refinement is not refined
    assert second_refinement.parent_image_id == refined_artifact.image_id
    assert second_refinement.captured_at == source.captured_at
    assert second_refinement.expires_at == source.expires_at
    assert second_refinement.to_image_artifact().parent_image_id == (
        refined_artifact.image_id
    )
