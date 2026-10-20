"""Responses wire adapter for the single canonical Chat/tool loop.

No tools execute here. Native outputs remain private provider context; only
output text and provider-authored summary_text reach the public trace.
"""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace as NS

from openai import APIError


def native_tools():
    from osia.tool_contracts import effective_static_tool_specs, dynamic_tool_specs

    return [
        {"type": "function", "name": spec.name, "description": spec.capability,
         "parameters": deepcopy(spec.input_schema)}
        for spec in (*effective_static_tool_specs(), *dynamic_tool_specs())
    ]


def input_items(messages):
    result = []
    for message in messages:
        role = message["role"]
        content = message.get("content", "")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        ordinary = []

        def flush():
            if ordinary:
                result.append({"role": role, "content": list(ordinary)})
                ordinary.clear()

        for part in parts:
            kind = part.get("type")
            if role == "assistant" and kind in {"reasoning", "function_call", "message"}:
                flush()
                result.append(deepcopy(part))
            elif role == "user" and kind == "function_call_output":
                flush()
                result.append(deepcopy(part))
            elif kind == "text":
                ordinary.append({"type": "output_text" if role == "assistant" else "input_text",
                                 "text": part.get("text", "")})
            elif kind == "image_url":
                image = part["image_url"]
                image = image if isinstance(image, dict) else {"url": image}
                ordinary.append({"type": "input_image", "image_url": image["url"],
                                 **({"detail": image["detail"]} if "detail" in image else {})})
        flush()
        if role == "tool":
            # Standard native Chat Completions history, if provided by a caller.
            if result and result[-1].get("role") == "tool":
                result.pop()
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": content})
    return result


def response_message(payload):
    output = deepcopy(payload.get("output") or [])
    public = "".join(part.get("text") or part.get("refusal", "") for item in output
                     if item.get("type") == "message" for part in item.get("content", [])
                     if part.get("type") in {"output_text", "refusal"})
    summary = "".join(part.get("text", "") for item in output
                      if item.get("type") == "reasoning" for part in item.get("summary", [])
                      if part.get("type") == "summary_text")
    return NS(
        content=public, id=payload.get("id", ""), public_thinking=summary,
        native_content=output, response_output=output,
        tool_calls=[NS(id=item["call_id"], type="function", function=NS(
            name=item["name"], arguments=item.get("arguments", "{}")))
            for item in output if item.get("type") == "function_call"],
    )


class ResponsesStream:
    def __init__(self, raw, *, public_reasoning_text=False):
        self.raw, self.message = raw, None
        self.public_reasoning_text = public_reasoning_text

    def close(self):
        self.raw.close()

    def __iter__(self):
        complete, summaries = False, {}
        try:
            for event in self.raw:
                data = event.model_dump() if hasattr(event, "model_dump") else event
                kind = data.get("type")
                delta = NS(content=None, tool_calls=None)
                if kind in {"response.output_text.delta", "response.refusal.delta"}:
                    delta.content = data.get("delta", "")
                elif kind == "response.reasoning_summary_text.delta" or (
                    kind == "response.reasoning_text.delta" and self.public_reasoning_text
                ):
                    key = (data.get("item_id", ""), data.get("summary_index", 0))
                    summaries[key] = summaries.get(key, "") + data.get("delta", "")
                    delta.public_thinking = summaries[key]
                    delta.thinking_source = f"responses:{key[0]}:{key[1]}"
                elif kind in {"response.completed", "response.incomplete"}:
                    payload = data["response"]
                    self.message = response_message(payload)
                    complete = True
                    # Native calls are emitted only once, with complete arguments.
                    delta.tool_calls = [NS(index=i, id=call.id, function=call.function)
                                        for i, call in enumerate(self.message.tool_calls)]
                    yield NS(choices=[NS(delta=delta, finish_reason=(
                        "length" if kind == "response.incomplete" else "stop"))])
                    continue
                elif kind in {"error", "response.failed"}:
                    raise APIError("Responses request failed", request=self.raw.response.request, body=None)
                yield NS(choices=[NS(delta=delta, finish_reason=None)])
            if not complete:
                raise APIError("Responses stream ended before completion", request=self.raw.response.request, body=None)
        finally:
            self.close()


class ResponsesClient:
    def __init__(self, client, provider):
        self.http_client, self.provider = client, provider
        self.timeout = client.timeout
        self.chat = NS(completions=NS(create=self.create))

    def close(self):
        self.http_client.close()

    def create(self, *, model, messages, stream=False, timeout=None, reasoning_effort=None, **kwargs):
        from osia.models import model_metadata

        metadata = model_metadata(self.provider, model) or {}
        if metadata.get("api_protocol") != "responses":
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
            return self.http_client.chat.completions.create(
                model=model, messages=messages, stream=stream,
                **({"timeout": timeout} if timeout is not None else {}), **kwargs)
        native_stream = stream and metadata.get("streaming", True)
        body = {"model": model, "input": input_items(messages), "stream": native_stream,
                "store": False, "tools": native_tools(),
                "include": ["reasoning.encrypted_content"]}
        # Grok returns its public summary by default; OpenAI requests it.
        reasoning = {"summary": "auto"} if (
            self.provider == "openai" and metadata.get("request_reasoning_summary", True)
        ) else {}
        if reasoning_effort is not None:
            reasoning["effort"] = reasoning_effort
        if reasoning:
            body["reasoning"] = reasoning
        if timeout is not None:
            body["timeout"] = timeout
        response = self.http_client.responses.create(**body)
        if native_stream:
            return ResponsesStream(response, public_reasoning_text=bool(
                self.provider == "xai" and metadata.get("public_reasoning_text")))
        payload = response.model_dump()
        if payload.get("status") == "failed":
            raise APIError("Responses request failed", request=None, body=None)
        message = response_message(payload)
        finish = "length" if payload.get("status") == "incomplete" else "stop"
        if stream:
            return CompletedResponseStream(message, finish)
        return NS(choices=[NS(message=message, finish_reason=finish)])


class CompletedResponseStream:
    """Project one completed reply when the provider does not support SSE."""
    def __init__(self, message, finish):
        self.message, self.finish = message, finish

    def close(self):
        pass

    def __iter__(self):
        delta = NS(content=self.message.content,
                   tool_calls=[NS(index=i, id=call.id, function=call.function)
                               for i, call in enumerate(self.message.tool_calls)])
        if self.message.public_thinking:
            delta.public_thinking = self.message.public_thinking
            delta.thinking_source = "responses:" + self.message.id
        yield NS(choices=[NS(delta=delta, finish_reason=self.finish)])


def tool_feedback(message, canonical_calls, results):
    remaining = list(enumerate(canonical_calls))
    outputs = []
    for call in getattr(message, "response_output", []) or []:
        if call.get("type") != "function_call":
            continue
        expected = {"tool": call["name"], **json.loads(call.get("arguments", "{}"))}
        match = next(((pos, index) for pos, (index, value) in enumerate(remaining)
                      if value == expected), None)
        content = "No canonical execution receipt is correlated with this call."
        if match is not None:
            pos, index = match
            remaining.pop(pos)
            if index < len(results):
                content = results[index]
        outputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": content})
    return outputs
