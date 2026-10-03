"""Turn cancellation/supersession keeps command and tool truth correlated."""

import asyncio
import json
import os
import threading

import pytest

from osia import conversations, tool_attempts
from osia.durable_transport import DurableTransportStore
from osia.server import server
from osia.tool_attempts import ToolAttemptCoordinator, ToolAttemptStore
from osia.turn_terminality import (
    TerminalState,
    TurnTerminalityController,
    load_persisted_terminal_result,
)


class Socket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, frame):
        self.sent.append(dict(frame))

    async def close(self, **_kwargs):
        pass

    async def iter_text(self):
        for message in self.messages:
            yield json.dumps(message)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    durable = DurableTransportStore(tmp_path / "transport")
    attempts = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", durable)
    monkeypatch.setattr(
        "osia.tool_attempts.get_tool_attempt_store", lambda: attempts,
    )
    monkeypatch.setattr(
        conversations, "CONVERSATIONS_DIR", tmp_path / "conversations",
    )
    monkeypatch.setattr(
        "osia.turn_terminality.CONFIG_DIR", str(tmp_path / "turnality"),
    )
    monkeypatch.setattr(server, "_TURN_EVENT_SEQUENCE_DIR", tmp_path / "turn-events")
    monkeypatch.setattr(server, "_TURN_EVENT_SEQUENCE_LOCK", threading.RLock())
    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server, "ATTACHMENT_UPLOAD_DIR", tmp_path / "attachments")
    monkeypatch.setattr(server, "MOBILE_TOKEN", "supersession-test-token")
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(server, "_build_config_snapshot", lambda: {})
    for name in (
        "_ACTIVE_TURNS_BY_CONV", "_ACTIVE_TURN_META_BY_CONV",
        "_ACTIVE_CONV_BY_REQUEST", "_POST_TURN_MEDIA_BY_CONV",
        "_POST_TURN_MEDIA_META_BY_CONV", "_POST_TURN_MEDIA_TASKS",
        "_WS_CLIENTS", "_DETACHED_TURNS", "_DETACHED_BY_CONV",
        "_PENDING_FINAL_PERSIST_TASKS",
    ):
        monkeypatch.setattr(
            server,
            name,
            set() if name in {"_WS_CLIENTS", "_DETACHED_TURNS", "_POST_TURN_MEDIA_TASKS", "_PENDING_FINAL_PERSIST_TASKS"} else {},
        )
    return durable, attempts, tmp_path


def _user_message(conversation_id, request_id, content):
    return {
        "type": "text",
        "content": content,
        "request_id": request_id,
        "conversation_id": conversation_id,
        "tts_enabled": False,
    }


def _accept_fixture_turn(store, conversation_id, request_id, content):
    message = _user_message(conversation_id, request_id, content)
    command_id, binding = server._durable_command_identity("text", message)
    claim = store.claim_command(
        command_id=command_id,
        command_type="text",
        binding=binding,
        owner_id="supersession-fixture",
        owner_pid=os.getpid(),
        owner_process_identity="supersession-fixture-process",
    )
    meta = server._new_turn_meta(conversation_id, request_id)
    meta["_original_user_text"] = content
    meta["_terminality_objective"] = content
    meta["_durable_command_id"] = command_id
    meta["_durable_command_epoch"] = claim.execution_epoch
    server._bind_pre_agent_cancellation_authority(meta)
    assert server._persist_accepted_user_message(conversation_id, content, meta)
    meta["persisted"] = True
    server._turn_accepted_frame(meta)
    return message, meta, command_id


def _command_record(store, command_id):
    return store._read(store._inbox_path(command_id))


def _stream(durable, meta):
    return durable.get_stream(meta["conversation_id"], meta["run_id"])


