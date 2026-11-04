"""OpenRouter discovery and canonical Chat with the real SDK and HTTP fixtures."""

import base64
import io
import json
import sys
import time
from types import SimpleNamespace

import httpx
from openai import OpenAI
from PIL import Image
import pytest

from osia import agent as runtime, config, mobile_model_catalogue, models, reasoning
from osia.image_inputs import (
    ImageInputError, ImageSourceType, create_image_artifact, ensure_image_input_supported,
    register_image_artifact,
)
from osia.mobile_model_catalogue import build_model_inventory
from osia.openrouter import OpenRouterClient, _input_messages


MODEL = "qwen/qwen3.8-27b"
BASE = "https://openrouter.ai/api/v1"
PRIVATE_DETAIL = {"type": "reasoning.encrypted", "index": 0, "data": "opaque-provider-state"}


def _listing(model=MODEL, *, inputs=("text", "image"), outputs=("text",), tools=True):
    return {
        "id": model, "name": "Fixture Model", "context_length": 131_072,
        "architecture": {"input_modalities": list(inputs), "output_modalities": list(outputs)},
        "endpoints": ["/api/v1/chat/completions"],
        "supported_parameters": ["reasoning_effort", *(["tools"] if tools else [])],
        "reasoning": {"supported_efforts": ["none", "medium", "high"], "default_effort": "medium"},
    }


@pytest.fixture
def catalogue(monkeypatch):
    entries = [models._discovered_model("openrouter", row) for row in (
        _listing(), _listing("fixture/text-only", inputs=("text",), tools=False),
    )]
    monkeypatch.setattr(models, "_DISCOVERY", {"openrouter": {
        "models": entries, "checked_at": time.time(), "status": "ready",
    }})
    return entries


def test_key_check_and_catalogue_preserve_exact_provider_identity(monkeypatch):
    calls = []
    status = 401

    def get(url, *, headers, timeout):
        assert headers == {"Authorization": "Bearer fixture-only"}
        calls.append(url)
        if url.endswith("/key"):
            return SimpleNamespace(status_code=status)
        return SimpleNamespace(status_code=200, json=lambda: {"data": [
            _listing(), _listing("fixture/text-only", inputs=("text",), tools=False),
            _listing("fixture/image-only", outputs=("image",)), _listing("openrouter/auto"),
            _listing("openrouter/free"), _listing("deepseek/deepseek-v4-flash"),
            {**_listing("fixture/responses-only"), "endpoints": ["/api/v1/responses"]},
        ]})

    monkeypatch.setattr(models.requests, "get", get)
    assert models.fetch_remote_models("openrouter", "fixture-only") == []
    assert calls == [BASE + "/key"]
    status = 200
    found = models.fetch_remote_models("openrouter", "fixture-only")
    assert calls[-2:] == [BASE + "/key", BASE + "/models/user"]
    assert [entry["id"] for entry in found] == [MODEL, "fixture/text-only"]
    inventory = build_model_inventory([{**found[0], "provider": "groq"}, *found], {"openrouter", "groq"})
    assert {row["stable_key"] for row in inventory} == {
        f"groq:{MODEL}", f"openrouter:{MODEL}", "openrouter:fixture/text-only",
    }
    entry = next(row for row in inventory if row["stable_key"] == f"openrouter:{MODEL}")
    assert entry["input_modalities"] == ["text", "image"]
    assert entry["capabilities"]["tool_calls"] is True


def test_reasoning_uses_exact_openrouter_model_metadata(catalogue):
    assert reasoning.build_reasoning_kwargs("openrouter", MODEL, {"default": "off"}) == {"reasoning_effort": "none"}
    assert reasoning.build_reasoning_kwargs("openrouter", MODEL, {"default": "max"}) == {}
    shared = {"models": {MODEL: "high", f"openrouter:{MODEL}": "medium"}}
    assert reasoning.build_reasoning_kwargs("groq", MODEL, shared) == {"reasoning_effort": "high"}
    assert reasoning.build_reasoning_kwargs("openrouter", MODEL, shared) == {"reasoning_effort": "medium"}


