"""Focused durability edge proofs added during the WOR-265 final safety pass."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from osia import conversations, document_sources
from osia.server import server
from osia.tool_attempts import ToolAttemptStore


def test_first_chat_request_is_reserved_once_across_concurrent_retries(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", Path(tmp_path))
    request_id = "request_wor265_first_chat"

    def resolve(_index):
        return conversations.resolve_request_conversation(
            request_id, "First Chat",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(resolve, range(8)))

    assert len({item[0]["id"] for item in results}) == 1
    assert sum(item[1] for item in results) == 1
    assert len(list(Path(tmp_path).glob("*.json"))) == 1
    assert all(
        item[0]["_first_request_id"] == request_id for item in results
    )


def test_first_chat_resolver_reuses_the_conversation_containing_the_request(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", Path(tmp_path))
    request_id = "request_wor265_existing_first_chat"
    reserved, created = conversations.resolve_request_conversation(request_id)
    assert created is True
    conversations.commit_message(
        reserved["id"],
        "user",
        "hello",
        metadata={
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": "turn_wor265_existing_first_chat",
            "_message_id": "message_wor265_existing_first_chat",
            "_operation_id": "operation_wor265_existing_first_chat",
        },
    )

    resolved, created_again = conversations.resolve_request_conversation(
        request_id, "A title that must not create another chat",
    )

    assert created_again is False
    assert resolved["id"] == reserved["id"]
    assert len(list(Path(tmp_path).glob("*.json"))) == 1


def test_media_release_cas_distinguishes_release_absence_and_indeterminate(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", Path(tmp_path))

    assert conversations.release_post_turn_media_cas(
        "missing", "lease_wor265",
    ) == "confirmed_absent_or_different"

    malformed = Path(tmp_path) / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    assert conversations.release_post_turn_media_cas(
        "malformed", "lease_wor265",
    ) == "indeterminate"

    conv = conversations.create_conversation()
    conversations.commit_message(
        conv["id"],
        "assistant",
        "done",
        metadata={
            "_post_turn_media_lease_id": "lease_wor265",
            "_post_turn_media_owner_pid": os.getpid(),
        },
    )
    assert conversations.release_post_turn_media_cas(
        conv["id"], "different_lease",
    ) == "confirmed_absent_or_different"
    assert conversations.get_conversation(conv["id"])[
        "_post_turn_media_lease"
    ]["lease_id"] == "lease_wor265"
    assert conversations.release_post_turn_media_cas(
        conv["id"], "lease_wor265",
    ) == "released"
    assert "_post_turn_media_lease" not in conversations.get_conversation(
        conv["id"]
    )
    assert conversations.release_post_turn_media_cas(
        conv["id"], "lease_wor265",
    ) == "confirmed_absent_or_different"


def _write_private_source(
    root: Path,
    ref: str,
    *,
    owner_id: str,
    bound_run_hash: str = "",
) -> Path:
    directory = root / ref
    directory.mkdir(parents=True)
    (directory / "private.json").write_text(
        json.dumps({
            "owner_hash": document_sources.owner_hash(owner_id),
            "bound_run_hash": bound_run_hash,
        }),
        encoding="utf-8",
    )
    return directory


def test_source_cleanup_confirms_public_detach_even_if_tombstone_delete_fails(
    tmp_path,
):
    store = document_sources.DocumentSourceStore(tmp_path / "sources")
    ref = "src_" + "a" * 32
    public_directory = _write_private_source(
        store.root, ref, owner_id="device_wor265",
    )

    with patch(
        "osia.document_sources.shutil.rmtree",
        side_effect=OSError("simulated Windows file lock"),
    ):
        pending = store.release_owned_confirmed(
            [ref], owner_id="device_wor265",
        )

    assert pending == ()
    assert not public_directory.exists()
    assert len(list(store.root.glob(f".{ref}.*.delete"))) == 1


def test_source_cleanup_retries_only_an_indeterminate_public_detach(tmp_path):
    store = document_sources.DocumentSourceStore(tmp_path / "sources")
    ref = "src_" + "b" * 32
    public_directory = _write_private_source(
        store.root, ref, owner_id="device_wor265",
    )

    with patch(
        "osia.document_sources.os.replace",
        side_effect=OSError("simulated rename failure"),
    ):
        pending = store.release_owned_confirmed(
            [ref], owner_id="device_wor265",
        )

    assert pending == (ref,)
    assert public_directory.exists()


class _Frames:
    def __init__(self):
        self.values = []

    async def send_json(self, frame):
        self.values.append(dict(frame))


def test_complete_preversion_audio_replay_without_route_sends_history_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", Path(tmp_path))
    request_id = "request_wor265_legacy_audio"
    turn_id = "turn_wor265_legacy_audio"
    b64_data = "legacy-audio-payload"
    transcript = "What is two plus two?"
    legacy_hash = hashlib.sha256(b64_data.encode("utf-8")).hexdigest()
    conv = conversations.create_conversation()
    conversations.commit_message(
        conv["id"],
        "user",
        transcript,
        metadata={
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": turn_id,
            "_message_id": "message_wor265_legacy_user",
            "_operation_id": "operation_wor265_legacy_user",
            "_client_content_hash": legacy_hash,
        },
    )
    conversations.commit_message(
        conv["id"],
        "assistant",
        "Four.",
        metadata={
            "_request_id": request_id,
            "_client_request_id": request_id,
            "_turn_id": turn_id,
            "_message_id": "message_wor265_legacy_assistant",
            "_operation_id": "operation_wor265_legacy_assistant",
            "_response_event_id": "event_wor265_legacy_response",
            "_response_sequence": 1,
            "_terminal_event_id": "event_wor265_legacy_terminal",
            "_terminal_sequence": 2,
        },
    )
    channel = _Frames()
    monkeypatch.setattr(
        server,
        "decode_base64_wav",
        lambda _data: (server.np.asarray([0.5], dtype="float32"), 16_000),
    )
    monkeypatch.setattr(
        "osia.voice.stt.transcribe",
        lambda _audio, sample_rate: transcript,
    )

    result = asyncio.run(
        server._handle_audio(
            channel,
            b64_data,
            "wav",
            [],
            conv["id"],
            get_agent=lambda: pytest.fail("provider path must not run"),
            turn_meta={
                "request_id": request_id,
                "client_request_id": request_id,
                "_legacy_audio_replay_v1": {
                    "legacy_audio_hash": legacy_hash,
                },
            },
        )
    )

    assert result == ([], conv["id"])
    assert len(channel.values) == 1
    frame = channel.values[0]
    assert frame["type"] == "conversation_history"
    assert frame["replayed"] is True
    assert frame["legacy_route_unavailable"] is True
    assert frame["request_id"] == request_id
    assert frame["client_request_id"] == request_id
    assert [(item["role"], item["request_id"]) for item in frame["messages"]] == [
        ("user", request_id),
        ("assistant", request_id),
    ]


def test_durable_replace_failure_is_sanitized_and_preserves_destination(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path / "tool-attempts")
    store._ensure_dirs()
    destination = store.attempts_dir / "attempt_diagnostic.json"
    destination.write_text('{"state":"old-private-value"}', encoding="utf-8")

    def deny_replace(source, target):
        raise PermissionError(13, "Access is denied", source, 5, target)

    monkeypatch.setattr("osia.tool_attempts.os.replace", deny_replace)
    with pytest.raises(PermissionError) as caught:
        with store._store_lock():
            store._write_atomic(destination, {"state": "new-private-value"})

    diagnostic = caught.value.osia_durable_diagnostic
    rendered = json.dumps(diagnostic, sort_keys=True)
    assert diagnostic["operation_id"].startswith("atomic_replace_")
    assert len(diagnostic["path_hash"]) == 64
    assert diagnostic["path_kind"] == "attempts"
    assert diagnostic["pid"] == os.getpid()
    assert diagnostic["thread_lock_depth"] >= 1
    assert diagnostic["thread_lock_owned"] is True
    assert diagnostic["interprocess_lock_held_by_thread"] is True
    assert diagnostic["errno"] == 13
    assert diagnostic["winerror"] == 5
    assert diagnostic["temporary"]["exists"] is True
    assert diagnostic["temporary"]["json_valid"] is True
    assert diagnostic["destination"]["exists"] is True
    assert diagnostic["destination"]["readable"] is True
    assert "old-private-value" not in rendered
    assert "new-private-value" not in rendered
    assert str(tmp_path) not in rendered
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "state": "old-private-value",
    }
    assert list(store.attempts_dir.glob("*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_windows_incompatible_reader_produces_same_replace_error_class(
    tmp_path,
):
    store = ToolAttemptStore(tmp_path / "tool-attempts")
    store._ensure_dirs()
    destination = store.attempts_dir / "attempt_reader_lock.json"
    destination.write_text('{"revision":1}', encoding="utf-8")

    with destination.open("r", encoding="utf-8"):
        with pytest.raises(PermissionError) as caught:
            with store._store_lock():
                store._write_atomic(destination, {"revision": 2})

    assert caught.value.winerror == 5
    assert caught.value.osia_durable_diagnostic["winerror"] == 5
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "revision": 1,
    }
    assert list(store.attempts_dir.glob("*.tmp")) == []
