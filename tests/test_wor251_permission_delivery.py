from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from osia import action_policy
from osia import agent as agent_module
from osia.mcp_client import registry as mcp_registry
from osia.permission_outcome import PermissionOutcome, PermissionResult
from osia.server import server
from osia.chatgpt_account.provider import _AccountChatClient


class _Channel:
    def __init__(self, device_id: str = "device-wor251") -> None:
        self._closed = False
        self._permission_device_id = device_id
        self.frames: list[dict] = []
        self.sent = threading.Event()

    async def send_json(self, frame) -> None:
        if self._closed:
            return
        self.frames.append(dict(frame))
        self.sent.set()


class _FailingChannel(_Channel):
    async def send_json(self, frame) -> None:
        self.sent.set()
        raise ConnectionError("synthetic projection disconnect")


@pytest.fixture()
def event_loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


@pytest.fixture(autouse=True)
def clear_confirmation_state():
    with server._pending_confirms_lock:
        entries = list(server._pending_confirms.values())
        server._pending_confirms.clear()
        server._confirmation_channels_by_device.clear()
    for entry in entries:
        server._settle_confirmation(
            entry, PermissionResult(PermissionOutcome.INTERNAL_FAILURE),
        )
    yield
    with server._pending_confirms_lock:
        entries = list(server._pending_confirms.values())
        server._pending_confirms.clear()
        server._confirmation_channels_by_device.clear()
    for entry in entries:
        server._settle_confirmation(
            entry, PermissionResult(PermissionOutcome.INTERNAL_FAILURE),
        )


def _start_provider(loop, channel, *, result, correlation=None):
    def run() -> None:
        target_token = server._turn_target.set((channel, loop))
        correlation_token = server._turn_confirmation_correlation.set(
            correlation or {},
        )
        try:
            result.append(
                server._ws_confirm_provider(
                    "osia", "run_command", "Run one local capability",
                )
            )
        finally:
            server._turn_confirmation_correlation.reset(correlation_token)
            server._turn_target.reset(target_token)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_stream_provider_thread_preserves_permission_context():
    seen = []
    capability_event = SimpleNamespace(
        source="provider_native",
        phase="accepted",
        outcome="dispatch_started",
    )

    class _Provider:
        def complete(self, **kwargs):
            seen.append(server._turn_target.get())
            kwargs["on_delta"]("ok")
            return SimpleNamespace(
                text="ok",
                capability_events=(capability_event,),
            )

    client = _AccountChatClient(_Provider())
    expected = (object(), object())
    token = server._turn_target.set(expected)
    try:
        stream = client.chat.completions.create(
            model="test", messages=[], stream=True,
        )
    finally:
        server._turn_target.reset(token)

    assert [chunk.choices[0].delta.content for chunk in stream] == ["ok"]
    assert seen == [expected]
    assert client.consume_capability_events() == (capability_event,)
    assert client.consume_capability_events() == ()


def test_destructive_hint_non_deletion_reaches_write_confirmation(monkeypatch):
    class _Manager:
        def __init__(self):
            self.calls = []

        def call_tool(self, server_id, tool_name, arguments, *, timeout):
            self.calls.append((server_id, tool_name, arguments, timeout))
            return "ok"

    manager = _Manager()
    gate_calls = []
    monkeypatch.setattr(
        action_policy, "system_mutation_barrier",
        lambda _tool, _arguments: None,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit",
        lambda _tool, _arguments: True,
    )
    monkeypatch.setattr(
        mcp_registry, "_confirm_gate",
        lambda server_id, tool_name, arguments: (
            gate_calls.append((server_id, tool_name, arguments)) or None
        ),
    )
    server_id = "wor251_hint"
    try:
        names = mcp_registry.register_server_tools(manager, server_id, [{
            "name": "save_comment",
            "description": "Save one comment",
            "inputSchema": {
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": True,
            },
        }])
        assert names == ["mcp__wor251_hint__save_comment"]
        assert agent_module.TOOL_MAP[names[0]](body="marker") == "ok"
        assert gate_calls == [(server_id, "save_comment", {"body": "marker"})]
        assert len(manager.calls) == 1
    finally:
        mcp_registry.unregister_server_tools(server_id)


@pytest.mark.parametrize("approved", [True, False])
def test_exact_response_settles_one_correlated_request(
    event_loop_thread, approved,
):
    channel = _Channel()
    result = []
    correlation = {
        "conversation_id": "conversation-251",
        "request_id": "request-251",
        "turn_id": "turn-251",
    }
    thread = _start_provider(
        event_loop_thread,
        channel,
        result=result,
        correlation=correlation,
    )

    assert channel.sent.wait(2)
    assert len(channel.frames) == 1
    frame = channel.frames[0]
    assert frame["id"] == frame["permission_request_id"]
    assert frame["id"] == frame["operation_id"]
    assert frame["conversation_id"] == correlation["conversation_id"]
    assert frame["request_id"] == correlation["request_id"]
    assert frame["turn_id"] == correlation["turn_id"]
    assert frame["expires_at"] > time.time()

    assert server._resolve_confirm(
        frame["id"], approved, channel=channel,
    ) is True
    assert server._resolve_confirm(
        frame["id"], approved, channel=channel,
    ) is False
    thread.join(timeout=2)
    expected = (
        PermissionOutcome.ALLOWED if approved else PermissionOutcome.USER_DENIED
    )
    assert [item.outcome for item in result] == [expected]
    assert result[0].correlation_id == frame["id"]


