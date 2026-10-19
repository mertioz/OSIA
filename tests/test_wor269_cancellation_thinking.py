from __future__ import annotations

import asyncio
import threading
import time
import types
from unittest.mock import patch

import pytest

from osia import conversations
from osia.agent import OSIAAgent, _ExecutionGenerationCancelled
from osia.durable_transport import DurableTransportStore
from osia.server import server
from osia.turn_terminality import TurnTerminalityController


def _binding(turn_id: str) -> dict[str, str]:
    return {
        "conversation_id": f"conv_{turn_id}",
        "request_id": f"request_{turn_id}",
        "turn_id": turn_id,
        "run_id": f"run_{turn_id}",
        "provider_id": "fixture_provider",
        "model_id": "fixture_model",
        "effective_reasoning": "high",
    }


class _BlockingStream:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.started.set()
        self.closed.wait(5)
        raise StopIteration

    def close(self) -> None:
        self.closed.set()


class _SummaryClient:
    def __init__(self, stream: _BlockingStream | None = None) -> None:
        self.stream = stream
        self.conversation_id = "native_conversation"
        self.summary_callback = None
        self.cancelled = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create),
        )

    def _create(self, **_kwargs):
        if self.stream is None:
            raise AssertionError("provider must not start")
        return self.stream

    def set_public_reasoning_summary_callback(self, callback) -> None:
        self.summary_callback = callback

    def cancel_conversation(self, conversation_id: str) -> None:
        self.cancelled.append(conversation_id)


def _summary_payload(binding: dict[str, str], text: str) -> dict[str, str]:
    return {
        **{
            key: binding[key]
            for key in (
                "conversation_id", "request_id", "turn_id", "run_id",
                "provider_id", "model_id",
            )
        },
        "summary": text,
        "native_thread_id": "native_thread",
        "native_turn_id": "native_turn",
    }


def test_cancel_during_provider_reasoning_closes_stream_and_trace_immediately(
    tmp_path,
):
    stream = _BlockingStream()
    client = _SummaryClient(stream)
    events = []
    binding = _binding("turn_cancel_reasoning")
    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        agent = OSIAAgent()
        agent.client = client
        agent.provider = binding["provider_id"]
        agent.model = binding["model_id"]
        agent.configure_execution_trace(**binding, callback=events.append)
        agent.configure_turn_terminality_intent(
            objective="Wait safely while reasoning",
            execution_binding=binding,
        )
        agent._emit_provider_public_reasoning_summary(
            _summary_payload(binding, "A bounded public summary."),
        )

        cancelled = threading.Event()

        def consume() -> None:
            generation = agent._activate_execution_generation(
                binding["turn_id"], bind_local=True,
            )
            try:
                list(agent._stream_deltas())
            except _ExecutionGenerationCancelled:
                cancelled.set()
            finally:
                agent._clear_execution_generation_context(
                    binding["turn_id"], generation,
                )

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        assert stream.started.wait(2)

        started = time.monotonic()
        assert agent.request_turn_cancellation_intent(binding["turn_id"])
        assert time.monotonic() - started < 0.5
        assert stream.closed.wait(2)
        assert cancelled.wait(2)
        worker.join(2)

        terminal = agent.execution_trace_snapshot(binding["turn_id"])
        assert terminal is not None
        assert terminal["events"][-1]["display"]["key"] == (
            "execution.terminal.cancelled"
        )
        event_count = len(events)
        agent._emit_provider_public_reasoning_summary(
            _summary_payload(binding, "Late summary must not reopen Thinking."),
        )
        assert len(events) == event_count
        assert agent.request_turn_cancellation_intent(binding["turn_id"])


