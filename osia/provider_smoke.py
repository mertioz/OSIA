"""Offline provider protocol checks run inside the real frozen executable."""
from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI


def run_claude_messages_smoke():
    from osia.agent import OSIAAgent

    model = "claude-sonnet-4-6"
    requests = []
    with tempfile.TemporaryDirectory(prefix="osia-claude-smoke-") as temporary:
        path = Path(temporary) / "fixture.txt"
        path.write_text("OSIA frozen provider fixture", encoding="utf-8")
        answer = "The file contains OSIA frozen provider fixture."

        def reject(message):
            return httpx.Response(400, json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": message},
            })

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if body["model"] != model or len(requests) > 3:
                return reject("unexpected frozen provider request")
            if len(requests) == 1:
                content = [{"type": "tool_use", "id": "frozen_read", "name": "read_file", "input": {"path": str(path)}}]
                stop = "tool_use"
            else:
                blocks = [block for message in body["messages"] for block in message["content"]]
                if not any(block.get("type") == "tool_result" and block.get("tool_use_id") == "frozen_read"
                           and "OSIA frozen provider fixture" in block.get("content", "") for block in blocks):
                    return reject("canonical frozen tool receipt was not reinjected")
                if len(requests) == 3:
                    texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
                    if answer not in texts or not any("The last assistant reply was a draft" in text for text in texts):
                        return reject("canonical frozen completion review did not retain the draft")
                content, stop = [{"type": "text", "text": answer}], "end_turn"
            if not body.get("stream"):
                return httpx.Response(200, json={
                    "id": "msg_frozen", "type": "message", "role": "assistant",
                    "model": model, "content": content, "stop_reason": stop,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                })
            events = [{"type": "message_start", "message": {"id": "msg_frozen", "usage": {"input_tokens": 1}}}]
            for index, block in enumerate(content):
                events.extend([{"type": "content_block_start", "index": index, "content_block": block},
                               {"type": "content_block_stop", "index": index}])
            events.extend([{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 1}},
                           {"type": "message_stop"}])
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join("data: " + json.dumps(e) + "\n\n" for e in events))

        def factory(**kwargs):
            kwargs.update(
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return OpenAI(**kwargs)

        # Exercise the same canonical entry and correlated receipts as Chat.
        # The compatibility run() wrapper has a legacy strategy compiler and
        # is not a product-runtime acceptance probe. Every HTTP client here is
        # mocked; no real provider credential or inference is used.
        with patch("osia.agent.OpenAI", factory):
            agent = OSIAAgent(lang="en")
            agent._provider_key_chain = lambda provider: ["offline-fixture"]
            agent.set_model(model, provider="anthropic")
            identity = "package-smoke-" + secrets.token_hex(12)
            agent.configure_tool_attempts(
                conversation_id=identity, turn_id=identity,
                request_id=identity, final_response_message_id=identity,
            )
            try:
                result = agent.run_terminal_turn(
                    "Read the file " + str(path), turn_id=identity,
                    current_conversation_id=identity, history=[],
                    on_sentence=lambda text: None,
                )
                if (
                    "OSIA frozen provider fixture" not in result
                    or len(requests) != 3
                    or agent.last_execution_strategy_snapshot is not None
                ):
                    raise RuntimeError("frozen canonical Claude tool round did not complete")
            finally:
                agent.clear_tool_attempts()
                agent.client.close()
