from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows WGC lifecycle")
def test_persistent_wgc_owns_pixels_starts_once_and_joins_once(monkeypatch):
    import numpy as np

    from osia import desktop_scene
    from osia.desktop_surface import SurfaceBounds

    bounds = SurfaceBounds(10, 20, 130, 100)
    source_pixels = np.zeros((80, 120, 4), dtype=np.uint8)
    source_pixels[:, :, :] = (20, 60, 180, 255)
    source_pixels[8:72, 18:102, :3] = (200, 40, 80)
    controls = []
    captures = []

    class Control:
        def __init__(self):
            self.stop_count = 0
            self.wait_count = 0

        def stop(self):
            self.stop_count += 1

        def wait(self):
            self.wait_count += 1

        def is_finished(self):
            return self.wait_count > 0

    class Capture:
        def __init__(self, **kwargs):
            assert kwargs["dirty_region"] is False
            assert kwargs["window_hwnd"] == 101
            self.handlers = {}
            self.frame_handler = None
            self.closed_handler = None
            captures.append(self)

        def event(self, handler):
            self.handlers[handler.__name__] = handler
            if handler.__name__ == "on_frame_arrived":
                self.frame_handler = handler
            else:
                self.closed_handler = handler
            return handler

        def start_free_threaded(self):
            self.handlers["on_frame_arrived"](
                SimpleNamespace(frame_buffer=source_pixels),
                SimpleNamespace(),
            )
            control = Control()
            controls.append(control)
            return control

    monkeypatch.setitem(
        sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=Capture),
    )
    monkeypatch.setattr(
        desktop_scene, "_window_advanced_color_enabled", lambda _hwnd: False,
    )
    monkeypatch.setattr(desktop_scene, "_window_bounds", lambda _hwnd: bounds)
    monkeypatch.setattr(
        desktop_scene, "_window_client_bounds", lambda _hwnd: bounds,
    )

    session = desktop_scene.PersistentWgcWindowCapture(101)
    first, method, first_provenance = session.snapshot(bounds)
    source_pixels[:, :, :] = 0
    second, _method, second_provenance = session.snapshot(bounds)

    assert method == "win32_windows_graphics_capture_persistent"
    assert first.getpixel((60, 40)) == (80, 40, 200)
    assert second.getpixel((60, 40)) == (80, 40, 200)
    assert first_provenance["wgc_session_sequence"] == 1
    assert second_provenance["wgc_session_sequence"] == 1
    assert second_provenance["continuous_unchanged_observation"] is True
    assert session.lifecycle_snapshot()["capture_start_count"] == 1

    session.close()
    lifecycle = session.lifecycle_snapshot()
    assert lifecycle["capture_stop_count"] == 1
    assert lifecycle["latest_frame_retained"] is False
    assert controls[0].stop_count == 1
    assert controls[0].wait_count == 1
    assert captures[0].frame_handler is None
    assert captures[0].closed_handler is None


def test_v4_persistent_source_releases_the_old_window_before_switching(monkeypatch):
    from osia import desktop_scene
    from osia.computer_use_v4 import _PersistentV4CaptureSource

    sessions = []

    class Session:
        def __init__(self, hwnd):
            assert all(session.closed for session in sessions)
            self.hwnd = hwnd
            self.closed = False
            sessions.append(self)

        def snapshot(self, bounds):
            return self.hwnd, bounds, {}

        def close(self):
            self.closed = True

        def lifecycle_snapshot(self):
            return {
                "capture_start_count": 1,
                "capture_stop_count": int(self.closed),
            }

    monkeypatch.setattr(desktop_scene, "PersistentWgcWindowCapture", Session)
    source = _PersistentV4CaptureSource()
    state = {"bounds_object": "bounds"}

    assert source(101, state)[:2] == (101, "bounds")
    assert source(202, state)[:2] == (202, "bounds")
    assert len(sessions) == 2
    assert sessions[0].closed is True
    assert sessions[1].closed is False
    source.close()
    assert sessions[1].closed is True
    assert source.snapshot()["capture_stop_count"] == 1


def test_capture_cleanup_error_does_not_leak_the_active_turn():
    from osia.computer_use_v4_integration import (
        computer_use_v4_turn_scope,
        current_computer_use_v4_state,
    )

    def fail_close():
        raise RuntimeError("capture cleanup failed")

    with pytest.raises(RuntimeError, match="capture cleanup failed"):
        with computer_use_v4_turn_scope(
            "openai", "gpt-5.4", conversation_id="fixture", turn_id="fixture",
        ) as state:
            state.loop = SimpleNamespace(close=fail_close)
    assert current_computer_use_v4_state() is None
