"""Authenticated Mobile edits use the canonical PC settings owner."""
import asyncio

import pytest

from osia import config
from osia.durable_transport import DurableTransportStore
from osia.server import server
from tools.test_attachment_server_transaction import _FakeWebSocket, _idle_heartbeat


@pytest.mark.parametrize("value,accepted", [
    (" workspace_fixture ", True), ("", True), (None, False),
    ({"unexpected": "value"}, False), ("line\nbreak", False),
])
def test_mobile_claude_workspace_write_roundtrip(monkeypatch, tmp_path, value, accepted):
    written = {}
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-only")
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(server, "_active_conversation_busy_ids", lambda: [])
    monkeypatch.setattr(server, "_heartbeat_push_loop", _idle_heartbeat)
    monkeypatch.setattr(server.conversations, "conversation_snapshot", lambda: {"type": "conversations", "items": []})
    monkeypatch.setattr(config, "set_setting", lambda key, val: written.update({key: val}))
    monkeypatch.setattr(server, "_build_config_snapshot", lambda: dict(written))
    socket = _FakeWebSocket([
        {"type": "auth", "token": "test-only"},
        {"type": "set_setting", "key": "anthropic_workspace_id", "value": value},
        {"type": "get_config"},
    ])
    asyncio.run(server.websocket_endpoint(socket))
    snapshots = [f["config"] for f in socket.sent if f.get("type") == "config"]
    if accepted:
        assert written == {"anthropic_workspace_id": value.strip()}
        assert snapshots[-1] == written
    else:
        assert written == {}
        assert any(f.get("status") == "error" for f in socket.sent)
