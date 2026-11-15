"""Native-input feedback is observational and never takes desktop ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil
import pytest
from PyQt6.QtCore import QCoreApplication, QRect, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

from osia import desktop_activity as activity
from osia.gui.desktop_feedback import ControlEdge, DesktopControlFeedback, cursor_file_bytes, cursor_image
from osia.gui.theme import ThemeManager, PALETTES
from osia.control_cursor import validate_cursor_bytes

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def activity_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "activity_directory", lambda: tmp_path)
    assert activity._depth == 0
    return tmp_path


def test_nested_and_concurrent_input_remains_active_until_last_return(activity_dir):
    started, release = threading.Event(), threading.Event()

    def worker():
        with activity.native_input_activity():
            started.set()
            release.wait(3)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(2)
        with activity.native_input_activity():
            with activity.native_input_activity():
                assert activity.desktop_activity_visible()
        assert activity.desktop_activity_visible(now=time.time() + 5)
    finally:
        release.set()
        thread.join(3)
    assert activity.desktop_activity_visible()  # brief visual hold, no input delay
    assert not activity.desktop_activity_visible(now=time.time() + 5)
    stored = json.loads(next(activity_dir.glob("*.json")).read_text())
    assert set(stored) == {"pid", "process_started", "active", "changed_at"}


def test_observer_errors_do_not_change_input_results_or_exceptions(activity_dir, monkeypatch):
    def broken():
        raise OSError("read-only profile")

    monkeypatch.setattr(activity, "activity_directory", broken)
    result = object()

    @activity.shows_native_input
    def operation():
        return result

    assert operation() is result

    @activity.shows_native_input
    def failing():
        raise ValueError("backend failure")

    with pytest.raises(ValueError, match="backend failure"):
        failing()
    assert activity._depth == 0
    assert not list(activity_dir.iterdir())


def test_stale_pid_reuse_corrupt_and_old_files_are_not_activity(activity_dir):
    for name, data in {
        "corrupt.json": "[1,2]",
        "reuse.json": json.dumps({"pid": os.getpid(), "process_started": 1, "active": True}),
        "old.json": json.dumps({"pid": os.getpid(), "process_started": psutil.Process().create_time(), "active": False, "changed_at": 1}),
        "gone.json": json.dumps({"pid": 2147483647, "process_started": 1, "active": True}),
    }.items():
        (activity_dir / name).write_text(data)
    assert not activity.desktop_activity_visible()


def test_server_process_activity_is_visible_and_disappears_when_process_exits(activity_dir):
    code = """
from pathlib import Path
import sys
from osia import desktop_activity as a
a.activity_directory = lambda: Path(sys.argv[1])
with a.native_input_activity():
    sys.stdin.buffer.read(1)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(activity_dir)], stdin=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 4
        while not list(activity_dir.glob("*.json")) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert activity.desktop_activity_visible()
    finally:
        child.communicate(b"x", timeout=5)
    assert child.returncode == 0
    assert not activity.desktop_activity_visible()  # even inside visual hold


def test_prepared_click_dispatch_observes_actual_backend_without_changing_result(activity_dir, monkeypatch):
    from osia.tools import desktop_control as desktop

    seen = []

    class Backend:
        def click(self, x, y, button):
            seen.append((x, y, button, activity.desktop_activity_visible()))

    monkeypatch.setattr(desktop, "_backend", lambda: "pyautogui")
    monkeypatch.setattr(desktop, "_pyautogui_ready_error", lambda: None)
    monkeypatch.setattr(desktop, "_load_pyautogui", lambda: Backend())
    prepared = desktop.prepare_click(-200, 400)
    assert not activity.desktop_activity_visible()
    assert desktop.dispatch_prepared_click(prepared) == "clicked left at (-200, 400)"
    assert seen == [(-200, 400, "left", True)]
    assert not activity.desktop_activity_visible(now=time.time() + 5)


