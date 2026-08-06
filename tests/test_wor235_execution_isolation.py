from __future__ import annotations

import asyncio
from copy import deepcopy

from osia.autonomous import state as autonomous_state
from osia.server import server
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore


class _Channel:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


class _Orchestrator:
    def __init__(self):
        self.stop_count = 0

    def stop(self):
        self.stop_count += 1

    def request_tool_cancellation(self):
        raise AssertionError("abandon must not resume or replay the tool path")


class _HandoffAgent:
    model = "fixture-model"
    provider = "fixture-provider"
    _applied_selected_model = None
    _in_use = False

    def run(self, _text, *, on_sentence, on_first_token, **_kwargs):
        server._autonomous["active"] = True
        server._autonomous["st"] = _autonomous_fixture("run.wor235.handoff")
        on_first_token()
        on_sentence("The Autonomous mission has started.")
        return "The Autonomous mission has started."

    def exported_history(self):
        return [
            {"role": "user", "content": "Start the mission."},
            {"role": "assistant", "content": "The Autonomous mission has started."},
        ]


def _autonomous_fixture(run_id: str = "run.wor235") -> dict:
    st = autonomous_state.new_task_state(
        "Synthetic isolation objective",
        "fixture-provider",
        "fixture-model",
        [{
            "name": "Synthetic phase",
            "tasks": [{
                "id": "task.wor235",
                "objective": "Synthetic task",
                "success_criterion": "Synthetic evidence exists",
            }],
        }],
        run_id=run_id,
    )
    _phase, _task, current = autonomous_state.current_task(st)
    current["tool_operation_id"] = "operation.wor235"
    st["status"] = "in_progress"
    st["run_state"] = "running"
    return st


def _unknown_effect_attempt(store: ToolAttemptStore, st: dict) -> dict:
    _phase, _task, current = autonomous_state.current_task(st)
    created = store.create_attempt(
        conversation_id="autonomous.wor235",
        turn_id="turn.wor235",
        request_id="request.wor235",
        tool_call_id="call.wor235",
        tool_name="synthetic_external_action",
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={"language": "en", "source": "fixture"},
        argument_fingerprint=store.fingerprint_arguments({"fixture": True}),
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        owner_scope="autonomous",
        autonomous_run_id=st["run_id"],
        autonomous_task_id=current["id"],
        autonomous_operation_id=current["tool_operation_id"],
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    terminal, changed = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
        reason_code="verification_deadline_expired",
    )
    assert changed
    current["tool_attempt_id"] = terminal["tool_attempt_id"]
    current["tool_durable_state"] = "blocked_unknown_effect"
    st["run_state"] = "blocked_unknown_effect"
    return terminal


def _install_turn(conversation_id: str, request_id: str):
    meta = server._new_turn_meta(conversation_id, request_id)
    holder = asyncio.get_running_loop().create_future()
    assert server._claim_conversation_turn(conversation_id, holder, meta)
    return meta, holder


def _clear_turn(conversation_id: str, holder) -> None:
    server._clear_conversation_turn(conversation_id, holder)
    if not holder.done():
        holder.set_result(None)


def test_abandon_terminalizes_unknown_effect_without_replay(tmp_path, monkeypatch):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    st = _autonomous_fixture("run.wor235.unknown")
    terminal_receipt = _unknown_effect_attempt(store, st)
    saved = []
    orch = _Orchestrator()
    monkeypatch.setattr(server._astate, "save_task_state", lambda value: saved.append(deepcopy(value)))
    monkeypatch.setattr(
        server._aauthorization,
        "revoke_run_authorization",
        lambda value, **_kwargs: value.update({"authorization_status": "revoked"}),
    )

    assert server._terminalize_autonomous_abandon(st, orch)
    assert st["status"] == "aborted"
    assert st["run_state"] == "cancelled"
    assert st["abandon_state"] == "abandoned_proven"
    assert st["abandon_effect_state"] == "effect_possible_or_unknown"
    assert orch.stop_count == 1
    assert len(saved) == 1
    assert store.get_attempt(terminal_receipt["tool_attempt_id"]) == terminal_receipt

    assert not server._terminalize_autonomous_abandon(st, orch)
    assert orch.stop_count == 1
    assert len(saved) == 1
    assert store.get_attempt(terminal_receipt["tool_attempt_id"]) == terminal_receipt


