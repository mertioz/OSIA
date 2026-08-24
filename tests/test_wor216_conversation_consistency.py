"""Focused WOR-216 canonical PC/Mobile conversation consistency proofs."""

from __future__ import annotations

import asyncio
import inspect
import multiprocessing
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import conversations
from osia import main as desktop
from osia.conversation_context import build_context
from osia.server import server
from tools import wor216_isolated_mobile_gate as physical_gate


def _append_worker(
    store_dir: str,
    conv_id: str,
    owner: str,
    count: int,
    barrier,
    results,
) -> None:
    from osia import conversations as child_conversations

    child_conversations.CONVERSATIONS_DIR = Path(store_dir)
    try:
        barrier.wait(timeout=15)
        for index in range(count):
            receipt = child_conversations.commit_message(
                conv_id,
                "user",
                f"{owner}-{index}",
                metadata={
                    "_message_id": f"{owner}-message-{index}",
                    "_operation_id": f"{owner}-operation-{index}",
                },
            )
            if not receipt or not receipt["committed"]:
                raise AssertionError("append was not committed")
        results.put((owner, "ok"))
    except BaseException as exc:  # pragma: no cover - child failure reporting
        results.put((owner, type(exc).__name__))


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    monkeypatch.setattr(desktop, "_notify_mobile_conversation_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(server.conversations, "CONVERSATIONS_DIR", tmp_path)
    server._WS_CLIENTS.clear()
    server._LAST_BROADCAST_REVISIONS.clear()
    server._CONVERSATION_BROADCAST_LOCK = None
    yield tmp_path
    server._WS_CLIENTS.clear()
    server._LAST_BROADCAST_REVISIONS.clear()
    server._CONVERSATION_BROADCAST_LOCK = None


def _turn_metadata(prefix: str) -> dict:
    return {
        "_conversation_id": "conv",
        "_request_id": f"request-{prefix}",
        "_client_request_id": f"request-{prefix}",
        "_turn_id": f"turn-{prefix}",
        "_message_id": f"message-{prefix}",
        "_operation_id": f"message-{prefix}",
    }


def test_release_reinstall_targets_only_verified_official_apk(
    tmp_path, monkeypatch,
):
    apk = tmp_path / "app-release.apk"
    apk.write_bytes(b"verified-official-release")
    release = physical_gate.VerifiedReleaseApk(
        path=apk.resolve(),
        sha256=physical_gate._sha256(apk),
        version_name="1.2.3",
        version_code="42",
        certificate_sha256="a" * 64,
    )
    calls = []

    def fake_adb(_adb, *args):
        calls.append(args)
        return "Success"

    monkeypatch.setattr(physical_gate, "_adb_output", fake_adb)
    monkeypatch.setattr(
        physical_gate,
        "_installed_release_version",
        lambda _adb: ("1.2.3", "42"),
    )

    physical_gate._reinstall_verified_release(Path("adb.exe"), release)

    assert calls == [("install", "-r", str(apk.resolve()))]


def test_release_reinstall_rejects_changed_apk_before_adb(tmp_path, monkeypatch):
    apk = tmp_path / "app-release.apk"
    apk.write_bytes(b"verified-official-release")
    release = physical_gate.VerifiedReleaseApk(
        path=apk.resolve(),
        sha256=physical_gate._sha256(apk),
        version_name="1.2.3",
        version_code="42",
        certificate_sha256="b" * 64,
    )
    apk.write_bytes(b"changed-after-verification")
    calls = []
    monkeypatch.setattr(
        physical_gate,
        "_adb_output",
        lambda _adb, *args: calls.append(args),
    )

    with pytest.raises(
        physical_gate.GateError,
        match="bytes changed before install",
    ):
        physical_gate._reinstall_verified_release(Path("adb.exe"), release)

    assert calls == []


def test_physical_command_is_official_release_build_only():
    command = physical_gate._flutter_release_test_build_command(
        Path("flutter.bat"),
        defines=Path("temporary-defines.json"),
    )

    assert command[2:4] == ["build", "apk"]
    assert "--release" in command
    assert "--flavor" not in command
    assert "--debug" not in command
    assert "test" not in command


def test_physical_gate_sibling_repo_defaults_are_cross_platform():
    args = physical_gate._parser().parse_args([])
    workspace_root = Path(physical_gate.__file__).resolve().parents[2]

    assert Path(args.mobile_repo) == workspace_root / "osia-mobile"
    assert Path(args.worker_repo) == workspace_root / "osia-worker"


def test_android_power_guard_restores_stay_awake_and_screen_timeout(monkeypatch):
    settings = {
        ("global", "stay_on_while_plugged_in"): "0",
        ("system", "screen_off_timeout"): "30000",
    }

    def fake_adb(_adb, *args):
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake\nmIsPowered=true\nmPlugType=2\n"
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return "showing=false\nscreenState=SCREEN_STATE_ON\n"
        if args[:3] == ("shell", "dumpsys", "display"):
            return "mScreenState=ON\n"
        if args[:3] == ("shell", "settings", "get"):
            return settings[(args[3], args[4])]
        if args[:3] == ("shell", "settings", "put"):
            settings[(args[3], args[4])] = args[5]
            return ""
        if args[:3] == ("shell", "settings", "delete"):
            settings[(args[3], args[4])] = "null"
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(physical_gate, "_adb_output", fake_adb)
    guard = physical_gate._AndroidPowerGuard(Path("adb.exe"))

    guard.enable()
    assert settings[("global", "stay_on_while_plugged_in")] == "2"
    assert settings[("system", "screen_off_timeout")] == "2147483647"
    assert guard.guarded == {
        "interactive": True,
        "screen_on": True,
        "secure_keyguard_showing": False,
        "usb_powered": True,
        "stay_on_while_plugged_in": "2",
        "screen_off_timeout": "2147483647",
    }

    guard.restore()
    assert settings == {
        ("global", "stay_on_while_plugged_in"): "0",
        ("system", "screen_off_timeout"): "30000",
    }
    assert guard.restored == {
        "stay_on_while_plugged_in": "0",
        "screen_off_timeout": "30000",
    }


def test_two_processes_preserve_every_append_and_total_order(isolated_store):
    conv = conversations.create_conversation()
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_append_worker,
            args=(str(isolated_store), conv["id"], owner, 12, barrier, results),
        )
        for owner in ("desktop", "server")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0

    outcomes = []
    for _ in workers:
        try:
            outcomes.append(results.get(timeout=5))
        except queue.Empty:
            pytest.fail("worker produced no result")
    assert sorted(outcomes) == [("desktop", "ok"), ("server", "ok")]

    stored = conversations.get_conversation(conv["id"])
    assert stored is not None
    assert len(stored["messages"]) == 24
    assert {item["content"] for item in stored["messages"]} == {
        f"{owner}-{index}"
        for owner in ("desktop", "server")
        for index in range(12)
    }
    sequences = [item["_sequence"] for item in stored["messages"]]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert stored["revision"] == conv["revision"] + 24
    assert not list(isolated_store.glob(".*.tmp"))


