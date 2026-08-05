from __future__ import annotations

import asyncio
import time

from osia.autonomous import state as autonomous_state
from osia.server import server
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore


class _Channel:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _create_attempt(
    store: ToolAttemptStore,
    *,
    conversation_id: str,
    turn_id: str,
    request_id: str,
) -> dict:
    return store.create_attempt(
        conversation_id=conversation_id,
        turn_id=turn_id,
        request_id=request_id,
        tool_call_id="call_wor231",
        tool_name="find_and_click",
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={"language": "en", "source": "fixture"},
        argument_fingerprint=store.fingerprint_arguments(
            {"semantic_target": "synthetic"},
        ),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        deadline_at=time.time() + 120,
        final_response_message_id="assistant_wor231",
    )


def _install_active_turn(conv_id: str, request_id: str):
    meta = server._new_turn_meta(conv_id, request_id)
    holder = asyncio.get_running_loop().create_future()
    server._ACTIVE_TURNS_BY_CONV[conv_id] = holder
    server._ACTIVE_TURN_META_BY_CONV[conv_id] = meta
    server._ACTIVE_CONV_BY_REQUEST[request_id] = conv_id
    return meta, holder


def _clear_active_turn(conv_id: str, request_id: str, holder) -> None:
    server._ACTIVE_TURNS_BY_CONV.pop(conv_id, None)
    server._ACTIVE_TURN_META_BY_CONV.pop(conv_id, None)
    server._ACTIVE_CONV_BY_REQUEST.pop(request_id, None)
    if not holder.done():
        holder.set_result(None)


def test_exact_chat_cancellation_before_dispatch_is_proven_and_deduplicated(
    tmp_path, monkeypatch,
):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)

    async def exercise():
        conv_id = "conv.wor231.before"
        request_id = "request.wor231.before"
        meta, holder = _install_active_turn(conv_id, request_id)
        attempt = _create_attempt(
            store,
            conversation_id=conv_id,
            turn_id=meta["turn_id"],
            request_id=request_id,
        )
        message = {
            "conversation_id": conv_id,
            "request_id": request_id,
            "turn_id": meta["turn_id"],
            "tool_attempt_id": attempt["tool_attempt_id"],
        }
        channel = _Channel()
        try:
            assert await server._handle_correlated_chat_cancellation(
                channel, message,
            )
            assert await server._handle_correlated_chat_cancellation(
                channel, message,
            )
            durable = store.get_attempt(attempt["tool_attempt_id"])
            assert durable["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
            assert durable["dispatch_state"] == "cancelled_before_effect"
            assert channel.frames[0]["type"] == "turn_status"
            assert channel.frames[0]["status"] == "cancelled_proven"
            assert channel.frames[0]["request_id"] == request_id
            assert channel.frames[0]["turn_id"] == meta["turn_id"]
            assert channel.frames[0]["tool_attempt_id"] == attempt[
                "tool_attempt_id"
            ]
            assert channel.frames[1]["status"] == "cancelled_proven"
            assert meta["_cancellation_control_target"] == (
                conv_id,
                request_id,
                meta["turn_id"],
                attempt["tool_attempt_id"],
            )
        finally:
            _clear_active_turn(conv_id, request_id, holder)

    asyncio.run(exercise())


def test_chat_cancellation_after_dispatch_stays_requested_until_evidence(
    tmp_path, monkeypatch,
):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)

    async def exercise():
        conv_id = "conv.wor231.after"
        request_id = "request.wor231.after"
        meta, holder = _install_active_turn(conv_id, request_id)
        created = _create_attempt(
            store,
            conversation_id=conv_id,
            turn_id=meta["turn_id"],
            request_id=request_id,
        )
        accepted = store.mark_accepted(created["tool_attempt_id"])
        running = store.mark_running(accepted["tool_attempt_id"])
        channel = _Channel()
        try:
            assert await server._handle_correlated_chat_cancellation(
                channel,
                {
                    "conversation_id": conv_id,
                    "client_request_id": request_id,
                    "turn_id": meta["turn_id"],
                    "tool_attempt_id": running["tool_attempt_id"],
                },
            )
            durable = store.get_attempt(running["tool_attempt_id"])
            assert durable["status"] == "cancellation_requested"
            assert durable["dispatch_state"] == "dispatch_started"
            assert channel.frames[-1]["status"] == "cancellation_requested"
            assert channel.frames[-1]["terminal"] is False
        finally:
            _clear_active_turn(conv_id, request_id, holder)

    asyncio.run(exercise())