def test_failed_run_accepts_idempotent_abandon_and_preserves_failure(monkeypatch):
    st = _autonomous_fixture("run.wor235.failed")
    st["status"] = "failed"
    st["run_state"] = "failed"
    saved = []
    releases = []
    monkeypatch.setitem(server._autonomous, "st", st)
    monkeypatch.setitem(server._autonomous, "orch", _Orchestrator())
    monkeypatch.setattr(server._astate, "save_task_state", lambda value: saved.append(deepcopy(value)))
    monkeypatch.setattr(server, "_release_single_runner", lambda: releases.append(1))

    async def exercise():
        channel = _Channel()
        exact = server._autonomous_cancellation_correlation(st)
        assert not await server._prepare_correlated_autonomous_cancellation(channel, exact)
        assert not await server._prepare_correlated_autonomous_cancellation(channel, exact)
        accepted = [frame for frame in channel.frames if frame.get("state") == "accepted"]
        assert len(accepted) == 2
        assert all(frame["run_id"] == st["run_id"] for frame in accepted)
        assert all(frame["abandon_state"] == "abandoned_proven" for frame in accepted)

    try:
        asyncio.run(exercise())
        assert st["status"] == "failed"
        assert st["run_state"] == "failed"
        assert st["abandon_state"] == "abandoned_proven"
        assert len(saved) == 1
        assert releases == [1]
    finally:
        server._autonomous.pop("cancellation_control_target", None)
        server._autonomous.pop("correlated_cancellation_published", None)
        server._autonomous["st"] = None
        server._autonomous["orch"] = None


def test_uncorrelated_autonomous_abandon_is_rejected(monkeypatch):
    st = _autonomous_fixture("run.wor235.exact")
    monkeypatch.setitem(server._autonomous, "st", st)

    async def exercise():
        channel = _Channel()
        assert not await server._prepare_correlated_autonomous_cancellation(
            channel, {},
        )
        assert channel.frames == [{
            "type": "cancellation_control",
            "schema_version": 1,
            "scope": "autonomous",
            "state": "rejected",
            "reason_code": "incomplete_target",
        }]

    try:
        asyncio.run(exercise())
        assert st["status"] == "in_progress"
        assert st["run_state"] == "running"
        assert "abandon_state" not in st
    finally:
        server._autonomous["st"] = None


def test_abandon_is_valid_from_every_recoverable_run_state(monkeypatch):
    saves = []
    broadcasts = []
    monkeypatch.setattr(server._astate, "save_task_state", lambda value: saves.append(deepcopy(value)))
    monkeypatch.setattr(server, "_broadcast_autonomous", broadcasts.append)
    monkeypatch.setattr(
        server._aauthorization,
        "revoke_run_authorization",
        lambda value, **_kwargs: value.update({"authorization_status": "revoked"}),
    )

    async def exercise():
        for index, run_state in enumerate((
            "running",
            "waiting_tool",
            "verifying_tool",
            "blocked_unknown_effect",
        )):
            st = _autonomous_fixture(f"run.wor235.state.{index}")
            st["run_state"] = run_state
            orch = _Orchestrator()
            server._autonomous["st"] = st
            server._autonomous["orch"] = orch
            server._autonomous.pop("cancellation_control_target", None)
            server._autonomous.pop("correlated_cancellation_published", None)
            channel = _Channel()
            exact = server._autonomous_cancellation_correlation(st)
            assert await server._prepare_correlated_autonomous_cancellation(
                channel, exact,
            )
            assert channel.frames[-1]["state"] == "accepted"
            assert channel.frames[-1]["abandon_state"] == "abandon_requested"
            assert server._terminalize_autonomous_abandon(st, orch)
            assert st["status"] == "aborted"
            assert st["run_state"] == "cancelled"
            assert st["abandon_state"] == "abandoned_proven"
            assert orch.stop_count == 1

    try:
        asyncio.run(exercise())
    finally:
        server._autonomous.pop("cancellation_control_target", None)
        server._autonomous.pop("correlated_cancellation_published", None)
        server._autonomous["st"] = None
        server._autonomous["orch"] = None


def test_chat_and_autonomous_progress_and_cancel_with_exact_independent_ids(
    tmp_path, monkeypatch,
):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    st = _autonomous_fixture("run.wor235.concurrent")
    orch = _Orchestrator()
    monkeypatch.setitem(server._autonomous, "active", True)
    monkeypatch.setitem(server._autonomous, "st", st)
    monkeypatch.setitem(server._autonomous, "orch", orch)
    monkeypatch.setattr(server._astate, "save_task_state", lambda _value: None)
    monkeypatch.setattr(
        server._aauthorization,
        "revoke_run_authorization",
        lambda value, **_kwargs: value.update({"authorization_status": "revoked"}),
    )

    async def exercise():
        conversation_id = "conv.wor235"
        first, first_holder = _install_turn(conversation_id, "request.wor235.first")
        channel = _Channel()
        try:
            assert await server._handle_correlated_chat_cancellation(channel, {
                "conversation_id": conversation_id,
                "request_id": first["request_id"],
                "turn_id": first["turn_id"],
            })
            assert st["run_id"] == "run.wor235.concurrent"
            assert st["run_state"] == "running"
        finally:
            _clear_turn(conversation_id, first_holder)

        second, second_holder = _install_turn(conversation_id, "request.wor235.second")
        try:
            recovered = _Channel()
            assert await server._send_recovered_turn_state(recovered, conversation_id)
            assert {frame["request_id"] for frame in recovered.frames} == {
                "request.wor235.second",
            }
            assert {frame["turn_id"] for frame in recovered.frames} == {
                second["turn_id"],
            }
            autonomous = server._autonomous_frame({"event": "state_updated"}, st)
            assert autonomous["run_id"] == "run.wor235.concurrent"
            assert "request_id" not in autonomous
            assert "turn_id" not in autonomous

            assert server._terminalize_autonomous_abandon(st, orch)
            assert server._ACTIVE_TURNS_BY_CONV[conversation_id] is second_holder
            assert server._ACTIVE_CONV_BY_REQUEST[second["request_id"]] == conversation_id
        finally:
            _clear_turn(conversation_id, second_holder)

    try:
        asyncio.run(exercise())
    finally:
        server._autonomous["active"] = False
        server._autonomous["st"] = None
        server._autonomous["orch"] = None


