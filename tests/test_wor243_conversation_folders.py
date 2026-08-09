import asyncio
import json
from pathlib import Path

from osia import conversation_context, conversations


def _use_store(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)


def _conversation(title, *messages):
    conv = conversations.create_conversation(title)
    for role, content in messages:
        conversations.add_message(conv["id"], role, content)
    return conversations.get_conversation(conv["id"])


def test_old_data_migrates_ungrouped_without_rewriting(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)
    conv = _conversation("Legacy", ("user", "Keep this exact history."))
    before = (tmp_path / f"{conv['id']}.json").read_bytes()

    snapshot = conversations.conversation_snapshot()

    assert snapshot["version"] == 2
    assert snapshot["folders"] == []
    assert snapshot["items"][0]["folder_id"] is None
    assert not (tmp_path / "_folders" / "state.json").exists()
    assert (tmp_path / f"{conv['id']}.json").read_bytes() == before


def test_folder_lifecycle_membership_order_and_restart(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)
    conv = _conversation(
        "Stable",
        ("user", "Original user message"),
        ("assistant", "Original assistant answer"),
    )
    original = json.loads(json.dumps(conv))

    created = conversations.create_folder("Project Alpha", operation_id="op-create")
    replay = conversations.create_folder("Ignored", operation_id="op-create")
    second = conversations.create_folder("Project Beta", operation_id="op-second")
    folder_id = created["folder"]["id"]
    second_id = second["folder"]["id"]

    assert created["success"] and replay["replayed"]
    assert replay["folder"]["id"] == folder_id
    assert conversations.rename_folder(
        folder_id, "Renamed", operation_id="op-rename"
    )["success"]
    moved = conversations.move_conversation_to_folder(
        conv["id"], folder_id, operation_id="op-move-in"
    )
    assert moved["success"]
    after_move = conversations.get_conversation(conv["id"])
    assert after_move == original

    assert conversations.set_folder_collapsed(
        folder_id, True, operation_id="op-collapse"
    )["success"]
    assert conversations.set_folder_context_policy(
        folder_id, True, operation_id="op-context"
    )["success"]
    assert conversations.reorder_folders(
        [second_id, folder_id], operation_id="op-order"
    )["success"]

    restarted = conversations.conversation_snapshot()
    assert [folder["id"] for folder in restarted["folders"]] == [
        second_id,
        folder_id,
    ]
    restored = restarted["folders"][1]
    assert restored["name"] == "Renamed"
    assert restored["collapsed"] is True
    assert restored["allow_folder_context"] is True
    assert restarted["items"][0]["folder_id"] == folder_id

    moved_out = conversations.move_conversation_to_folder(
        conv["id"], None, operation_id="op-move-out"
    )
    assert moved_out["success"]
    assert conversations.get_conversation(conv["id"]) == original
    assert conversations.conversation_snapshot()["items"][0]["folder_id"] is None


def test_projects_render_before_ungrouped_conversations():
    source = (
        Path(__file__).parents[1] / "osia" / "gui" / "main_popup.py"
    ).read_text(encoding="utf-8")
    method = source.index("def refresh(self, *, snapshot: dict | None = None)")
    project_section = source.index('i18n.t("conversations.projects")')
    project_rows = source.index("for index, folder in enumerate(folders)", method)
    ungrouped_rows = source.index("for conv in ungrouped", method)

    assert method < project_section < project_rows < ungrouped_rows


def test_project_ui_copy_has_no_folder_terminology():
    from osia import i18n

    keys = {
        "conversations.create_folder",
        "conversations.folder_name",
        "conversations.rename_folder",
        "conversations.folder_settings",
        "conversations.allow_folder_context",
        "conversations.move_to_folder",
        "conversations.remove_from_project",
        "conversations.projects",
        "conversations.ungrouped",
        "conversations.move_folder_up",
        "conversations.move_folder_down",
        "conversations.bulk_delete_title",
        "conversations.bulk_delete_confirm",
        "conversations.bulk_delete_none",
    }
    visible_copy = "\n".join(
        value
        for key in keys
        for value in i18n.TRANSLATIONS[key].values()
    ).casefold()

    for forbidden in (
        "folder", "dossier", "ordner", "carpeta", "文件夹", "フォルダー",
    ):
        assert forbidden not in visible_copy
    assert i18n.TRANSLATIONS["conversations.create_folder"]["en"] == (
        "Create project"
    )
    assert i18n.TRANSLATIONS["conversations.create_folder"]["fr"] == (
        "Créer un projet"
    )


