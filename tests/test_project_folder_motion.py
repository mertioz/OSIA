"""Project transitions keep canonical rows and interrupted motion consistent."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from osia.gui import main_popup


APP = QApplication.instance() or QApplication([])


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
    snapshot = {
        "snapshot_revision": 1,
        "folders": [
            {"id": "stage", "name": "Stage", "collapsed": True},
            {"id": "next", "name": "Other project", "collapsed": True},
        ],
        "items": [
            {"id": str(index), "title": f"Chat {index}", "folder_id": "stage"}
            for index in range(8)
        ],
    }
    monkeypatch.setattr(
        main_popup.conversations, "conversation_snapshot", lambda: snapshot,
    )
    panel = main_popup._ConversationsPanel()
    panel._poll_timer.stop()
    panel.resize(440, 560)
    panel.refresh(snapshot=snapshot)
    panel.show()
    APP.processEvents()

    def update(request):
        snapshot["folders"][0]["collapsed"] = request["collapsed"]
        snapshot["snapshot_revision"] += 1

    panel.folder_mutation_requested.connect(update)
    yield panel
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


def _toggle(panel):
    QTest.mouseClick(
        panel._folder_widgets["stage"]._toggle, Qt.MouseButton.LeftButton,
    )
    APP.processEvents()
    return panel._folder_contents["stage"]


def _advance(contents, fraction):
    animation = contents._animation
    animation.setCurrentTime(round(animation.duration() * fraction))
    # Deliver layout requests through the section and the scroll-area host.
    for _ in range(4):
        APP.processEvents()


def test_open_and_close_reveal_rows_and_move_following_project_smoothly(panel):
    following = panel._folder_widgets["next"].parentWidget()
    initial_y = following.y()
    opened = _toggle(panel)
    assert opened._animation.state() == opened._animation.State.Running
    full_height = opened.layout().sizeHint().height()
    _advance(opened, 0.5)
    assert 0 < opened.height() < full_height
    assert all(
        row.height() == row.sizeHint().height()
        for row in panel._row_widgets.values()
    )
    following = panel._folder_widgets["next"].parentWidget()
    assert initial_y < following.y() < initial_y + full_height
    assert panel.visible_conversation_ids() == [str(index) for index in range(8)]
    _advance(opened, 1)
    assert opened.height() == full_height

    closed = _toggle(panel)
    assert not closed.isEnabled()
    assert panel.visible_conversation_ids() == []
    _advance(closed, 0.5)
    assert 0 < closed.height() < full_height
    _advance(closed, 1)
    assert closed.isHidden()
    assert panel._folder_widgets["next"].parentWidget().y() == initial_y


def test_rapid_reversal_and_snapshot_refresh_continue_from_visible_height(panel):
    contents = _toggle(panel)
    _advance(contents, 0.4)
    midpoint = contents.height()
    contents = _toggle(panel)
    assert abs(contents.maximumHeight() - midpoint) <= 1
    _advance(contents, 0.3)
    midpoint = contents.height()
    contents = _toggle(panel)
    assert abs(contents.maximumHeight() - midpoint) <= 1
    _advance(contents, 0.4)
    midpoint = contents.height()
    snapshot = panel.conversation_snapshot()
    snapshot["items"][0]["title"] = "Renamed during expansion"
    snapshot["snapshot_revision"] += 1
    panel.refresh(snapshot=snapshot)
    contents = panel._folder_contents["stage"]
    assert abs(contents.maximumHeight() - midpoint) <= 1
    _advance(contents, 1)
    assert not contents.isHidden()
    assert panel._row_widgets["0"]._full_title == "Renamed during expansion"


def test_nested_row_delete_and_rollback_preserve_project_layout(panel):
    contents = _toggle(panel)
    _advance(contents, 1)
    snapshot = panel.conversation_snapshot()
    full_height = contents.height()
    panel.set_optimistic_hidden({"0"}, True)
    for _ in range(4):
        APP.processEvents()
    assert contents._rows_layout.count() == 7
    assert contents.height() < full_height
    assert panel.visible_conversation_ids() == [str(index) for index in range(1, 8)]
    assert panel._folder_widgets["stage"]._count_label.text() == "7"
    panel.set_optimistic_hidden({"0"}, False, snapshot=snapshot)
    assert panel._folder_contents["stage"]._rows_layout.count() == 8
    assert panel.visible_conversation_ids() == [str(index) for index in range(8)]


def test_project_removed_mid_animation_cannot_leave_rows_or_callbacks(panel):
    contents = _toggle(panel)
    _advance(contents, 0.4)
    snapshot = panel.conversation_snapshot()
    snapshot["folders"] = snapshot["folders"][1:]
    snapshot["items"] = []
    snapshot["snapshot_revision"] += 1
    panel.refresh(snapshot=snapshot)
    assert contents._animation.state() == contents._animation.State.Stopped
    assert list(panel._folder_widgets) == ["next"]
    assert panel.visible_conversation_ids() == []


def test_reduced_motion_settles_immediately(panel, monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    contents = _toggle(panel)
    assert contents._animation.state() == contents._animation.State.Stopped
    assert not contents.isHidden()
    assert contents.height() == contents.layout().sizeHint().height()
    contents = _toggle(panel)
    assert contents.isHidden()


def test_unacknowledged_toggle_keeps_canonical_project_state(panel):
    panel.folder_mutation_requested.disconnect()
    contents = _toggle(panel)
    assert contents.isHidden()
    assert panel._folder_widgets["stage"]._collapsed
    assert panel.visible_conversation_ids() == []
