"""Authenticated /ws claim scopes; no live providers or physical actions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading

import pytest

from osia.durable_transport import DurableTransportStore, InboxDisposition
from osia.server import server
from osia import conversations
from osia.turn_terminality import live_process_identity


class Socket:
    def __init__(self, messages, close_error=None):
        self.messages = messages
        self.sent = []
        self.close_error = close_error

    async def accept(self):
        pass

    async def send_json(self, frame):
        self.sent.append(frame)

    async def close(self, **kwargs):
        pass

    async def iter_text(self):
        for message in self.messages:
            yield json.dumps(message)
        if self.close_error:
            raise self.close_error


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    store = DurableTransportStore(tmp_path / "transport")
    from osia.tool_attempts import ToolAttemptStore
    store._test_attempt_creations = []
    def forbidden_attempt(*args, **kwargs):
        store._test_attempt_creations.append(1)
        raise AssertionError("no ToolAttempt may be created by these no-tool cases")
    monkeypatch.setattr(ToolAttemptStore, "create_attempt", forbidden_attempt)
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", store)
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server, "ATTACHMENT_UPLOAD_DIR", tmp_path / "attachments")
    monkeypatch.setattr(server, "MOBILE_TOKEN", "synthetic")
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(server, "_build_config_snapshot", lambda: {})
    monkeypatch.setattr(server, "_CONVERSATION_BROADCAST_LOCK", None)
    for name in ("_ACTIVE_TURNS_BY_CONV", "_ACTIVE_TURN_META_BY_CONV",
                 "_ACTIVE_CONV_BY_REQUEST", "_WS_CLIENTS", "_LAST_BROADCAST_REVISIONS"):
        monkeypatch.setattr(server, name, {} if name != "_WS_CLIENTS" else set())
    monkeypatch.setattr(server, "_DETACHED_TURNS", set())
    monkeypatch.setattr(server, "_DETACHED_BY_CONV", {})
    return store


def text(request="request_test", **fields):
    return {"type": "text", "content": "Reply exactly with: WOR305_WS_OK",
            "request_id": request, "tts_enabled": False, **fields}


def record(store, message):
    cid, _ = server._durable_command_identity("text", message)
    return store._read(store._inbox_path(cid))


async def drive(socket):
    await server.websocket_endpoint(socket)
    tasks = list(server._DETACHED_TURNS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


def no_execution(store):
    assert not store._test_attempt_creations
    assert not list(store.streams_dir.glob("*.json"))
    assert not any(json.loads(p.read_text()).get("messages")
                   for p in conversations.CONVERSATIONS_DIR.glob("*.json"))


def _stub_model_catalogue(monkeypatch):
    from osia import mobile_model_catalogue

    model = {
        "id": "deepseek-flash",
        "providerId": "deepseek",
        "name": "DeepSeek Flash",
        "reasoning": {"options": [], "default": "medium"},
    }
    catalogue = {
        "version": 1,
        "providers": [{"providerId": "deepseek", "models": [model]}],
        "models": [model],
        "current": "deepseek-flash",
        "currentProvider": "deepseek",
        "account": {
            "available": False,
            "connected": False,
            "runtimeAvailable": False,
            "models": [],
        },
        "inventory": [model],
    }
    monkeypatch.setattr(
        mobile_model_catalogue, "build_catalogue",
        lambda **_kwargs: catalogue,
    )
    monkeypatch.setattr(
        mobile_model_catalogue, "request_remote_refresh",
        lambda _callback: None,
    )
    return catalogue


def test_authenticated_get_models_is_non_durable_and_repeatable(
    isolated, monkeypatch,
):
    expected = _stub_model_catalogue(monkeypatch)
    socket = Socket([
        {"type": "auth", "token": "synthetic"},
        {"type": "get_models"},
        {"type": "get_models"},
    ])

    asyncio.run(drive(socket))

    catalogues = [
        frame for frame in socket.sent if frame.get("type") == "models_list"
    ]
    assert len(catalogues) == 2
    for frame in catalogues:
        assert frame["current"] == expected["current"]
        assert frame["current_provider"] == expected["currentProvider"]
        assert frame["models"] == expected["models"]
        assert frame["catalogue"]["providers"] == expected["providers"]
    assert isolated.list_commands() == []
    no_execution(isolated)


def test_get_models_then_text_on_same_socket_preserves_durable_turn(
    isolated, monkeypatch,
):
    _stub_model_catalogue(monkeypatch)
    request = "request_after_get_models"
    message = {
        **text(request),
        "content": "Reply exactly with: WOR305_GET_MODELS_OK",
    }
    reply = "WOR305_GET_MODELS_OK"
    stages = []

    async def worker(out, _value, history, conv, *, turn_meta, **_kwargs):
        saved = record(isolated, message)
        stages.append(saved["state"])
        assert saved["state"] == "running"
        response = server._turn_event_frame(
            turn_meta,
            "response",
            message_id=turn_meta["assistant_message_id"],
            payload={"content": reply},
        )
        terminal = server._turn_terminal_frame(
            turn_meta, {"status": "done"},
        )
        assert server._persist_completed_assistant_message(
            conv, reply, turn_meta, response, terminal,
        )
        await out.send_json(response)
        await out.send_json(terminal)
        return history, conv

    monkeypatch.setattr(server, "_run_agent_and_respond", worker)
    socket = Socket([
        {"type": "auth", "token": "synthetic"},
        {"type": "get_models"},
        message,
        {"type": "get_models"},
    ])

    asyncio.run(drive(socket))

    saved = record(isolated, message)
    assert saved["state"] == "completed"
    assert saved["result"]["state"] == "turn_completed"
    assert saved["claimed_at"] > 0
    assert saved["acceptance_started_at"] > 0
    assert saved["turn_accepted_at"] > 0
    assert saved["completed_at"] >= saved["turn_accepted_at"]
    assert stages == ["running"]
    assert any(frame.get("type") == "turn_accepted" for frame in socket.sent)
    assert any(
        frame.get("type") == "response" and frame.get("content") == reply
        for frame in socket.sent
    )
    assert sum(
        frame.get("type") == "models_list" for frame in socket.sent
    ) == 2
    assert len(isolated.list_commands()) == 1
    assert not isolated._test_attempt_creations


def test_non_durable_control_and_unknown_message_do_not_break_socket(
    isolated, monkeypatch,
):
    _stub_model_catalogue(monkeypatch)
    socket = Socket([
        {"type": "auth", "token": "synthetic"},
        {"type": "set_model", "provider": "", "model": ""},
        {"type": "unknown_control_for_test"},
        {"type": "get_models"},
    ])

    asyncio.run(drive(socket))

    assert any(
        frame.get("type") == "status" and frame.get("content") == "error"
        for frame in socket.sent
    )
    assert any(frame.get("type") == "models_list" for frame in socket.sent)
    assert isolated.list_commands() == []
    no_execution(isolated)


def test_non_durable_message_categories_do_not_claim_commands():
    assert server._durable_command_identity("get_models", {}) is None
    assert server._durable_command_identity("event_ack", {}) is None
    assert server._durable_command_identity("image", {"content": "image"}) is None
    assert server._durable_command_identity("unknown", {}) is None
    assert server._durable_command_identity("text", text()) is not None
    assert server._durable_command_identity(
        "audio", {"request_id": "audio_request", "data": "audio"},
    ) is not None
    assert server._durable_command_identity(
        "cancel_active_work",
        {
            "schema_version": 1,
            "scope": "turn",
            "run_id": "run_cancel_test",
        },
    ) is not None


def test_catalogue_request_does_not_touch_active_turn_ownership(
    isolated, monkeypatch,
):
    _stub_model_catalogue(monkeypatch)
    active_task = object()
    server._ACTIVE_TURNS_BY_CONV["conversation_active"] = active_task
    before = dict(server._ACTIVE_TURNS_BY_CONV)
    supersede_calls = []

    async def forbidden_supersede(*_args, **_kwargs):
        supersede_calls.append(True)
        raise AssertionError("read-only catalogue request superseded a turn")

    monkeypatch.setattr(
        server, "_supersede_active_conversation_turn", forbidden_supersede,
    )
    socket = Socket([
        {"type": "auth", "token": "synthetic"},
        {"type": "get_models"},
    ])

    asyncio.run(drive(socket))

    assert server._ACTIVE_TURNS_BY_CONV == before
    assert server._ACTIVE_TURNS_BY_CONV["conversation_active"] is active_task
    assert supersede_calls == []
    assert isolated.list_commands() == []


@pytest.mark.parametrize("failure", ["immediate", "turn_creation", "disconnect", "cancel", "keepalive"])
def test_pre_turn_failure_settles(isolated, monkeypatch, failure):
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("provider/tool execution must not start")
    monkeypatch.setattr(server, "_run_agent_and_respond", forbidden)
    exc = (asyncio.CancelledError() if failure == "cancel" else
           server.WebSocketDisconnect(code=1006) if failure == "disconnect" else
           server.WebSocketDisconnect(code=1011) if failure == "keepalive" else
           RuntimeError("injected pre-turn failure"))
    symbol = "_new_turn_meta" if failure == "turn_creation" else "_turn_input_identity"
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr(server, symbol, fail)
    message = text()
    socket = Socket([{"type": "auth", "token": "synthetic"}, message])
    async def run():
        try:
            await drive(socket)
        except asyncio.CancelledError:
            assert failure == "cancel"
    asyncio.run(run())
    saved = record(isolated, message)
    assert saved["state"] == "completed"
    assert saved["completed_at"] > 0
    assert saved["result"]["state"] == "failed_before_execution"
    assert saved["result"]["accepted"] is False
    assert not calls
    no_execution(isolated)


@pytest.mark.parametrize("writer_failure", [False, True])
def test_normal_request_completes_after_final_and_detaches(isolated, monkeypatch, writer_failure):
    stages = []
    async def worker(out, value, history, conv, *, turn_meta, **kwargs):
        saved = record(isolated, message)
        stages.append(saved["state"])
        assert saved["state"] == "running"
        assert saved["execution_binding"]["turn_id"] == turn_meta["turn_id"]
        response = server._turn_event_frame(turn_meta, "response",
            message_id=turn_meta["assistant_message_id"], payload={"content": "WOR305_WS_OK"})
        terminal = server._turn_terminal_frame(turn_meta, {"status": "done"})
        assert server._persist_completed_assistant_message(
            conv, "WOR305_WS_OK", turn_meta, response, terminal)
        await out.send_json(response)
        await out.send_json(terminal)
        return history, conv
    monkeypatch.setattr(server, "_run_agent_and_respond", worker)
    message = text()
    class ResultSocket(Socket):
        async def send_json(self, frame):
            await super().send_json(frame)
            if writer_failure and frame.get("type") == "turn_accepted":
                raise OSError("transport closed after dispatch")
    socket = ResultSocket([{"type": "auth", "token": "synthetic"}, message])
    asyncio.run(drive(socket))
    assert stages == ["running"]
    saved = record(isolated, message)
    assert saved["state"] == "completed"
    assert saved["result"]["state"] == "turn_completed"
    assert saved["completed_at"] >= saved["turn_accepted_at"] >= saved["acceptance_started_at"] >= saved["claimed_at"]
    assert saved["owner_id"].startswith("mobile-connection:")
    assert any(f["type"] == "turn_accepted" for f in socket.sent)
    isolated.settle_unstarted_command(saved["command_id"], execution_epoch=saved["execution_epoch"])
    assert record(isolated, message) == saved


def test_cancel_during_claim_disk_write(isolated, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = isolated.claim_command
    def delayed(**kwargs):
        entered.set()
        release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(isolated, "claim_command", delayed)
    message = text()
    async def run():
        task = asyncio.create_task(drive(Socket([{"type": "auth", "token": "synthetic"}, message])))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert record(isolated, message)["result"]["state"] == "failed_before_execution"
    no_execution(isolated)


def test_owner_loss_after_acceptance_is_uncertain(isolated, monkeypatch):
    async def fail_broadcast(*args, **kwargs):
        raise RuntimeError("after acceptance write, before dispatch")
    monkeypatch.setattr(server, "_broadcast_committed_receipt", fail_broadcast)
    message = text()
    asyncio.run(drive(Socket([{"type": "auth", "token": "synthetic"}, message])))
    saved = record(isolated, message)
    assert saved["state"] == "completed"
    assert saved["result"]["state"] == "outcome_unknown"
    duplicate = isolated.claim_command(command_id=saved["command_id"], command_type="text", binding=saved["binding"])
    assert duplicate.disposition is InboxDisposition.DUPLICATE_COMPLETED
    assert saved["execution_binding"]["turn_id"]
    assert any(json.loads(p.read_text()).get("messages")
               for p in conversations.CONVERSATIONS_DIR.glob("*.json"))


def test_two_owners_one_claim_and_stale_epoch_fenced(isolated):
    binding = {"request_id": "same"}
    def claim(index):
        # Independent store instances exercise the OS file lock, not one RLock.
        return DurableTransportStore(isolated.root).claim_command(command_id="same", command_type="text", binding=binding,
                                      execution_epoch=f"epoch_{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert sum(c.disposition is InboxDisposition.CLAIMED for c in claims) == 1
    winner = next(c for c in claims if c.disposition is InboxDisposition.CLAIMED)
    with pytest.raises(ValueError, match="stale_execution_epoch"):
        isolated.complete_command("same", execution_epoch="epoch_stale", result={})
    assert isolated.settle_unstarted_command("same", execution_epoch=winner.execution_epoch)
    unrelated = isolated.claim_command(command_id="unrelated", command_type="text", binding={"request_id": "new"})
    assert unrelated.disposition is InboxDisposition.CLAIMED


def test_dead_owner_recovery_never_replays_running_or_legacy(isolated, monkeypatch):
    for cid in ("pre", "post", "live", "legacy"):
        claim = isolated.claim_command(command_id=cid, command_type="text", binding={"request_id": cid},
                owner_pid=123 if cid != "live" else os.getpid(), owner_process_identity="identity")
        if cid == "post":
            isolated.mark_command_running(cid, execution_epoch=claim.execution_epoch,
                                          execution_binding={"turn_id": "turn", "request_id": cid})
        if cid == "legacy":
            path = isolated._inbox_path(cid)
            raw = isolated._read(path)
            raw.pop("execution_boundary_version")
            isolated._write_atomic(path, raw)
    monkeypatch.setattr("osia.turn_terminality.live_process_identity",
                        lambda pid: (pid == os.getpid(), "identity" if pid == os.getpid() else ""))
    assert isolated.recover_unstarted_commands() == ["pre"]
    assert isolated.recover_unstarted_commands() == []
    assert isolated._read(isolated._inbox_path("post"))["state"] == "running"
    assert isolated._read(isolated._inbox_path("live"))["state"] == "claimed"
    assert isolated._read(isolated._inbox_path("legacy"))["state"] == "claimed"


def test_duplicate_socket_cannot_dispatch(isolated, monkeypatch):
    message = text()
    cid, binding = server._durable_command_identity("text", message)
    isolated.claim_command(command_id=cid, command_type="text", binding=binding)
    def forbidden(*args, **kwargs):
        raise AssertionError("duplicate must not create a turn")
    monkeypatch.setattr(server, "_new_turn_meta", forbidden)
    socket = Socket([{"type": "auth", "token": "synthetic"}, message])
    asyncio.run(drive(socket))
    assert any(f.get("state") == "duplicate_in_progress" for f in socket.sent)
    assert record(isolated, message)["state"] == "claimed"
    no_execution(isolated)


def test_post_dispatch_cancellation_never_duplicates_effect(isolated, monkeypatch):
    effects = []
    async def worker(*args, **kwargs):
        effects.append("physical boundary reached")
        raise asyncio.CancelledError()
    monkeypatch.setattr(server, "_run_agent_and_respond", worker)
    message = text()
    asyncio.run(drive(Socket([{"type": "auth", "token": "synthetic"}, message])))
    saved = record(isolated, message)
    assert saved["state"] == "completed"
    assert saved["result"]["state"] == "outcome_unknown"
    asyncio.run(drive(Socket([{"type": "auth", "token": "synthetic"}, message])))
    assert len(effects) == 1
    assert record(isolated, message) == saved


def test_acceptance_write_rejection_has_no_provider(isolated, monkeypatch):
    calls = []
    def fail_commit(*args, **kwargs):
        raise OSError("injected persistence failure")
    monkeypatch.setattr(conversations, "commit_message", fail_commit)
    monkeypatch.setattr(server, "_run_agent_and_respond", lambda *args, **kwargs: calls.append(1))
    message = text()
    asyncio.run(drive(Socket([{"type": "auth", "token": "synthetic"}, message])))
    saved = record(isolated, message)
    assert saved["state"] == "completed"
    assert saved["result"]["accepted"] is False
    assert not calls
    no_execution(isolated)


def test_validation_continue_settles_before_next_receive(isolated, monkeypatch):
    message = text(turn_directives={"invalid": True})
    class ObservedSocket(Socket):
        async def iter_text(self):
            yield json.dumps({"type": "auth", "token": "synthetic"})
            yield json.dumps(message)
            assert record(isolated, message)["state"] == "completed"
    asyncio.run(drive(ObservedSocket([])))
    assert record(isolated, message)["result"]["accepted"] is False


def test_two_authenticated_sockets_only_one_worker(isolated, monkeypatch):
    calls = []
    async def worker(out, value, history, conv, *, turn_meta, **kwargs):
        calls.append(turn_meta["turn_id"])
        await asyncio.sleep(0)
        return history, conv
    monkeypatch.setattr(server, "_run_agent_and_respond", worker)
    message = text()
    async def run():
        await asyncio.gather(*(drive(Socket([{"type": "auth", "token": "synthetic"}, message]))
                               for _ in range(2)))
    asyncio.run(run())
    assert len(calls) == 1


def test_stale_owner_cannot_cross_fence_after_settlement(isolated):
    claim = isolated.claim_command(command_id="stale", command_type="text", binding={})
    isolated.settle_unstarted_command("stale", execution_epoch=claim.execution_epoch)
    with pytest.raises(ValueError, match="command_already_completed"):
        isolated.mark_command_running("stale", execution_epoch=claim.execution_epoch,
                                      execution_binding={"turn_id": "turn"})
    before = isolated._read(isolated._inbox_path("stale"))
    isolated.complete_command("stale", execution_epoch=claim.execution_epoch,
                              result={"state": "success"})
    assert isolated._read(isolated._inbox_path("stale")) == before


def test_indeterminate_process_is_not_dead_proof(isolated, monkeypatch):
    isolated.claim_command(command_id="uncertain_owner", command_type="text", binding={},
                          owner_pid=123, owner_process_identity="identity")
    monkeypatch.setattr("osia.turn_terminality.live_process_identity", lambda pid: (True, ""))
    assert isolated.recover_unstarted_commands() == []
    assert isolated._read(isolated._inbox_path("uncertain_owner"))["state"] == "claimed"


def test_claim_rejection_does_not_close_socket(isolated, monkeypatch):
    def reject(**kwargs):
        raise ValueError("inbox validation rejected")
    monkeypatch.setattr(isolated, "claim_command", reject)
    socket = Socket([{"type": "auth", "token": "synthetic"}, text(),
                     {"type": "auth", "token": "synthetic"}])
    asyncio.run(drive(socket))
    assert sum(f["type"] == "event_transport_context" for f in socket.sent) == 2
    assert any(f.get("reason_code") == "inbox_unavailable" for f in socket.sent)
    no_execution(isolated)


def test_write_then_claim_receipt_failure_settles_exact_epoch(isolated, monkeypatch):
    original = isolated.claim_command
    def lost_receipt(**kwargs):
        original(**kwargs)
        raise OSError("claim returned no receipt after durable write")
    monkeypatch.setattr(isolated, "claim_command", lost_receipt)
    message = text()
    asyncio.run(drive(Socket([{"type": "auth", "token": "synthetic"}, message])))
    assert record(isolated, message)["result"]["state"] == "failed_before_execution"
    no_execution(isolated)


def test_writer_death_interrupts_parked_pre_turn_dispatch(isolated, monkeypatch):
    async def parked(channel, *args, **kwargs):
        await channel.send_json({"type": "status", "content": "injected_wait"})
        await asyncio.Event().wait()
    monkeypatch.setattr(server, "_send_existing_turn_status_for_request", parked)
    class DeadSocket(Socket):
        async def send_json(self, frame):
            if frame.get("content") == "injected_wait":
                raise OSError("socket closed before acceptance")
            await super().send_json(frame)
    message = text()
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(drive(DeadSocket([
                {"type": "auth", "token": "synthetic"}, message])), timeout=3)
    asyncio.run(run())
    assert record(isolated, message)["result"]["state"] == "failed_before_execution"
    no_execution(isolated)