def test_operation_replay_is_idempotent_without_revision_bump(isolated_store):
    conv = conversations.create_conversation()
    metadata = _turn_metadata("user")
    first = conversations.commit_message(
        conv["id"], "user", "durable question", metadata=metadata,
    )
    second = conversations.commit_message(
        conv["id"], "user", "durable question", metadata=metadata,
    )

    assert first and first["committed"] is True
    assert second and second["committed"] is False
    assert second["revision"] == first["revision"]
    assert len(second["conversation"]["messages"]) == 1

    assert conversations.rename_conversation(conv["id"], "Stable title")
    rename_revision = conversations.get_conversation(conv["id"])["revision"]
    assert conversations.rename_conversation(conv["id"], "Stable title")
    assert conversations.get_conversation(conv["id"])["revision"] == rename_revision


def test_stale_completion_is_explicit_and_preserves_both_users(isolated_store):
    conv = conversations.create_conversation()
    user_a = _turn_metadata("a-user")
    user_a.update({
        "_request_id": "request-a",
        "_client_request_id": "request-a",
        "_turn_id": "turn-a",
        "_artifact_refs": [{"artifact_id": "doc_" + "a" * 32}],
        "_attachments": [{"id": "attachment-a", "kind": "document"}],
    })
    user_b = _turn_metadata("b-user")
    user_b.update({
        "_request_id": "request-b",
        "_client_request_id": "request-b",
        "_turn_id": "turn-b",
    })
    conversations.commit_message(conv["id"], "user", "A", metadata=user_a)
    conversations.commit_message(conv["id"], "user", "B", metadata=user_b)

    assistant_a = dict(user_a)
    assistant_a.update({
        "_message_id": "message-a-assistant",
        "_operation_id": "message-a-assistant",
    })
    with pytest.raises(ValueError, match="stale assistant"):
        conversations.commit_message(
            conv["id"], "assistant", "answer A", metadata=assistant_a,
        )

    stored = conversations.get_conversation(conv["id"])
    assert stored is not None
    assert [item["content"] for item in stored["messages"]] == ["A", "B"]
    assert stored["messages"][0]["_artifact_refs"] == user_a["_artifact_refs"]
    assert stored["messages"][0]["_attachments"] == user_a["_attachments"]
    found = conversations.find_correlated_turn(conv["id"], "request-a")
    assert found and found["assistant_content"] is None