def _make_tool_attempt(attempts, meta, call_id, *, terminal_status=None, tmp_path=None):
    arguments = {"command": "synthetic harmless command"}
    created = attempts.create_attempt(
        conversation_id=meta["conversation_id"],
        turn_id=meta["turn_id"],
        request_id=meta["request_id"],
        tool_call_id=call_id,
        tool_name="run_command",
        provider_id=meta["provider_id"],
        model_id=meta["model_id"],
        turn_context={},
        argument_fingerprint=attempts.fingerprint_arguments(arguments),
        side_effect="unknown_high_risk",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        final_response_message_id=meta["assistant_message_id"],
    )
    attempt_id = created["tool_attempt_id"]
    attempts.mark_accepted(attempt_id)
    if terminal_status != "rejected_before_dispatch":
        attempts.mark_running(attempt_id)
        attempts.arm_owner_dispatch_boundary(attempt_id)
        _started, dispatched = attempts.claim_owner_dispatch(attempt_id)
        assert dispatched
    if terminal_status == "rejected_before_dispatch":
        attempts.request_cancellation(attempt_id)
    elif terminal_status:
        outcome, dispatch, reason = {
            "succeeded": ("success_unverified", "effect_confirmed", "fixture_success"),
            "failed_after_effect": ("command_completed", "effect_confirmed", "fixture_failure"),
            "unknown_after_start": ("outcome_unknown", "effect_unknown", "fixture_unknown"),
        }[terminal_status]
        facts = None
        if terminal_status in {"succeeded", "failed_after_effect"}:
            facts = {
                "kind": "process", "source": "local_subprocess",
                "command": arguments["command"],
                "cwd": str(tmp_path or os.getcwd()),
                "exit_code": 0 if terminal_status == "succeeded" else 7,
                "stdout": "", "stderr": "", "combined_output": None,
                "output_truncated": False,
            }
        attempts.record_terminal_result(
            attempt_id,
            status=terminal_status,
            terminal_outcome=outcome,
            dispatch_state=dispatch,
            process_facts=facts,
            reason_code=reason,
        )
    meta["_tool_attempt_id"] = attempt_id
    return attempt_id


async def _blocked_worker():
    await asyncio.Future()


