"""Native Claude Messages transport for the canonical OSIA Chat engine.

The existing HTTP SDK supplies retries, typed errors, cancellation and TLS.
This adapter translates protocol objects only; it never executes tools or
reads Claude Code/Console CLI credentials.
"""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace as NS
from typing import Any

import httpx
from openai import APIError, Stream


def native_tools():
    from osia.tool_contracts import effective_static_tool_specs, dynamic_tool_specs

    return [
        {"name": spec.name, "description": spec.capability,
         "input_schema": deepcopy(spec.input_schema)}
        for spec in (*effective_static_tool_specs(), *dynamic_tool_specs())
        if not spec.providers or "anthropic" in spec.providers
    ]


def content_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content or []:
        if part.get("type") != "image_url":
            blocks.append(deepcopy(part))
            continue
        url = part["image_url"]
        url = url.get("url", "") if isinstance(url, dict) else url
        if url.startswith("data:"):
            header, data = url.split(",", 1)
            source = {"type": "base64", "media_type": header[5:].split(";")[0], "data": data}
        else:
            source = {"type": "url", "url": url}
        blocks.append({"type": "image", "source": source})
    return blocks


def request_body(model, messages, *, stream=False, extra_body=None, **kwargs):
    system, history = [], []
    for message in messages:
        role = message["role"]
        blocks = content_blocks(message.get("content", ""))
        if role in {"system", "developer"}:
            system.extend(blocks)
            continue
        if role == "tool":
            blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": blocks}]
            role = "user"
        for call in message.get("tool_calls") or []:
            function = call["function"]
            blocks.append({"type": "tool_use", "id": call["id"], "name": function["name"],
                           "input": json.loads(function["arguments"])})
        if not blocks:
            continue
        if history and history[-1]["role"] == role:
            history[-1]["content"].extend(blocks)
        else:
            history.append({"role": role, "content": blocks})
    body = {"model": model, "messages": history, "stream": stream, "tools": native_tools()}
    if system:
        body["system"] = system
    body.update(deepcopy(extra_body or {}))
    # Messages requires this per-response parameter. Reaching it is reported as
    # truncation to the canonical engine, never as objective completion.
    body["max_tokens"] = kwargs.get("max_tokens", 16384)
    thinking = body.get("thinking") or {}
    if thinking.get("type") == "adaptive":
        thinking["display"] = "summarized"
    return body


def response_message(payload):
    blocks = deepcopy(payload.get("content") or [])
    return NS(
        content="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
        tool_calls=[NS(id=b["id"], type="function", function=NS(
            name=b["name"], arguments=json.dumps(b["input"], ensure_ascii=False),
        )) for b in blocks if b.get("type") == "tool_use"],
        native_content=blocks,
        public_thinking="".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking"),
        id=payload.get("id", ""),
    )


class MessagesStream:
    def __init__(self, raw, owner):
        self.raw, self.owner = raw, owner
        self.blocks, self.json_fragments = {}, {}
        self.id, self.stop_reason = "", None
        self.message = None

    def close(self):
        self.raw.close()

    def __iter__(self):
        complete = False
        try:
            for event in self.raw:
                kind = event.get("type")
                delta = NS(content=None, tool_calls=None)
                if kind == "message_start":
                    message = event["message"]
                    self.id = message["id"]
                    self.owner.usage = dict(message.get("usage") or {})
                elif kind == "content_block_start":
                    index = event["index"]
                    block = self.blocks[index] = deepcopy(event["content_block"])
                    if block["type"] == "tool_use":
                        self.json_fragments[index] = ""
                        delta.tool_calls = [NS(index=index, id=block["id"], function=NS(name=block["name"], arguments=""))]
                    elif block["type"] == "text":
                        delta.content = block.get("text", "")
                elif kind == "content_block_delta":
                    index, fragment = event["index"], event["delta"]
                    block = self.blocks[index]
                    dtype = fragment["type"]
                    if dtype == "text_delta":
                        delta.content = fragment["text"]
                        block["text"] += fragment["text"]
                    elif dtype == "input_json_delta":
                        value = fragment["partial_json"]
                        self.json_fragments[index] += value
                        delta.tool_calls = [NS(index=index, id=None, function=NS(name=None, arguments=value))]
                    elif dtype == "thinking_delta":
                        block["thinking"] += fragment["thinking"]
                        delta.public_thinking = block["thinking"]
                        delta.thinking_source = f"claude:{self.id}:{index}"
                    elif dtype == "signature_delta":
                        block["signature"] += fragment["signature"]
                elif kind == "content_block_stop":
                    index = event["index"]
                    if index in self.json_fragments:
                        value = self.json_fragments[index]
                        self.blocks[index]["input"] = json.loads(value) if value else self.blocks[index]["input"]
                elif kind == "message_delta":
                    self.stop_reason = event["delta"].get("stop_reason")
                    self.owner.usage.update(event.get("usage") or {})
                elif kind == "message_stop":
                    complete = True
                    self.message = response_message({"id": self.id, "content": [b for _, b in sorted(self.blocks.items())]})
                yield NS(choices=[NS(delta=delta, finish_reason=("length" if self.stop_reason == "max_tokens" else self.stop_reason))])
            if not complete:
                raise APIError("Claude stream ended before message_stop", request=self.raw.response.request, body=None)
        finally:
            self.close()


class ClaudeMessagesClient:
    def __init__(self, http_client):
        self.http_client = http_client
        self.timeout = http_client.timeout
        self.chat = NS(completions=NS(create=self.create))
        self.usage, self.rate_limits = {}, {}

    def close(self):
        self.http_client.close()

    def create(self, *, model, messages, stream=False, timeout=None, extra_body=None, **kwargs):
        body = request_body(model, messages, stream=stream, extra_body=extra_body, **kwargs)
        options = {"headers": {"anthropic-version": "2023-06-01"}}
        if timeout is not None:
            options["timeout"] = timeout
        if stream:
            raw = self.http_client.post("/messages", body=body, cast_to=dict[str, Any], stream=True, stream_cls=Stream[dict[str, Any]], options=options)
            self.rate_limits = {k: v for k, v in raw.response.headers.items() if k.startswith("anthropic-ratelimit") or k in {"retry-after", "request-id"}}
            return MessagesStream(raw, self)
        raw = self.http_client.post("/messages", body=body, cast_to=httpx.Response, options=options)
        self.rate_limits = {k: v for k, v in raw.headers.items() if k.startswith("anthropic-ratelimit") or k in {"retry-after", "request-id"}}
        payload = raw.json()
        self.usage = dict(payload.get("usage") or {})
        return NS(choices=[NS(message=response_message(payload), finish_reason=("length" if payload.get("stop_reason") == "max_tokens" else payload.get("stop_reason")))])


def provider_client(client, provider):
    from osia.config import get_setting

    if provider == "anthropic" and get_setting("anthropic_transport", "messages") != "compatibility":
        return ClaudeMessagesClient(client)
    return client


def tool_feedback(message, results, feedback):
    """Bind each native call to the receipts produced by canonical dispatch."""
    calls = [b for b in getattr(message, "native_content", []) if b.get("type") == "tool_use"]
    if not calls:
        return []
    return [{"type": "tool_result", "tool_use_id": call["id"],
             "content": results[index] if index < len(results) else "No execution receipt is available for this call."}
            for index, call in enumerate(calls)] + [{"type": "text", "text": feedback}]
