"""OpenRouter Chat Completions transport for OSIA's canonical tool loop."""

from copy import deepcopy
import json
import re
from types import SimpleNamespace

from osia.deepseek import LEGACY_API_IDS


_MODEL_ID = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CHAT_ENDPOINT = "/api/v1/chat/completions"
_TOOL_REQUEST = "openrouter_tool_request"
_TOOL_RESULT = "openrouter_tool_result"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _concrete_model(model: str) -> bool:
    if (
        not _MODEL_ID.fullmatch(model)
        or model.startswith("openrouter/")
        or model.endswith(":batch")
    ):
        return False
    author, slug = model.split("/", 1)
    return author != "deepseek" or slug.split(":", 1)[0] not in LEGACY_API_IDS


def model_capabilities(model: str, item: dict) -> dict | None:
    """Read capabilities from this exact OpenRouter listing, not its author."""
    architecture = item.get("architecture")
    if not _concrete_model(model) or not isinstance(architecture, dict):
        return None
    inputs, outputs = architecture.get("input_modalities"), architecture.get("output_modalities")
    if not all(isinstance(value, list) and "text" in value for value in (inputs, outputs)):
        return None
    endpoints = item.get("endpoints")
    if endpoints is not None and (not isinstance(endpoints, list) or _CHAT_ENDPOINT not in endpoints):
        return None
    parameters = item.get("supported_parameters")
    parameters = parameters if isinstance(parameters, list) else []
    metadata = {
        "label": item.get("name") if isinstance(item.get("name"), str) else model,
        "inputModalities": [value for value in ("text", "image") if value in inputs],
        "vision": "image" in inputs,
        "tool_calls": "tools" in parameters,
        "api_protocol": "chat_completions",
    }
    reasoning = item.get("reasoning")
    if isinstance(reasoning, dict) and ("reasoning" in parameters or "reasoning_effort" in parameters):
        supported = reasoning.get("supported_efforts")
        if isinstance(supported, list):
            metadata["supportedReasoningEfforts"] = [
                effort for effort in REASONING_EFFORTS if effort in supported
                and (effort != "none" or reasoning.get("mandatory") is not True)
            ]
            metadata["defaultReasoningEffort"] = reasoning.get("default_effort")
    return metadata


def _native_tools() -> list[dict]:
    from osia.tool_contracts import dynamic_tool_specs, effective_static_tool_specs

    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.capability,
                "parameters": spec.input_schema,
            },
        }
        for spec in (*effective_static_tool_specs(), *dynamic_tool_specs())
        if _TOOL_NAME.fullmatch(spec.name) and (not spec.providers or "openrouter" in spec.providers)
    ]


def _assistant_message(message, *, details=None, reasoning=None):
    content = getattr(message, "content", None)
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        return message

    wire_calls, parsed, ids = [], [], set()
    for call in tool_calls:
        call_id = getattr(call, "id", None)
        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
        if (
            not isinstance(call_id, str) or not call_id or call_id in ids
            or not isinstance(name, str) or not name
        ):
            raise RuntimeError("OpenRouter returned an uncorrelated tool call")
        try:
            kwargs = json.loads(arguments)
        except (TypeError, ValueError):
            kwargs = None
        if not isinstance(kwargs, dict) or "tool" in kwargs:
            raise RuntimeError("OpenRouter returned invalid tool arguments")
        ids.add(call_id)
        wire_calls.append({
            "type": "function", "id": call_id,
            "function": {"name": name, "arguments": arguments},
        })
        parsed.append({"tool": name, **kwargs})

    wire = {"role": "assistant", "content": content, "tool_calls": wire_calls}
    if details:
        wire["reasoning_details"] = details
    elif reasoning:
        wire["reasoning"] = reasoning
    fallback = "\n".join(f"<tool_call>{json.dumps(call, ensure_ascii=False)}</tool_call>" for call in parsed)
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, openrouter_calls=list(zip(wire_calls, parsed)),
        native_content=[{"type": _TOOL_REQUEST, "message": wire, "fallback": fallback}],
        route_bound_tool_content=True,
    )


def tool_feedback(message, canonical_calls, results, feedback):
    remaining = list(enumerate(canonical_calls))
    parts = []
    for wire, expected in message.openrouter_calls:
        result = "No execution receipt is available for this call. See the batch feedback."
        for position, (index, call) in enumerate(remaining):
            if call == expected:
                remaining.pop(position)
                if index < len(results):
                    result = results[index]
                break
        parts.append({"type": _TOOL_RESULT, "tool_call_id": wire["id"], "content": result})
    return [*parts, {"type": "text", "text": feedback}]