def test_mobile_reasoning_keeps_groq_and_openrouter_settings_separate(monkeypatch, catalogue):
    inventory = build_model_inventory(catalogue, {"openrouter"})
    settings = {"models": {MODEL: "high"}}
    saved = []
    monkeypatch.setattr(mobile_model_catalogue, "build_catalogue", lambda **kwargs: {
        "models": [mobile_model_catalogue._mobile_model(entry) for entry in inventory],
    })
    monkeypatch.setattr(mobile_model_catalogue, "get_reasoning_settings", lambda: settings)
    monkeypatch.setattr(mobile_model_catalogue, "set_reasoning_settings", saved.append)

    assert mobile_model_catalogue.validate_and_set_reasoning("openrouter", MODEL, "medium")
    assert saved[0]["models"] == {MODEL: "high", f"openrouter:{MODEL}": "medium"}


def _image_input():
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    return [{"type": "text", "text": "Inspect the image, run the local test command and report its result."},
            {"type": "image_url", "image_url": {"url": url}}]


def _completion(message, finish, *, stream=False):
    base = {"id": "gen-fixture", "model": MODEL, "created": 1}
    if not stream:
        return httpx.Response(200, json={**base, "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish},
        ]})
    deltas = [message]
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        arguments = call["function"]["arguments"]
        deltas = [
            {"reasoning_details": [{**PRIVATE_DETAIL, "data": "opaque-"}], "tool_calls": [
                {**call, "index": 0, "function": {**call["function"], "arguments": arguments[:8]}},
            ]},
            {"reasoning_details": [{**PRIVATE_DETAIL, "data": "provider-state"}], "tool_calls": [
                {"index": 0, "function": {"arguments": arguments[8:]}},
            ]},
        ]
    chunks = [{**base, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish if i == len(deltas) - 1 else None},
    ]} for i, delta in enumerate(deltas)]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ))


@pytest.mark.parametrize("stream", [False, True])
def test_image_and_real_local_command_reach_the_same_model(monkeypatch, tmp_path, catalogue, stream):
    calls = []
    marker = "OSIA_OPENROUTER_COMMAND_OK"
    command = f"Write-Output {marker}" if sys.platform == "win32" else f"printf '%s' {marker}"
    tool_call = {"type": "function", "id": "call-command", "function": {
        "name": "run_command", "arguments": json.dumps({"command": command}),
    }}

    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        if len(calls) == 1:
            return _completion({"content": None, "tool_calls": [tool_call], "reasoning_details": [PRIVATE_DETAIL]},
                               "tool_calls", stream=body["stream"])
        return _completion({"content": f"The image is red. The command returned {marker}."}, "stop", stream=body["stream"])

    def factory(**kwargs):
        return OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(runtime, "OpenAI", factory)
    monkeypatch.setattr(config, "get_setting", lambda _name, default=None: default)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "turns"))
    agent = runtime.OSIAAgent(lang="en")
    monkeypatch.setattr(agent, "_provider_key_chain", lambda _provider: ["fixture-only"])
    agent.set_model(MODEL, provider="openrouter")
    visible = []
    try:
        text_part, image_part = _image_input()
        pixels = base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1])
        upload = create_image_artifact(pixels, "image/png", ImageSourceType.USER_UPLOAD)
        content = [text_part, register_image_artifact(upload, owner_id="openrouter-fixture")]
        answer = agent.run_terminal_turn(content, semantic_objective=text_part["text"],
                                         turn_id="openrouter-command", history=[],
                                         current_conversation_id="openrouter-fixture",
                                         on_sentence=visible.append if stream else None)
        assert len(calls) == 2 and marker in answer and "red" in answer
        for request, body in calls:
            assert str(request.url) == BASE + "/chat/completions"
            assert request.headers["authorization"] == "Bearer fixture-only"
            assert body["model"] == MODEL
            assert body["provider"] == {"require_parameters": True}
            assert {tool["function"]["name"] for tool in body["tools"]} >= {"read_file", "run_command"}
            assert any(
                part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
                for m in body["messages"] if isinstance(m.get("content"), list) for part in m["content"]
            )
        assistant = next(m for m in calls[1][1]["messages"] if m.get("tool_calls"))
        assert assistant["reasoning_details"] == [PRIVATE_DETAIL]
        assert assistant["tool_calls"] == [tool_call]
        receipt = next(m for m in calls[1][1]["messages"] if m["role"] == "tool")
        assert receipt["tool_call_id"] == "call-command" and marker in receipt["content"]
        assert "opaque-provider-state" not in json.dumps([answer, visible, agent.exported_history()])
    finally:
        agent.client.close()