def test_desktop_user_is_durable_before_effect_callback(isolated_store):
    conv = conversations.create_conversation()
    observed = []

    def effect(meta):
        stored = conversations.get_conversation(conv["id"])
        observed.append(stored["messages"][-1]["_message_id"])
        return meta["user_message_id"]

    message_id = desktop._dispatch_after_persisted_user(
        conv["id"], "persist me first", effect,
    )
    assert observed == [message_id]


def test_desktop_notification_and_effect_both_observe_durable_state(
    isolated_store,
    monkeypatch,
):
    conv = conversations.create_conversation()
    observed = []

    def notify(conv_id, **payload):
        stored = conversations.get_conversation(conv_id)
        observed.append(("broadcast", stored["revision"], payload["revision"]))

    def effect(_meta):
        stored = conversations.get_conversation(conv["id"])
        observed.append(("effect", stored["revision"], stored["revision"]))

    monkeypatch.setattr(desktop, "_notify_mobile_conversation_changed", notify)
    desktop._dispatch_after_persisted_user(conv["id"], "ordered", effect)
    assert [kind for kind, _stored, _sent in observed] == ["broadcast", "effect"]
    assert all(stored == sent == 2 for _kind, stored, sent in observed)


def test_desktop_persistence_failure_prevents_effect(isolated_store, monkeypatch):
    conv = conversations.create_conversation()
    called = False

    def fail_commit(*_args, **_kwargs):
        raise OSError("synthetic persistence failure")

    def effect(_meta):
        nonlocal called
        called = True

    monkeypatch.setattr(conversations, "commit_message", fail_commit)
    with pytest.raises(OSError):
        desktop._dispatch_after_persisted_user(conv["id"], "never sent", effect)
    assert called is False