def _parts(message: dict) -> list[dict]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _input_messages(messages: list[dict]) -> list[dict]:
    """Project complete native pairs. A suspended turn can use its text receipt."""
    output = []
    for index, message in enumerate(messages):
        parts = _parts(message)
        request = next((part for part in parts if part.get("type") == _TOOL_REQUEST), None)
        if request and message["role"] == "assistant":
            following = messages[index + 1] if index + 1 < len(messages) else {}
            receipts = [part for part in _parts(following) if part.get("type") == _TOOL_RESULT]
            wire = request["message"]
            receipt_ids = [part["tool_call_id"] for part in receipts]
            call_ids = [call["id"] for call in wire["tool_calls"]]
            if following.get("role") == "user" and receipt_ids == call_ids:
                output.append(wire)
                output.extend({
                    "role": "tool", "tool_call_id": part["tool_call_id"], "content": part["content"],
                } for part in receipts)
            else:
                # Compaction or a durable suspend can remove the matching tool
                # response. Keep the proposal as text and let the real receipt
                # inform the next request rather than inventing a tool result.
                output.append({"role": "assistant", "content": request["fallback"]})
        elif parts:
            # Other providers also use internal chunk types. Only the public
            # text and actual image content belong in this transport.
            content = [part for part in parts if part.get("type") in {"text", "image_url"}]
            if content:
                output.append({**message, "content": content})
        else:
            output.append(message)
    return output


class OpenRouterStream:
    def __init__(self, raw):
        self.raw = raw
        self._details = []
        self._indexed_details = {}
        self._reasoning = []

    def close(self):
        self.raw.close()

    def _add_detail(self, fragment: dict) -> None:
        index = fragment.get("index")
        key = (index, fragment.get("type")) if isinstance(index, int) else None
        detail = self._indexed_details.get(key) if key is not None else None
        if detail is None:
            detail = deepcopy(fragment)
            self._details.append(detail)
            if key is not None:
                self._indexed_details[key] = detail
            return
        for field, value in fragment.items():
            if value is None:
                continue
            if field in {"text", "summary", "data"} and isinstance(value, str):
                detail[field] = (detail.get(field) or "") + value
            else:
                detail[field] = value

    def __iter__(self):
        finish = None
        try:
            for chunk in self.raw:
                if chunk.choices:
                    choice = chunk.choices[0]
                    finish = choice.finish_reason or finish
                    if finish == "error":
                        raise ConnectionError("OpenRouter stream failed before completion")
                    if finish == "content_filter":
                        raise RuntimeError("OpenRouter returned a failed completion")
                    delta = choice.delta
                    for detail in getattr(delta, "reasoning_details", None) or []:
                        if isinstance(detail, dict):
                            self._add_detail(detail)
                    reasoning = getattr(delta, "reasoning", None)
                    if isinstance(reasoning, str):
                        self._reasoning.append(reasoning)
                yield chunk
            if finish is None:
                raise ConnectionError("OpenRouter stream ended before completion")
        finally:
            self.close()

    def finalize_message(self, message):
        return _assistant_message(message, details=self._details, reasoning="".join(self._reasoning))


class OpenRouterClient:
    def __init__(self, http_client):
        self.http_client = http_client
        self.timeout = http_client.timeout
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def close(self):
        self.http_client.close()

    def create(self, *, model, messages, stream=False, timeout=None, **kwargs):
        from osia.models import model_metadata

        if not _concrete_model(model):
            raise ValueError("ERROR: Select an exact OpenRouter chat model.")
        metadata = model_metadata("openrouter", model) or {}
        if metadata.get("tool_calls") is True:
            kwargs["tools"] = _native_tools()
        if kwargs.get("tools") or kwargs.get("reasoning_effort"):
            kwargs["extra_body"] = {
                **(kwargs.get("extra_body") or {}), "provider": {"require_parameters": True},
            }
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self.http_client.chat.completions.create(
            model=model, messages=_input_messages(messages), stream=stream, **kwargs,
        )
        if stream:
            return OpenRouterStream(response)
        choice = response.choices[0]
        if choice.finish_reason in {"error", "content_filter"}:
            raise RuntimeError("OpenRouter returned a failed completion")
        message = choice.message
        if choice.finish_reason != "length":
            message = _assistant_message(
                message, details=getattr(message, "reasoning_details", None),
                reasoning=getattr(message, "reasoning", None),
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=choice.finish_reason)])
