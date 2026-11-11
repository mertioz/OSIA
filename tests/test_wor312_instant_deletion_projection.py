from __future__ import annotations

import json
import os
import importlib.util
import urllib.request
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from osia import conversations
from osia.gui.main_popup import _ConvRow, _ConversationsPanel, _FolderRow


_APP = QApplication.instance() or QApplication([])


def _item(conv_id: str, *, folder_id: str | None = None) -> dict:
    return {
        "id": conv_id,
        "title": conv_id.upper(),
        "message_count": 1,
        "updated_at": "2026-10-03T10:00:00Z",
        "folder_id": folder_id,
    }


def _rows(panel: _ConversationsPanel) -> list[str]:
    return panel.visible_conversation_ids()


def _conversation_rows(panel: _ConversationsPanel) -> list[_ConvRow]:
    return list(panel._row_widgets.values())


def test_single_delete_persists_and_removes_project_membership(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    conversation = conversations.create_conversation("Delete me")
    conversations.add_message(conversation["id"], "user", "Keep canonical cleanup")
    folder = conversations.create_folder("Archive", operation_id="folder-1")
    assert conversations.move_conversation_to_folder(
        conversation["id"], folder["folder"]["id"], operation_id="move-1",
    )["success"]
    history_path = tmp_path / f"{conversation['id']}.json"
    assert history_path.exists()

    assert conversations.delete_conversation(conversation["id"]) is True

    assert not history_path.exists()
    assert conversations.get_conversation(conversation["id"]) is None
    snapshot = conversations.conversation_snapshot()
    assert snapshot["items"] == []
    assert snapshot["folders"][0]["id"] == folder["folder"]["id"]


def test_single_delete_failure_keeps_canonical_row_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    conversation = conversations.create_conversation("Busy")
    history_path = tmp_path / f"{conversation['id']}.json"
    monkeypatch.setattr(
        conversations,
        "_path_has_active_canonical_chat_lease",
        lambda _path: True,
    )

    assert conversations.delete_conversation(conversation["id"]) is False

    assert history_path.exists()
    assert conversations.get_conversation(conversation["id"]) is not None
    assert conversations.conversation_snapshot()["items"][0]["id"] == conversation["id"]


def test_ungrouped_single_delete_advances_revision_and_survives_new_store(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    conversation = conversations.create_conversation("Persisted delete")
    operation_id = "delete-revision-1"

    before = conversations.conversation_snapshot()
    receipt = conversations.delete_conversation_with_receipt(
        conversation["id"],
        operation_id=operation_id,
    )

    assert receipt["success"] is True
    assert receipt["deleted_ids"] == [conversation["id"]]
    assert receipt["snapshot_revision"] == before["snapshot_revision"] + 1
    assert not (tmp_path / f"{conversation['id']}.json").exists()

    # Load the store module afresh, as the Desktop and server do after restart.
    spec = importlib.util.spec_from_file_location(
        "osia._wor312_store_reload",
        conversations.__file__,
    )
    assert spec is not None and spec.loader is not None
    reloaded_store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reloaded_store)
    reloaded_store.CONVERSATIONS_DIR = tmp_path

    snapshot = reloaded_store.conversation_snapshot()
    assert snapshot["snapshot_revision"] == receipt["snapshot_revision"]
    assert snapshot["operation_id"] == operation_id
    assert conversation["id"] not in {
        item["id"] for item in snapshot["items"]
    }
    assert reloaded_store.get_conversation(conversation["id"]) is None
    assert reloaded_store.create_conversation("Catalogue rebuild")["id"] != (
        conversation["id"]
    )
    assert conversation["id"] not in {
        item["id"] for item in reloaded_store.conversation_snapshot()["items"]
    }


def test_single_delete_releases_existing_media_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    import osia.image_inputs as image_inputs

    conversation = conversations.create_conversation("With media")
    released = []
    monkeypatch.setattr(image_inputs, "release_media_owner", released.append)

    assert conversations.delete_conversation(conversation["id"]) is True

    assert released == [conversation["id"]]


def test_desktop_delete_uses_existing_mobile_invalidation_path(monkeypatch):
    from osia import main

    requests = []

    class _InlineThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            return None

        def read(self):
            return b'{"broadcast":true}'

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        return _Response()

    monkeypatch.setattr(main, "threading", SimpleNamespace(Thread=_InlineThread))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    main._notify_mobile_conversation_changed(
        "conv-deleted",
        revision=9,
        operation_id="desktop-delete-9",
        action="delete",
    )

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url.endswith("/internal/conversation_changed")
    assert timeout == 1.5
    assert json.loads(request.data) == {
        "conversation_id": "conv-deleted",
        "revision": 9,
        "operation_id": "desktop-delete-9",
        "action": "delete",
        "deleted_ids": [],
    }


def test_single_delete_projection_hides_synchronously_and_restores_order(
    monkeypatch,
):
    original = {
        "items": [_item("a"), _item("b")],
        "folders": [],
        "snapshot_revision": 1,
    }
    canonical = {"items": list(original["items"]), "folders": [], "snapshot_revision": 1}
    monkeypatch.setattr(
        conversations, "conversation_snapshot", lambda: dict(canonical),
    )
    panel = _ConversationsPanel()
    panel.resize(640, 560)
    panel.show()
    panel.refresh(snapshot=original)
    _APP.processEvents()

    panel.set_optimistic_hidden({"a"}, True)
    assert _rows(panel) == ["b"]

    # A simulated canonical failure leaves only B in the current projection;
    # rollback uses the pre-operation snapshot and restores A at its old index.
    canonical.update(items=[_item("b")], snapshot_revision=1)
    panel.set_optimistic_hidden({"a"}, False, snapshot=original)
    assert _rows(panel) == ["a", "b"]
    panel.close()
    panel.deleteLater()
    _APP.processEvents()


def test_bulk_delete_projection_preserves_folder_rows_and_selected_chat(
    monkeypatch,
):
    original = {
        "items": [_item("a"), _item("b", folder_id="folder"), _item("c")],
        "folders": [{"id": "folder", "name": "Project", "order": 0}],
        "snapshot_revision": 4,
    }
    canonical = dict(original)
    monkeypatch.setattr(
        conversations, "conversation_snapshot", lambda: dict(canonical),
    )
    panel = _ConversationsPanel()
    panel.resize(640, 560)
    panel.show()
    panel.set_active("b")
    panel.refresh(snapshot=original)
    _APP.processEvents()

    panel.set_optimistic_hidden({"a", "c"}, True)
    assert _rows(panel) == ["b"]
    assert _conversation_rows(panel)[0].property("active")
    assert panel._host_layout.count() > 0
    assert any(row.isVisible() for row in panel.findChildren(_FolderRow))

    canonical.update(
        items=[_item("b", folder_id="folder")],
        snapshot_revision=4,
    )
    panel.set_optimistic_hidden({"a", "c"}, False, snapshot=original)
    assert _rows(panel) == ["b", "a", "c"]
    panel.close()
    panel.deleteLater()
    _APP.processEvents()
