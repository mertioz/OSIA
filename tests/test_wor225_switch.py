"""Focused WOR-225 switch behavior and PC integration contracts."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QMessageBox, QWidget

from osia.gui.main_popup import _OsiaPanel
from osia.gui.switch import OsiaSwitch
from osia.gui.theme import PALETTES, ThemeManager
from osia.gui.update_ui import UpdateSettingsSection


ROOT = Path(__file__).resolve().parents[1]


class _FakeUpdateController(QObject):
    state_changed = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.preview_opt_in = False
        self.preview_writes: list[bool] = []
        self.state = {
            "status": "idle",
            "installedVersion": "1.0.0",
            "previewOptIn": False,
        }

    def set_preview_opt_in(self, enabled: bool) -> None:
        self.preview_opt_in = bool(enabled)
        self.preview_writes.append(self.preview_opt_in)

    def check(self, *, manual: bool) -> None:
        del manual

    def install(self, _parent) -> None:
        pass


def _manager(theme: str = "osia") -> ThemeManager:
    manager = ThemeManager(lambda: {"ui_theme": theme}, lambda *_args: None)
    assert manager.key == theme
    return manager


def _light_manager() -> ThemeManager:
    manager = _manager()
    manager._palette = replace(
        PALETTES["osia"],
        key="test-light",
        background="#f7f5fb",
        surface="#ffffff",
        elevated_surface="#eeeaf5",
        glass="#f1edf8",
        primary="#6542c7",
        secondary="#8066c9",
        text="#17131f",
        muted_text="#625d6d",
        border="#bbb4c8",
        disabled="#77717f",
        user_bubble="#e9e2f8",
        assistant_bubble="#ffffff",
    )
    return manager


class TestCanonicalSwitch:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _switch(self, **kwargs) -> OsiaSwitch:
        switch = OsiaSwitch(theme_manager=_manager(), **kwargs)
        switch.resize(switch.sizeHint())
        return switch

    def test_off_and_on_initial_positions_are_immediate(self):
        off = self._switch(reduced_motion=lambda: False)
        assert off.isChecked() is False
        assert off.thumb_position == 0.0
        assert off.active_animation_count == 0

        on = self._switch(reduced_motion=lambda: False)
        on.setChecked(True)
        assert on.isChecked() is True
        assert on.thumb_position == 1.0
        assert on.active_animation_count == 0
        on.show()
        self.app.processEvents()
        assert on.thumb_position == 1.0
        assert on.active_animation_count == 0

    def test_click_toggles_and_emits_each_logical_signal_once(self):
        switch = self._switch(reduced_motion=lambda: True)
        switch.show()
        self.app.processEvents()
        toggled = QSignalSpy(switch.toggled)
        changed = QSignalSpy(switch.stateChanged)
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
        assert switch.isChecked() is True
        assert len(toggled) == 1
        assert len(changed) == 1
        assert switch.thumb_position == 1.0

    def test_text_and_track_share_one_comfortable_click_target(self):
        switch = OsiaSwitch(
            "Automatically run commands",
            reduced_motion=lambda: True,
            theme_manager=_manager(),
        )
        switch.resize(switch.sizeHint())
        switch.show()
        self.app.processEvents()
        toggled = QSignalSpy(switch.toggled)
        QTest.mouseClick(
            switch,
            Qt.MouseButton.LeftButton,
            pos=QPoint(switch.width() - 2, switch.height() // 2),
        )
        assert switch.isChecked() is True
        assert len(toggled) == 1

    def test_space_uses_the_native_checkbox_keyboard_contract(self):
        switch = self._switch(reduced_motion=lambda: True)
        switch.show()
        switch.setFocus()
        self.app.processEvents()
        toggled = QSignalSpy(switch.toggled)
        QTest.keyClick(switch, Qt.Key.Key_Space)
        assert switch.isChecked() is True
        assert len(toggled) == 1
        assert switch.hasFocus()

    def test_programmatic_updates_have_no_feedback_loop_and_blocked_updates_paint(self):
        switch = self._switch(reduced_motion=lambda: True)
        writes: list[bool] = []
        switch.toggled.connect(writes.append)
        switch.setChecked(True)
        switch.setChecked(True)
        assert writes == [True]
        assert switch.thumb_position == 1.0

        switch.blockSignals(True)
        switch.setChecked(False)
        switch.blockSignals(False)
        assert writes == [True]
        assert switch.isChecked() is False
        assert switch.thumb_position == 0.0

    def test_rapid_reversal_reuses_one_animation_and_settles_deterministically(self):
        switch = self._switch(reduced_motion=lambda: False)
        switch.show()
        self.app.processEvents()
        toggled = QSignalSpy(switch.toggled)
        for _index in range(25):
            QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
            assert switch.active_animation_count <= 1
        assert len(toggled) == 25
        assert switch.isChecked() is True
        assert len(switch.findChildren(QTimer)) == 0
        QTest.qWait(switch.ANIMATION_MS + 40)
        self.app.processEvents()
        assert switch.active_animation_count == 0
        assert switch.thumb_position == 1.0

    def test_disabled_state_rejects_mouse_and_keyboard_mutation(self):
        switch = self._switch(reduced_motion=lambda: True)
        switch.setEnabled(False)
        switch.show()
        switch.setFocus()
        toggled = QSignalSpy(switch.toggled)
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
        QTest.keyClick(switch, Qt.Key.Key_Space)
        assert switch.isChecked() is False
        assert len(toggled) == 0
        assert switch.thumb_position == 0.0

    def test_live_theme_and_light_palette_repaint_the_same_component(self):
        manager = _manager()
        switch = OsiaSwitch(
            theme_manager=manager,
            reduced_motion=lambda: True,
        )
        switch.setChecked(True)
        switch.resize(switch.sizeHint())
        switch.show()
        self.app.processEvents()
        before = switch.grab().toImage().pixelColor(5, switch.height() // 2)
        assert manager.set_theme("azure", persist=False)
        self.app.processEvents()
        after = switch.grab().toImage().pixelColor(5, switch.height() // 2)
        assert before != after

        light = OsiaSwitch(
            theme_manager=_light_manager(),
            reduced_motion=lambda: True,
        )
        light.setChecked(True)
        light.resize(light.sizeHint())
        light.show()
        self.app.processEvents()
        image = light.grab().toImage()
        assert image.pixelColor(5, light.height() // 2).isValid()
        assert light.thumb_position == 1.0

    def test_hover_and_focus_repaint_without_mutating_checked_state(self):
        switch = self._switch(reduced_motion=lambda: True)
        switch.setChecked(True)
        switch.show()
        self.app.processEvents()
        QApplication.sendEvent(switch, QEvent(QEvent.Type.Leave))
        switch.clearFocus()
        self.app.processEvents()
        baseline = switch.grab().toImage()
        QApplication.sendEvent(switch, QEvent(QEvent.Type.Enter))
        self.app.processEvents()
        hovered = switch.grab().toImage()
        assert hovered != baseline
        assert switch.isChecked() is True
        QApplication.sendEvent(switch, QEvent(QEvent.Type.Leave))
        switch.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        focused = switch.grab().toImage()
        assert focused != baseline
        assert switch.isChecked() is True

    def test_reduced_motion_is_immediate_and_has_no_animation_backlog(self):
        switch = self._switch(reduced_motion=lambda: True)
        switch.show()
        self.app.processEvents()
        for checked in (True, False, True, False, True):
            switch.setChecked(checked)
            assert switch.thumb_position == float(checked)
            assert switch.active_animation_count == 0
        assert len(switch.findChildren(QTimer)) == 0

    def test_widget_destruction_owns_and_deletes_the_running_animation(self):
        host = QWidget()
        switch = OsiaSwitch(
            parent=host,
            reduced_motion=lambda: False,
            theme_manager=_manager(),
        )
        host.show()
        self.app.processEvents()
        switch.setChecked(True)
        assert switch.active_animation_count == 1
        animation = switch._animation
        host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        assert sip.isdeleted(animation)

    def test_checked_state_and_accessibility_metadata_remain_native_qt_properties(self):
        switch = self._switch(reduced_motion=lambda: True)
        assert isinstance(switch, QCheckBox)
        switch.setAccessibleName("Automatic commands")
        switch.setAccessibleDescription("Allow commands without confirmation")
        switch.setChecked(True)
        checked_property = switch.metaObject().property(
            switch.metaObject().indexOfProperty("checked")
        )
        assert checked_property.read(switch) is True
        assert switch.accessibleName() == "Automatic commands"
        assert switch.accessibleDescription() == "Allow commands without confirmation"


class TestSensitiveIntegrationContracts:
    def test_skills_switch_calls_the_existing_persistence_callback_once(self):
        switch = OsiaSwitch(reduced_motion=lambda: True, theme_manager=_manager())
        switch.setChecked(True)
        with patch(
            "osia.gui.main_popup.personality.set_skill_enabled",
            return_value=True,
        ) as persist:
            _OsiaPanel._on_skill_toggle("example.md", switch)
        persist.assert_called_once_with("example.md", True)
        assert switch.isChecked() is True

    def test_stable_preview_refresh_does_not_write_back_and_user_toggle_writes_once(self):
        controller = _FakeUpdateController()
        section = UpdateSettingsSection(controller)
        section.apply_state({
            "status": "idle",
            "installedVersion": "1.0.0",
            "previewOptIn": False,
        })
        assert controller.preview_writes == []
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            section.preview_toggle.setChecked(True)
        assert controller.preview_writes == [True]
        assert section.preview_toggle.isChecked() is True
        section.deleteLater()

    def test_provider_and_voice_binary_settings_keep_their_existing_callbacks(self):
        source = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
        assert "self._hier_enabled_toggle = OsiaSwitch()" in source
        assert 'set_setting("ai_hierarchy_enabled"' in source
        assert "self._wake_word_toggle = OsiaSwitch()" in source
        assert "self._wake_enabled_toggle = OsiaSwitch()" in source
        assert 'set_setting("wake_greeting_enabled", enabled)' in source
        assert "self._tts_provider_combo = QComboBox()" in source


    def test_scheduled_tasks_and_dynamic_controls_use_the_canonical_switch(self):
        source = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
        assert "enable = OsiaSwitch()" in source
        assert 'self._on_cron_field(i, "enabled", c.isChecked())' in source
        assert "toggle = OsiaSwitch()" in source
        assert "self._hb_check_toggles[name] = toggle" in source
        assert "self._build_skill_row(name)" in source


    def test_source_and_frozen_package_discover_the_same_switch_module(self):
        spec = (ROOT / "packaging" / "windows" / "osia.spec").read_text("utf-8")
        assert 'collect_submodules(\n    "osia"' in spec
        for module in ("main_popup.py", "update_ui.py"):
            source = (ROOT / "osia" / "gui" / module).read_text("utf-8")
            assert "from osia.gui.switch import OsiaSwitch" in source
