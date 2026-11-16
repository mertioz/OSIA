"""Activity motion stays confined to the existing capsule's three shapes."""
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from osia.gui import dynamic_island as module
from osia.gui.theme import PALETTES

_LIVE_APPS = []


@pytest.fixture
def island(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _LIVE_APPS.append(app)
    monkeypatch.setattr("osia.config.load_settings", lambda: {})
    monkeypatch.setattr(module, "reduced_motion_requested", lambda *_: False)
    widget = module.DynamicIsland()
    widget._launch_anim.stop()
    widget._launch_t = 1.0
    try:
        yield widget
    finally:
        widget.hide()
        widget.deleteLater()
        app.processEvents()


def _image(widget, ms):
    widget._anim_ms = ms
    widget._phase = ms * 0.10 / 33
    frame = QImage(widget.size(), QImage.Format.Format_ARGB32)
    frame.fill(0)
    painter = QPainter(frame)
    widget.render(painter)
    painter.end()
    return frame


def test_idle_and_hidden_capsules_have_no_animation_timer(island):
    assert island.current_state() == "idle"
    assert not island._timer.isActive()
    island.set_state("thinking")
    assert island._timer.isActive()
    island.hide()
    assert not island._timer.isActive()
    island.show()
    assert island.current_state() == "thinking"
    assert island._timer.isActive()
    island.set_state("idle")
    assert not island._timer.isActive()
    island.set_state("unknown")
    assert not island._timer.isActive()


def test_elapsed_time_drives_motion_after_delayed_frames(island, monkeypatch):
    island.set_state("thinking")
    monkeypatch.setattr(island, "_state_clock", SimpleNamespace(elapsed=lambda: 920))
    island._tick()
    assert island._anim_ms == 920
    phase = island._phase
    island._tick()
    assert island._anim_ms == 920 and island._phase == phase


@pytest.mark.parametrize("state", ["listening", "thinking", "speaking"])
def test_reduced_motion_keeps_static_feedback_without_timer(island, monkeypatch, state):
    monkeypatch.setattr(module, "reduced_motion_requested", lambda *_: True)
    island.set_state(state)
    assert not island._timer.isActive()
    before = _image(island, 0)
    island._tick()
    assert island._anim_ms == 0
    assert _image(island, 0) == before
    assert island.current_state() == state


@pytest.mark.parametrize("shape,size", [("capsule", (320, 52)), ("circle", (60, 60)), ("docked", (28, 96))])
@pytest.mark.parametrize("theme", list(PALETTES))
def test_wake_and_thinking_are_distinct_animated_states_without_geometry_changes(island, monkeypatch, shape, size, theme):
    monkeypatch.setattr(island._theme, "_palette", PALETTES[theme])
    island._refresh_theme_colors()
    island._shape = shape
    island._dock_side = "left"
    island._apply_shape_geometry(animate=False)
    assert (island.width(), island.height()) == size
    idle = _image(island, 0)
    assert idle == _image(island, 500)
    frames = []
    for state in ("listening", "thinking"):
        island.set_state(state)
        island._morph_anim.stop()
        island._apply_shape_geometry(animate=False, slid_out=shape == "docked")
        first = _image(island, 90)
        later = _image(island, 650)
        assert first != later
        frames.append(first)
        assert (island.width(), island.height()) == size
    assert frames[0] != frames[1]
    assert island.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    island.set_state("idle")
    island._morph_anim.stop()
    island._apply_shape_geometry(animate=False, slid_out=False)
    assert _image(island, 0) == idle


@pytest.mark.parametrize("side", ["left", "right"])
def test_docked_activity_preserves_narrow_peek_and_return_position(island, side):
    island._set_shape("docked", side)
    island._morph_anim.stop()
    island._apply_shape_geometry(animate=False, slid_out=False)
    resting = island.geometry()
    for state in ("listening", "thinking", "speaking"):
        island.set_state(state)
        island._morph_anim.stop()
        island._apply_shape_geometry(animate=False, slid_out=True)
        assert island.width() == module.DOCK_PILL_W == 28
        assert island.height() == module.DOCK_PILL_H == 96
        assert abs(island.x() - resting.x()) == module.DOCK_PEEK == 14
    island.set_state("idle")
    island._morph_anim.stop()
    island._apply_shape_geometry(animate=False, slid_out=False)
    assert island.geometry() == resting