def test_crash_after_acceptance_replays_without_duplicate_then_completes(isolated_store):
    conv = conversations.create_conversation()
    meta = desktop._new_desktop_turn_meta(conv["id"])
    desktop._persist_desktop_user_turn(conv["id"], "question", turn_meta=meta)
    revision_after_accept = conversations.get_conversation(conv["id"])["revision"]

    desktop._persist_desktop_user_turn(conv["id"], "question", turn_meta=meta)
    assert conversations.get_conversation(conv["id"])["revision"] == revision_after_accept

    agent = SimpleNamespace(
        messages=[
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
        turn_context=None,
        last_run_artifacts=[],
        terminality_result=lambda _turn_id: {
            "terminal_state": "success_verified",
            "reservation_id": "reservation-crash-replay",
            **{
                key: meta[key]
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            },
        },
    )
    desktop._persist_agent_delta(conv["id"], agent, 0, turn_meta=meta)
    stored = conversations.get_conversation(conv["id"])
    assert [(item["role"], item["content"]) for item in stored["messages"]] == [
        ("user", "question"),
        ("assistant", "answer"),
    ]


def test_assistant_completion_preserves_concurrent_rename(isolated_store):
    conv = conversations.create_conversation()
    meta = desktop._new_desktop_turn_meta(conv["id"])
    desktop._persist_desktop_user_turn(conv["id"], "question", turn_meta=meta)
    assert conversations.rename_conversation(conv["id"], "Concurrent title")
    agent = SimpleNamespace(
        messages=[
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
        turn_context=None,
        last_run_artifacts=[],
        terminality_result=lambda _turn_id: {
            "terminal_state": "success_verified",
            "reservation_id": "reservation-concurrent-rename",
            **{
                key: meta[key]
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            },
        },
    )

    desktop._persist_agent_delta(conv["id"], agent, 0, turn_meta=meta)
    stored = conversations.get_conversation(conv["id"])
    assert stored["title"] == "Concurrent title"
    assert [item["content"] for item in stored["messages"]] == [
        "question", "answer",
    ]


def test_mobile_origin_uses_canonical_ids_and_retry_is_one_message(isolated_store):
    conv = conversations.create_conversation()
    meta = server._new_turn_meta(conv["id"], "mobile-request")
    meta["client_content_hash"] = "synthetic-hash"
    assert server._persist_accepted_user_message(conv["id"], "mobile", meta)
    first_receipt = meta["_user_commit"]
    assert first_receipt["committed"] is True
    accepted = server._turn_accepted_frame(meta)
    assert accepted["conversation_id"] == conv["id"]
    assert accepted["request_id"] == "mobile-request"
    assert accepted["message_id"] == meta["user_message_id"]

    assert server._persist_accepted_user_message(conv["id"], "mobile", meta)
    assert meta["_user_commit"]["committed"] is False
    stored = conversations.get_conversation(conv["id"])
    assert len(stored["messages"]) == 1


def test_context_builder_receives_canonical_commit_order(isolated_store):
    conv = conversations.create_conversation()
    for role, content in (
        ("user", "first"),
        ("assistant", "second"),
        ("user", "third"),
    ):
        conversations.commit_message(conv["id"], role, content)
    stored = conversations.get_conversation(conv["id"])
    result = build_context(
        stored["messages"],
        provider="deepseek",
        model="deepseek-v4-flash",
        conversation_id=conv["id"],
        turn_id="context-turn",
    )
    assert [item["content"] for item in result.messages] == [
        "first", "second", "third",
    ]


class _FakeChannel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def test_canonical_broadcast_is_durable_monotonic_and_duplicate_safe(isolated_store):
    conv = conversations.create_conversation()
    channel = _FakeChannel()
    server._WS_CLIENTS.add(channel)
    first = conversations.commit_message(
        conv["id"], "user", "question", metadata=_turn_metadata("broadcast"),
    )

    assert asyncio.run(server._broadcast_committed_receipt(conv["id"], first))
    history_frames = [f for f in channel.frames if f.get("type") == "conversation_history"]
    assert len(history_frames) == 1
    assert history_frames[0]["revision"] == first["revision"]
    assert history_frames[0]["operation_id"] == first["operation_id"]
    assert history_frames[0]["messages"][0]["message_id"] == first["message_id"]

    before = len(channel.frames)
    assert not asyncio.run(server._broadcast_committed_receipt(conv["id"], first))
    assert len(channel.frames) == before

    newer = conversations.commit_message(conv["id"], "assistant", "answer")
    assert asyncio.run(
        server._broadcast_canonical_conversation_change(
            conv["id"], revision=first["revision"],
        )
    )
    latest = [f for f in channel.frames if f.get("type") == "conversation_history"][-1]
    assert latest["revision"] == newer["revision"]
    assert [item["content"] for item in latest["messages"]] == ["question", "answer"]


def test_source_and_packaged_imports_share_one_conversation_authority():
    assert Path(inspect.getfile(conversations)).resolve().name == "conversations.py"
    assert server.conversations is conversations
    assert desktop.conversations is conversations
