"""Focused WOR-155 Settings master/detail scroll-ownership regressions."""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QAbstractItemView

from osia import updater
from osia.gui import main_popup
from osia.gui.update_ui import UpdateController


_QT_APP = QApplication.instance() or QApplication([])


class _DormantThread:
    def __init__(self, *args, target=None, **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)

    def start(self) -> None:
        return None


@pytest.fixture(scope="module")
def qt_app():
    yield _QT_APP


@pytest.fixture(scope="module")
def settings_panel(qt_app, tmp_path_factory):
    patch = pytest.MonkeyPatch()
    patch.setenv("OSIA_REDUCED_MOTION", "1")
    patch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    state_path = tmp_path_factory.mktemp("wor155") / "update-state.json"
    controller = UpdateController(store=updater.UpdateStateStore(state_path))
    panel = main_popup._SettingsPanel(controller)
    panel.resize(640, 560)
    panel.show()
    qt_app.processEvents()
    yield panel
    panel.close()
    panel.deleteLater()
    controller.deleteLater()
    qt_app.processEvents()
    patch.undo()


def _wheel(
    widget,
    *,
    angle_y: int = 0,
    pixel_y: int = 0,
    inverted: bool = False,
) -> QWheelEvent:
    local = QPointF(widget.rect().center())
    global_pos = QPointF(widget.mapToGlobal(widget.rect().center()))
    return QWheelEvent(
        local,
        global_pos,
        QPoint(0, pixel_y),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        inverted,
    )


def _send_wheel(
    panel,
    target,
    *,
    angle_y: int = 0,
    pixel_y: int = 0,
    inverted: bool = False,
):
    """Send a wheel event, including Qt's ignored-viewport parent fallback."""
    event = _wheel(
        target,
        angle_y=angle_y,
        pixel_y=pixel_y,
        inverted=inverted,
    )
    QApplication.sendEvent(target, event)
    delivered = [event]
    nav = panel._settings_nav
    if target is nav.viewport() and not event.isAccepted():
        parent_event = _wheel(
            nav,
            angle_y=angle_y,
            pixel_y=pixel_y,
            inverted=inverted,
        )
        QApplication.sendEvent(nav, parent_event)
        delivered.append(parent_event)
    QApplication.processEvents()
    return delivered


def _make_nav_scrollable(panel, *, height: int = 100):
    nav = panel._settings_nav
    nav.setFixedHeight(height)
    QApplication.processEvents()
    bar = nav.verticalScrollBar()
    assert bar.maximum() > 0
    return bar


def _show_category(panel, key: str) -> None:
    panel.show_category(key)
    QApplication.processEvents()
    assert panel._settings_pages[panel._settings_nav.currentRow()][0] == key


def test_rapid_settings_section_changes_coalesce_extent_recalculation(
    qt_app, tmp_path, monkeypatch,
):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    controller = UpdateController(
        store=updater.UpdateStateStore(tmp_path / "extent-state.json")
    )
    panel = main_popup._SettingsPanel(controller)
    panel.resize(640, 560)
    panel.show()
    qt_app.processEvents()
    qt_app.processEvents()
    panel._settings_extent_refresh_scheduled = False

    with patch.object(
        panel,
        "_sync_settings_detail_extent",
        wraps=panel._sync_settings_detail_extent,
    ) as sync_extent:
        panel.show_category("settings.cat.general")
        panel.show_category("settings.cat.models")
        panel.show_category("settings.cat.wake")

        assert sync_extent.call_count == 0
        qt_app.processEvents()
        assert sync_extent.call_count == 1

    panel.close()
    panel.deleteLater()
    controller.deleteLater()
    qt_app.processEvents()