def test_pending_request_reprojects_once_to_same_device(event_loop_thread):
    first = _Channel()
    result = []
    thread = _start_provider(event_loop_thread, first, result=result)
    assert first.sent.wait(2)
    original = dict(first.frames[0])

    server._deny_confirms_for_channel(first)
    replacement = _Channel()
    assert server._attach_confirmation_channel(
        replacement._permission_device_id, replacement, event_loop_thread,
    ) == 1
    assert replacement.sent.wait(2)
    assert replacement.frames == [original]

    # The disconnected transport and a double tap cannot resolve the rebound
    # request. Only the exact replacement channel consumes it once.
    assert server._resolve_confirm(
        original["id"], True, channel=first,
    ) is False
    assert server._resolve_confirm(
        original["id"], True, channel=replacement,
    ) is True
    assert server._resolve_confirm(
        original["id"], True, channel=replacement,
    ) is False
    thread.join(timeout=2)
    assert [item.outcome for item in result] == [PermissionOutcome.ALLOWED]
    assert len(first.frames) == 1
    assert len(replacement.frames) == 1


def test_projection_disconnect_preserves_device_bound_request(event_loop_thread):
    first = _FailingChannel()
    result = []
    thread = _start_provider(event_loop_thread, first, result=result)
    assert first.sent.wait(2)

    replacement = _Channel()
    assert server._attach_confirmation_channel(
        replacement._permission_device_id, replacement, event_loop_thread,
    ) == 1
    assert replacement.sent.wait(2)
    request_id = replacement.frames[0]["id"]
    assert server._resolve_confirm(
        request_id, True, channel=replacement,
    ) is True

    thread.join(timeout=2)
    assert [item.outcome for item in result] == [PermissionOutcome.ALLOWED]


def test_new_channel_waits_for_old_detach_before_reprojection(event_loop_thread):
    first = _Channel()
    result = []
    thread = _start_provider(event_loop_thread, first, result=result)
    assert first.sent.wait(2)
    original = dict(first.frames[0])

    replacement = _Channel()
    assert server._attach_confirmation_channel(
        replacement._permission_device_id, replacement, event_loop_thread,
    ) == 0
    assert replacement.frames == []
    server._deny_confirms_for_channel(first)
    assert replacement.sent.wait(2)
    assert replacement.frames == [original]

    assert server._resolve_confirm(
        original["id"], False, channel=replacement,
    ) is True
    thread.join(timeout=2)
    assert [item.outcome for item in result] == [PermissionOutcome.USER_DENIED]


def test_expired_request_cannot_be_reused(event_loop_thread, monkeypatch):
    monkeypatch.setattr(server, "_MCP_CONFIRM_WS_TIMEOUT", 0.05)
    channel = _Channel()
    result = []
    thread = _start_provider(event_loop_thread, channel, result=result)
    assert channel.sent.wait(2)
    request_id = channel.frames[0]["id"]
    thread.join(timeout=2)
    assert [item.outcome for item in result] == [PermissionOutcome.TIMEOUT]
    assert server._resolve_confirm(
        request_id, True, channel=channel,
    ) is False


def test_unbound_disconnect_still_fails_closed(event_loop_thread):
    channel = _Channel(device_id="")
    result = []
    thread = _start_provider(event_loop_thread, channel, result=result)
    assert channel.sent.wait(2)
    server._deny_confirms_for_channel(channel)
    thread.join(timeout=2)
    assert [item.outcome for item in result] == [PermissionOutcome.DISCONNECTED]


def test_wrong_device_cannot_restore_pending_request(event_loop_thread):
    first = _Channel()
    result = []
    thread = _start_provider(event_loop_thread, first, result=result)
    assert first.sent.wait(2)
    request_id = first.frames[0]["id"]
    server._deny_confirms_for_channel(first)

    foreign = _Channel(device_id="device-foreign")
    assert server._attach_confirmation_channel(
        foreign._permission_device_id, foreign, event_loop_thread,
    ) == 0
    assert foreign.frames == []
    assert server._resolve_confirm(
        request_id, True, channel=foreign,
    ) is False

    with server._pending_confirms_lock:
        entry = server._pending_confirms.pop(request_id)
    server._settle_confirmation(
        entry, PermissionResult(PermissionOutcome.INTERNAL_FAILURE),
    )
    thread.join(timeout=2)
    assert [item.outcome for item in result] == [
        PermissionOutcome.INTERNAL_FAILURE,
    ]
