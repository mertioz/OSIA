"""Presentation transitions preserve immediate input and canonical UI state."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from osia.gui import main_popup
from osia.gui import motion as motion_module
from osia.gui.motion import ContentTransition, MOTION, MotionController, ThemeTransition
from osia.gui.switch import OsiaSwitch


APP = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("transition_type", [ContentTransition, ThemeTransition])
@pytest.mark.parametrize("background_alpha", [0, 80, 169, 255])
@pytest.mark.parametrize("scale", [1.0, 1.25, 2.0])
def test_crossfade_preserves_background_alpha_at_every_frame(
    transition_type, background_alpha, scale, monkeypatch,
):
    """A constant translucent surface must never reveal more of the desktop.

    Exercise the actual overlay painter on a known backing-store pixel. A full
    QWidget.grab alone hid this regression by repainting the whole window.
    """
    root = QWidget()
    root.resize(40, 40)
    transition = transition_type(root)
    transition.resize(root.size())
    original = QImage(round(40 * scale), round(40 * scale), QImage.Format.Format_ARGB32_Premultiplied)
    original.setDevicePixelRatio(scale)
    original.fill(QColor(32, 24, 45, background_alpha))
    transition._frame = QPixmap.fromImage(original)
    try:
        for percent in range(0, 101, 5):
            canvas = original.copy()
            monkeypatch.setattr(
                motion_module, "QPainter",
                lambda device: QPainter(canvas if device is transition else device),
            )
            # Leave enum references usable by the production painting code.
            motion_module.QPainter.CompositionMode = QPainter.CompositionMode
            transition._alpha = percent / 100
            transition.paintEvent(None)
            for x, y in [(20, 20), (canvas.width() - 1, canvas.height() - 1)]:
                pixel = canvas.pixelColor(x, y)
                assert abs(pixel.alpha() - background_alpha) <= 1, (percent, pixel.getRgb())
                # Observe the final composited colour over a white desktop too.
                before = original.pixelColor(x, y)
                for old_channel, new_channel in zip(before.getRgb()[:3], pixel.getRgb()[:3]):
                    expected = old_channel * before.alphaF() + 255 * (1 - before.alphaF())
                    actual = new_channel * pixel.alphaF() + 255 * (1 - pixel.alphaF())
                    assert abs(actual - expected) <= 2, (percent, before.getRgb(), pixel.getRgb())
        transition.settle()
        assert transition._opacity_layer.isNull()
    finally:
        dispose(root)


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


@pytest.fixture
def surface(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
    root = QWidget()
    root.resize(320, 180)
    root.setStyleSheet("background: #292630;")
    button = QPushButton("Before", root)
    button.setGeometry(20, 30, 120, 35)
    transition = ContentTransition(root)
    root.show()
    root.activateWindow()
    button.setFocus()
    APP.processEvents()
    yield root, button, transition
    dispose(root)


def test_transition_keeps_live_input_focus_geometry_and_one_bounded_frame(surface):
    root, button, transition = surface
    geometry = button.geometry()
    focused = APP.focusWidget()
    transition.capture()
    button.setText("After")
    transition.start()
    assert transition._frame.deviceIndependentSize() == root.size().toSizeF()
    assert root.childAt(button.geometry().center()) is button
    assert APP.focusWidget() is focused
    clicked = QSignalSpy(button.clicked)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert clicked.count() == 1
    assert button.text() == "After"
    for fraction in (0.15, 0.4, 0.7):
        transition._animation.setCurrentTime(round(transition._animation.duration() * fraction))
        assert button.geometry() == geometry
        assert root.graphicsEffect() is None
        assert 0 < transition._alpha < 1
    transition._animation.setCurrentTime(transition._animation.duration())
    assert transition.isHidden()
    assert transition._frame.isNull()
    assert transition._opacity_layer.isNull()


def test_rapid_navigation_reuses_current_frame_without_animation_queue(surface):
    root, button, transition = surface
    for i in range(25):
        transition.capture()
        button.setText(str(i))
        transition.start()
        transition._animation.setCurrentTime(35)
        before = root.grab().toImage()
        transition.capture()
        after = root.grab().toImage()
        assert before == after
    assert len(transition.findChildren(QVariantAnimation)) == 1
    transition.start()
    QTest.qWait(MOTION.navigation_ms + 100)
    assert transition._frame.isNull()
    assert transition.isHidden()


@pytest.mark.parametrize("event", ["hide", "resize"])
def test_hidden_or_resized_surface_releases_motion_and_old_pixels(surface, event):
    root, _, transition = surface
    transition.capture()
    transition.start()
    if event == "hide":
        root.hide()
    else:
        root.resize(340, 200)
    assert transition._animation.state() == QVariantAnimation.State.Stopped
    assert transition._frame.isNull()


def test_reduced_motion_does_not_capture_any_frame(surface, monkeypatch):
    root, _, transition = surface
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    monkeypatch.setattr(root, "grab", lambda *_args: pytest.fail("unexpected capture"))
    transition.capture()
    transition.start()
    assert transition.isHidden()
    assert transition._frame.isNull()


def test_destroying_surface_during_transition_has_no_late_callbacks(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    root = QWidget()
    root.show()
    transition = ContentTransition(root)
    transition.capture()
    transition.start()
    dispose(root)
    assert errors == []


def test_translucent_surface_starts_with_exact_old_frame_not_double_text(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
    root = QWidget()
    root.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    root.setStyleSheet("background: rgba(25, 20, 32, 160);")
    root.resize(300, 150)
    button = QPushButton("Before", root)
    button.setGeometry(10, 10, 130, 35)
    transition = ContentTransition(root)
    root.show()
    APP.processEvents()
    try:
        before = root.grab().toImage()
        transition.capture()
        button.setText("After")
        transition.start()
        assert root.grab().toImage() == before
    finally:
        dispose(root)


def test_existing_fade_restarts_from_visible_opacity(surface):
    root, button, _ = surface
    motion = MotionController(root, reduced_motion=lambda: False)
    motion.fade_in(button)
    motion._states[id(button)][2].setCurrentTime(50)
    before = button.graphicsEffect().opacity()
    motion.fade_in(button)
    assert button.graphicsEffect().opacity() == pytest.approx(before)
    motion.settle()


@pytest.fixture
def popup(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
    # Tests only use synthetic conversations and the already cached model list.
    monkeypatch.setattr(main_popup._SettingsPanel, "_refresh_visible_probes", lambda self: None)
    monkeypatch.setattr(main_popup._SettingsPanel, "_probe_chatgpt_account", lambda self, **kw: None)
    monkeypatch.setattr(main_popup._SettingsPanel, "_refresh_startup_status", lambda self, **kw: None)
    monkeypatch.setattr(main_popup.MainPopup, "_request_local_models_refresh", lambda self: None)
    monkeypatch.setattr(main_popup._ModelPanel, "_request_visible_refreshes", lambda self: None)
    monkeypatch.setattr(main_popup.conversations, "conversation_snapshot", lambda: {
        "snapshot_revision": 1, "items": [{"id": "sample", "title": "Sample"}], "folders": [],
    })
    widget = main_popup.MainPopup()
    widget.show()
    widget.activateWindow()
    APP.processEvents()
    yield widget
    dispose(widget)


def click_nav(popup, index):
    QTest.mouseClick(popup._nav_buttons[index], Qt.MouseButton.LeftButton)
    APP.processEvents()


def test_chat_open_is_immediate_during_page_crossfade(popup):
    popup.open_conversation.connect(lambda _id: popup.set_messages([
        {"role": "user", "content": "Saved message"},
    ]))
    click_nav(popup, popup.PAGE_CONVS)
    opened = QSignalSpy(popup.open_conversation)
    QTest.mouseClick(popup._convs._row_widgets["sample"], Qt.MouseButton.LeftButton)
    assert opened.count() == 1
    assert popup._stack.currentWidget() is popup._chat
    assert [b.text() for b in popup._chat.findChildren(main_popup._Bubble)] == ["Saved message"]
    assert popup._page_transition.isVisible()
    assert popup._chat.graphicsEffect() is None
    assert popup.width() == main_popup.POPUP_W


def test_first_settings_click_during_page_transition_stays_usable(popup):
    click_nav(popup, popup.PAGE_SETTINGS)
    assert popup.width() == main_popup.POPUP_W_WIDE
    panel = popup._settings
    nav = panel._settings_nav
    index = 1
    position = nav.visualItemRect(nav.item(index)).center()
    global_position = nav.viewport().mapTo(popup, position)
    assert popup.childAt(global_position) is nav.viewport()
    QTest.mouseClick(nav.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert panel._settings_stack.currentIndex() == index
    APP.processEvents()
    assert panel._detail_transition.isVisible()
    assert panel._settings_scroll.verticalScrollBar().value() == 0
    for _ in range(10):
        panel.show_category("settings.cat.general")
        panel.show_category("settings.cat.models")
    assert popup.width() == main_popup.POPUP_W_WIDE
    assert panel._settings_stack.currentWidget().graphicsEffect() is None
    popup.hide()
    assert popup._page_transition._frame.isNull()
    assert panel._detail_transition._frame.isNull()


def test_rapid_wide_compact_navigation_never_moves_window_during_animation(popup):
    for index in (
        popup.PAGE_SETTINGS, popup.PAGE_MODELS,
        popup.PAGE_SETTINGS, popup.PAGE_CONVS, popup.PAGE_SETTINGS,
    ):
        click_nav(popup, index)
        expected = main_popup.POPUP_W_WIDE if index == popup.PAGE_SETTINGS else main_popup.POPUP_W
        assert popup.width() == expected
        geometry = popup.geometry()
        popup._page_transition._animation.setCurrentTime(40)
        APP.processEvents()
        assert popup.geometry() == geometry
        assert popup.height() == main_popup.POPUP_H
    popup.show_chat()
    APP.processEvents()
    assert popup.width() == main_popup.POPUP_W


def test_provider_navigation_and_back_animate_without_changing_selected_model(popup):
    panel = popup._models
    panel.set_inventory([{
        "model_id": "example", "model_display_name": "Example", "provider_id": "openai",
        "available": True,
    }])
    panel.set_current("example", "openai")
    changed = QSignalSpy(panel.model_changed)
    click_nav(popup, popup.PAGE_MODELS)
    card = next(card for card in panel.findChildren(main_popup._ProviderGridCard)
                if card.isVisible() and card._provider == "openai")
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    assert panel._view == "openai"
    assert panel._content_transition.isVisible()
    assert changed.count() == 0
    QTest.mouseClick(panel._back_btn, Qt.MouseButton.LeftButton)
    assert panel._view is None
    assert panel._content_transition.isVisible()
    assert panel._current_model == "example"
    assert changed.count() == 0


def test_provider_settings_crossfade_keeps_new_configuration_interactive(popup):
    popup.show_settings_category("settings.cat.models")
    APP.processEvents()
    panel = popup._settings
    combo = panel._ai_provider_combo
    combo.setCurrentIndex(combo.findData("openai"))
    APP.processEvents()
    assert panel._provider_cards["openai"].isVisible()
    assert panel._detail_transition.isVisible()
    assert not panel._chatgpt_card.isVisible()
    combo.setCurrentIndex(combo.findData("anthropic"))
    assert panel._provider_cards["anthropic"].isVisible()
    assert not panel._provider_cards["openai"].isVisible()


def test_switch_updates_immediately_and_reverses_from_current_position(surface):
    root, _, _ = surface
    switch = OsiaSwitch(parent=root, reduced_motion=lambda: False)
    switch.move(200, 70)
    switch.show()
    seen = QSignalSpy(switch.toggled)
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
    assert switch.isChecked()
    assert seen.count() == 1
    switch._animation.setCurrentTime(45)
    before = switch.thumb_position
    assert 0 < before < 1
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
    assert not switch.isChecked()
    assert seen.count() == 2
    assert switch.thumb_position == pytest.approx(before)
    switch._animation.setCurrentTime(switch.ANIMATION_MS)
    assert switch.thumb_position == 0
