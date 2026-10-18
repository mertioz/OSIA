"""Offline provider protocol checks run inside the real frozen executable."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
from openai import OpenAI


def run_claude_messages_smoke():
    from osia.agent import OSIAAgent
    from osia.claude_messages import ClaudeMessagesClient

    model = "claude-sonnet-4-6"
    requests = []
    with tempfile.TemporaryDirectory(prefix="osia-claude-smoke-") as temporary:
        path = Path(temporary) / "fixture.txt"
        path.write_text("OSIA frozen provider fixture", encoding="utf-8")

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if body["model"] != model or len(requests) > 2:
                raise RuntimeError("unexpected frozen provider request")
            if len(requests) == 1:
                content = [{"type": "tool_use", "id": "frozen_read", "name": "read_file", "input": {"path": str(path)}}]
                stop = "tool_use"
            else:
                results = body["messages"][-1]["content"]
                if not any(b.get("type") == "tool_result" and b.get("tool_use_id") == "frozen_read"
                           and "OSIA frozen provider fixture" in b.get("content", "") for b in results):
                    raise RuntimeError("canonical frozen tool receipt was not reinjected")
                content, stop = [{"type": "text", "text": "The file contains OSIA frozen provider fixture."}], "end_turn"
            events = [{"type": "message_start", "message": {"id": "msg_frozen", "usage": {"input_tokens": 1}}}]
            for index, block in enumerate(content):
                events.extend([{"type": "content_block_start", "index": index, "content_block": block},
                               {"type": "content_block_stop", "index": index}])
            events.extend([{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 1}},
                           {"type": "message_stop"}])
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join("data: " + json.dumps(e) + "\n\n" for e in events))

        agent = OSIAAgent(lang="en")
        agent.model, agent.provider = model, "anthropic"
        agent.client = ClaudeMessagesClient(OpenAI(
            base_url="https://fixture.invalid/v1", api_key="offline-fixture",
            max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ))
        try:
            # _run_turn avoids the persistent turn controller in this isolated
            # offline probe. Actual OSIA dispatch still owns read_file.
            result = agent.run("Read the file " + str(path), on_sentence=lambda text: None,
                               canonical_output=False)
            if "OSIA frozen provider fixture" not in result or len(requests) != 2:
                raise RuntimeError("frozen Claude tool round did not complete")
        finally:
            agent.client.close()
