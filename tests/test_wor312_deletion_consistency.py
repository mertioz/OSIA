"""WOR-312 deletion classification and real-store consistency regressions."""

from __future__ import annotations

import json
import importlib.util
from types import SimpleNamespace

from osia import conversations, main, tool_attempts, turn_terminality


def _persist_orphaned_turn(tmp_path, monkeypatch, *, live: bool = False):
    monkeypatch.setattr(
        turn_terminality, "CONFIG_DIR", str(tmp_path / "config"),
    )
    conv = conversations.create_conversation("Orphaned turn")
    binding = {
        "conversation_id": conv["id"],
        "request_id": f"desktop_req_{conv['id']}",
        "turn_id": f"desktop_turn_{conv['id']}",
        "run_id": f"desktop_run_{conv['id']}",
        "provider_id": "deepseek",
        "model_id": "deepseek-flash",
        "effective_reasoning": "off",
    }
    message = {
        "role": "user",
        "content": "unfinished objective",
        "_assistant_message_id": f"assistant_{conv['id']}",
        "_terminality_objective": "unfinished objective",
        "_execution_owner": "desktop",
        "_execution_owner_pid": 123456,
        "_execution_owner_process_identity": "original-owner",
        **{"_" + key: value for key, value in binding.items()},
    }
    path = tmp_path / f"{conv['id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"] = [message]
    path.write_text(json.dumps(data), encoding="utf-8")
    controller = turn_terminality.TurnTerminalityController(
        turn_id=binding["turn_id"],
        objective=message["_terminality_objective"],
        execution_required=True,
        storage_dir=tmp_path / "config" / "turn_terminality",
    )
    controller.set_execution_binding(binding)
    monkeypatch.setattr(
        turn_terminality, "live_process_identity",
        lambda _pid: (live, "original-owner" if live else ""),
    )
    return conv, path, binding


def test_cancelled_desktop_turn_is_not_recovered_as_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    conv = conversations.create_conversation("Cancelled")
    binding = {
        "conversation_id": conv["id"],
        "request_id": "desktop_req_cancelled",
        "turn_id": "desktop_turn_cancelled",
        "run_id": "desktop_run_cancelled",
        "provider_id": "deepseek",
        "model_id": "deepseek-flash",
        "effective_reasoning": "off",
    }
    user = {
        "role": "user",
        "content": "cancelled objective",
        "_assistant_message_id": "assistant-cancelled",
        "_terminality_objective": "cancelled objective",
        "_execution_owner": "desktop",
        "_execution_owner_pid": 0,
        **{"_" + key: value for key, value in binding.items()},
    }
    path = tmp_path / f"{conv['id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"] = [user]
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        tool_attempts,
        "get_tool_attempt_store",
        lambda: SimpleNamespace(find_resumable_chat_turn=lambda _id: None),
    )
    monkeypatch.setattr(
        turn_terminality,
        "load_persisted_terminal_result",
        lambda _turn_id: {
            "terminal_state": "user_cancelled",
            "execution_binding": binding,
            "reservation_id": "final-cancelled",
            "final_text": "",
        },
    )

    assert main._incomplete_desktop_chat_turns() == []