async def _drive(socket):
    await server.websocket_endpoint(socket)
    pending = list(server._DETACHED_TURNS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def _persist_fake_final(conversation_id, meta, content):
    response = server._turn_event_frame(
        meta,
        "response",
        message_id=meta["assistant_message_id"],
        payload={"content": content},
    )
    terminal = server._turn_terminal_frame(meta, {
        "terminal_state": TerminalState.SUCCESS_VERIFIED.value,
        "reason_code": "selected_model_completed",
        "final_text": content,
    })
    assert server._persist_completed_assistant_message(
        conversation_id, content, meta, response, terminal,
    )
    return response, terminal


def _persist_normal_old_final(meta, content):
    controller = TurnTerminalityController(
        turn_id=meta["turn_id"],
        objective=meta["_terminality_objective"],
        required_postconditions=("old final committed",),
        execution_required=True,
    )
    binding = {
        key: str(meta.get(key) or "")
        for key in (
            "conversation_id", "request_id", "turn_id", "run_id",
            "provider_id", "model_id", "effective_reasoning",
        )
    }
    controller.set_execution_binding(binding)
    controller.prove_postcondition("old final committed", {"receipt": content})
    decision = controller.evaluate()
    assert decision.terminal_state == TerminalState.SUCCESS_VERIFIED.value
    assert controller.reserve_final(
        decision.terminal_state,
        reason_code=decision.reason_code,
        final_text=content,
    )
    return _persist_fake_final(meta["conversation_id"], meta, content)


@pytest.mark.parametrize("status,terminal_outcome,dispatch_state", [
    ("succeeded", "success_unverified", "effect_confirmed"),
    ("failed_after_effect", "command_completed", "effect_confirmed"),
    ("unknown_after_start", "outcome_unknown", "effect_unknown"),
])
def test_parent_cancellation_never_rewrites_terminal_tool_truth(
    isolated, status, terminal_outcome, dispatch_state,
):
    durable, attempts, tmp_path = isolated
    conversation = conversations.create_conversation()["id"]
    _message, meta, command_id = _accept_fixture_turn(
        durable, conversation, f"request_{status}", f"old {status} turn",
    )
    arguments = {"command": "synthetic harmless command"}
    created = attempts.create_attempt(
        conversation_id=conversation,
        turn_id=meta["turn_id"],
        request_id=meta["request_id"],
        tool_call_id=f"call_{status}",
        tool_name="run_command",
        provider_id=meta["provider_id"],
        model_id=meta["model_id"],
        turn_context={},
        argument_fingerprint=attempts.fingerprint_arguments(arguments),
        side_effect="unknown_high_risk",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        final_response_message_id=meta["assistant_message_id"],
    )
    attempt_id = created["tool_attempt_id"]
    attempts.mark_accepted(attempt_id)
    attempts.mark_running(attempt_id)
    attempts.arm_owner_dispatch_boundary(attempt_id)
    _started, dispatched = attempts.claim_owner_dispatch(attempt_id)
    assert dispatched
    facts = None
    if status in {"succeeded", "failed_after_effect"}:
        facts = {
            "kind": "process", "source": "local_subprocess",
            "command": arguments["command"], "cwd": str(tmp_path),
            "exit_code": 0 if status == "succeeded" else 7,
            "stdout": "", "stderr": "", "combined_output": None,
            "output_truncated": False,
        }
    attempts.record_terminal_result(
        attempt_id,
        status=status,
        terminal_outcome=terminal_outcome,
        dispatch_state=dispatch_state,
        process_facts=facts,
        reason_code=f"fixture_{status}",
    )
    before = attempts.get_attempt(attempt_id)
    meta["_tool_attempt_id"] = attempt_id
    owner = asyncio.run(_cancel_owner_for_test(meta, durable))

    after = attempts.get_attempt(attempt_id)
    assert after["status"] == before["status"]
    assert after["terminal_outcome"] == before["terminal_outcome"]
    assert after["dispatch_state"] == before["dispatch_state"]
    assert after.get("process_facts") == before.get("process_facts")
    assert owner is not None
    turn_state = TurnTerminalityController(
        turn_id=meta["turn_id"],
        objective=meta["_terminality_objective"],
        execution_required=True,
        storage_dir=tmp_path / "turnality" / "turn_terminality",
    ).snapshot()
    if status == "unknown_after_start":
        assert attempt_id in turn_state["unknown_effects"]
    else:
        assert "cancellation_possible_effect" not in turn_state["unknown_effects"]
        if status == "failed_after_effect":
            assert attempt_id not in turn_state["unknown_effects"]
        else:
            # Exit 0 can be success_unverified for the semantic postcondition;
            # preserve that existing uncertainty without adding cancellation
            # uncertainty.
            assert attempt_id in turn_state["unknown_effects"]
    parent = _command_record(durable, command_id)
    assert parent["state"] == "completed"
    assert parent["result"]["state"] == "superseded"
    assert "final_text" not in parent["result"]


@pytest.mark.parametrize(
    "attempt_state,expected_state,expected_effect_unknown",
    [
        ("rejected_before_dispatch", "cancelled_proven", False),
        ("running", "cancellation_requested", True),
    ],
)
def test_interruption_only_claims_what_the_tool_owner_proved(
    isolated, attempt_state, expected_state, expected_effect_unknown,
):
    durable, attempts, tmp_path = isolated
    conversation = conversations.create_conversation()["id"]
    _message, meta, command_id = _accept_fixture_turn(
        durable, conversation, f"request_{attempt_state}", "old interruptible turn",
    )
    attempt_id = _make_tool_attempt(
        attempts, meta, f"call_{attempt_state}",
        terminal_status=(
            "rejected_before_dispatch" if attempt_state == "rejected_before_dispatch"
            else None
        ),
    )
    worker_cancellation = []
    meta["_terminality_cancel"] = lambda turn_id, *, effect_possible=False: (
        worker_cancellation.append((turn_id, effect_possible)) or True
    )
    async def scenario():
        async def hold():
            await asyncio.Future()
        old_task = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation] = old_task
        server._ACTIVE_TURN_META_BY_CONV[conversation] = meta
        server._ACTIVE_CONV_BY_REQUEST[meta["request_id"]] = conversation
        result = await server._commit_canonical_chat_cancellation(
            meta, reason_code="turn_superseded",
            superseded_by_turn_id="turn_new",
        )
        await asyncio.sleep(0.02)
        await asyncio.gather(old_task, return_exceptions=True)
        return result
    result = asyncio.run(scenario())
    saved_attempt = attempts.get_attempt(attempt_id)
    assert result["state"] == "superseded"
    assert saved_attempt["status"] == expected_state
    turn_state = TurnTerminalityController(
        turn_id=meta["turn_id"],
        objective=meta["_terminality_objective"],
        execution_required=True,
        storage_dir=tmp_path / "turnality" / "turn_terminality",
    ).snapshot()
    assert turn_state["terminal_state"] == "user_cancelled"
    assert ("cancellation_possible_effect" in turn_state.get("unknown_effects", {})) is expected_effect_unknown
    assert _command_record(durable, command_id)["state"] == "completed"
    assert worker_cancellation == [(meta["turn_id"], expected_effect_unknown)]


