"""A slow provider metadata read cannot hold the PC config reply behind it."""
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

from osia import conversations, mobile_model_catalogue
from osia.durable_transport import DurableTransportStore
from osia.server import server


@pytest.mark.parametrize("request_kind,reply", [
    ("get_models", "models_list"),
    ("get_chatgpt_account", "chatgpt_account_state"),
])
def test_config_overtakes_pending_provider_read_without_losing_reply(tmp_path, monkeypatch, request_kind, reply):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-only")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server, "ATTACHMENT_UPLOAD_DIR", tmp_path / "attachments")
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_build_config_snapshot", lambda: {"time_format": "12h"})
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(mobile_model_catalogue, "request_remote_refresh", lambda callback: None)

    def slow_read(**kwargs):
        started.set()
        assert release.wait(5), "test metadata read was not released"
        return {"version": 1, "providers": [], "models": [], "current": None,
                "currentProvider": None, "account": {}}

    monkeypatch.setattr(mobile_model_catalogue,
                        "build_catalogue" if request_kind == "get_models" else "account_snapshot", slow_read)
    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    timer = threading.Timer(2, release.set)
    try:
        with TestClient(app, client=("127.0.0.1", 54321)) as client:
            with client.websocket_connect("/ws") as socket:
                socket.send_json({"type": "auth", "token": "test-only", "event_cursors": {}})
                while socket.receive_json().get("type") != "conversations":
                    pass
                socket.send_json({"type": request_kind})
                assert started.wait(2)
                timer.start()  # Old serialized code safely fails instead of hanging the test.
                socket.send_json({"type": "get_config"})
                while True:
                    frame = socket.receive_json()
                    if frame.get("type") in {"config", reply}:
                        break
                assert frame["type"] == "config"
                assert frame["config"]["time_format"] == "12h"
                assert not release.is_set()
                release.set()
                while socket.receive_json().get("type") != reply:
                    pass
    finally:
        release.set()
        timer.cancel()