def test_successful_handoff_terminalizes_and_releases_originating_chat(monkeypatch):
    conversation_id = "conv.wor235.handoff"
    request_id = "request.wor235.handoff"
    agent = _HandoffAgent()
    monkeypatch.setattr(server, "_agent_needs_model_apply", lambda *_args: False)
    monkeypatch.setattr(server, "_persist_accepted_user_message", lambda *_args: True)
    monkeypatch.setattr(server, "_persist_completed_assistant_message", lambda *_args: True)

    async def exercise():
        channel = _Channel()
        current = asyncio.current_task()
        assert current is not None
        meta = server._new_turn_meta(conversation_id, request_id)
        meta["tts_enabled"] = False
        assert server._claim_conversation_turn(conversation_id, current, meta)

        await server._run_agent_and_respond(
            channel,
            "Start the mission.",
            [],
            conversation_id,
            get_agent=lambda _conversation_id: agent,
            turn_meta=meta,
        )

        terminal = [frame for frame in channel.frames if frame.get("terminal")]
        assert len(terminal) == 1
        assert terminal[0]["status"] == "done"
        assert terminal[0]["request_id"] == request_id
        assert terminal[0]["turn_id"] == meta["turn_id"]
        assert not server._is_conversation_busy(conversation_id)
        assert server._autonomous["active"] is True
        assert server._autonomous["st"]["run_id"] == "run.wor235.handoff"

        next_holder = asyncio.get_running_loop().create_future()
        next_meta = server._new_turn_meta(
            conversation_id, "request.wor235.after.handoff",
        )
        assert server._claim_conversation_turn(
            conversation_id, next_holder, next_meta,
        )
        _clear_turn(conversation_id, next_holder)

    try:
        asyncio.run(exercise())
    finally:
        server._ACTIVE_TURNS_BY_CONV.pop(conversation_id, None)
        server._ACTIVE_TURN_META_BY_CONV.pop(conversation_id, None)
        server._ACTIVE_CONV_BY_REQUEST.pop(request_id, None)
        server._ACTIVE_CONV_BY_REQUEST.pop("request.wor235.after.handoff", None)
        server._autonomous["active"] = False
        server._autonomous["st"] = None


def test_restart_preserves_terminal_abandon_truth(tmp_path, monkeypatch):
    state_file = tmp_path / "task_state.json"
    monkeypatch.setattr(autonomous_state, "TASK_STATE_FILE", state_file)
    st = _autonomous_fixture("run.wor235.restart")
    st.update({
        "status": "aborted",
        "run_state": "cancelled",
        "abandon_state": "abandoned_proven",
        "abandon_effect_state": "effect_possible_or_unknown",
    })
    autonomous_state.save_task_state(st)

    restored = autonomous_state.load_task_state()
    assert restored is not None
    assert restored["run_id"] == "run.wor235.restart"
    assert restored["abandon_state"] == "abandoned_proven"
    assert restored["abandon_effect_state"] == "effect_possible_or_unknown"
    frame = server._autonomous_frame({"event": "aborted"}, restored)
    assert frame["terminal"] is True
    assert frame["durable_state"] == ""
    assert frame["abandon_state"] == "abandoned_proven"
    assert frame["abandon_effect_state"] == "effect_possible_or_unknown"


def test_late_engine_callbacks_cannot_revive_or_duplicate_abandoned_run(monkeypatch):
    st = _autonomous_fixture("run.wor235.late")
    st.update({
        "status": "aborted",
        "run_state": "cancelled",
        "abandon_state": "abandoned_proven",
        "abandon_effect_state": "effect_possible_or_unknown",
    })
    broadcasts = []
    monkeypatch.setitem(server._autonomous, "st", st)
    monkeypatch.setitem(server._autonomous, "active", True)
    monkeypatch.setattr(server, "_broadcast_autonomous", broadcasts.append)

    try:
        for event in ("running", "task_done", "failed", "done", "cancelled_proven"):
            server._on_autonomous_progress({"event": event})
        assert broadcasts == []
        assert st["status"] == "aborted"
        assert st["run_state"] == "cancelled"
    finally:
        server._autonomous["st"] = None
        server._autonomous["active"] = False