def test_multiple_attempts_cancel_only_active_one_and_keep_terminal_one(isolated, tmp_path):
    durable, attempts, _ = isolated
    conversation = conversations.create_conversation()["id"]
    _message, meta, _command_id = _accept_fixture_turn(
        durable, conversation, "request_multiple_attempts", "old multi-step turn",
    )
    completed_id = _make_tool_attempt(
        attempts, meta, "call_completed", terminal_status="succeeded",
        tmp_path=tmp_path,
    )
    completed_before = attempts.get_attempt(completed_id)
    active_id = _make_tool_attempt(attempts, meta, "call_active")
    meta["_tool_attempt_id"] = active_id
    async def scenario():
        async def hold():
            await asyncio.Future()
        task = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation] = task
        server._ACTIVE_TURN_META_BY_CONV[conversation] = meta
        server._ACTIVE_CONV_BY_REQUEST[meta["request_id"]] = conversation
        await server._commit_canonical_chat_cancellation(
            meta, reason_code="turn_superseded",
            superseded_by_turn_id="turn_next",
        )
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
    assert attempts.get_attempt(completed_id) == completed_before
    assert attempts.get_attempt(active_id)["status"] == "cancellation_requested"


def test_tool_receipt_that_wins_during_cancel_keeps_its_process_facts(isolated, monkeypatch, tmp_path):
    durable, attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    _message, meta, command_id = _accept_fixture_turn(
        durable, conversation, "request_tool_race", "old process turn",
    )
    attempt_id = _make_tool_attempt(attempts, meta, "call_tool_race")
    facts = {
        "kind": "process", "source": "local_subprocess",
        "command": "synthetic harmless command", "cwd": str(tmp_path),
        "exit_code": 0, "stdout": "completed", "stderr": "",
        "combined_output": None, "output_truncated": False,
    }
    original_cancel = attempts.request_cancellation

    def complete_then_cancel(attempt_to_cancel):
        attempts.record_terminal_result(
            attempt_to_cancel,
            status="succeeded",
            terminal_outcome="success_unverified",
            dispatch_state="effect_confirmed",
            process_facts=facts,
            reason_code="process_completed_during_turn_cancel",
        )
        return original_cancel(attempt_to_cancel)

    monkeypatch.setattr(attempts, "request_cancellation", complete_then_cancel)
    async def scenario():
        async def hold():
            await asyncio.Future()
        owner = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation] = owner
        server._ACTIVE_TURN_META_BY_CONV[conversation] = meta
        server._ACTIVE_CONV_BY_REQUEST[meta["request_id"]] = conversation
        result = await server._commit_canonical_chat_cancellation(
            meta, reason_code="turn_superseded",
            superseded_by_turn_id="turn_after_process",
        )
        await asyncio.gather(owner, return_exceptions=True)
        return result
    result = asyncio.run(scenario())

    saved = attempts.get_attempt(attempt_id)
    assert result["state"] == "superseded"
    assert saved["status"] == "succeeded"
    assert saved["terminal_outcome"] == "success_unverified"
    assert saved["process_facts"] == facts
    assert _command_record(durable, command_id)["result"]["state"] == "superseded"


async def _cancel_owner_for_test(meta, durable):
    return await server._commit_canonical_chat_cancellation(
        meta,
        reason_code="turn_superseded",
        superseded_by_turn_id="turn_new_test",
    )