def test_cancel_before_dispatch_prevents_provider_and_next_action(tmp_path):
    binding = _binding("turn_cancel_before_dispatch")
    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        agent = OSIAAgent()
        agent.client = _SummaryClient()
        agent.provider = binding["provider_id"]
        agent.model = binding["model_id"]
        agent.configure_execution_trace(**binding)
        agent.configure_turn_terminality_intent(
            objective="Perform a safe read-only task",
            execution_binding=binding,
        )
        assert agent.request_turn_cancellation_intent(binding["turn_id"])

        provider_calls = []

        def forbidden_run(*_args, **_kwargs):
            provider_calls.append(True)
            raise AssertionError("cancelled provider continuation started")

        agent.run = forbidden_run
        final = agent.run_terminal_turn(
            "Perform a safe read-only task",
            turn_id=binding["turn_id"],
            execution_required=True,
        )
        assert final == ""
        assert provider_calls == []
        result = agent.terminality_result(binding["turn_id"])
        assert result is not None
        assert result["terminal_state"] == "user_cancelled"
        assert result["final_text"] == ""


def test_full_cancel_calls_supported_provider_interrupt_idempotently(tmp_path):
    binding = _binding("turn_provider_interrupt")
    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        agent = OSIAAgent()
        client = _SummaryClient()
        agent.client = client
        agent.provider = binding["provider_id"]
        agent.model = binding["model_id"]
        agent.configure_execution_trace(**binding)
        agent.configure_turn_terminality_intent(
            objective="Read a public status",
            execution_binding=binding,
        )
        assert agent.request_turn_cancellation(binding["turn_id"])
        assert agent.request_turn_cancellation(binding["turn_id"])
        assert client.cancelled == ["native_conversation", "native_conversation"]


def test_read_only_late_evidence_is_retained_without_continuation(tmp_path):
    controller = TurnTerminalityController(
        turn_id="turn_read_only_inflight",
        objective="Read the current value",
        required_postconditions=("value observed",),
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.set_attempt_state("attempt_read", status="pending")
    assert controller.try_request_cancellation(effect_possible=False)
    controller.record_typed_evidence(
        outcome="success_verified",
        evidence={"sanitized": "read result retained"},
        attempt_id="attempt_read",
    )
    snapshot = controller.snapshot()
    assert snapshot["terminal_state"] == "user_cancelled"
    assert snapshot["typed_evidence"]
    assert controller.evaluate().terminal_state == "user_cancelled"
    assert controller.begin_invocation(
        provider_route="fixture:model",
        strategy_id="late_continuation",
    ) == ""


def test_effect_possible_evidence_never_overwrites_user_cancelled(tmp_path):
    controller = TurnTerminalityController(
        turn_id="turn_effect_possible",
        objective="Perform an effect",
        required_postconditions=("effect verified",),
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.set_attempt_state("attempt_effect", status="running")
    assert controller.try_request_cancellation(effect_possible=True)
    before = controller.snapshot()
    assert before["unknown_effects"]
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"sanitized": "observer unavailable"},
        attempt_id="attempt_effect",
        reason_code="late_observer_unknown",
    )
    after = controller.snapshot()
    assert after["terminal_state"] == "user_cancelled"
    assert after["final_reservation"]["final_text"] == ""
    assert after["unknown_effects"]
    assert controller.evaluate().terminal_state == "user_cancelled"


def test_new_turn_is_unaffected_by_cancelled_generation_callbacks(tmp_path):
    first = _binding("turn_old_cancelled")
    second = _binding("turn_new_active")
    events = []
    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        agent = OSIAAgent()
        client = _SummaryClient()
        agent.client = client
        agent.provider = first["provider_id"]
        agent.model = first["model_id"]
        agent.configure_execution_trace(**first, callback=events.append)
        agent.configure_turn_terminality_intent(
            objective="Old turn", execution_binding=first,
        )
        assert agent.request_turn_cancellation_intent(first["turn_id"])

        agent.configure_execution_trace(**second, callback=events.append)
        agent.configure_turn_terminality_intent(
            objective="New turn", execution_binding=second,
        )
        before = len(events)
        agent._emit_provider_public_reasoning_summary(
            _summary_payload(first, "Stale old callback"),
        )
        assert len(events) == before
        agent._emit_provider_public_reasoning_summary(
            _summary_payload(second, "Current new-turn summary"),
        )
        assert len(events) == before + 1
        assert agent._execution_generation_is_current(
            turn_id=second["turn_id"],
            generation=agent._execution_generation_by_turn[second["turn_id"]],
        )