def test_legacy_metadata_id_can_delete_its_unique_storage_file(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    path = tmp_path / "legacy-storage-name.json"
    path.write_text(json.dumps({
        "id": "legacy-visible-id",
        "title": "Legacy",
        "messages": [],
    }), encoding="utf-8")
    before = conversations.conversation_snapshot()
    assert [item["id"] for item in before["items"]] == ["legacy-visible-id"]

    receipt = conversations.delete_conversation_with_receipt(
        "legacy-visible-id", operation_id="legacy-delete",
    )

    assert receipt["success"] is True
    assert receipt["deleted_ids"] == ["legacy-visible-id"]
    assert receipt["snapshot_revision"] == before["snapshot_revision"] + 1
    assert not path.exists()
    assert conversations.conversation_snapshot()["items"] == []


def test_duplicate_legacy_ids_fail_without_deleting_either_file(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    paths = [tmp_path / "old-a.json", tmp_path / "old-b.json"]
    for path in paths:
        path.write_text(json.dumps({
            "id": "duplicate-visible-id", "title": path.stem,
            "messages": [],
        }), encoding="utf-8")
    before = conversations.conversation_snapshot()["snapshot_revision"]

    receipt = conversations.delete_conversation_with_receipt(
        "duplicate-visible-id", operation_id="ambiguous-delete",
    )

    assert receipt["success"] is False
    assert receipt["error"] == "conversation_id_ambiguous"
    assert receipt["snapshot_revision"] == before
    assert all(path.exists() for path in paths)


def test_dead_owner_turn_is_cancelled_exactly_then_single_deleted(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    conv, path, binding = _persist_orphaned_turn(tmp_path, monkeypatch)
    before = conversations.conversation_snapshot()["snapshot_revision"]

    receipt = conversations.delete_conversation_with_receipt(
        conv["id"], operation_id="delete-orphaned",
    )

    assert receipt["success"] is True
    assert receipt["snapshot_revision"] == before + 1
    assert not path.exists()
    terminal = turn_terminality.load_persisted_terminal_result(
        binding["turn_id"]
    )
    assert terminal["terminal_state"] == "user_cancelled"
    assert terminal["execution_binding"] == binding

    spec = importlib.util.spec_from_file_location(
        "osia._wor312_consistency_reload", conversations.__file__,
    )
    reloaded = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(reloaded)
    reloaded.CONVERSATIONS_DIR = tmp_path
    assert reloaded.conversation_snapshot()["items"] == []


def test_live_turn_blocks_single_delete_with_factual_reason(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    conv, path, binding = _persist_orphaned_turn(
        tmp_path, monkeypatch, live=True,
    )
    before = conversations.conversation_snapshot()["snapshot_revision"]

    receipt = conversations.delete_conversation_with_receipt(
        conv["id"], operation_id="delete-live",
    )

    assert receipt["success"] is False
    assert receipt["error"] == "conversation_busy"
    assert receipt["snapshot_revision"] == before
    assert path.exists()
    assert turn_terminality.load_persisted_terminal_result(
        binding["turn_id"]
    ) is None


def test_mixed_real_store_single_and_confirmed_bulk_survive_reload(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    folder = conversations.create_folder(
        "Project", operation_id="create-project",
    )["folder"]

    def make_set(prefix: str):
        # Desktop and Mobile both create through this PC canonical API.
        desktop = conversations.create_conversation(prefix + " Desktop")
        mobile = conversations.create_conversation(prefix + " Mobile")
        empty = conversations.create_conversation(prefix + " Empty")
        long = conversations.create_conversation(prefix + " Long")
        for index in range(12):
            conversations.add_message(long["id"], "user", f"turn {index}")
        grouped = conversations.create_conversation(prefix + " Grouped")
        moved = conversations.move_conversation_to_folder(
            grouped["id"], folder["id"],
            operation_id=prefix + "-move",
        )
        assert moved["success"] is True
        return desktop, mobile, empty, long, grouped

    first = make_set("Single")
    for conv in first:
        receipt = conversations.delete_conversation_with_receipt(
            conv["id"], operation_id="single-" + conv["id"],
        )
        assert receipt["success"] is True
        assert not (tmp_path / f"{conv['id']}.json").exists()
    assert conversations.conversation_snapshot()["items"] == []

    second = make_set("Bulk")
    orphaned, orphan_path, orphan_binding = _persist_orphaned_turn(
        tmp_path, monkeypatch,
    )
    before = conversations.conversation_snapshot()
    expected_ids = {conv["id"] for conv in second[:-1]}
    expected_ids.add(orphaned["id"])
    receipt = conversations.delete_confirmed_ungrouped_conversations(
        len(expected_ids), operation_id="bulk-mixed",
    )
    assert receipt["success"] is True
    assert set(receipt["deleted_ids"]) == expected_ids
    assert receipt["snapshot_revision"] == before["snapshot_revision"] + 1
    assert not orphan_path.exists()
    assert turn_terminality.load_persisted_terminal_result(
        orphan_binding["turn_id"]
    )["terminal_state"] == "user_cancelled"

    spec = importlib.util.spec_from_file_location(
        "osia._wor312_bulk_reload", conversations.__file__,
    )
    reloaded = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(reloaded)
    reloaded.CONVERSATIONS_DIR = tmp_path
    after = reloaded.conversation_snapshot()
    assert {item["id"] for item in after["items"]} == {
        second[-1]["id"]
    }
    assert after["folders"][0]["id"] == folder["id"]
    assert after["snapshot_revision"] == receipt["snapshot_revision"]


def test_bulk_live_turn_failure_is_atomic_and_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    valid = conversations.create_conversation("Valid")
    blocked, blocked_path, _binding = _persist_orphaned_turn(
        tmp_path, monkeypatch, live=True,
    )
    before = conversations.conversation_snapshot()

    receipt = conversations.delete_confirmed_ungrouped_conversations(
        2, operation_id="bulk-live",
    )

    assert receipt["success"] is False
    assert receipt["error"] == "conversation_busy"
    assert receipt["busy_ids"] == [blocked["id"]]
    assert receipt.get("deleted_ids", []) == []
    assert receipt["snapshot_revision"] == before["snapshot_revision"]
    assert blocked_path.exists()
    assert (tmp_path / f"{valid['id']}.json").exists()
    assert {item["id"] for item in conversations.conversation_snapshot()["items"]} == {
        blocked["id"], valid["id"],
    }
