import asyncio
import json
from pathlib import Path

import pytest

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


def test_pc_new_chat_in_project_is_atomic_and_replay_safe(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)
    created = conversations.create_folder("PC Project", operation_id="project-pc")
    folder_id = created["folder"]["id"]
    conversations.set_folder_collapsed(
        folder_id, True, operation_id="collapse-before-new"
    )

    first = conversations.create_conversation_in_folder(
        folder_id, operation_id="pc-new-in-project"
    )
    replay = conversations.create_conversation_in_folder(
        folder_id, operation_id="pc-new-in-project"
    )

    assert first["success"] and replay["success"] and replay["replayed"]
    assert first["conversation_id"] == replay["conversation_id"]
    snapshot = conversations.conversation_snapshot()
    assert len(snapshot["items"]) == 1
    assert snapshot["items"][0]["id"] == first["conversation_id"]
    assert snapshot["items"][0]["folder_id"] == folder_id
    assert conversations.get_conversation(first["conversation_id"])["messages"] == []
    assert snapshot["folders"][0]["collapsed"] is False


def test_mobile_new_chat_in_project_frame_creates_exactly_one_chat(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, tmp_path)
    folder = conversations.create_folder("Mobile Project", operation_id="mobile-p")
    frame = {
        "type": "new_conversation_in_project",
        "folder_id": folder["folder"]["id"],
        "operation_id": "mobile-new-in-project",
    }

    first = conversations.apply_folder_mutation(frame)
    replay = conversations.apply_folder_mutation(frame)

    assert first["success"] and replay["replayed"]
    assert first["conversation_id"] == replay["conversation_id"]
    snapshot = conversations.conversation_snapshot()
    assert [(item["id"], item["folder_id"]) for item in snapshot["items"]] == [
        (first["conversation_id"], folder["folder"]["id"])
    ]


@pytest.mark.parametrize("member_count", [0, 1, 3])
def test_delete_project_is_exact_atomic_and_replay_safe(
    monkeypatch, tmp_path, member_count
):
    _use_store(monkeypatch, tmp_path)
    target = conversations.create_folder("Delete Me", operation_id="target")
    other = conversations.create_folder("Keep Project", operation_id="other")
    target_id = target["folder"]["id"]
    other_id = other["folder"]["id"]
    members = [_conversation(f"Member {index}") for index in range(member_count)]
    for index, conv in enumerate(members):
        assert conversations.move_conversation_to_folder(
            conv["id"], target_id, operation_id=f"target-member-{index}"
        )["success"]
    outside = _conversation("Outside", ("user", "must survive"))
    other_member = _conversation("Other member", ("user", "also survives"))
    conversations.move_conversation_to_folder(
        other_member["id"], other_id, operation_id="other-member"
    )
    outside_before = conversations.get_conversation(outside["id"])
    other_before = conversations.get_conversation(other_member["id"])

    stale = conversations.delete_folder_with_conversations(
        target_id, member_count + 1, operation_id="stale-project-count"
    )
    assert not stale["success"]
    assert stale["error"] == "folder_count_changed"
    assert stale["actual_count"] == member_count
    assert any(
        folder["id"] == target_id
        for folder in conversations.conversation_snapshot()["folders"]
    )

    first = conversations.delete_folder_with_conversations(
        target_id, member_count, operation_id="delete-project"
    )
    replay = conversations.delete_folder_with_conversations(
        target_id, member_count, operation_id="delete-project"
    )

    assert first["success"] and first["deleted_count"] == member_count
    assert replay["success"] and replay["replayed"]
    assert set(first["deleted_ids"]) == {conv["id"] for conv in members}
    assert all(conversations.get_conversation(conv["id"]) is None for conv in members)
    assert conversations.get_conversation(outside["id"]) == outside_before
    assert conversations.get_conversation(other_member["id"]) == other_before
    restarted = conversations.conversation_snapshot()
    assert [folder["id"] for folder in restarted["folders"]] == [other_id]
    assert all(item.get("folder_id") != target_id for item in restarted["items"])
    state = json.loads(
        (tmp_path / "_folders" / "state.json").read_text(encoding="utf-8")
    )
    assert target_id not in state["memberships"].values()