def test_server_projects_cancelled_before_stopping_local_turn_owner(tmp_path):
    class _Channel:
        def __init__(self) -> None:
            self.frames = []

        async def send_json(self, frame) -> None:
            self.frames.append(dict(frame))

    async def exercise() -> None:
        conversation_id = "conv_wor269_local_owner"
        request_id = "request_wor269_local_owner"
        meta = server._new_turn_meta(conversation_id, request_id)
        meta["_terminality_objective"] = "Wait during provider reasoning"
        server._bind_pre_agent_cancellation_authority(meta)

        async def local_owner() -> None:
            await asyncio.Future()

        owner = asyncio.create_task(local_owner())
        server._ACTIVE_TURNS_BY_CONV[conversation_id] = owner
        server._ACTIVE_TURN_META_BY_CONV[conversation_id] = meta
        server._ACTIVE_CONV_BY_REQUEST[request_id] = conversation_id
        channel = _Channel()
        try:
            assert await server._handle_correlated_chat_cancellation(
                channel,
                {
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "turn_id": meta["turn_id"],
                    "run_id": meta["run_id"],
                },
            )
            await asyncio.sleep(0)
            assert owner.cancelled()
            terminal = next(
                frame for frame in channel.frames
                if frame.get("status") == "cancelled"
            )
            assert terminal["terminal"] is True
            busy_frame = channel.frames[-1]
            assert {key: busy_frame[key] for key in (
                "type", "conversation_id", "busy",
            )} == {
                "type": "conversation_busy_changed",
                "conversation_id": conversation_id,
                "busy": False,
            }
            # Canonical envelopes also retain the exact cancellation identity.
            assert busy_frame["request_id"] == request_id
            assert busy_frame["turn_id"] == meta["turn_id"]
            assert busy_frame["run_id"] == meta["run_id"]
            assert conversation_id not in server._ACTIVE_TURNS_BY_CONV
            assert conversation_id not in server._ACTIVE_TURN_META_BY_CONV
            assert request_id not in server._ACTIVE_CONV_BY_REQUEST
            frame_count = len(channel.frames)
            assert not await server._send_recovered_turn_state(
                channel, conversation_id,
            )
            assert len(channel.frames) == frame_count
            assert await server._handle_correlated_chat_cancellation(
                channel,
                {
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "turn_id": meta["turn_id"],
                    "run_id": meta["run_id"],
                },
            )
            assert channel.frames[-1]["status"] == "cancelled"
            assert not await server._handle_correlated_chat_cancellation(
                channel,
                {
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "turn_id": meta["turn_id"],
                    "run_id": meta["run_id"],
                    "tool_attempt_id": "attempt_mismatched_history",
                },
            )
            assert channel.frames[-1]["state"] == "rejected"

            next_meta = server._new_turn_meta(
                conversation_id, "request_wor269_rearmed",
            )
            next_owner = asyncio.get_running_loop().create_future()
            assert server._claim_conversation_turn(
                conversation_id, next_owner, next_meta,
            )
            assert next_meta["request_id"] != request_id
            assert next_meta["turn_id"] != meta["turn_id"]
            assert next_meta["run_id"] != meta["run_id"]
            assert not server._clear_conversation_turn(conversation_id, owner)
            assert server._ACTIVE_TURNS_BY_CONV[conversation_id] is next_owner
            next_owner.cancel()
        finally:
            server._ACTIVE_TURNS_BY_CONV.pop(conversation_id, None)
            server._ACTIVE_TURN_META_BY_CONV.pop(conversation_id, None)
            server._ACTIVE_CONV_BY_REQUEST.pop(request_id, None)

    with (
        patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)),
        patch.object(
            server,
            "_DURABLE_TRANSPORT",
            DurableTransportStore(tmp_path / "transport"),
        ),
    ):
        asyncio.run(exercise())