@pytest.mark.parametrize(
    ("start_at_bottom", "angle_y"),
    [(False, -120), (True, 120)],
    ids=["down", "up"],
)
def test_scrollable_left_pane_owns_angle_wheel(
    settings_panel, start_at_bottom, angle_y,
):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    left = _make_nav_scrollable(panel)
    right = panel._settings_scroll.verticalScrollBar()
    left.setValue(left.maximum() if start_at_bottom else left.minimum())
    right.setValue(right.maximum() // 2)
    left_before, right_before = left.value(), right.value()

    delivered = _send_wheel(panel, panel._settings_nav.viewport(), angle_y=angle_y)

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    if start_at_bottom:
        assert left.value() < left_before
    else:
        assert left.value() > left_before
    assert right.value() == right_before


@pytest.mark.parametrize(
    ("at_bottom", "angle_y"),
    [(False, 120), (True, -120)],
    ids=["top", "bottom"],
)
def test_left_boundary_consumes_wheel_without_detail_leak(
    settings_panel, at_bottom, angle_y,
):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    left = _make_nav_scrollable(panel)
    right = panel._settings_scroll.verticalScrollBar()
    left.setValue(left.maximum() if at_bottom else left.minimum())
    right.setValue(right.maximum() // 2)
    before = (left.value(), right.value())

    delivered = _send_wheel(panel, panel._settings_nav.viewport(), angle_y=angle_y)

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    assert (left.value(), right.value()) == before


def test_non_scrollable_left_pane_consumes_wheel(settings_panel):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    nav = panel._settings_nav
    nav.setFixedHeight(514)
    QApplication.processEvents()
    left = nav.verticalScrollBar()
    right = panel._settings_scroll.verticalScrollBar()
    assert left.maximum() == 0
    right.setValue(right.maximum() // 2)
    right_before = right.value()

    delivered = _send_wheel(panel, nav.viewport(), angle_y=-120)

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    assert left.value() == 0
    assert right.value() == right_before


def test_right_detail_wheel_keeps_left_navigation_fixed(settings_panel):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    left = _make_nav_scrollable(panel)
    right = panel._settings_scroll.verticalScrollBar()
    left.setValue(left.maximum() // 2)
    right.setValue(0)
    left_before = left.value()

    delivered = _send_wheel(
        panel,
        panel._settings_scroll.viewport(),
        angle_y=-120,
    )

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    assert right.value() > 0
    assert left.value() == left_before


def test_rapid_wheel_events_are_applied_once_without_delayed_leak(settings_panel):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    left = _make_nav_scrollable(panel, height=60)
    right = panel._settings_scroll.verticalScrollBar()
    right.setValue(right.maximum() // 2)
    right_before = right.value()

    left.setValue(0)
    _send_wheel(panel, panel._settings_nav.viewport(), angle_y=-120)
    one_event_distance = left.value()
    assert 0 < one_event_distance < left.maximum()
    left.setValue(0)
    for _ in range(2):
        delivered = _send_wheel(
            panel,
            panel._settings_nav.viewport(),
            angle_y=-120,
        )
        assert len(delivered) == 1 and delivered[0].isAccepted()
    assert left.value() == min(left.maximum(), one_event_distance * 2)

    for _ in range(20):
        _send_wheel(panel, panel._settings_nav.viewport(), angle_y=-120)
    assert left.value() == left.maximum()
    for _ in range(20):
        _send_wheel(panel, panel._settings_nav.viewport(), angle_y=120)
    QApplication.processEvents()
    QApplication.processEvents()
    assert left.value() == left.minimum()
    assert right.value() == right_before


@pytest.mark.parametrize("inverted", [False, True], ids=["normal", "inverted"])
def test_pixel_delta_trackpad_event_stays_in_left_pane(settings_panel, inverted):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    left = _make_nav_scrollable(panel)
    right = panel._settings_scroll.verticalScrollBar()
    assert (
        panel._settings_nav.verticalScrollMode()
        == QAbstractItemView.ScrollMode.ScrollPerPixel
    )
    left.setValue(0)
    right.setValue(right.maximum() // 2)
    right_before = right.value()

    delivered = _send_wheel(
        panel,
        panel._settings_nav.viewport(),
        pixel_y=-11,
        inverted=inverted,
    )

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    assert left.value() == 11
    assert right.value() == right_before


def test_actual_navigation_item_target_cannot_leak_to_detail(settings_panel):
    panel = settings_panel
    _show_category(panel, "settings.cat.models")
    left = _make_nav_scrollable(panel)
    nav = panel._settings_nav
    item_rect = nav.visualItemRect(nav.item(nav.currentRow()))
    target = QApplication.widgetAt(nav.viewport().mapToGlobal(item_rect.center()))
    assert target is nav.viewport()
    left.setValue(left.maximum())
    right = panel._settings_scroll.verticalScrollBar()
    right.setValue(right.maximum() // 2)
    before = (left.value(), right.value())

    delivered = _send_wheel(panel, target, angle_y=-120)

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    assert (left.value(), right.value()) == before


@pytest.mark.parametrize(
    "category_key",
    [
        "settings.cat.general",
        "settings.cat.models",
        "settings.cat.voice",
        "settings.cat.mobile",
        "settings.cat.updates",
        "settings.cat.advanced",
    ],
)
def test_category_switching_preserves_independent_scroll_ownership(
    settings_panel, category_key,
):
    panel = settings_panel
    _show_category(panel, category_key)
    left = _make_nav_scrollable(panel)
    left.setValue(left.maximum())
    right = panel._settings_scroll.verticalScrollBar()
    right.setValue(right.maximum() // 2)
    before = (left.value(), right.value(), panel._settings_nav.currentRow())

    delivered = _send_wheel(panel, panel._settings_nav.viewport(), angle_y=-120)

    assert len(delivered) == 1
    assert delivered[0].isAccepted()
    assert (left.value(), right.value(), panel._settings_nav.currentRow()) == before


def test_every_category_resets_only_right_detail_without_recreation(settings_panel):
    panel = settings_panel
    nav = panel._settings_nav
    left = _make_nav_scrollable(panel)
    right = panel._settings_scroll.verticalScrollBar()
    window = panel.window()
    geometry = window.geometry()
    stack_identity = id(panel._settings_stack)
    page_identities = tuple(
        id(panel._settings_stack.widget(index))
        for index in range(panel._settings_stack.count())
    )

    for row, (category_key, _sections) in enumerate(panel._settings_pages):
        nav.setCurrentRow((row + 1) % nav.count())
        left.setValue(left.maximum() // 2)
        left_before = left.value()
        right.setValue(right.maximum())

        panel.show_category(category_key)
        QApplication.processEvents()

        assert right.value() == 0
        assert left.value() == left_before
        assert nav.currentRow() == row
        assert panel._settings_stack.currentIndex() == row
        assert id(panel._settings_stack) == stack_identity
        assert tuple(
            id(panel._settings_stack.widget(index))
            for index in range(panel._settings_stack.count())
        ) == page_identities
        assert window.geometry() == geometry


def test_short_category_uses_its_own_content_bounds(settings_panel, qt_app):
    panel = settings_panel
    long_row = next(
        row for row, (key, _sections) in enumerate(panel._settings_pages)
        if key == "settings.cat.models"
    )
    short_row = next(
        row for row, (key, _sections) in enumerate(panel._settings_pages)
        if key == "settings.cat.wake"
    )
    bar = panel._settings_scroll.verticalScrollBar()

    panel._settings_nav.setCurrentRow(long_row)
    qt_app.processEvents()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())

    panel._settings_nav.setCurrentRow(short_row)
    qt_app.processEvents()

    assert bar.value() == 0
    expected_scroll_range = max(
        0,
        panel._settings_stack.height()
        - panel._settings_scroll.viewport().height(),
    )
    assert bar.maximum() == expected_scroll_range
    assert panel._settings_stack.sizeHint() == (
        panel._settings_stack.currentWidget().sizeHint()
    )


def test_late_retry_restore_does_not_reopen_a_previous_category(settings_panel):
    panel = settings_panel
    old_row = next(
        row for row, (key, _sections) in enumerate(panel._settings_pages)
        if key == "settings.cat.models"
    )
    current_row = next(
        row for row, (key, _sections) in enumerate(panel._settings_pages)
        if key == "settings.cat.general"
    )
    panel._settings_nav.setCurrentRow(old_row)
    panel._settings_scroll.verticalScrollBar().setValue(24)
    panel._chatgpt_retry_view_state = (old_row, 24)

    panel._settings_nav.setCurrentRow(current_row)
    panel._restore_chatgpt_retry_view(clear=True)

    assert panel._settings_nav.currentRow() == current_row
    assert panel._settings_scroll.verticalScrollBar().value() == 0
    assert panel._chatgpt_retry_view_state is None


def test_main_popup_keeps_settings_shell_fixed_and_bounds_full_content(
    qt_app, tmp_path, monkeypatch,
):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    controller = UpdateController(
        store=updater.UpdateStateStore(tmp_path / "settings-update-state.json")
    )
    monkeypatch.setattr(
        main_popup,
        "UpdateController",
        lambda _parent: controller,
    )
    monkeypatch.setattr(
        main_popup.MainPopup,
        "_refresh_model_inventory",
        lambda _self: None,
    )
    monkeypatch.setattr(
        main_popup.MainPopup,
        "_request_local_models_refresh",
        lambda _self: None,
    )
    monkeypatch.setattr(
        main_popup._SettingsPanel,
        "_probe_chatgpt_account",
        lambda _self, force=False: None,
    )
    monkeypatch.setattr(
        main_popup._SettingsPanel,
        "_refresh_startup_status",
        lambda _self, force=False: None,
    )
    monkeypatch.setattr(
        main_popup._SettingsPanel,
        "_refresh_mcp_status",
        lambda _self: None,
    )

    popup = main_popup.MainPopup()
    popup.resize(640, 560)
    popup._stack.setCurrentIndex(popup.PAGE_SETTINGS)
    popup._settings.show_category("settings.cat.models")
    popup.show()
    qt_app.processEvents()

    panel = popup._settings
    nav = panel._settings_nav
    scroll = panel._settings_scroll
    bar = scroll.verticalScrollBar()
    shell_geometry = popup.geometry()
    header_geometry = panel._settings_title.geometry()
    nav_geometry = nav.geometry()
    scroll_geometry = scroll.geometry()
    viewport_geometry = scroll.viewport().geometry()

    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    qt_app.processEvents()

    assert bar.value() == bar.maximum()
    assert bar.maximum() == max(
        0,
        panel._settings_stack.height() - scroll.viewport().height(),
    )
    assert popup.geometry() == shell_geometry
    assert panel._settings_title.geometry() == header_geometry
    assert nav.geometry() == nav_geometry
    assert scroll.geometry() == scroll_geometry
    assert scroll.viewport().geometry() == viewport_geometry

    last_section = panel._settings_pages[
        panel._settings_nav.currentRow()
    ][1][-1]
    last_section_bottom = last_section.mapTo(
        scroll.viewport(),
        last_section.rect().bottomLeft(),
    ).y()
    assert last_section_bottom <= scroll.viewport().height()
    assert scroll.viewport().height() - last_section_bottom <= 18

    for _ in range(20):
        _send_wheel(
            panel,
            scroll.viewport(),
            angle_y=-120,
        )
    assert bar.value() == bar.maximum()
    assert popup.geometry() == shell_geometry
    assert panel._settings_title.geometry() == header_geometry
    assert nav.geometry() == nav_geometry
    assert scroll.geometry() == scroll_geometry
    assert scroll.viewport().geometry() == viewport_geometry

    panel.show_category("settings.cat.wake")
    qt_app.processEvents()
    assert bar.value() == 0
    assert bar.maximum() == 0
    assert popup.geometry() == shell_geometry
    assert nav.geometry() == nav_geometry
    assert scroll.geometry() == scroll_geometry
    assert scroll.viewport().geometry() == viewport_geometry

    panel.show_category("settings.cat.models")
    qt_app.processEvents()
    assert bar.value() == 0
    assert bar.maximum() > 0
    assert popup.geometry() == shell_geometry
    assert nav.geometry() == nav_geometry
    assert scroll.geometry() == scroll_geometry
    assert scroll.viewport().geometry() == viewport_geometry

    popup.close()
    popup.deleteLater()
    controller.deleteLater()
    qt_app.processEvents()


def test_keyboard_navigation_and_focus_keep_native_pane_ownership(
    settings_panel, qt_app,
):
    panel = settings_panel
    _show_category(panel, "settings.cat.general")
    left = _make_nav_scrollable(panel)
    right = panel._settings_scroll.verticalScrollBar()
    nav = panel._settings_nav
    assert nav.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert panel._settings_scroll.focusPolicy() == Qt.FocusPolicy.StrongFocus

    nav.setCurrentRow(0)
    left.setValue(left.maximum())
    navigation_scroll = left.value()
    nav.setFocus()
    QTest.keyClick(nav, Qt.Key.Key_Down)
    qt_app.processEvents()
    assert nav.currentRow() == 1
    assert left.value() == navigation_scroll
    assert right.value() == 0

    QTest.keyClick(nav, Qt.Key.Key_PageDown)
    qt_app.processEvents()
    assert nav.currentRow() > 1
    assert left.value() == navigation_scroll
    assert right.value() == 0

    _show_category(panel, "settings.cat.general")
    left.setValue(left.maximum() // 2)
    right.setValue(0)
    left_before = left.value()
    panel._settings_scroll.setFocus()
    QTest.keyClick(panel._settings_scroll, Qt.Key.Key_PageDown)
    qt_app.processEvents()
    assert right.value() > 0
    assert left.value() == left_before

    nav.setFocus()
    QTest.keyClick(nav, Qt.Key.Key_Tab)
    qt_app.processEvents()
    assert qt_app.focusWidget() is not None
    assert qt_app.focusWidget() is not nav