def test_explicit_websocket_cancel_routes_and_settles_both_commands(isolated):
    durable, _attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    old_message, old_meta, old_command_id = _accept_fixture_turn(
        durable, conversation, "request_old_control", "old waiting turn",
    )
    async def running_owner():
        await asyncio.Future()
    async def scenario():
        old_task = asyncio.create_task(running_owner())
        server._ACTIVE_TURNS_BY_CONV[conversation] = old_task
        server._ACTIVE_TURN_META_BY_CONV[conversation] = old_meta
        server._ACTIVE_CONV_BY_REQUEST[old_meta["request_id"]] = conversation
        cancel = {
            "type": "cancel_active_work", "schema_version": 1,
            "scope": "chat", "conversation_id": conversation,
            "request_id": old_meta["request_id"],
            "turn_id": old_meta["turn_id"], "run_id": old_meta["run_id"],
            "operation_id": "explicit-cancel-once",
        }
        socket = Socket([
            {"type": "auth", "token": "supersession-test-token"},
            cancel,
            cancel,
        ])
        await _drive(socket)
        await asyncio.gather(old_task, return_exceptions=True)
        return cancel, socket

    cancel, socket = asyncio.run(scenario())
    parent = _command_record(durable, old_command_id)
    control_id, _ = server._durable_command_identity("cancel_active_work", cancel)
    control = _command_record(durable, control_id)
    assert parent["state"] == control["state"] == "completed"
    assert parent["result"]["state"] == "cancelled"
    assert control["result"]["control"] == "cancel_active_work"
    assert control["result"]["accepted"] is True
    assert "final_text" not in parent["result"]
    busy_clear_frames = [
        frame for frame in socket.sent
        if frame.get("type") == "conversation_busy_changed"
        and frame.get("busy") is False
    ]
    assert len(busy_clear_frames) == 1
    busy_clear = busy_clear_frames[0]
    assert busy_clear["conversation_id"] == conversation
    assert busy_clear["request_id"] == old_meta["request_id"]
    assert busy_clear["turn_id"] == old_meta["turn_id"]
    assert busy_clear["run_id"] == old_meta["run_id"]
    assert any(frame.get("type") == "cancellation_control" and frame.get("state") == "cancelled"
               for frame in socket.sent)
    assert any(frame.get("type") == "command_ack" and frame.get("state") == "completed"
               for frame in socket.sent)
    assert any(frame.get("type") == "command_ack" and frame.get("state") == "duplicate_completed"
               for frame in socket.sent)
    terminal_frames = [
        frame for frame in durable.replay(
            conversation_id=conversation, run_id=old_meta["run_id"],
        )
        if frame.get("terminal") and frame.get("status") == "cancelled"
    ]
    assert len(terminal_frames) == 1
    replayed = durable.replay(
        conversation_id=conversation, run_id=old_meta["run_id"],
    )
    historical_busy_clear = [
        frame for frame in replayed
        if frame.get("type") == "conversation_busy_changed"
        and frame.get("busy") is False
    ]
    assert len(historical_busy_clear) == 1
    assert historical_busy_clear[0]["request_id"] == old_meta["request_id"]
    assert historical_busy_clear[0]["turn_id"] == old_meta["turn_id"]
    assert historical_busy_clear[0]["run_id"] == old_meta["run_id"]
    stream = _stream(durable, old_meta)
    assert stream["authority_state"] == "cancelled"
    assert stream["terminal_sequence"] == terminal_frames[0]["sequence"]


def test_malformed_correlated_cancel_does_not_cancel_another_turn(isolated):
    durable, _attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    _message, old_meta, old_command_id = _accept_fixture_turn(
        durable, conversation, "request_safe_cancel_target", "active target",
    )
    async def running_owner():
        await asyncio.Future()
    async def scenario():
        owner = asyncio.create_task(running_owner())
        server._ACTIVE_TURNS_BY_CONV[conversation] = owner
        server._ACTIVE_TURN_META_BY_CONV[conversation] = old_meta
        bad = {
            "type": "cancel_active_work", "schema_version": 1,
            "scope": "chat", "conversation_id": conversation,
            "request_id": old_meta["request_id"],
            "turn_id": "different-turn", "run_id": "different-turn",
            "operation_id": "wrong-target-control",
        }
        socket = Socket([
            {"type": "auth", "token": "supersession-test-token"}, bad,
        ])
        await _drive(socket)
        assert not owner.cancelled()
        control_id, _ = server._durable_command_identity("cancel_active_work", bad)
        control = _command_record(durable, control_id)
        assert control["state"] == "completed"
        assert control["result"]["accepted"] is False
        assert control["result"]["reason_code"] == "target_mismatch"
        assert _command_record(durable, old_command_id)["state"] == "running"
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
    asyncio.run(scenario())