def test_text_only_model_uses_text_tools_and_rejects_images(catalogue, monkeypatch, tmp_path):
    fixture = tmp_path / "text-fixture.txt"
    fixture.write_text("OpenRouter text-tool receipt", encoding="utf-8")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        response = (
            "<tool_call>" + json.dumps({"tool": "read_file", "path": str(fixture)}) + "</tool_call>"
            if len(calls) == 1 else "The file contains OpenRouter text-tool receipt."
        )
        return _completion({"content": response}, "stop", stream=body["stream"])

    monkeypatch.setattr(config, "get_setting", lambda _name, default=None: default)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "turns"))
    with pytest.raises(ImageInputError):
        ensure_image_input_supported("openrouter", "fixture/text-only", _image_input())
    with OpenAI(api_key="test", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler))) as sdk:
        agent = runtime.OSIAAgent(lang="en")
        agent.provider, agent.model, agent.client = "openrouter", "fixture/text-only", OpenRouterClient(sdk)
        answer = agent.run_terminal_turn("Read the fixture and report its contents.",
                                         turn_id="openrouter-text", history=[])
    assert len(calls) == 2 and "OpenRouter text-tool receipt" in answer
    assert all("tools" not in body and "provider" not in body for body in calls)
    assert "OpenRouter text-tool receipt" in json.dumps(calls[1]["messages"])


def test_orphan_native_request_uses_canonical_text_after_suspend():
    messages = [{"role": "assistant", "content": [{"type": "openrouter_tool_request",
                "message": {"reasoning_details": [PRIVATE_DETAIL], "tool_calls": [{"id": "call-1"}]},
                "fallback": "<tool_call>fixture</tool_call>"}]},
                {"role": "user", "content": "Durable result receipt"}]
    wire = _input_messages(messages)
    assert wire[0]["content"] == "<tool_call>fixture</tool_call>"
    assert wire[1]["content"] == "Durable result receipt"
    assert "opaque-provider-state" not in json.dumps(wire)


def test_incomplete_tool_stream_does_not_expose_an_executable_message(catalogue):
    call = {"type": "function", "id": "call-incomplete", "function": {
        "name": "run_command", "arguments": '{"command":"echo unsafe to dispatch"}',
    }}
    with OpenAI(api_key="test", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: _completion({"tool_calls": [call]}, None, stream=True),
    ))) as sdk:
        stream = OpenRouterClient(sdk).create(model=MODEL, messages=[{"role": "user", "content": "Hello"}], stream=True)
        with pytest.raises(ConnectionError, match="ended before completion"):
            list(stream)


def test_route_change_cannot_forward_openrouter_private_state(catalogue):
    agent = runtime.OSIAAgent(lang="en")
    agent.provider, agent.model = "groq", MODEL  # Same slug, different provider.
    agent.messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": [{"type": "openrouter_tool_request", "reasoning_details": [PRIVATE_DETAIL]}],
         "_response_route": ["openrouter", MODEL], "_response_fallback": "Factual call history"},
    ]
    wire = json.dumps(agent._wire_messages())
    assert "opaque-provider-state" not in wire and "Factual call history" in wire
