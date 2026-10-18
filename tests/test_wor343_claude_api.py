"""Official Claude Console API contracts; every HTTP response is a fixture."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI, APIStatusError

from osia import agent as agent_module, config, models
from osia.agent import OSIAAgent


WORKSPACE = "wrkspc_fixture"
MODEL = "claude-sonnet-4-6"


def configured_agent(monkeypatch, handler):
    agent = OSIAAgent(lang="en")
    clients = []

    def client_factory(**kwargs):
        client = OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        clients.append(client)
        return client

    monkeypatch.setattr(config, "get_setting", lambda name, default=None: WORKSPACE if name == "anthropic_workspace_id" else default)
    monkeypatch.setattr(agent_module, "OpenAI", client_factory)
    monkeypatch.setattr(agent, "_provider_key_chain", lambda provider: ["fixture-key-1", "fixture-key-2"])
    agent.set_model(MODEL, provider="anthropic")
    return agent, clients


def completion(text="Fixture answer", *, tool_calls=None):
    return {"id": "fixture", "object": "chat.completion", "created": 1, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "tool_calls": tool_calls},
                         "finish_reason": "tool_calls" if tool_calls else "stop"}]}


def test_workspace_survives_chat_key_rotation_and_endpoint_rebuild(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    agent, clients = configured_agent(monkeypatch, handler)
    try:
        agent.client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}])
        assert agent._activate_next_api_key() == 2
        agent.client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Again"}])
        agent._fallback_base_url = "https://fallback.example.invalid/v1"
        assert agent._activate_fallback_endpoint()
        agent.client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Last"}])
        assert [request.headers.get("anthropic-workspace-id") for request in requests] == [WORKSPACE] * 3
        assert [request.headers["x-api-key"] for request in requests] == ["fixture-key-1", "fixture-key-2", "fixture-key-2"]
        assert all(json.loads(request.content)["model"] == MODEL for request in requests)
        assert agent.provider == "anthropic"
    finally:
        for client in clients:
            client.close()


def test_model_discovery_uses_the_same_workspace(monkeypatch):
    monkeypatch.setattr(config, "get_setting", lambda name, default=None: WORKSPACE if name == "anthropic_workspace_id" else default)
    request = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": MODEL}]}))
    monkeypatch.setattr(models.requests, "get", request)
    assert models.fetch_remote_models("anthropic", "fixture-key")[0]["id"] == MODEL
    assert request.call_args.kwargs["headers"]["anthropic-workspace-id"] == WORKSPACE


def test_single_workspace_keys_keep_the_existing_header_contract(monkeypatch):
    from osia.anthropic_api import request_headers

    monkeypatch.setattr(config, "get_setting", lambda _name, default=None: "")
    assert request_headers("fixture-key") == {"x-api-key": "fixture-key"}


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_provider_errors_keep_status_and_selected_route(monkeypatch, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"type": "fixture_provider_error", "message": "Fixture rejection"}})

    agent, clients = configured_agent(monkeypatch, handler)
    try:
        with pytest.raises(APIStatusError) as caught:
            agent.client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}])
        assert caught.value.status_code == status
        assert len(requests) == 1
        assert requests[0].url.host == "api.anthropic.com"
        assert requests[0].headers["anthropic-workspace-id"] == WORKSPACE
        assert (agent.provider, agent.model) == ("anthropic", MODEL)
    finally:
        for client in clients:
            client.close()


def test_real_canonical_read_tool_receipt_continues_same_claude_model(monkeypatch, tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("provider fixture contents", encoding="utf-8")
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert len(bodies) <= 2, "unexpected provider replay"
        if len(bodies) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "fixture-read", "type": "function",
                       "function": {"name": "read_file", "arguments": json.dumps({"path": str(fixture)})}}]}
            finish = "tool_calls"
        else:
            delta = {"content": "The file contains provider fixture contents."}
            finish = "stop"
        chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=completion(delta.get("content"), tool_calls=delta.get("tool_calls")))

    agent, clients = configured_agent(monkeypatch, handler)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "turns"))
    try:
        answer = agent.run_terminal_turn(
            f"Read {fixture} and report its contents.", turn_id="wor343-claude-read", history=[],
        )
        assert answer == "The file contains provider fixture contents."
        assert len(bodies) == 2
        assert all(body["model"] == MODEL for body in bodies)
        assert "read_file" in json.dumps(bodies[0]["messages"])
        assert "provider fixture contents" in json.dumps(bodies[1]["messages"])
        assert (agent.provider, agent.model) == ("anthropic", MODEL)
    finally:
        for client in clients:
            client.close()


def test_claude_api_card_exposes_workspace_and_billing_without_account_login(monkeypatch):
    from PyQt6.QtWidgets import QApplication, QLabel
    from osia.gui import main_popup

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_popup, "get_setting", lambda name, default=None: WORKSPACE if name == "anthropic_workspace_id" else default)
    writes = []
    monkeypatch.setattr(main_popup, "set_setting", lambda *args: writes.append(args))
    monkeypatch.setattr(main_popup, "set_api_key", lambda *args: writes.append(args))
    card = main_popup._ProviderCard("anthropic")
    try:
        assert card._workspace_input.text() == WORKSPACE
        card._workspace_input.setText("wrkspc_changed")
        card._key_input.setText("fixture-key")
        card._on_save()
        assert writes == [("anthropic_workspace_id", "wrkspc_changed"), ("anthropic", "fixture-key")]
        labels = " ".join(label.text() for label in card.findChildren(QLabel))
        assert "Claude API" in labels
        assert "Claude Account" not in labels
    finally:
        card.deleteLater()
        app.processEvents()