def test_same_conversation_new_turn_supersedes_and_starts_before_old_finishes(
    isolated, monkeypatch,
):
    from osia import agent as agent_module

    durable, attempts, tmp_path = isolated
    conversation = conversations.create_conversation()["id"]
    coordinator = ToolAttemptCoordinator(attempts)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    class FakeAgent:
        def __init__(self):
            self._in_use = False

    monkeypatch.setattr(agent_module, "OSIAAgent", FakeAgent)
    a_started = asyncio.Event()
    a_cancelled = asyncio.Event()
    b_started = asyncio.Event()
    histories = {}
    agents = {}
    async def fake_worker(out, _value, history, conv, *, turn_meta, **_kwargs):
        request = turn_meta["request_id"]
        text = turn_meta["_original_user_text"]
        if text == "WOR305_SUPERSEDE_A":
            agent = _kwargs["get_agent"](conv)
            agents[request] = agent
            agent._in_use = True
            _make_tool_attempt(
                attempts, turn_meta, "active-at-waiting-boundary",
                tmp_path=tmp_path,
            )
            a_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                a_cancelled.set()
                raise
        assert text == "Reply exactly with: WOR305_SUPERSEDE_OK"
        agent = _kwargs["get_agent"](conv)
        agents[request] = agent
        assert agent is not agents["request_supersede_a"]
        histories[request] = list(history)
        b_started.set()
        response, terminal = _persist_fake_final(
            conv, turn_meta, "WOR305_SUPERSEDE_OK",
        )
        await out.send_json(response)
        await out.send_json(terminal)
        return history + [{"role": "assistant", "content": "WOR305_SUPERSEDE_OK"}], conv
    monkeypatch.setattr(server, "_run_agent_and_respond", fake_worker)
    msg_a = _user_message(conversation, "request_supersede_a", "WOR305_SUPERSEDE_A")
    msg_b = _user_message(
        conversation, "request_supersede_b",
        "Reply exactly with: WOR305_SUPERSEDE_OK",
    )

    class SequencedSocket(Socket):
        async def iter_text(self):
            yield json.dumps({"type": "auth", "token": "supersession-test-token"})
            yield json.dumps(msg_a)
            await asyncio.wait_for(a_started.wait(), timeout=3)
            yield json.dumps(msg_b)

    socket = SequencedSocket([])
    asyncio.run(_drive(socket))

    cmd_a, _ = server._durable_command_identity("text", msg_a)
    cmd_b, _ = server._durable_command_identity("text", msg_b)
    rec_a = _command_record(durable, cmd_a)
    rec_b = _command_record(durable, cmd_b)
    conv_record = conversations.get_conversation(conversation)
    assert a_cancelled.is_set() and b_started.is_set()
    assert agents["request_supersede_a"] is not agents["request_supersede_b"]
    # Model the old worker thread finishing after the cancelled orchestration.
    agents["request_supersede_a"]._in_use = False
    assert rec_a["state"] == "completed"
    assert rec_a["result"]["state"] == "superseded"
    assert rec_a["result"]["superseded_by_turn_id"] == rec_b["execution_binding"]["turn_id"]
    assert rec_b["state"] == "completed"
    assert rec_b["result"]["state"] == "turn_completed"
    assert "final_text" not in rec_a["result"]
    assert not any(
        item.get("role") == "user"
        and item.get("content") == "WOR305_SUPERSEDE_A"
        for item in histories["request_supersede_b"]
    )
    assert any(
        item.get("role") == "assistant"
        and item.get("content") == "WOR305_SUPERSEDE_OK"
        for item in conv_record["messages"]
    )
    assert any(
        item.get("role") == "user"
        and item.get("_request_id") == "request_supersede_b"
        and item.get("content") == msg_b["content"]
        for item in conv_record["messages"]
    )
    assert any(frame.get("content") == "WOR305_SUPERSEDE_OK" for frame in socket.sent)


def test_different_conversation_is_not_superseded(isolated):
    durable, _attempts, _tmp = isolated
    conversation_a = conversations.create_conversation()["id"]
    conversation_b = conversations.create_conversation()["id"]
    _message, old_meta, _command_id = _accept_fixture_turn(
        durable, conversation_a, "request_conversation_a", "A remains active",
    )
    async def scenario():
        async def hold():
            await asyncio.Future()
        old_task = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation_a] = old_task
        server._ACTIVE_TURN_META_BY_CONV[conversation_a] = old_meta
        server._ACTIVE_CONV_BY_REQUEST[old_meta["request_id"]] = conversation_a
        new_meta = server._new_turn_meta(conversation_b, "request_conversation_b")
        new_meta["_original_user_text"] = "B is independent"
        new_meta["_terminality_objective"] = "B is independent"
        assert await server._supersede_active_conversation_turn(
            None, conversation_b, new_meta,
        )
        assert server._ACTIVE_TURNS_BY_CONV[conversation_a] is old_task
        assert not old_task.cancelled()
        assert load_persisted_terminal_result(old_meta["turn_id"]) is None
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
    asyncio.run(scenario())


