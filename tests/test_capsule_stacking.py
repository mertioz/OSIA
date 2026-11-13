"""The capsule repairs its Windows stacking without activating other windows."""
import ctypes
from types import SimpleNamespace

import pytest

_LIVE_APPS = []


def test_windows_repair_preserves_focus_geometry_and_owner_and_does_not_reraise_topmost(monkeypatch):
    from osia.gui.dynamic_island import _restore_windows_topmost

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    calls = []
    topmost = [False]
    user32 = SimpleNamespace(
        IsWindow=Function(lambda hwnd: hwnd == 123),
        GetWindowLongW=Function(lambda *_: 8 if topmost[0] else 0),
        SetWindowPos=Function(lambda *args: calls.append(args) or 1),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: user32, raising=False)
    assert _restore_windows_topmost(123)
    hwnd, after, x, y, w, h, flags = calls[0]
    assert hwnd == 123 and after.value == ctypes.c_void_p(-1).value
    assert (x, y, w, h) == (0, 0, 0, 0)
    assert flags & 0x0010  # SWP_NOACTIVATE
    assert flags & 0x0003 == 0x0003  # SWP_NOMOVE | SWP_NOSIZE
    assert flags & 0x0200  # SWP_NOOWNERZORDER
    assert not flags & 0x0004  # Do repair the capsule's own Z-order band.
    assert not _restore_windows_topmost(0)
    assert not _restore_windows_topmost(456)
    topmost[0] = True
    assert _restore_windows_topmost(123)
    assert len(calls) == 1


@pytest.mark.parametrize("shape", ["capsule", "circle", "docked"])
def test_capsule_rechecks_after_show_and_handle_change_but_never_reshows_when_disabled(monkeypatch, shape):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication
    from osia.gui import dynamic_island

    app = QApplication.instance() or QApplication([])
    _LIVE_APPS.append(app)
    monkeypatch.setattr("osia.config.load_settings", lambda: {})
    monkeypatch.setattr(dynamic_island, "reduced_motion_requested", lambda *_: True)
    monkeypatch.setattr(dynamic_island, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(QApplication, "platformName", lambda: "windows")
    calls = []
    monkeypatch.setattr(dynamic_island, "_restore_windows_topmost", calls.append)
    island = dynamic_island.DynamicIsland()
    try:
        app.processEvents()
        assert calls
        island._shape = shape
        island._dock_side = "left"
        island._apply_shape_geometry(animate=False)
        geometry = island.geometry()
        QApplication.sendEvent(island, QEvent(QEvent.Type.WinIdChange))
        app.processEvents()
        assert len(calls) >= 2
        assert island.geometry() == geometry
        assert island._stacking_timer.isActive()
        island.set_capsule_visible(False)
        assert not island._stacking_timer.isActive()
        count = len(calls)
        island._ensure_topmost()
        QApplication.sendEvent(island, QEvent(QEvent.Type.WinIdChange))
        app.processEvents()
        assert len(calls) == count
        assert not island.isVisible()
        island.set_capsule_visible(True)
        app.processEvents()
        assert len(calls) > count
        assert island.size() == geometry.size()
        assert island._shape == shape
        geometry = island.geometry()
        island._ensure_topmost()
        assert island.geometry() == geometry
    finally:
        island.close()
        island.deleteLater()
        app.processEvents()
