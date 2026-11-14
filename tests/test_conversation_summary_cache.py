"""Conversation list projections stay fresh without rereading unchanged histories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from osia import conversations


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    return tmp_path


def _write(root, conv_id="first", title="Before"):
    path = root / f"{conv_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": conv_id, "title": title, "revision": 1,
        "updated_at": "2026-11-14T10:00:00",
        "messages": [{"role": "user", "content": "history " * 1000}],
    }), encoding="utf-8")
    return path


def _record_history_reads(monkeypatch, root):
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.parent == root and path.suffix == ".json":
            reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return reads


def test_unchanged_snapshots_reuse_only_summaries_and_return_independent_data(store, monkeypatch):
    _write(store)
    _write(store, "second")
    initial = conversations.conversation_snapshot()
    reads = _record_history_reads(monkeypatch, store)
    snapshot = conversations.conversation_snapshot()
    assert snapshot == initial
    snapshot["items"][0]["title"] = "Caller must not change the cache"
    snapshot["items"].clear()
    assert conversations.list_conversations() == initial["items"]
    assert reads == []


def test_only_changed_history_is_reloaded_after_canonical_mutation(store, monkeypatch):
    _write(store)
    _write(store, "second")
    conversations.conversation_snapshot()
    assert conversations.rename_conversation("first", "Renamed")
    reads = _record_history_reads(monkeypatch, store)
    items = {item["id"]: item for item in conversations.list_conversations()}
    assert items["first"]["title"] == "Renamed"
    assert items["first"]["revision"] == 2
    assert reads == ["first.json"]


def test_external_process_atomic_replace_with_same_size_and_mtime_is_detected(store):
    path = _write(store)
    conversations.conversation_snapshot()
    subprocess.run([
        sys.executable, "-c",
        "import json, os, sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); info=p.stat(); data=json.loads(p.read_text(encoding='utf-8')); "
        "data['title']='After!'; q=p.with_suffix('.replacement'); "
        "q.write_text(json.dumps(data), encoding='utf-8'); "
        "os.utime(q, ns=(info.st_atime_ns, info.st_mtime_ns)); os.replace(q, p)",
        str(path),
    ], check=True, timeout=20)
    assert conversations.conversation_snapshot()["items"][0]["title"] == "After!"


def test_folder_membership_and_policy_refresh_without_history_reads(store, monkeypatch):
    _write(store)
    conversations.conversation_snapshot()
    folder = conversations.create_folder("Project")["folder"]["id"]
    assert conversations.move_conversation_to_folder("first", folder)["success"]
    assert conversations.set_folder_collapsed(folder, True)["success"]
    assert conversations.set_folder_context_policy(folder, True)["success"]
    reads = _record_history_reads(monkeypatch, store)
    snapshot = conversations.conversation_snapshot()
    assert snapshot["items"][0]["folder_id"] == folder
    assert snapshot["folders"][0]["collapsed"]
    assert snapshot["folders"][0]["allow_folder_context"]
    assert reads == []


def test_deleted_corrupt_and_recreated_files_never_reuse_a_stale_summary(store):
    path = _write(store)
    original = path.read_bytes()
    conversations.conversation_snapshot()
    path.write_text("{broken", encoding="utf-8")
    assert conversations.conversation_snapshot()["items"] == []
    path.write_bytes(original)
    assert conversations.conversation_snapshot()["items"][0]["title"] == "Before"
    path.unlink()
    assert conversations.conversation_snapshot()["items"] == []
    _write(store, title="Recreated")
    assert conversations.conversation_snapshot()["items"][0]["title"] == "Recreated"


def test_store_switch_does_not_share_summaries_between_profiles(store, monkeypatch):
    _write(store)
    assert conversations.conversation_snapshot()["items"][0]["title"] == "Before"
    other = store / "other-profile"
    _write(other, title="Other profile")
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", other)
    assert conversations.conversation_snapshot()["items"][0]["title"] == "Other profile"
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", store)
    assert conversations.conversation_snapshot()["items"][0]["title"] == "Before"


def test_file_replaced_during_read_is_not_cached_under_the_wrong_stamp(store, monkeypatch):
    path = _write(store)
    original = Path.read_text
    replaced = []

    def read(path_to_read, *args, **kwargs):
        text = original(path_to_read, *args, **kwargs)
        if path_to_read == path and not replaced:
            replacement = path.with_suffix(".replacement")
            replacement.write_text(text.replace("Before", "After!"), encoding="utf-8")
            os.replace(replacement, path)
            replaced.append(True)
        return text

    monkeypatch.setattr(Path, "read_text", read)
    assert conversations.conversation_snapshot()["items"][0]["title"] == "Before"
    assert conversations.conversation_snapshot()["items"][0]["title"] == "After!"