def test_mobile_mutation_frames_round_trip_through_pc_snapshot(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, tmp_path)
    conv = _conversation("Mobile move", ("user", "history stays canonical"))
    created = conversations.apply_folder_mutation({
        "type": "create_conversation_folder",
        "name": "From Mobile",
        "operation_id": "mobile-create",
    })
    folder_id = created["folder"]["id"]
    replay = conversations.apply_folder_mutation({
        "type": "create_conversation_folder",
        "name": "Duplicate",
        "operation_id": "mobile-create",
    })
    moved = conversations.apply_folder_mutation({
        "type": "move_conversation_to_folder",
        "conversation_id": conv["id"],
        "folder_id": folder_id,
        "operation_id": "mobile-move",
    })
    empty = conversations.apply_folder_mutation({
        "type": "create_conversation_folder",
        "name": "Empty mirror",
        "operation_id": "mobile-empty",
    })

    assert replay["replayed"] and replay["folder"]["id"] == folder_id
    assert moved["success"] and empty["success"]

    from osia.server import server

    class Client:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(frame)

    client = Client()
    server._WS_CLIENTS.add(client)
    try:
        asyncio.run(server._broadcast_conversations())
    finally:
        server._WS_CLIENTS.discard(client)

    snapshot = client.frames[-1]
    assert snapshot["type"] == "conversations"
    assert snapshot["version"] == 2
    assert snapshot["items"][0]["id"] == conv["id"]
    assert snapshot["items"][0]["folder_id"] == folder_id
    assert {folder["id"] for folder in snapshot["folders"]} == {
        folder_id,
        empty["folder"]["id"],
    }


def test_empty_folders_survive_and_bulk_delete_is_exact_and_replay_safe(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, tmp_path)
    keep = _conversation("Keep", ("user", "foldered history"))
    delete_a = _conversation("Delete A", ("user", "ungrouped A"))
    delete_b = _conversation("Delete B", ("user", "ungrouped B"))
    populated = conversations.create_folder("Populated", operation_id="folder-1")
    empty = conversations.create_folder("Empty", operation_id="folder-2")
    assert conversations.move_conversation_to_folder(
        keep["id"], populated["folder"]["id"], operation_id="move-keep"
    )["success"]

    mismatch = conversations.delete_confirmed_ungrouped_conversations(
        1, operation_id="stale-count"
    )
    assert not mismatch["success"]
    assert mismatch["error"] == "ungrouped_count_changed"
    assert conversations.get_conversation(delete_a["id"]) is not None
    assert conversations.get_conversation(delete_b["id"]) is not None

    first = conversations.delete_confirmed_ungrouped_conversations(
        2,
        operation_id="bulk-delete"
    )
    replay = conversations.delete_confirmed_ungrouped_conversations(
        2,
        operation_id="bulk-delete"
    )

    assert first["success"] and first["deleted_count"] == 2
    assert set(first["deleted_ids"]) == {delete_a["id"], delete_b["id"]}
    assert replay["success"] and replay["replayed"]
    assert replay["deleted_count"] == 2
    assert conversations.get_conversation(keep["id"])["messages"] == keep["messages"]
    assert conversations.get_conversation(delete_a["id"]) is None
    assert conversations.get_conversation(delete_b["id"]) is None
    snapshot = conversations.conversation_snapshot()
    assert {folder["id"] for folder in snapshot["folders"]} == {
        populated["folder"]["id"],
        empty["folder"]["id"],
    }