def test_model_intent_survives_input_gaps_until_exact_turn_is_cancelled(activity_dir, tmp_path):
    from osia.turn_terminality import TurnTerminalityController
    activity.observe_desktop_intent("turn-first", "desktop_click")
    activity.observe_desktop_intent("turn-other", "desktop_press")
    assert activity.desktop_activity_visible(now=time.time() + 1000)
    controller = TurnTerminalityController(turn_id="turn-first", objective="a requested action", execution_required=True, storage_dir=tmp_path/"turns")
    controller.try_request_cancellation(effect_possible=False)
    assert activity.desktop_activity_visible()  # another turn still owns its phase
    activity.finish_desktop_intent("turn-other")
    assert not activity.desktop_activity_visible()
    assert not list(activity_dir.glob("intent-*.json"))


def test_read_only_tools_do_not_invent_a_desktop_intention(activity_dir):
    for tool in ("request_visual_observation", "inspect_desktop_surfaces", "read_file", "navigate"):
        activity.observe_desktop_intent("turn-read", tool)
    assert not activity.desktop_activity_visible()
    assert not list(activity_dir.iterdir())


def test_terminal_turn_cannot_be_reactivated_by_a_late_visual_notification(activity_dir):
    activity.finish_desktop_intent("turn-finished")
    activity.observe_desktop_intent("turn-finished", "desktop_click")
    assert not activity.desktop_activity_visible()
    activity.observe_desktop_intent("turn-new", "desktop_click")
    assert activity.desktop_activity_visible()
    activity.finish_desktop_intent("turn-new")


def test_tool_attempt_records_intention_before_dispatch_and_terminal_final_clears_it(activity_dir, tmp_path):
    from osia.tool_attempts import ToolAttemptStore
    from osia.turn_terminality import TurnTerminalityController
    store = ToolAttemptStore(tmp_path/"attempts")
    arguments = {"target": "an observed button"}
    attempt = store.create_attempt(conversation_id="conv-intent", turn_id="turn-intent", request_id="req-intent",
                                   tool_call_id="call-intent", tool_name="desktop_click", provider_id="fixture", model_id="fixture",
                                   turn_context={"language": "en", "source": "fixture"},
                                   argument_fingerprint=store.fingerprint_arguments(arguments), side_effect="external_mutation",
                                   idempotence="non_idempotent", retry_policy="never_after_dispatch")
    assert attempt["status"] == "not_started"
    assert activity.desktop_activity_visible(now=time.time()+1000)
    controller = TurnTerminalityController(turn_id="turn-intent", objective="an answer", execution_required=False,
                                          required_postconditions=("complete",), storage_dir=tmp_path/"turns")
    controller.prove_postcondition("complete", {"receipt": "verified"})
    decision = controller.evaluate()
    assert controller.reserve_final(decision.terminal_state, reason_code=decision.reason_code, final_text="complete")
    assert not activity.desktop_activity_visible()


def test_neon_fades_retargets_and_stops_rendering_when_idle(monkeypatch):
    from osia.gui import desktop_feedback as module
    monkeypatch.setattr(module, "reduced_motion_requested", lambda *_: False)
    theme = ThemeManager(lambda: {}, lambda *_: None)
    edge = ControlEdge(APP.primaryScreen(), theme)
    try:
        edge.set_active(True)
        assert edge.reveal == 0 and edge._frames.isActive()
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            APP.processEvents(); time.sleep(0.005)
        assert 0 < edge.reveal < 1
        current = edge.reveal
        edge.set_active(False)
        assert edge.reveal == current
        edge.set_active(True)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            APP.processEvents(); time.sleep(0.005)
        assert edge.reveal == 1 and edge.isVisible()
        edge.set_active(False)
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            APP.processEvents(); time.sleep(0.005)
        assert edge.reveal == 0 and not edge.isVisible() and not edge._frames.isActive()
    finally:
        edge.close()