def test_durable_cancelled_turn_releases_conversation_lease_for_fresh_turn(
    tmp_path, monkeypatch,
):
    conversation_store = tmp_path / "conversations"
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", conversation_store)
    monkeypatch.setattr(server.conversations, "CONVERSATIONS_DIR", conversation_store)
    conversation = conversations.create_conversation(title="Cancellation rearm")
    old = _binding("turn_cancelled_conversation_lease")
    old["conversation_id"] = conversation["id"]
    old_user = {
        "_conversation_id": conversation["id"],
        "_request_id": old["request_id"],
        "_client_request_id": old["request_id"],
        "_turn_id": old["turn_id"],
        "_run_id": old["run_id"],
        "_provider_id": old["provider_id"],
        "_model_id": old["model_id"],
        "_effective_reasoning": old["effective_reasoning"],
        "_message_id": "message_old_user",
        "_assistant_message_id": "message_old_assistant",
        "_terminality_objective": "Inspect safely",
    }
    assert conversations.commit_message(
        conversation["id"], "user", "Inspect safely", metadata=old_user,
    )["committed"]

    class _Channel:
        def __init__(self) -> None:
            self.frames = []

        async def send_json(self, frame) -> None:
            self.frames.append(dict(frame))

    with (
        patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)),
        patch.object(
            server,
            "_DURABLE_TRANSPORT",
            DurableTransportStore(tmp_path / "transport"),
        ),
    ):
        controller = TurnTerminalityController(
            turn_id=old["turn_id"],
            objective="Inspect safely",
            execution_required=False,
        )
        controller.set_execution_binding(old)
        assert controller.try_request_cancellation(effect_possible=False)
        cancelled_meta = {
            **old,
            "client_request_id": old["request_id"],
            "assistant_message_id": "message_old_assistant",
            "persisted": True,
            "_event_sequence": 0,
        }
        cancelled_frame = server._turn_status_frame(cancelled_meta, "cancelled")
        channel = _Channel()
        assert asyncio.run(server._send_persisted_turn_for_request(
            channel, conversation["id"], old["request_id"],
        ))
        assert len(channel.frames) == 1
        assert channel.frames[0]["event_id"] == cancelled_frame["event_id"]
        assert channel.frames[0]["status"] == "cancelled"
        assert channel.frames[0]["terminal"] is True
        assert channel.frames[0].get("replayed") is True
        assert all(frame.get("type") != "response" for frame in channel.frames)
        with pytest.raises(
            ValueError,
            match="cancelled canonical turn rejects assistant output",
        ):
            conversations.commit_message(
                conversation["id"],
                "assistant",
                "Late answer must not appear",
                metadata={
                    "_conversation_id": conversation["id"],
                    "_request_id": old["request_id"],
                    "_client_request_id": old["request_id"],
                    "_turn_id": old["turn_id"],
                    "_run_id": old["run_id"],
                    "_provider_id": old["provider_id"],
                    "_model_id": old["model_id"],
                    "_effective_reasoning": old["effective_reasoning"],
                    "_message_id": "message_old_assistant",
                    "_terminal_state": "user_cancelled",
                    "_terminal_reservation_id": "late_callback_reservation",
                },
            )

        fresh = _binding("turn_fresh_after_cancel")
        fresh["conversation_id"] = conversation["id"]
        receipt = conversations.commit_message(
            conversation["id"],
            "user",
            "Fresh turn",
            metadata={
                "_conversation_id": conversation["id"],
                "_request_id": fresh["request_id"],
                "_client_request_id": fresh["request_id"],
                "_turn_id": fresh["turn_id"],
                "_run_id": fresh["run_id"],
                "_provider_id": fresh["provider_id"],
                "_model_id": fresh["model_id"],
                "_effective_reasoning": fresh["effective_reasoning"],
                "_message_id": "message_fresh_user",
                "_assistant_message_id": "message_fresh_assistant",
                "_terminality_objective": "Fresh turn",
            },
        )
    assert receipt is not None and receipt["committed"] is True
    persisted = conversations.get_conversation(conversation["id"])
    assert persisted is not None
    assert [item["role"] for item in persisted["messages"]] == ["user", "user"]
