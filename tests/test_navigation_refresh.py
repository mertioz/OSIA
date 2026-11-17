"""Reuse unchanged UI without hiding canonical or presentation changes."""
from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QScrollArea

from osia.gui import main_popup as ui


APP = QApplication.instance() or QApplication([])


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "0")
    snapshot = {
        "snapshot_revision": 1,
        "folders": [{"id": "project", "name": "Project", "collapsed": True}],
        "items": [{
            "id": str(i), "title": f"Conversation {i}", "message_count": i + 1,
            "updated_at": "2026-11-17T14:30:00", "folder_id": "",
        } for i in range(40)],
    }
    monkeypatch.setattr(ui.conversations, "conversation_snapshot", lambda: deepcopy(snapshot))
    widget = ui._ConversationsPanel()
    widget._poll_timer.stop()
    widget.resize(440, 560)
    widget.refresh(snapshot=deepcopy(snapshot))
    widget.show()
    widget.activateWindow()
    APP.processEvents()
    yield widget, snapshot
    dispose(widget)


def test_duplicate_snapshot_keeps_rows_scroll_focus_and_selected_chat(panel):
    widget, snapshot = panel
    widget.set_active("35")
    row = widget._row_widgets["35"]
    row.setFocus()
    bar = widget._scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    focused, scroll = APP.focusWidget(), bar.value()
    rows = dict(widget._row_widgets)
    for _ in range(8):
        widget.refresh(snapshot=deepcopy(snapshot))
    assert widget._row_widgets == rows
    assert APP.focusWidget() is focused
    assert bar.value() == scroll
    assert row.property("active") is True
    assert widget.conversation_snapshot() == snapshot


def test_reused_rows_still_receive_new_nonvisual_snapshot_metadata(panel):
    widget, snapshot = panel
    before = widget._row_widgets["0"]
    snapshot["items"][0]["control_disabled"] = True
    widget.refresh(snapshot=snapshot)
    assert widget._row_widgets["0"] is before
    assert widget.conversation_snapshot()["items"][0]["control_disabled"] is True


@pytest.mark.parametrize("field,value", [
    ("title", "Renamed"), ("message_count", 72),
    ("updated_at", "2026-11-18T15:30:00"), ("folder_id", "project"),
])
def test_visible_metadata_changes_are_not_hidden_by_equal_revision(panel, field, value):
    widget, snapshot = panel
    row = widget._row_widgets["0"]
    snapshot["items"][0][field] = value
    widget._poll_canonical_store()
    assert widget.conversation_snapshot()["items"][0][field] == value
    if field == "folder_id":
        assert "0" not in widget.visible_conversation_ids()
    else:
        assert widget._row_widgets["0"] is not row
        if field == "title":
            assert widget._row_widgets["0"]._full_title == value
        if field == "message_count":
            assert "72" in widget._row_widgets["0"]._metadata


def test_local_delete_and_failed_delete_restore_stay_visible(panel):
    widget, snapshot = panel
    widget.set_active("3")
    widget.set_optimistic_hidden({"0"}, True)
    widget.refresh(snapshot=deepcopy(snapshot))
    assert "0" not in widget.visible_conversation_ids()
    widget.set_optimistic_hidden({"0"}, False, snapshot=deepcopy(snapshot))
    widget.refresh(snapshot=deepcopy(snapshot))
    assert widget.visible_conversation_ids() == [str(i) for i in range(40)]
    assert widget._row_widgets["3"].property("active") is True


def test_stale_snapshot_does_not_undo_a_newer_rename(panel):
    widget, snapshot = panel
    old = deepcopy(snapshot)
    snapshot["snapshot_revision"] = 2
    snapshot["items"][0]["title"] = "Current title"
    widget.refresh(snapshot=snapshot)
    current = widget._row_widgets["0"]
    widget.refresh(snapshot=old)
    assert widget._row_widgets["0"] is current
    assert current._full_title == "Current title"