def test_bulk_delete_restart_recovers_before_and_after_metadata_commit(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, tmp_path)
    rollback = _conversation("Rollback", ("user", "must survive"))
    rollback_path = tmp_path / f"{rollback['id']}.json"
    rollback_txn = tmp_path / "_folders" / "bulk-before-commit"
    rollback_txn.mkdir(parents=True)
    (rollback_txn / "manifest.json").write_text(
        json.dumps({
            "operation_id": "op-before-commit",
            "files": [rollback_path.name],
        }),
        encoding="utf-8",
    )
    rollback_path.replace(rollback_txn / rollback_path.name)

    conversations.conversation_snapshot()

    assert conversations.get_conversation(rollback["id"]) is not None
    assert not rollback_txn.exists()

    conversations.create_folder("Persist state", operation_id="state-folder")
    finish = _conversation("Finish", ("user", "must be deleted"))
    finish_path = tmp_path / f"{finish['id']}.json"
    finish_txn = tmp_path / "_folders" / "bulk-after-commit"
    finish_txn.mkdir(parents=True)
    (finish_txn / "manifest.json").write_text(
        json.dumps({
            "operation_id": "op-after-commit",
            "files": [finish_path.name],
        }),
        encoding="utf-8",
    )
    finish_path.replace(finish_txn / finish_path.name)
    state_path = tmp_path / "_folders" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["recent_operations"].append({
        "id": "op-after-commit",
        "kind": "delete_ungrouped",
        "result": {
            "success": True,
            "deleted_count": 1,
            "deleted_ids": [finish["id"]],
        },
    })
    state_path.write_text(json.dumps(state), encoding="utf-8")

    conversations.conversation_snapshot()

    assert conversations.get_conversation(finish["id"]) is None
    assert not finish_txn.exists()


def test_folder_context_off_on_relevant_only_cross_folder_and_off_again(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, tmp_path)
    current = _conversation("Current")
    relevant = _conversation(
        "Launch codename",
        ("user", "The launch codename is AMBER-FALCON-731."),
        ("assistant", "I will remember the launch codename."),
    )
    irrelevant = _conversation(
        "Groceries",
        ("user", "Buy oat milk and oranges."),
        ("assistant", "Added to the grocery list."),
    )
    cross_folder = _conversation(
        "Other launch codename",
        ("user", "The launch codename is FORBIDDEN-CROSS-992."),
        ("assistant", "That belongs to another project."),
    )
    same = conversations.create_folder("Same", operation_id="same-folder")
    other = conversations.create_folder("Other", operation_id="other-folder")
    for conv in (current, relevant, irrelevant):
        assert conversations.move_conversation_to_folder(
            conv["id"],
            same["folder"]["id"],
            operation_id=f"move-{conv['id']}",
        )["success"]
    assert conversations.move_conversation_to_folder(
        cross_folder["id"],
        other["folder"]["id"],
        operation_id="move-cross",
    )["success"]

    question = "What is the launch codename?"
    off = conversation_context.retrieve_folder_context(current["id"], question)
    assert off.conversation_ids == ()

    assert conversations.set_folder_context_policy(
        same["folder"]["id"], True, operation_id="context-on"
    )["success"]
    selected = conversation_context.retrieve_folder_context(current["id"], question)
    assert selected.conversation_ids == (relevant["id"],)
    assert "AMBER-FALCON-731" in selected.prompt
    assert "oat milk" not in selected.prompt
    assert "FORBIDDEN-CROSS-992" not in selected.prompt

    built = conversation_context.build_context(
        [{"role": "user", "content": question}],
        provider="openai",
        model="gpt-4.1",
        conversation_id=current["id"],
    )
    assert built.folder_context_conversation_ids == (relevant["id"],)
    assert any(
        "AMBER-FALCON-731" in str(message.get("content") or "")
        for message in built.messages
    )

    assert conversations.set_folder_context_policy(
        same["folder"]["id"], False, operation_id="context-off"
    )["success"]
    isolated = conversation_context.build_context(
        [{"role": "user", "content": question}],
        provider="openai",
        model="gpt-4.1",
        conversation_id=current["id"],
    )
    assert isolated.folder_context_conversation_ids == ()
    assert all(
        "AMBER-FALCON-731" not in str(message.get("content") or "")
        for message in isolated.messages
    )