def test_stale_chat_cancellation_is_rejected_without_touching_active_turn(
    tmp_path, monkeypatch,
):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)

    async def exercise():
        conv_id = "conv.wor231.stale"
        request_id = "request.wor231.current"
        meta, holder = _install_active_turn(conv_id, request_id)
        channel = _Channel()
        try:
            assert not await server._handle_correlated_chat_cancellation(
                channel,
                {
                    "conversation_id": conv_id,
                    "request_id": "request.wor231.stale",
                    "turn_id": meta["turn_id"],
                },
            )
            assert channel.frames[0] == {
                "type": "cancellation_control",
                "schema_version": 1,
                "scope": "chat",
                "state": "rejected",
                "reason_code": "target_mismatch",
                "conversation_id": conv_id,
                "request_id": "request.wor231.stale",
                "client_request_id": "request.wor231.stale",
                "turn_id": meta["turn_id"],
            }
            assert any(frame["type"] == "turn_status" for frame in channel.frames)
            assert "_tool_cancel_requested" not in meta
            assert "_cancellation_control_target" not in meta
        finally:
            _clear_active_turn(conv_id, request_id, holder)

    asyncio.run(exercise())


def test_chat_cancellation_never_targets_a_stale_conversation_receipt(
    tmp_path, monkeypatch,
):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)

    async def exercise():
        conv_id = "conv.wor231.receipt"
        request_id = "request.wor231.current"
        meta, holder = _install_active_turn(conv_id, request_id)
        stale = _create_attempt(
            store,
            conversation_id=conv_id,
            turn_id="turn.wor231.stale",
            request_id="request.wor231.stale",
        )
        channel = _Channel()
        try:
            assert not await server._handle_correlated_chat_cancellation(
                channel,
                {
                    "conversation_id": conv_id,
                    "request_id": request_id,
                    "turn_id": meta["turn_id"],
                },
            )
            assert channel.frames[-1]["reason_code"] == "active_receipt_mismatch"
            durable = store.get_attempt(stale["tool_attempt_id"])
            assert durable["status"] == "not_started"
            assert durable["dispatch_state"] == "not_dispatched"
        finally:
            _clear_active_turn(conv_id, request_id, holder)

    asyncio.run(exercise())


def _autonomous_fixture() -> dict:
    st = autonomous_state.new_task_state(
        "Synthetic objective",
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Synthetic phase",
            "tasks": [{
                "id": "task.wor231",
                "objective": "Synthetic task",
                "success_criterion": "Synthetic result exists",
            }],
        }],
        run_id="run.wor231",
    )
    _phase, _task, current = autonomous_state.current_task(st)
    current["tool_operation_id"] = "operation.wor231"
    current["tool_attempt_id"] = "attempt.wor231"
    st["status"] = "in_progress"
    st["run_state"] = "running"
    return st


def test_autonomous_cancellation_validates_exact_run_and_deduplicates(
    monkeypatch,
):
    st = _autonomous_fixture()
    broadcasts = []
    saves = []
    monkeypatch.setitem(server._autonomous, "st", st)
    monkeypatch.setattr(server, "_broadcast_autonomous", broadcasts.append)
    monkeypatch.setattr(server._astate, "save_task_state", saves.append)

    async def exercise():
        channel = _Channel()
        exact = server._autonomous_cancellation_correlation(st)
        assert await server._prepare_correlated_autonomous_cancellation(
            channel, exact,
        )
        assert st["run_state"] == "cancellation_requested"
        assert len(broadcasts) == 1
        assert len(saves) == 1

        assert not await server._prepare_correlated_autonomous_cancellation(
            channel, exact,
        )
        assert len(broadcasts) == 1
        assert channel.frames[-1]["durable_state"] == "cancellation_requested"

        stale = dict(exact, operation_id="operation.stale")
        assert not await server._prepare_correlated_autonomous_cancellation(
            channel, stale,
        )
        assert channel.frames[-2]["type"] == "cancellation_control"
        assert channel.frames[-2]["reason_code"] == "operation_id_mismatch"
        assert channel.frames[-1]["run_id"] == "run.wor231"

        recovered = server._autonomous_frame({"event": "state_updated"}, st)
        assert recovered["run_id"] == "run.wor231"
        assert recovered["durable_state"] == "cancellation_requested"
        assert recovered["operation_id"] == "operation.wor231"
        assert recovered["tool_attempt_id"] == "attempt.wor231"

    try:
        asyncio.run(exercise())
    finally:
        server._autonomous.pop("cancellation_control_target", None)
        server._autonomous.pop("correlated_cancellation_published", None)
        server._autonomous["st"] = None