def test_normal_final_that_wins_first_remains_authoritative(isolated):
    durable, _attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    _message, old_meta, old_command_id = _accept_fixture_turn(
        durable, conversation, "request_final_wins", "old completed turn",
    )
    _persist_normal_old_final(old_meta, "OLD_NORMAL_FINAL")
    durable.complete_command(
        old_command_id,
        execution_epoch=old_meta["_durable_command_epoch"],
        result={"accepted": True, "state": "turn_completed"},
    )

    async def scenario():
        async def hold():
            await asyncio.Future()
        owner = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation] = owner
        server._ACTIVE_TURN_META_BY_CONV[conversation] = old_meta
        server._ACTIVE_CONV_BY_REQUEST[old_meta["request_id"]] = conversation
        new_meta = server._new_turn_meta(conversation, "request_after_final")
        new_meta["_original_user_text"] = "continue after prior final"
        new_meta["_terminality_objective"] = "continue after prior final"
        assert await server._supersede_active_conversation_turn(
            None, conversation, new_meta,
        )
        assert not owner.cancelled()
        assert server._ACTIVE_TURN_META_BY_CONV.get(conversation) is None
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        return new_meta

    new_meta = asyncio.run(scenario())
    old_result = load_persisted_terminal_result(old_meta["turn_id"])
    assert old_result["terminal_state"] == TerminalState.SUCCESS_VERIFIED.value
    assert old_result["final_text"] == "OLD_NORMAL_FINAL"
    assert _command_record(durable, old_command_id)["result"]["state"] == "turn_completed"
    assert new_meta["_supersedes_turn_id"] == old_meta["turn_id"]


def test_cancel_winner_rejects_stale_old_final_and_durable_completion(isolated):
    durable, _attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    _message, old_meta, old_command_id = _accept_fixture_turn(
        durable, conversation, "request_cancel_wins", "old cancellable turn",
    )
    async def scenario():
        async def hold():
            await asyncio.Future()
        owner = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation] = owner
        server._ACTIVE_TURN_META_BY_CONV[conversation] = old_meta
        server._ACTIVE_CONV_BY_REQUEST[old_meta["request_id"]] = conversation
        result = await server._commit_canonical_chat_cancellation(
            old_meta,
            reason_code="turn_superseded",
            superseded_by_turn_id="turn_new_winner",
        )
        await asyncio.gather(owner, return_exceptions=True)
        return result
    result = asyncio.run(scenario())
    stale_response = {
        "event_id": "stale-response-event", "sequence": 100,
    }
    stale_terminal = {
        "event_id": "stale-terminal-event", "sequence": 101,
        "terminal": True, "status": "done",
    }
    persisted = server._persist_completed_assistant_message(
        conversation, "STALE_OLD_FINAL", old_meta,
        stale_response, stale_terminal,
    )
    durable.complete_command(
        old_command_id,
        execution_epoch=old_meta["_durable_command_epoch"],
        result={"accepted": True, "state": "turn_completed"},
    )
    assert result["state"] == "superseded"
    assert persisted is False
    assert _command_record(durable, old_command_id)["result"]["state"] == "superseded"
    assert not any(
        item.get("role") == "assistant"
        and item.get("_turn_id") == old_meta["turn_id"]
        for item in conversations.get_conversation(conversation)["messages"]
    )


def test_two_rapid_same_conversation_turns_leave_only_newest_active(
    isolated, monkeypatch,
):
    durable, _attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    started = {key: asyncio.Event() for key in ("A", "B")}
    cancelled = {key: asyncio.Event() for key in ("A", "B")}
    c_final = asyncio.Event()
    histories = {}

    async def fake_worker(out, _value, history, conv, *, turn_meta, **_kwargs):
        label = turn_meta["_original_user_text"]
        if label in {"RAPID_A", "RAPID_B"}:
            key = label[-1]
            started[key].set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled[key].set()
                raise
        assert label == "RAPID_C"
        histories[label] = list(history)
        assert cancelled["A"].is_set() and cancelled["B"].is_set()
        response, terminal = _persist_fake_final(conv, turn_meta, "RAPID_C_FINAL")
        c_final.set()
        await out.send_json(response)
        await out.send_json(terminal)
        return history + [{"role": "assistant", "content": "RAPID_C_FINAL"}], conv

    monkeypatch.setattr(server, "_run_agent_and_respond", fake_worker)
    messages = [
        _user_message(conversation, "request_rapid_a", "RAPID_A"),
        _user_message(conversation, "request_rapid_b", "RAPID_B"),
        _user_message(conversation, "request_rapid_c", "RAPID_C"),
    ]

    class RapidSocket(Socket):
        async def iter_text(self):
            yield json.dumps({"type": "auth", "token": "supersession-test-token"})
            for index, message in enumerate(messages):
                yield json.dumps(message)
                if index < 2:
                    await asyncio.wait_for(started["AB"[index]].wait(), timeout=3)

    socket = RapidSocket([])
    asyncio.run(_drive(socket))
    records = []
    for message in messages:
        command_id, _ = server._durable_command_identity("text", message)
        records.append(_command_record(durable, command_id))
    assert cancelled["A"].is_set() and cancelled["B"].is_set()
    assert c_final.is_set()
    assert [item["result"]["state"] for item in records] == [
        "superseded", "superseded", "turn_completed",
    ]
    assert not any(
        item.get("role") == "user" and item.get("content") in {"RAPID_A", "RAPID_B"}
        for item in histories["RAPID_C"]
    )
    assert conversation not in server._ACTIVE_TURNS_BY_CONV