def test_reduced_motion_has_static_neon_without_a_frame_loop(monkeypatch):
    from osia.gui import desktop_feedback as module
    monkeypatch.setattr(module, "reduced_motion_requested", lambda *_: True)
    edge = ControlEdge(APP.primaryScreen(), ThemeManager(lambda: {}, lambda *_: None))
    try:
        edge.set_active(True)
        assert edge.reveal == 1 and not edge._frames.isActive()
        edge.set_active(False)
        assert edge.reveal == 0 and not edge._frames.isActive()
    finally:
        edge.close()


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.closed = False

    def set_active(self, active, cursors=None):
        self.calls.append((active, cursors))

    def close(self):
        self.closed = True


def test_feedback_is_click_through_theme_aware_and_stops_after_close():
    theme = ThemeManager(lambda: {}, lambda *_: None)
    cursor = FakeCursor()
    feedback = DesktopControlFeedback(theme=theme, activity_reader=lambda: False, cursor_client=cursor)
    try:
        assert not feedback.edges and not cursor.calls
        feedback.set_active(True)
        assert len(feedback.edges) == len(APP.screens())
        for edge in feedback.edges.values():
            flags = edge.windowFlags()
            assert flags & Qt.WindowType.WindowTransparentForInput
            assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
            assert edge.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            assert edge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert edge.focusPolicy() == Qt.FocusPolicy.NoFocus
            assert edge.height() == ControlEdge.HEIGHT
            assert edge.geometry().bottom() == edge.screen_ref.geometry().bottom()
        old = cursor.calls[-1][1]
        other = next(key for key in PALETTES if key != theme.key)
        theme.set_theme(other, persist=False)
        APP.processEvents()
        assert cursor.calls[-1][1] != old
        for name in ("arrow", "hand"):
            import base64
            validate_cursor_bytes(base64.b64decode(cursor.calls[-1][1][name]))
        feedback.set_active(False)
        assert all(not edge.target_active for edge in feedback.edges.values())
        assert cursor.calls[-1] == (False, None)
        feedback.close()
        feedback.set_active(True)  # a late queued callback cannot resurrect it
        assert cursor.closed and not feedback.edges
    finally:
        feedback.close()
        APP.processEvents()


def test_activity_from_worker_reaches_gui_and_cancellation_removes_feedback(activity_dir):
    cursor = FakeCursor()
    theme = ThemeManager(lambda: {}, lambda *_: None)
    feedback = DesktopControlFeedback(theme=theme, cursor_client=cursor)
    try:
        with activity.native_input_activity():
            end = time.monotonic() + 2
            while not feedback.active and time.monotonic() < end:
                APP.processEvents()
                time.sleep(0.01)
            assert feedback.active
        end = time.monotonic() + 2
        while feedback.active and time.monotonic() < end:
            APP.processEvents()
            time.sleep(0.01)
        assert not feedback.active and cursor.calls[-1][0] is False
    finally:
        feedback.close()


def test_negative_origin_screen_and_resize():
    class Signal:
        def connect(self, _callback):
            pass

    class Screen:
        geometryChanged = Signal()
        geometry_value = QRect(-1920, -200, 1920, 1080)

        def geometry(self):
            return self.geometry_value

    theme = ThemeManager(lambda: {}, lambda *_: None)
    screen = Screen()
    edge = ControlEdge(screen, theme)
    try:
        assert edge.geometry() == QRect(-1920, 834, 1920, 46)
        screen.geometry_value = QRect(100, 0, 1280, 720)
        edge.sync_geometry()
        assert edge.geometry() == QRect(100, 674, 1280, 46)
    finally:
        edge.close()


@pytest.mark.parametrize("key", tuple(PALETTES))
def test_cursor_is_black_with_theme_outline_and_transparent_background(key):
    theme = ThemeManager(lambda: {"ui_theme": key}, lambda *_: None)
    image = cursor_image(theme.palette.color("primary"))
    assert image.pixelColor(31, 43).alpha() == 0
    assert image.pixelColor(6, 15) == QColor("black")
    assert any(image.pixelColor(x, y) == theme.palette.color("primary")
               for y in range(image.height()) for x in range(image.width()))
    validate_cursor_bytes(cursor_file_bytes(image, 3, 3))
