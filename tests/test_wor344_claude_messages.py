"""Native Messages fixtures, including real canonical dispatch; no paid calls."""
import json

import httpx
import pytest
from openai import OpenAI, APIStatusError, APIError

from osia import agent as agent_module, config
from osia.agent import OSIAAgent
from osia.claude_messages import ClaudeMessagesClient, request_body

MODEL = "claude-sonnet-4-6"


def payload(blocks, stop="end_turn"):
    return {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
            "content": blocks, "stop_reason": stop, "usage": {"input_tokens": 20, "output_tokens": 8}}


def sse(blocks, stop="end_turn", complete=True):
    events = [{"type": "message_start", "message": payload([])}]
    for index, block in enumerate(blocks):
        kind = block["type"]
        initial = dict(block)
        if kind == "text":
            initial["text"] = ""
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif kind == "tool_use":
            initial["input"] = {}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        elif kind == "thinking":
            initial.update(thinking="", signature="")
            deltas = [{"type": "thinking_delta", "thinking": block["thinking"]},
                      {"type": "signature_delta", "signature": block["signature"]}]
        else:
            deltas = []
        events.append({"type": "content_block_start", "index": index, "content_block": initial})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 9}})
    if complete:
        events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def client(handler):
    return ClaudeMessagesClient(OpenAI(base_url="https://api.anthropic.com/v1", api_key="fixture-key",
        default_headers={"x-api-key": "fixture-key", "anthropic-workspace-id": "wrkspc_fixture"},
        max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler))))


def test_native_body_images_system_and_provider_thinking():
    body = request_body(MODEL, [{"role": "system", "content": "System"}, {"role": "user", "content": [
        {"type": "text", "text": "Describe"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
        extra_body={"thinking": {"type": "adaptive", "display": "omitted"}, "output_config": {"effort": "high"}})
    assert body["model"] == MODEL
    assert body["system"] == [{"type": "text", "text": "System"}]
    assert body["messages"][0]["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert body["thinking"]["display"] == "summarized"
    assert {t["name"] for t in body["tools"]} >= {"read_file", "run_command", "write_file"}


@pytest.mark.parametrize("stream", [False, True])
def test_stream_nonstream_tool_thinking_usage_and_headers(stream):
    blocks = [{"type": "thinking", "thinking": "Provider public summary", "signature": "fixture-signature"},
              {"type": "redacted_thinking", "data": "opaque-private"},
              {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "fixture"}}]
    requests = []
    def handler(request):
        requests.append(request)
        headers = {"content-type": "text/event-stream", "anthropic-ratelimit-requests-remaining": "9"}
        return httpx.Response(200, headers=headers, content=sse(blocks, "tool_use")) if stream else httpx.Response(200, json=payload(blocks))
    c = client(handler)
    try:
        result = c.create(model=MODEL, messages=[{"role": "user", "content": "Read"}], stream=stream)
        if stream:
            chunks = list(result)
            message = result.message
            assert any(getattr(ch.choices[0].delta, "public_thinking", "") == "Provider public summary" for ch in chunks)
            assert c.rate_limits["anthropic-ratelimit-requests-remaining"] == "9"
        else:
            message = result.choices[0].message
        assert message.native_content == blocks
        assert message.public_thinking == "Provider public summary"
        assert "opaque-private" not in message.content + message.public_thinking
        assert message.tool_calls[0].id == "call_1"
        assert c.usage["input_tokens"] == 20
        assert requests[0].url.path == "/v1/messages"
        assert requests[0].headers["anthropic-version"] == "2023-06-01"
        assert requests[0].headers["anthropic-workspace-id"] == "wrkspc_fixture"
    finally:
        c.close()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 529])
def test_native_typed_errors(status):
    c = client(lambda request: httpx.Response(status, json={"error": {"type": "fixture_error", "message": "Fixture rejection"}}))
    try:
        with pytest.raises(APIStatusError) as caught:
            c.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}])
        assert caught.value.status_code == status
    finally:
        c.close()


def test_incomplete_stream_is_unknown_transport_failure():
    c = client(lambda request: httpx.Response(200, content=sse([], complete=False), headers={"content-type": "text/event-stream"}))
    try:
        with pytest.raises(APIError, match="before message_stop"):
            list(c.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}], stream=True))
    finally:
        c.close()


@pytest.mark.parametrize("streaming", [False, True])
def test_real_canonical_read_and_native_receipt_reinjection(monkeypatch, tmp_path, streaming):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("native fixture contents", encoding="utf-8")
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert len(bodies) <= 2, "unexpected provider replay"
        if len(bodies) == 1:
            blocks = [{"type": "thinking", "thinking": "I will read the requested file.", "signature": "fixture-signature"},
                      {"type": "tool_use", "id": "call_read", "name": "read_file", "input": {"path": str(fixture)}}]
            stop = "tool_use"
        else:
            previous = body["messages"][-2]["content"]
            assert previous[0]["signature"] == "fixture-signature"
            results = body["messages"][-1]["content"]
            assert results[0]["type"] == "tool_result"
            assert results[0]["tool_use_id"] == "call_read"
            assert "native fixture contents" in results[0]["content"]
            blocks, stop = [{"type": "text", "text": "The file contains native fixture contents."}], "end_turn"
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(blocks, stop))
        return httpx.Response(200, json=payload(blocks, stop))
    real_factory = OpenAI
    def factory(**kwargs):
        return real_factory(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(agent_module, "OpenAI", factory)
    monkeypatch.setattr(config, "get_setting", lambda name, default=None: default)
    agent = OSIAAgent(lang="en")
    monkeypatch.setattr(agent, "_provider_key_chain", lambda provider: ["fixture-key"])
    agent.set_model(MODEL, provider="anthropic")
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "turns"))
    try:
        result = agent.run_terminal_turn(f"Read {fixture} and report its contents.", turn_id="claude-native-fixture", history=[], on_sentence=(lambda text: None) if streaming else None)
        assert "native fixture contents" in result
        assert len(bodies) == 2
        assert {body["model"] for body in bodies} == {MODEL}
    finally:
        agent.client.close()
