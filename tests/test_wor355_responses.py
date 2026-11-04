"""Real SDK + synthetic HTTP transport, including canonical OSIA dispatch."""
import json

import httpx
import pytest
from openai import OpenAI, APIError, APIStatusError

from osia import agent as agent_module, config
from osia.agent import OSIAAgent
from osia.responses_api import ResponsesClient

MODEL = "gpt-6.1-sol"


def payload(output, model=MODEL):
    return {"id": "resp_fixture", "object": "response", "status": "completed", "model": model, "output": output}


def sse(output, model=MODEL):
    events = []
    for index, item in enumerate(output):
        if item["type"] == "message":
            events.append({"type": "response.output_text.delta", "delta": item["content"][0]["text"]})
        elif item["type"] == "reasoning":
            events.append({"type": "response.reasoning_summary_text.delta", "item_id": item["id"], "summary_index": 0, "delta": item["summary"][0]["text"]})
    events.append({"type": "response.completed", "response": payload(output, model)})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


@pytest.mark.parametrize("provider,model", [("openai", MODEL), ("openai", "gpt-6.1-terra"), ("xai", "grok-4.7")])
@pytest.mark.parametrize("streaming", [False, True])
def test_canonical_read_and_native_reinjection(monkeypatch, tmp_path, streaming, provider, model):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("responses fixture contents", encoding="utf-8")
    bodies = []
    def handler(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        bodies.append(body)
        assert len(bodies) <= 2
        assert body["store"] is False
        assert request.headers["Authorization"] == "Bearer fixture-key"
        assert request.url.host == ("api.x.ai" if provider == "xai" else "api.openai.com")
        if provider == "xai":
            assert "summary" not in body.get("reasoning", {})
        assert {tool["name"] for tool in body["tools"]} >= {"read_file", "write_file", "run_command"}
        if len(bodies) == 1:
            output = [{"type": "reasoning", "id": "rs_fixture", "encrypted_content": "opaque-private", "summary": [{"type": "summary_text", "text": "I will read the requested file."}]},
                      {"type": "function_call", "id": "fc_fixture", "call_id": "call_read", "name": "read_file", "arguments": json.dumps({"path": str(fixture)})}]
        else:
            assert any(item.get("encrypted_content") == "opaque-private" for item in body["input"])
            receipts = [item for item in body["input"] if item.get("type") == "function_call_output"]
            assert receipts[0]["call_id"] == "call_read"
            assert "responses fixture contents" in receipts[0]["output"]
            output = [{"type": "message", "id": "msg_fixture", "role": "assistant", "content": [{"type": "output_text", "text": "I read the file successfully. Its contents are: responses fixture contents.", "annotations": []}]}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(output, model)) if body.get("stream") else httpx.Response(200, json=payload(output, model))
    def factory(**kwargs):
        return OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(agent_module, "OpenAI", factory)
    monkeypatch.setattr(config, "get_setting", lambda name, default=None: default)
    agent = OSIAAgent(lang="en")
    monkeypatch.setattr(agent, "_provider_key_chain", lambda provider: ["fixture-key"])
    agent.set_model(model, provider=provider)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "turns"))
    try:
        result = agent.run_terminal_turn(f"Read {fixture} and report its contents.", turn_id="responses-fixture", history=[], on_sentence=(lambda text: None) if streaming else None)
        assert "responses fixture contents" in result
        assert "opaque-private" not in result
        assert len(bodies) == 2
        assert {body["model"] for body in bodies} == {model}
    finally:
        agent.client.close()


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_typed_http_errors(status):
    client = ResponsesClient(OpenAI(api_key="fixture", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": {"message": "synthetic", "type": "test"}})))), "openai")
    try:
        with pytest.raises(APIStatusError) as caught:
            client.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}])
        assert caught.value.status_code == status
    finally:
        client.close()


def test_stream_eof_is_not_success():
    client = ResponsesClient(OpenAI(api_key="fixture", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"type":"response.output_text.delta","delta":"partial"}\n\n')))), "openai")
    try:
        with pytest.raises(APIError):
            list(client.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}], stream=True))
    finally:
        client.close()


def test_private_reasoning_never_becomes_public_summary():
    from osia.responses_api import ResponsesStream
    from types import SimpleNamespace as NS
    class Raw:
        response = NS(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        closed = False
        def close(self):
            self.closed = True
        def __iter__(self):
            yield {"type": "response.reasoning_text.delta", "delta": "private chain"}
            yield {"type": "response.reasoning_summary_text.delta", "item_id": "summary", "delta": "Public summary"}
            yield {"type": "response.completed", "response": payload([])}
    raw = Raw()
    stream = ResponsesStream(raw)
    chunks = list(stream)
    public = "".join(getattr(c.choices[0].delta, "public_thinking", "") for c in chunks)
    assert public == "Public summary"
    assert raw.closed


def test_close_propagates_to_exact_stream():
    from osia.responses_api import ResponsesStream
    from unittest.mock import Mock
    raw = Mock()
    stream = ResponsesStream(raw)
    stream.close()
    raw.close.assert_called_once_with()


def test_receipts_are_correlated_by_arguments_and_call_identity():
    from osia.responses_api import response_message, tool_feedback
    message = response_message(payload([
        {"type": "function_call", "name": "read_file", "call_id": call, "arguments": '{"path":"fixture"}'}
        for call in ["first", "second"]
    ]))
    outputs = tool_feedback(message, [{"tool": "read_file", "path": "fixture"}] * 2, ["receipt 1", "receipt 2"])
    assert [(item["call_id"], item["output"]) for item in outputs] == [("first", "receipt 1"), ("second", "receipt 2")]


def test_documented_nonstreaming_model_returns_one_completed_delta():
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body["stream"] is False
        assert "reasoning" not in body
        return httpx.Response(200, json=payload([
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Completed reply"}]}
        ], "gpt-5.5-pro"))
    client = ResponsesClient(OpenAI(api_key="fixture", http_client=httpx.Client(transport=httpx.MockTransport(handler))), "openai")
    try:
        response = client.create(model="gpt-5.5-pro", messages=[{"role": "user", "content": "Hello"}], stream=True)
        chunks = list(response)
        assert len(chunks) == 1
        assert chunks[0].choices[0].delta.content == "Completed reply"
        assert response.message.content == "Completed reply"
        assert len(bodies) == 1
    finally:
        client.close()


def test_provider_switch_never_forwards_another_models_ciphertext(monkeypatch):
    agent = OSIAAgent(lang="en")
    agent.provider, agent.model = "groq", "openai/gpt-oss-120b"
    agent.messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": [{"type": "reasoning", "encrypted_content": "opaque-private"}],
         "_response_route": ["openai", MODEL], "_response_fallback": "Factual call history"},
    ]
    wire = json.dumps(agent._wire_messages())
    assert "opaque-private" not in wire
    assert "Factual call history" in wire


def test_native_refusal_is_public_response_text():
    from osia.responses_api import response_message
    message = response_message(payload([
        {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "Provider refusal"}]}
    ]))
    assert message.content == "Provider refusal"