def test_startup_reconciliation_preserves_unknown_launch_and_no_final(isolated):
    durable, attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    _message, meta, command_id = _accept_fixture_turn(
        durable, conversation, "request_orphan_cancelled", "cancelled parent",
    )
    # Preserve a terminal unknown child while the canonical parent is stopped.
    created = attempts.create_attempt(
        conversation_id=conversation,
        turn_id=meta["turn_id"],
        request_id=meta["request_id"],
        tool_call_id="call_historical_unknown",
        tool_name="run_command",
        provider_id=meta["provider_id"],
        model_id=meta["model_id"],
        turn_context={},
        argument_fingerprint=attempts.fingerprint_arguments({"command": "x"}),
        side_effect="unknown_high_risk",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        final_response_message_id=meta["assistant_message_id"],
    )
    attempt_id = created["tool_attempt_id"]
    attempts.mark_accepted(attempt_id)
    attempts.mark_running(attempt_id)
    attempts.arm_owner_dispatch_boundary(attempt_id)
    _started, dispatched = attempts.claim_owner_dispatch(attempt_id)
    assert dispatched
    attempts.record_terminal_result(
        attempt_id,
        status="unknown_after_start",
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
        reason_code="reconciliation_stalled",
    )
    meta["_tool_attempt_id"] = attempt_id
    assert meta["_terminality_cancel_authority"](
        meta["turn_id"],
        effect_possible=True,
        reason_code="turn_superseded",
        superseded_by_turn_id="turn_after_restart",
    )
    before_attempt = attempts.get_attempt(attempt_id)

    settled = server._reconcile_cancelled_websocket_commands(
        conversation_id=conversation,
    )

    parent = _command_record(durable, command_id)
    stream = _stream(durable, meta)
    assert command_id in settled
    assert parent["state"] == "completed"
    assert parent["completed_at"] > 0
    assert parent["result"]["state"] == "superseded"
    assert parent["result"]["terminal_state"] == "user_cancelled"
    assert parent["result"]["superseded_by_turn_id"] == "turn_after_restart"
    assert "final_text" not in parent["result"]
    assert stream["authority_state"] == "cancelled"
    assert any(
        item.get("status") == "cancelled" and item.get("terminal")
        for item in durable.replay(
            conversation_id=conversation, run_id=meta["run_id"],
        )
    )
    assert attempts.get_attempt(attempt_id) == before_attempt


def test_new_request_can_issue_same_action_after_supersession(isolated):
    durable, _attempts, _tmp = isolated
    conversation = conversations.create_conversation()["id"]
    _message, old_meta, old_command_id = _accept_fixture_turn(
        durable, conversation, "request_action_old", "Run harmless command",
    )
    new_message = _user_message(
        conversation, "request_action_new", "Run harmless command",
    )
    async def scenario():
        async def hold():
            await asyncio.Future()
        old_task = asyncio.create_task(hold())
        server._ACTIVE_TURNS_BY_CONV[conversation] = old_task
        server._ACTIVE_TURN_META_BY_CONV[conversation] = old_meta
        server._ACTIVE_CONV_BY_REQUEST[old_meta["request_id"]] = conversation
        new_meta = server._new_turn_meta(conversation, new_message["request_id"])
        new_meta["_original_user_text"] = new_message["content"]
        new_meta["_terminality_objective"] = new_message["content"]
        assert await server._supersede_active_conversation_turn(
            None, conversation, new_meta,
        )
        # Transport identity is new; cancellation did not create a semantic
        # anti-replay barrier for the later request.
        new_id, new_binding = server._durable_command_identity("text", new_message)
        assert new_id != old_command_id
        assert durable.claim_command(
            command_id=new_id, command_type="text", binding=new_binding,
        ).disposition.value == "claimed"
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
    asyncio.run(scenario())
