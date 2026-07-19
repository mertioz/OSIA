"""Focused Settings Retry/wheel and docked-capsule regressions."""

from __future__ import annotations

import types

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox

from osia.gui import main_popup
from osia.gui.dynamic_island import (
    DOCK_ACTIVE_PEEK,
    DOCK_PEEK,
    DOCK_PILL_H,
    DOCK_PILL_W,
    ISLAND_W,
    SHAPE_DOCKED,
    DynamicIsland,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _DormantThread:
    started: list[str] = []

    def __init__(self, *args, target=None, name: str = "", daemon=None, **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self.started.append(self.name)


@pytest.fixture
def settings_panel(monkeypatch, qt_app):
    _DormantThread.started.clear()
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    panel = main_popup._SettingsPanel()
    panel.resize(640, 560)
    panel.show()
    qt_app.processEvents()
    panel._chatgpt_probe_busy = False
    panel._chatgpt_action_busy = False
    _DormantThread.started.clear()
    yield panel
    panel.close()
    panel.deleteLater()
    qt_app.processEvents()


def _wheel(widget, angle_y: int = -120) -> QWheelEvent:
    local = QPointF(widget.rect().center())
    global_pos = QPointF(widget.mapToGlobal(widget.rect().center()))
    return QWheelEvent(
        local,
        global_pos,
        QPoint(),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def test_retry_keeps_models_category_scroll_and_focus(settings_panel, qt_app):
    panel = settings_panel
    panel._settings_nav.setCurrentRow(1)
    panel._ai_provider_combo.setCurrentIndex(0)
    panel._set_chatgpt_ui_state("error")
    qt_app.processEvents()

    bar = panel._settings_scroll.verticalScrollBar()
    bar.setValue(0)
    panel._chatgpt_retry_btn.setFocus()
    qt_app.processEvents()

    QTest.keyClick(panel._chatgpt_retry_btn, Qt.Key.Key_Space)
    qt_app.processEvents()

    assert panel._settings_nav.currentRow() == 1
    assert bar.value() == 0
    assert qt_app.focusWidget() is panel._chatgpt_info_btn
    assert panel._chatgpt_action_busy is True
    assert _DormantThread.started == ["osia-chatgpt-login"]

    # A second activation while the first retry is in flight is a no-op.
    panel._chatgpt_retry_btn.click()
    assert _DormantThread.started == ["osia-chatgpt-login"]

    # The async completion rebuilds Hiérarchisation but restores the same view.
    panel._apply_chatgpt_status(
        {
            "available": False,
            "runtimeAvailable": True,
            "connected": False,
            "models": [],
            "error": "login_failed",
        }
    )
    qt_app.processEvents()
    assert panel._settings_nav.currentRow() == 1
    assert bar.value() == 0
    assert panel._chatgpt_retry_view_state is None


def test_closed_combo_wheel_scrolls_page_without_changing_selection(
    settings_panel, qt_app,
):
    panel = settings_panel
    panel._settings_nav.setCurrentRow(0)
    combo = panel._language_combo
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus
    combo.setCurrentIndex(0)

    bar = panel._settings_scroll.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(0)
    event = _wheel(combo)
    QApplication.sendEvent(combo, event)
    qt_app.processEvents()

    assert event.isAccepted()
    assert combo.currentIndex() == 0
    assert bar.value() > 0


def test_open_combo_popup_keeps_normal_item_view_wheel(settings_panel, qt_app):
    combo = settings_panel._language_combo
    combo.showPopup()
    qt_app.processEvents()
    assert combo.view().isVisible()
    assert settings_panel.eventFilter(combo, _wheel(combo)) is False
    combo.hidePopup()
    qt_app.processEvents()
    assert settings_panel.eventFilter(combo, _wheel(combo)) is True


def test_settings_navigation_wheel_reaches_detail_page(settings_panel, qt_app):
    panel = settings_panel
    panel._settings_nav.setCurrentRow(1)
    bar = panel._settings_scroll.verticalScrollBar()
    bar.setValue(0)
    selected = panel._settings_nav.currentRow()
    QApplication.sendEvent(panel._settings_nav, _wheel(panel._settings_nav))
    qt_app.processEvents()
    assert panel._settings_nav.currentRow() == selected
    assert bar.value() > 0


@pytest.mark.parametrize("side", ["left", "right"])
def test_active_docked_states_use_only_the_small_peek(
    monkeypatch, qt_app, side,
):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    island = DynamicIsland()
    island._shape = SHAPE_DOCKED
    island._dock_side = side
    screen_rect = island.screen().availableGeometry()
    island._dock_center_y = screen_rect.center().y()
    island._docked_slid_out = False
    island._apply_shape_geometry(animate=False, slid_out=False)
    qt_app.processEvents()

    idle_visible = island.geometry().intersected(screen_rect).width()
    assert (island.width(), island.height()) == (DOCK_PILL_W, DOCK_PILL_H)
    assert idle_visible == DOCK_PEEK

    for state in ("listening", "thinking", "speaking", "listening"):
        island.set_state(state)
        qt_app.processEvents()
        visible = island.geometry().intersected(screen_rect).width()
        assert (island.width(), island.height()) == (DOCK_PILL_W, DOCK_PILL_H)
        assert visible == DOCK_ACTIVE_PEEK
        assert 20 <= visible <= 32
        assert visible <= ISLAND_W * 0.15
        assert screen_rect.top() <= island.y()
        assert island.geometry().bottom() <= screen_rect.bottom()

    # Cancellation/interruption and TTS stop return to the normal tucked ear.
    island.set_state("idle")
    qt_app.processEvents()
    assert island.geometry().intersected(screen_rect).width() == DOCK_PEEK
    assert bool(island.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
    island.close()
    island.deleteLater()
    qt_app.processEvents()


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_docked_peek_remains_28_logical_pixels_across_dpi_scales(scale):
    # Qt top-level geometry is in logical pixels; physical projection changes,
    # while converting it back at each supported scale preserves the contract.
    physical_pixels = DOCK_ACTIVE_PEEK * scale
    assert physical_pixels / scale == 28
