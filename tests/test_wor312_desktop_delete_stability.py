"""Qt regressions for rapid Desktop conversation deletion."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from osia import main
from osia.gui.main_popup import MainPopup, _ChatPage, _ConversationsPanel


APP = QApplication.instance() or QApplication([])


def _snapshot(count: int, revision: int = 1) -> dict:
    return {
        "snapshot_revision": revision,
        "folders": [],
        "items": [
            {
                "id": f"conv-{index}",
                "title": f"Chat {index}",
                "message_count": 0,
                "updated_at": "",
                "folder_id": None,
            }
            for index in range(count)
        ],
    }


def _drain() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


def test_rapid_single_and_duplicate_deletes_do_not_rebuild_or_leave_live_rows():
    panel = _ConversationsPanel()
    panel.resize(440, 560)
    panel.show()
    panel.refresh(snapshot=_snapshot(12))
    APP.processEvents()
    first = panel._row_widgets["conv-0"]
    stale_button = first.findChild(QPushButton, "convActionDel")
    emitted = QSignalSpy(panel.delete_conversation)

    with patch.object(panel, "refresh", wraps=panel.refresh) as refresh:
        for index in range(12):
            conv_id = f"conv-{index}"
            panel.set_optimistic_hidden({conv_id}, True)
            panel.set_optimistic_hidden({conv_id}, True)  # duplicate click
            if index == 0:
                stale_button.click()
                assert len(emitted) == 0
            panel.set_optimistic_hidden({conv_id}, False, revision=index + 2)
            if index + 1 in (1, 5, 12):
                assert len(panel._row_widgets) == 12 - (index + 1)
        assert refresh.call_count == 0

    assert len(emitted) == 0
    assert not first.isVisible()
    assert panel._row_widgets == {}
    assert panel.findChild(QLabel, "panelEmpty").parent() is panel._host
    _drain()
    assert sip.isdeleted(first)
    panel.close()
    panel.deleteLater()
    _drain()


def test_selected_fallback_uses_current_visible_order():
    items = _snapshot(5)["items"]
    assert main._deletion_fallback_ids(items, "conv-1", {"conv-1"}) == [
        "conv-2", "conv-3", "conv-4", "conv-0",
    ]
    assert main._deletion_fallback_ids(
        items, "conv-1", {"conv-1", "conv-2", "conv-3"},
    ) == ["conv-4", "conv-0"]
    assert main._deletion_fallback_ids(
        items, "conv-4", {"conv-4"},
    ) == ["conv-3", "conv-2", "conv-1", "conv-0"]
    assert main._deletion_fallback_ids(items, "conv-0", {
        item["id"] for item in items
    }) == []


def test_selection_updates_only_old_and_new_rows_without_scanning_list():
    panel = _ConversationsPanel()
    panel.refresh(snapshot=_snapshot(150))
    panel.set_active("conv-10")

    class _NoFullScanDict(dict):
        def items(self):
            raise AssertionError("selection must not scan every conversation row")

    panel._row_widgets = _NoFullScanDict(panel._row_widgets)
    panel.set_active("conv-11")

    assert not panel._row_widgets["conv-10"].property("active")
    assert panel._row_widgets["conv-11"].property("active")
    panel.deleteLater()
    _drain()


def test_stale_snapshot_cannot_restore_acknowledged_delete():
    panel = _ConversationsPanel()
    old = _snapshot(2, revision=4)
    panel.refresh(snapshot=old)
    panel.set_optimistic_hidden({"conv-0"}, True)
    panel.set_optimistic_hidden({"conv-0"}, False, revision=5)
    panel.refresh(snapshot=old)
    assert list(panel._row_widgets) == ["conv-1"]
    assert panel.conversation_snapshot()["snapshot_revision"] == 5
    panel.deleteLater()
    _drain()


def test_refresh_during_adjacent_selected_deletes_keeps_valid_selection():
    panel = _ConversationsPanel()
    old = _snapshot(4, revision=8)
    panel.refresh(snapshot=old)
    panel.set_active("conv-1")
    panel.set_optimistic_hidden({"conv-1"}, True)
    panel.set_active("conv-2")
    panel.refresh(snapshot=old)  # list refresh while the first ack is pending
    assert "conv-1" not in panel._row_widgets
    assert panel._row_widgets["conv-2"].property("active")
    panel.set_optimistic_hidden({"conv-2"}, True)
    panel.set_active("conv-3")
    panel.set_optimistic_hidden({"conv-2"}, False, revision=10)
    panel.set_optimistic_hidden({"conv-1"}, False, revision=9)
    panel.refresh(snapshot=old)  # stale list after out-of-order acks
    assert list(panel._row_widgets) == ["conv-0", "conv-3"]
    assert panel._active_id == "conv-3"
    assert panel._row_widgets["conv-3"].property("active")
    panel.deleteLater()
    _drain()


def test_folder_count_updates_with_immediate_row_removal():
    panel = _ConversationsPanel()
    snapshot = _snapshot(2)
    snapshot["folders"] = [{"id": "folder", "name": "Project", "order": 0}]
    for item in snapshot["items"]:
        item["folder_id"] = "folder"
    panel.refresh(snapshot=snapshot)
    count = panel._folder_widgets["folder"]._count_label
    assert count.text() == "2"
    panel.set_optimistic_hidden({"conv-0"}, True)
    assert count.text() == "1"
    panel.set_optimistic_hidden({"conv-0"}, False, revision=2)
    assert count.text() == "1"
    panel.deleteLater()
    _drain()


def test_last_delete_keeps_empty_state_inside_existing_chat_window():
    with (
        patch("osia.gui.main_popup._SettingsPanel._probe_chatgpt_account"),
        patch("osia.gui.main_popup._SettingsPanel._refresh_startup_status"),
        patch(
            "osia.gui.main_popup.conversations.conversation_snapshot",
            return_value=_snapshot(1),
        ),
    ):
        popup = MainPopup()
        popup._stack.setCurrentIndex(popup.PAGE_CONVS)
        popup.show()
        popup._convs.refresh(snapshot=_snapshot(1))
        APP.processEvents()
        before = set(APP.topLevelWidgets())
        popup.show_conversation_delete_busy(count=2, bulk=True)
        assert popup._convs._delete_notice.isVisible()
        assert "2" in popup._convs._delete_notice.text()
        row = popup._convs._row_widgets["conv-0"]
        button = row.findChild(QPushButton, "convActionDel")
        clicked = QSignalSpy(popup.delete_conversation)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert len(clicked) == 1
        popup.set_optimistic_conversation_deletions({"conv-0"}, True)
        popup.refresh_conversations(None)
        popup.set_messages([])
        APP.processEvents()
        after = set(APP.topLevelWidgets())
        assert after == before
        assert popup._convs.findChild(QLabel, "panelEmpty").window() is popup
        assert popup._chat._empty.window() is popup
        assert popup._chat._empty.parent() is popup._chat._messages_host
        assert popup._stack.currentIndex() == popup.PAGE_CONVS
    popup.close()
    popup.deleteLater()
    _drain()


def test_rebuilt_chat_empty_label_is_parented_before_show():
    class ShowProbe(QObject):
        def __init__(self):
            super().__init__()
            self.orphan_shows = []

        def eventFilter(self, watched, event):
            if (
                event.type() == QEvent.Type.Show
                and isinstance(watched, QLabel)
                and watched.objectName() == "chatEmpty"
                and watched.parent() is None
            ):
                self.orphan_shows.append(watched)
            return False

    probe = ShowProbe()
    APP.installEventFilter(probe)
    page = _ChatPage()
    page.show()
    page._empty.deleteLater()
    _drain()
    page.set_messages([])
    APP.processEvents()
    assert probe.orphan_shows == []
    assert page._empty.parent() is page._messages_host
    assert set(APP.topLevelWidgets()) >= {page}
    page.close()
    page.deleteLater()
    _drain()
    APP.removeEventFilter(probe)
