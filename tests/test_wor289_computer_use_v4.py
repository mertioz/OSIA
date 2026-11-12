"""Focused deterministic tests for the isolated WOR-289 computer loop."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import pytest
from PIL import Image, ImageDraw

from osia.computer_use_v4 import (
    ComputerAction,
    ComputerUseV4,
    ComputerUseV4Error,
    computer_action_schema,
)
from osia.tool_attempts import ToolAttemptStore


def _draw_target(image, bbox=(65, 25, 135, 75), *, fill="#1D4ED8", hover=False):
    draw = ImageDraw.Draw(image)
    draw.rectangle(bbox, fill=fill, outline="white", width=2)
    left, top, right, bottom = bbox
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    draw.line((center_x - 16, center_y, center_x + 16, center_y), fill="white", width=2)
    draw.line((center_x, center_y - 10, center_x, center_y + 10), fill="white", width=2)
    if hover:
        draw.rectangle(
            (left + 4, top + 4, right - 4, bottom - 4),
            outline="#FACC15",
            width=2,
        )


class _Harness:
    def __init__(self, root: Path, *, maximum_image_dimension: int = 2048):
        self.image = Image.new("RGB", (200, 100), "navy")
        _draw_target(self.image)
        self.window = {
            "hwnd": 101,
            "pid": 42,
            "process_started_at": 10.25,
            "window_class": "FixtureClass",
            "window_title": "Fixture",
            "window_bounds": (100, 200, 500, 400),
            "client_bounds": (0, 0, 380, 170),
            "client_origin_screen": (110, 220),
            "dpi_scale": 1.5,
            "capture_protection": "",
            "foreground_hwnd": 101,
            "bounds_object": object(),
        }
        self.capture_count = 0
        self.dispatches = []
        self.permission_calls = []
        self.activation_calls = []
        self.activation_attempt_dispatch_states = []
        self.activation_behavior = None
        self.window_available = True
        self.clock_value = 100.0
        self.store = ToolAttemptStore(root)

        def window_state(hwnd):
            if (
                not self.window_available
                or int(hwnd) != int(self.window["hwnd"])
            ):
                return None
            return dict(self.window)

        def capture_window(_hwnd, _state):
            self.capture_count += 1
            return self.image.copy(), "mock_exact_window", {
                "captured_at": self.clock_value,
                "capture_screen_bounds": [100, 200, 500, 400],
                "capture_attempts": [{
                    "capture_driver": "mock_exact_window",
                    "outcome": "selected",
                    "reason_code": "capture_frame_stable",
                }],
            }

        def permission(action, frame):
            self.permission_calls.append((action.kind, frame.frame_id))
            return True, "test_permission"

        def foreground_activation(frame, state):
            self.activation_calls.append({
                "target_hwnd": frame.hwnd,
                "before_foreground_hwnd": int(
                    state.get("foreground_hwnd") or 0
                ),
            })
            dispatch_states = []
            for path in self.store.attempts_dir.glob("attempt_*.json"):
                durable = self.store.get_attempt(path.stem) or {}
                dispatch_states.append(bool(
                    (durable.get("action_truth") or {}).get(
                        "dispatch_started_at"
                    )
                ))
            self.activation_attempt_dispatch_states.append(dispatch_states)
            if callable(self.activation_behavior):
                return self.activation_behavior(frame, state)
            self.window["foreground_hwnd"] = frame.hwnd
            return {
                "attempted": True,
                "status": "verified",
                "target_hwnd": str(frame.hwnd),
                "target_pid": frame.pid,
                "before_foreground_hwnd": str(
                    int(state.get("foreground_hwnd") or 0)
                ),
                "verified_foreground_hwnd": str(frame.hwnd),
                "reason_code": "target_foreground_verified",
                "completed_at": time.time(),
            }

        def dispatch(action, point):
            self.dispatches.append((action.kind, point, action.text, action.key))
            return "mocked"

        self.loop = ComputerUseV4(
            "openai",
            "gpt-5.4",
            "turn_wor289_fixture",
            loop_id="loop_wor289_fixture",
            maximum_image_dimension=maximum_image_dimension,
            window_state=window_state,
            capture_window=capture_window,
            foreground_activation=foreground_activation,
            permission_check=permission,
            action_store=self.store,
            dispatchers={
                name: dispatch
                for name in ("click", "double_click", "type", "keypress", "scroll")
            },
            clock=lambda: self.clock_value,
        )

    def screenshot(self):
        return self.loop.screenshot(101)

    @staticmethod
    def click(frame, *, x=100, y=50, **values):
        return ComputerAction(
            "click", frame.frame_id, x=x, y=y, **values,
        )


def test_background_observation_leaves_focus_and_pointer_untouched(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    harness.window["foreground_hwnd"] = 202
    frame = harness.screenshot()
    assert frame.hwnd == 101 and frame.foreground_at_capture is False
    assert harness.window["foreground_hwnd"] == 202
    assert not harness.activation_calls and not harness.dispatches
    harness.window["process_started_at"] += 10
    with pytest.raises(ComputerUseV4Error, match="STALE_WINDOW"):
        harness.loop.screenshot(101, expected_window_token=frame.window_token)
    harness.window["minimized"] = True
    with pytest.raises(ComputerUseV4Error, match="WINDOW_MINIMIZED"):
        harness.screenshot()
    assert harness.capture_count == 1


@pytest.mark.parametrize("kind", ["click", "type", "keypress", "scroll"])
def test_denied_background_action_does_not_activate_its_window(tmp_path, kind):
    harness = _Harness(tmp_path / "denied-background")
    harness.window["foreground_hwnd"] = 202
    frame = harness.screenshot()
    harness.loop._permission_check = lambda *_: (False, "test_action_unavailable")
    action = ComputerAction(
        kind, frame.frame_id, x=100, y=50, text="fixture", key="Tab",
        direction="down", amount=1,
    )
    result = harness.loop.execute(action)
    assert result["status"] == "PERMISSION_DENIED"
    assert not harness.activation_calls and not harness.dispatches
    assert harness.window["foreground_hwnd"] == 202


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 exact window references")
def test_named_capture_prefers_exact_titles_and_refuses_ambiguous_apps(monkeypatch):
    from osia.computer_use_v4_integration import _resolve_window
    from osia.tools import desktop_control

    windows = [
        (202, "Report - preview", 12, "EditorClass"),
        (101, "Report", 11, "EditorClass"),
    ]
    monkeypatch.setattr(desktop_control, "_enum_app_windows_win32", lambda: windows)
    monkeypatch.setattr(
        desktop_control, "iter_app_windows",
        lambda name, candidates: (item[:3] for item in candidates if name == "editor"),
    )
    monkeypatch.setattr(
        desktop_control, "_foreground_window_win32",
        lambda: pytest.fail("Explicit capture must resolve its own target"),
    )
    assert _resolve_window("Report") == 101
    with pytest.raises(ComputerUseV4Error, match="WINDOW_AMBIGUOUS"):
        _resolve_window("editor")
    windows[0] = (202, "Report", 12, "EditorClass")
    with pytest.raises(ComputerUseV4Error, match="WINDOW_AMBIGUOUS"):
        _resolve_window("Report")


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 exact window references")
def test_background_capture_uses_a_validated_window_reference(monkeypatch):
    from osia import desktop_scene
    from osia.computer_use_v4_integration import _capture_target
    from osia.desktop_surface import SurfaceKind

    ref = SimpleNamespace(ref_id="window-fresh", surface_kind=SurfaceKind.WINDOW)
    scene = SimpleNamespace(window_ref=ref)
    monkeypatch.setattr(
        desktop_scene, "_observe_exact_bound_visual_scene",
        lambda requested: (scene, ref, "") if requested == "window-current"
        else (None, None, "SURFACE_STALE_HWND_RECYCLED"),
    )
    monkeypatch.setattr(
        desktop_scene.DESKTOP_SURFACE_REGISTRY, "native_identity",
        lambda refreshed: SimpleNamespace(
            native_window_id=101, pid=42, process_started_at=10.25,
            class_name="FixtureClass",
        )
        if refreshed == ref.ref_id else None,
    )
    hwnd, token = _capture_target("", "window-current")
    assert hwnd == 101 and len(token) == 32
    with pytest.raises(ComputerUseV4Error, match="SURFACE_STALE_HWND_RECYCLED"):
        _capture_target("", "window-stale")
    with pytest.raises(ComputerUseV4Error, match="INVALID_CAPTURE_TARGET"):
        _capture_target("Different window", "window-current")


def test_frame_creation_uses_exact_window_pixels_and_minimal_identity(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    assert frame.hwnd == 101
    assert frame.pid == 42
    assert frame.original_width == 200
    assert frame.original_height == 100
    assert frame.image_width == 200
    assert frame.image_height == 100
    assert frame.capture_method == "mock_exact_window"
    assert frame.scene_sha256
    assert frame.image_sha256
    assert frame.safe_dict()["mapping_chain"].startswith(
        "model_image_pixels_to_original_capture_pixels"
    )


def test_coordinate_mapping_uses_the_image_dimensions_the_model_saw(tmp_path):
    harness = _Harness(
        tmp_path / "attempts", maximum_image_dimension=100,
    )
    harness.image = Image.new("RGB", (800, 400), "navy")
    _draw_target(harness.image, (300, 150, 500, 250))
    frame = harness.screenshot()
    assert (frame.image_width, frame.image_height) == (256, 128)
    assert frame.map_image_point_to_screen(128, 64) == (300, 300)
    result = harness.loop.execute(_Harness.click(frame, x=128, y=64))
    assert result["status"] == "DISPATCHED"
    assert result["screen_point"] == [300, 300]
    assert harness.dispatches == [("click", (300, 300), "", "")]


def test_crop_mapping_preserves_original_capture_authority(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    source = harness.screenshot()
    crop = harness.loop.crop(source.frame_id, 50, 25, 150, 75)
    assert crop.parent_frame_id == source.frame_id
    assert crop.source_box == (50.0, 25.0, 150.0, 75.0)
    assert crop.map_image_point_to_screen(50, 25) == (300, 300)
    with pytest.raises(ComputerUseV4Error, match="STALE_FRAME"):
        harness.loop.crop(source.frame_id, 0, 0, 10, 10)


@pytest.mark.parametrize("dpi_scale", [1.0, 1.25, 1.5, 2.0])
def test_mapping_is_deterministic_across_windows_dpi_scales(
    tmp_path, dpi_scale,
):
    harness = _Harness(tmp_path / f"dpi-{dpi_scale}")
    base = harness.screenshot()
    left, top = 240, 160
    width = int(round(920 * dpi_scale))
    height = int(round(640 * dpi_scale))
    delivered_width = max(256, width // 2)
    delivered_height = max(128, height // 2)
    frame = replace(
        base,
        original_width=width,
        original_height=height,
        image_width=delivered_width,
        image_height=delivered_height,
        source_box=(0.0, 0.0, float(width), float(height)),
        capture_screen_bounds=(left, top, left + width, top + height),
        window_bounds=(left, top, left + width, top + height),
        client_bounds=(0, 0, width - 20, height - 50),
        client_origin_screen=(left + 10, top + 40),
        dpi_scale=dpi_scale,
    )
    model_point = (delivered_width * 0.625, delivered_height * 0.75)
    assert frame.map_image_point_to_screen(*model_point) == (
        left + round(width * 0.625),
        top + round(height * 0.75),
    )


def test_mapping_preserves_crop_resize_titlebar_and_secondary_monitor(
    tmp_path,
):
    harness = _Harness(tmp_path / "secondary-crop")
    base = harness.screenshot()
    left, top = -1680, 220
    width, height = 1200, 800
    frame = replace(
        base,
        original_width=width,
        original_height=height,
        image_width=300,
        image_height=200,
        source_box=(300.0, 200.0, 900.0, 600.0),
        capture_screen_bounds=(left, top, left + width, top + height),
        window_bounds=(left, top, left + width, top + height),
        client_bounds=(0, 0, 1170, 750),
        client_origin_screen=(left + 15, top + 42),
        dpi_scale=1.5,
    )
    # Center of the resized crop -> center of the full physical window.
    assert frame.map_image_point_to_screen(150, 100) == (
        left + width // 2,
        top + height // 2,
    )


def test_zoom_is_the_read_only_model_facing_crop_alias(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    source = harness.screenshot()
    zoomed = harness.loop.zoom(source.frame_id, 10.2, 5.2, 90.1, 45.1)
    assert zoomed.parent_frame_id == source.frame_id
    assert zoomed.source_box == (10.0, 5.0, 91.0, 46.0)
    assert harness.dispatches == []


@pytest.mark.parametrize("x,y", [(-1, 10), (200, 10), (10, -1), (10, 100)])
def test_click_outside_model_image_is_rejected_before_actiontruth(
    tmp_path, x, y,
):
    harness = _Harness(tmp_path / f"attempts-{x}-{y}")
    frame = harness.screenshot()
    result = harness.loop.execute(_Harness.click(frame, x=x, y=y))
    assert result["status"] == "OUT_OF_BOUNDS"
    assert result["dispatch_count"] == 0
    assert not harness.dispatches
    assert harness.activation_calls == []
    assert not harness.store.list_action_attempts(result["action_id"])


def test_unrelated_pixel_change_does_not_stale_the_target(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    ImageDraw.Draw(harness.image).rectangle((0, 0, 18, 18), fill="orange")
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "DISPATCHED"
    assert result["dispatch_count"] == 1
    assert len(harness.dispatches) == 1


@pytest.mark.parametrize(
    "variant",
    ["color", "hover", "animated_background", "renderer_noise"],
)
def test_cosmetic_visual_changes_keep_same_target_valid(
    tmp_path, variant,
):
    harness = _Harness(tmp_path / variant)
    frame = harness.screenshot()
    if variant == "color":
        _draw_target(harness.image, fill="#B91C1C")
    elif variant == "hover":
        _draw_target(harness.image, fill="#2563EB", hover=True)
    elif variant == "animated_background":
        draw = ImageDraw.Draw(harness.image)
        for offset in range(0, harness.image.width, 8):
            draw.line((offset, 0, offset + 30, 99), fill="#334155", width=2)
        _draw_target(harness.image)
    else:
        pixels = harness.image.load()
        for y in range(harness.image.height):
            for x in range(harness.image.width):
                red, green, blue = pixels[x, y]
                delta = 2 if (x + y) % 2 else -2
                pixels[x, y] = tuple(
                    max(0, min(255, value + delta))
                    for value in (red, green, blue)
                )
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "DISPATCHED"
    assert result["dispatch_count"] == 1
    assert len(harness.dispatches) == 1


def test_target_translation_stales_old_point_without_callback(tmp_path):
    harness = _Harness(tmp_path / "translated")
    frame = harness.screenshot()
    harness.image = Image.new("RGB", (200, 100), "navy")
    _draw_target(harness.image, (105, 25, 175, 75))
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] in {
        "target_structure_moved", "target_structure_changed",
    }
    assert result["dispatch_count"] == 0
    assert result["attempt_id"] == ""
    assert harness.dispatches == []


def test_target_disappearance_stales_old_point_without_callback(tmp_path):
    harness = _Harness(tmp_path / "disappeared")
    frame = harness.screenshot()
    harness.image = Image.new("RGB", (200, 100), "navy")
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == "target_structure_changed"
    assert result["dispatch_count"] == 0
    assert result["attempt_id"] == ""
    assert harness.dispatches == []


def test_wrong_hwnd_is_rejected_without_coordinate_dispatch(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    result = harness.loop.execute(
        _Harness.click(frame, target_hwnd=202),
    )
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == "action_window_mismatch"
    assert not harness.dispatches


def test_target_window_is_prepared_before_input_and_actiontruth(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    harness.window["foreground_hwnd"] = 202
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "DISPATCHED"
    assert result["foreground_preparation"]["status"] == "verified"
    assert result["foreground_preparation"][
        "verified_foreground_hwnd"
    ] == "101"
    assert len(harness.activation_calls) == 1
    assert harness.activation_attempt_dispatch_states == [[]]
    durable = harness.store.get_attempt(result["attempt_id"])
    assert durable["action_truth"]["dispatch_started_at"] > result[
        "foreground_preparation"
    ]["completed_at"]
    assert len(harness.dispatches) == 1


def test_failed_exact_foreground_activation_never_dispatches(tmp_path):
    harness = _Harness(tmp_path / "activation-failed")
    frame = harness.screenshot()
    harness.window["foreground_hwnd"] = 202
    harness.activation_behavior = lambda _frame, state: {
        "attempted": True,
        "status": "failed",
        "before_foreground_hwnd": str(state["foreground_hwnd"]),
        "verified_foreground_hwnd": str(state["foreground_hwnd"]),
        "reason_code": "target_foreground_activation_failed",
    }
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "FOREGROUND_PREPARATION_FAILED"
    assert result["reason_code"] == "target_foreground_activation_failed"
    assert result["dispatch_count"] == 0
    assert result["attempt_id"] == ""
    assert harness.dispatches == []


def test_destroyed_target_before_activation_never_dispatches(tmp_path):
    harness = _Harness(tmp_path / "destroyed")
    frame = harness.screenshot()
    harness.window["foreground_hwnd"] = 202
    harness.window_available = False
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == "target_window_missing"
    assert harness.activation_calls == []
    assert harness.dispatches == []


def test_activation_geometry_change_stales_old_action(tmp_path):
    harness = _Harness(tmp_path / "activation-geometry")
    frame = harness.screenshot()
    harness.window["foreground_hwnd"] = 202

    def change_geometry(target, state):
        harness.window["foreground_hwnd"] = target.hwnd
        harness.window["window_bounds"] = (100, 200, 520, 420)
        return {
            "attempted": True,
            "status": "verified",
            "before_foreground_hwnd": str(state["foreground_hwnd"]),
            "verified_foreground_hwnd": str(target.hwnd),
            "reason_code": "target_foreground_verified",
        }

    harness.activation_behavior = change_geometry
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == "window_geometry_changed"
    assert result["dispatch_count"] == 0
    assert harness.dispatches == []


def test_activation_color_highlight_does_not_stale_target(tmp_path):
    harness = _Harness(tmp_path / "activation-color")
    frame = harness.screenshot()
    harness.window["foreground_hwnd"] = 202

    def highlight(target, state):
        harness.window["foreground_hwnd"] = target.hwnd
        _draw_target(harness.image, fill="#B91C1C", hover=True)
        return {
            "attempted": True,
            "status": "verified",
            "before_foreground_hwnd": str(state["foreground_hwnd"]),
            "verified_foreground_hwnd": str(target.hwnd),
            "reason_code": "target_foreground_verified",
        }

    harness.activation_behavior = highlight
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "DISPATCHED"
    assert result["dispatch_count"] == 1
    assert len(harness.dispatches) == 1


def test_permission_denied_before_activation_never_dispatches(tmp_path):
    harness = _Harness(tmp_path / "activation-permission")
    frame = harness.screenshot()
    harness.window["foreground_hwnd"] = 202
    harness.loop._permission_check = lambda _action, _frame: (
        False, "permission_revoked",
    )
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "PERMISSION_DENIED"
    assert result["reason_code"] == "permission_revoked"
    assert result["dispatch_count"] == 0
    assert not harness.activation_calls
    assert harness.window["foreground_hwnd"] == 202
    assert harness.dispatches == []


def test_window_generation_mismatch_still_fails_closed(tmp_path):
    harness = _Harness(tmp_path / "generation")
    frame = harness.screenshot()
    harness.window["process_started_at"] = 11.25
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == "window_identity_changed"
    assert result["dispatch_count"] == 0
    assert result["attempt_id"] == ""
    assert harness.activation_calls == []
    assert harness.dispatches == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("window_bounds", (101, 200, 501, 400)),
        ("client_bounds", (0, 0, 381, 170)),
        ("client_origin_screen", (111, 220)),
    ],
)
def test_changed_geometry_is_rejected_before_capture_revalidation(
    tmp_path, field, value,
):
    harness = _Harness(tmp_path / field)
    frame = harness.screenshot()
    harness.window[field] = value
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == "window_geometry_changed"
    assert not harness.dispatches


def test_capture_protected_window_fails_closed(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    harness.window["capture_protection"] = "window_excluded_from_capture"
    with pytest.raises(ComputerUseV4Error) as raised:
        harness.screenshot()
    assert raised.value.code == "CAPTURE_PROTECTED"
    assert harness.capture_count == 0


def test_actiontruth_records_a_new_frame_attempt_after_unknown(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    action = _Harness.click(frame, effect_id="fixture_click_once")
    first = harness.loop.execute(action)
    assert first["status"] == "DISPATCHED"
    assert first["dispatch_count"] == 1
    durable = harness.store.get_attempt(first["attempt_id"])
    assert durable["action_truth"]["dispatch_started_at"] > 0
    assert durable["dispatch_state"] == "effect_unknown"
    frame_after_unknown = harness.screenshot()
    assert frame_after_unknown.frame_id != frame.frame_id
    next_action = _Harness.click(
        frame_after_unknown, effect_id="fixture_click_once",
    )
    second = harness.loop.execute(next_action)
    assert second["status"] == "DISPATCHED"
    assert second["dispatch_count"] == 1
    second_durable = harness.store.get_attempt(second["attempt_id"])
    assert second_durable["action_truth"]["dispatch_started_at"] > 0
    assert second_durable["action_truth"]["action_id"] != durable[
        "action_truth"
    ]["action_id"]
    assert len(harness.dispatches) == 2


def test_possible_effect_allows_reobservation_and_a_new_action_request(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame_a = harness.screenshot()
    first = harness.loop.execute(_Harness.click(frame_a))
    assert first["status"] == "DISPATCHED"
    assert harness.loop.observation_required is True
    frame_b = harness.screenshot()
    assert frame_b.frame_id != frame_a.frame_id
    assert harness.loop.observation_required is False
    next_request = harness.loop.execute(ComputerAction(
        "keypress", frame_b.frame_id, key="enter",
    ))
    assert next_request["status"] == "DISPATCHED"
    assert next_request["dispatch_count"] == 1
    assert len(harness.dispatches) == 2


def test_stale_frame_reobserves_instead_of_reusing_coordinates(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame_a = harness.screenshot()
    harness.image = Image.new("RGB", (200, 100), "navy")
    _draw_target(harness.image, (125, 55, 185, 95), fill="orange")
    stale = harness.loop.execute(_Harness.click(frame_a))
    assert stale["status"] == "STALE_FRAME"
    frame_b = harness.screenshot()
    assert frame_b.frame_id != frame_a.frame_id
    fresh = harness.loop.execute(_Harness.click(frame_b, x=150, y=75))
    assert fresh["status"] == "DISPATCHED"
    assert fresh["screen_point"] == [400, 350]
    assert harness.dispatches == [("click", (400, 350), "", "")]


def test_batch_executes_three_independent_primitives_then_requires_observation(
    tmp_path,
):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    result = harness.loop.execute_batch([
        _Harness.click(frame, x=100, y=50, effect_id="focus_search"),
        ComputerAction(
            "type", frame.frame_id, text="Subnautica", effect_id="type_query",
        ),
        ComputerAction(
            "keypress", frame.frame_id, key="enter", observe_after=True,
            effect_id="submit_query",
        ),
    ])
    assert result["status"] == "DISPATCHED"
    assert result["executed_count"] == 3
    assert [item[0] for item in harness.dispatches] == [
        "click", "type", "keypress",
    ]
    assert harness.loop.observation_required is True


@pytest.mark.parametrize(
    "action",
    [
        lambda frame: ComputerAction(
            "type", frame.frame_id, text="Subnautica",
        ),
        lambda frame: ComputerAction(
            "keypress", frame.frame_id, key="enter",
        ),
    ],
)
def test_keyboard_actions_ignore_caret_and_pixel_repaint(tmp_path, action):
    harness = _Harness(tmp_path / "keyboard")
    frame = harness.screenshot()
    ImageDraw.Draw(harness.image).rectangle(
        (98, 35, 101, 65), fill="white",
    )
    result = harness.loop.execute(action(frame))
    assert result["status"] == "DISPATCHED"
    assert result["dispatch_count"] == 1
    assert len(harness.dispatches) == 1
    assert harness.capture_count == 1


def test_batch_stops_at_explicit_observation_boundary(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    result = harness.loop.execute_batch([
        _Harness.click(frame, observe_after=True, effect_id="navigate"),
        ComputerAction(
            "keypress", frame.frame_id, key="enter", effect_id="must_not_run",
        ),
    ])
    assert result["status"] == "REOBSERVE_REQUIRED"
    assert result["executed_count"] == 1
    assert len(harness.dispatches) == 1


def test_batch_stops_when_dispatch_outcome_becomes_unknown(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()

    def failing(_action, _point):
        raise RuntimeError("mock_dispatch_failed_after_boundary")

    harness.loop._dispatchers["click"] = failing
    result = harness.loop.execute_batch([
        _Harness.click(frame, effect_id="uncertain_click"),
        ComputerAction(
            "keypress", frame.frame_id, key="enter", effect_id="must_not_run",
        ),
    ])
    assert result["status"] == "DISPATCH_UNKNOWN"
    assert result["executed_count"] == 1
    assert result["results"][0]["dispatch_state"] == "effect_unknown"
    assert not harness.dispatches


def test_batch_stops_if_foreground_changes_between_click_and_type(tmp_path):
    harness = _Harness(tmp_path / "batch-foreground")
    frame = harness.screenshot()
    original = harness.loop._dispatchers["click"]

    def lose_foreground(action, point):
        result = original(action, point)
        harness.window["foreground_hwnd"] = 202
        return result

    harness.loop._dispatchers["click"] = lose_foreground
    result = harness.loop.execute_batch([
        _Harness.click(frame, effect_id="focus_search"),
        ComputerAction(
            "type", frame.frame_id, text="Subnautica", effect_id="type_query",
        ),
    ])

    assert result["status"] == "FOREGROUND_PREPARATION_FAILED"
    assert result["reason_code"] == "batch_target_foreground_changed"
    assert result["executed_count"] == 1
    assert [item[0] for item in harness.dispatches] == ["click"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("process_started_at", 11.25, "window_identity_changed"),
        ("window_title", "Unexpected Navigation", "window_identity_changed"),
    ],
)
def test_batch_stops_if_window_changes_between_click_and_type(
    tmp_path, field, value, reason,
):
    harness = _Harness(tmp_path / f"batch-{field}")
    frame = harness.screenshot()
    original = harness.loop._dispatchers["click"]

    def change_window(action, point):
        result = original(action, point)
        harness.window[field] = value
        return result

    harness.loop._dispatchers["click"] = change_window
    result = harness.loop.execute_batch([
        _Harness.click(frame, effect_id="focus_search"),
        ComputerAction(
            "type", frame.frame_id, text="Subnautica", effect_id="type_query",
        ),
    ])

    assert result["status"] == "STALE_FRAME"
    assert result["reason_code"] == reason
    assert result["executed_count"] == 1
    assert [item[0] for item in harness.dispatches] == ["click"]


def test_permission_denial_is_before_actiontruth_and_dispatch(tmp_path):
    harness = _Harness(tmp_path / "attempts")
    frame = harness.screenshot()
    harness.loop._permission_check = lambda _action, _frame: (
        False, "permission_revoked",
    )
    result = harness.loop.execute(_Harness.click(frame))
    assert result["status"] == "PERMISSION_DENIED"
    assert result["reason_code"] == "permission_revoked"
    assert result["attempt_id"] == ""
    assert not harness.dispatches


def test_non_vision_route_rejects_image_before_transport():
    from osia.image_inputs import image_input_decision

    decision = image_input_decision(
        "groq", "llama-3.3-70b-versatile", ("image/png",),
    )
    assert decision.model_known
    assert not decision.model_supports_vision
    assert not decision.usable


def test_v4_observation_requires_exact_consumed_frame_and_window():
    from osia.execution_strategy import _v4_observation_correlates

    binding = {
        "conversation_id": "conversation",
        "turn_id": "turn",
        "provider_id": "openai",
        "model_id": "vision-model",
    }
    source = {
        **binding,
        "frame_id": "frame-1",
        "v4_loop_id": "loop-1",
        "window_generation": "window-1",
        "native_window_id": "123",
    }
    observation = {
        **source,
        "image_input_sent": True,
        "image_input_consumed": True,
    }
    assert _v4_observation_correlates(source, binding, observation)
    for key in (
        "conversation_id", "turn_id", "provider_id", "model_id",
        "frame_id", "v4_loop_id", "window_generation", "native_window_id",
    ):
        stale = {**observation, key: "stale"}
        assert not _v4_observation_correlates(source, binding, stale)
    assert _v4_observation_correlates(
        source, binding, {**observation, "image_input_consumed": False},
    )
    assert not _v4_observation_correlates(
        source, binding, {**observation, "image_input_consumed": False},
        require_consumed=True,
    )

def test_model_schema_is_bounded_and_exposes_only_small_vocabulary():
    schema = computer_action_schema()
    actions = schema["properties"]["actions"]
    assert actions["maxItems"] == 3
    assert actions["items"]["properties"]["kind"]["enum"] == [
        "click", "double_click", "type", "keypress", "scroll", "zoom",
    ]
    assert "proposal" not in repr(schema).lower()
    assert "lease" not in repr(schema).lower()