@pytest.mark.parametrize("expected_count", [-1, True, "invalid"])
def test_delete_project_rejects_invalid_counts_without_mutation(
    monkeypatch, tmp_path, expected_count
):
    _use_store(monkeypatch, tmp_path)
    target = conversations.create_folder("Keep", operation_id="keep-project")
    target_id = target["folder"]["id"]
    before = conversations.conversation_snapshot()

    result = conversations.delete_folder_with_conversations(
        target_id, expected_count, operation_id="invalid-count"
    )

    assert not result["success"]
    assert result["error"] == "invalid_expected_count"
    assert conversations.conversation_snapshot() == before


def test_project_create_and_delete_transactions_recover_on_restart(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, tmp_path)
    folder = conversations.create_folder("Recovery", operation_id="recovery-folder")
    folder_id = folder["folder"]["id"]

    created = conversations.create_conversation_in_folder(
        folder_id, operation_id="committed-create"
    )
    created_path = tmp_path / f"{created['conversation_id']}.json"
    create_txn = tmp_path / "_folders" / "project-create-restart"
    create_txn.mkdir(parents=True)
    (create_txn / "manifest.json").write_text(
        json.dumps({
            "operation_id": "committed-create",
            "kind": "create_in_folder",
            "files": [created_path.name],
        }),
        encoding="utf-8",
    )
    created_path.replace(create_txn / created_path.name)

    recovered_create = conversations.conversation_snapshot()

    assert created_path.is_file()
    assert not create_txn.exists()
    assert recovered_create["items"][0]["folder_id"] == folder_id

    delete_txn = tmp_path / "_folders" / "project-delete-restart"
    delete_txn.mkdir(parents=True)
    (delete_txn / "manifest.json").write_text(
        json.dumps({
            "operation_id": "uncommitted-delete",
            "kind": "delete_folder",
            "files": [created_path.name],
        }),
        encoding="utf-8",
    )
    created_path.replace(delete_txn / created_path.name)

    recovered_delete = conversations.conversation_snapshot()

    assert created_path.is_file()
    assert not delete_txn.exists()
    assert recovered_delete["items"][0]["folder_id"] == folder_id


def test_project_actions_use_canonical_count_and_cancel_has_no_mutation_path():
    source = (
        Path(__file__).parents[1] / "osia" / "gui" / "main_popup.py"
    ).read_text(encoding="utf-8")
    menu = source.index("def _show_settings(")
    new_chat = source.index('i18n.t("conversations.new_chat")', menu)
    rename = source.index('i18n.t("conversations.rename_folder")', menu)
    settings = source.index('i18n.t("conversations.folder_settings")', menu)
    delete = source.index('i18n.t("conversations.delete_project")', menu)
    confirm = source.index("def _confirm_delete(", menu)

    assert menu < new_chat < rename < settings < delete
    assert "snapshot = conversations.conversation_snapshot()" in source[confirm:]
    assert "if box.clickedButton() is delete_button:" in source[confirm:]

    main_source = (
        Path(__file__).parents[1] / "osia" / "main.py"
    ).read_text(encoding="utf-8")
    action = main_source.index('elif action == "new_conversation":')
    assert main_source.index("active_conv = result.get", action) > action
    assert main_source.index("popup.show_chat()", action) > action

    server_source = (
        Path(__file__).parents[1] / "osia" / "server" / "server.py"
    ).read_text(encoding="utf-8")
    mobile_action = server_source.index(
        'if msg_type == "new_conversation_in_project":'
    )
    active_frame = server_source.index('"type": "active_conversation"', mobile_action)
    snapshot_frame = server_source.index(
        "snapshot = conversations.conversation_snapshot()", mobile_action
    )
    assert mobile_action < active_frame < snapshot_frame
    assert '"operation_id": str(result.get("operation_id")' in server_source[
        active_frame:snapshot_frame
    ]


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
        "conversations.delete_project",
        "conversations.delete_project_confirm",
        "conversations.delete_project_confirm_one",
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
    assert i18n.TRANSLATIONS["conversations.delete_project"]["en"] == (
        "Delete project"
    )
    assert i18n.t(
        "conversations.delete_project_confirm", name="OSIA", count=3,
    ) == (
        "Delete project “OSIA” and its 3 conversations? "
        "This action is permanent."
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