def test_retranslation_and_clock_format_invalidate_the_presentation(panel, monkeypatch):
    widget, snapshot = panel
    before = widget._row_widgets["0"]
    monkeypatch.setattr(ui.timefmt, "use_12h", lambda: True)
    widget.refresh(snapshot=snapshot)
    changed = widget._row_widgets["0"]
    assert changed is not before
    assert "PM" in changed._metadata
    widget._retranslate()
    assert widget._row_widgets["0"] is not changed


def test_repeated_snapshot_does_not_restart_project_expansion(panel):
    widget, snapshot = panel
    snapshot["items"][0]["folder_id"] = "project"
    widget.refresh(snapshot=snapshot)

    def acknowledge(request):
        snapshot["folders"][0]["collapsed"] = request["collapsed"]
        snapshot["snapshot_revision"] += 1

    widget.folder_mutation_requested.connect(acknowledge)
    QTest.mouseClick(widget._folder_widgets["project"], Qt.MouseButton.LeftButton)
    APP.processEvents()
    contents = widget._folder_contents["project"]
    contents._animation.setCurrentTime(50)
    current_time = contents._animation.currentTime()
    widget.refresh(snapshot=deepcopy(snapshot))
    assert widget._folder_contents["project"] is contents
    assert contents._animation.currentTime() == current_time
    assert "0" in widget.visible_conversation_ids()


@pytest.fixture
def models():
    widget = ui._ModelPanel()
    widget.resize(440, 560)
    widget.show()
    widget._account_refresh_timer.stop()
    APP.processEvents()
    yield widget
    dispose(widget)


def inventory():
    return [{
        "model_id": f"example-{i}", "model_display_name": f"Example {i}",
        "provider_id": "openrouter", "available": True,
        "reasoning_options": [], "capabilities": {"vision": True, "tool_calls": True},
    } for i in range(30)]


def test_unchanged_catalog_preserves_search_pagination_rows_and_selection(models):
    data = inventory()
    models.set_inventory(data)
    models.set_current("example-1", "openrouter")
    models._show_provider("openrouter")
    models._model_search.setText("Example 1")
    models._model_search.setFocus()
    APP.processEvents()
    rows = models.findChildren(ui._ModelRow)
    scroll = models.findChild(QScrollArea).verticalScrollBar()
    scroll.setValue(scroll.maximum())
    position, focused = scroll.value(), APP.focusWidget()
    chosen = QSignalSpy(models.model_changed)
    models.set_inventory(deepcopy(data))
    models.set_current("example-1", "openrouter")
    assert models.findChildren(ui._ModelRow) == rows
    assert models._model_search.text() == "Example 1"
    assert models._current_model == "example-1"
    assert scroll.value() == position
    assert APP.focusWidget() is focused
    assert not chosen
    models.set_current("example-10", "openrouter")
    active = [models._host_layout.itemAt(i).widget() for i in range(models._host_layout.count() - 1)]
    assert [row._mid for row in active if isinstance(row, ui._ModelRow) and row.property("active")] == ["example-10"]


def test_changed_availability_and_nested_capabilities_refresh_models(models):
    data = inventory()
    models.set_inventory(data)
    models._show_provider("openrouter")
    data[0]["capabilities"]["vision"] = False
    data[0]["available"] = False
    assert models._inventory[0]["capabilities"]["vision"] is True
    models.set_inventory(data)
    row = models._host_layout.itemAt(0).widget()
    assert isinstance(row, ui._ModelRow) and row._mid == "example-0"
    assert not row.isEnabled()
    assert ui.i18n.t("models.openrouter_images") not in row.toolTip()
    models.set_inventory([])
    assert models._view is None


@pytest.mark.parametrize("refresh", ["inventory", "selection"])
def test_reasoning_preferences_refresh_even_if_catalog_is_unchanged(models, monkeypatch, refresh):
    state = {"default": "auto", "providers": {}, "models": {}}
    monkeypatch.setattr(ui, "get_reasoning_settings", lambda: deepcopy(state))
    models.set_inventory(inventory())
    models._show_provider("openrouter")
    old = models._host_layout.itemAt(0).widget()
    state["models"]["openrouter:example-0"] = "high"
    if refresh == "inventory":
        models.set_inventory(inventory())
    else:
        models.set_current(None, None)
    assert models._host_layout.itemAt(0).widget() is not old
    assert models._rendered_reasoning == state
