import json

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from osia import conversations
from osia.server import server
from test_wor312_conversation_delete_server_integration import _NoDurableTransport, _receive_type


def test_status_read_is_correlated_and_does_not_create_or_mutate_conversations(tmp_path, monkeypatch):
    import osia.chat_status as status

    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-token")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", _NoDurableTransport())
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_resolve_chat_route_binding", lambda: {"provider_id": "openai", "model_id": "model"})
    conv_id = conversations.create_conversation()["id"]
    before = conversations.get_conversation(conv_id)
    calls = []
    def build(conversation_id, provider_id, model_id, **kwargs):
        calls.append((conversation_id, provider_id, model_id))
        return {"schema_version": 1, "command_id": "status.info", "connection": "api",
                "model": {"provider_id": provider_id, "id": model_id, "label": model_id},
                "context": None, "quota": None, "usage": {"total_tokens": 0, "cost_usd": 0}}
    monkeypatch.setattr(status, "build_chat_status", build)
    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    with TestClient(app, client=("127.0.0.1", 54321)) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "test-token", "device_id": "status-mobile", "event_cursors": {}})
            _receive_type(ws, "conversations")
            for identifier, request in ((conv_id, "status_exact"), ("", "status_empty")):
                ws.send_json({"type": "get_chat_status", "operation_id": request, "conversation_id": identifier})
                reply = _receive_type(ws, "chat_status")
                assert reply["operation_id"] == request and reply["conversation_id"] == identifier
                assert reply["status"]["model"]["id"] == "model" and not reply["error"]
            ws.send_json({"type": "get_chat_status", "operation_id": "status_missing", "conversation_id": "missing"})
            reply = _receive_type(ws, "chat_status")
            assert reply["error"] == "conversation_not_found" and reply["status"] is None
    assert calls == [(conv_id, "openai", "model"), ("", "openai", "model")]
    assert conversations.get_conversation(conv_id) == before
    assert len(conversations.list_conversations()) == 1
