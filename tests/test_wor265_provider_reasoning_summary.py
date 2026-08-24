"""Focused WOR-265 provider public-reasoning-summary proofs."""

from __future__ import annotations

from copy import deepcopy

import pytest

from osia.agent import OSIAAgent
from osia.chatgpt_account.app_server_client import AppServerClient


@pytest.mark.parametrize("request_summary", [False, True])
def test_turn_opts_in_only_when_public_summary_has_a_consumer(request_summary):
    client = AppServerClient()
    captured = []
    summaries = []

    def fake_request(method, params, **_kwargs):
        if method != "turn/start":
            return {}
        captured.append(deepcopy(params))
        thread_id = params["threadId"]
        native_turn_id = "native-turn-wor265"
        client._attempts[thread_id].turn_id = native_turn_id
        if params.get("summary") == "concise":
            correlation = {
                "threadId": thread_id,
                "turnId": native_turn_id,
                "itemId": "reasoning-wor265",
                "summaryIndex": 0,
            }
            client._dispatch_notification(
                "item/reasoning/summaryPartAdded",
                {
                    **correlation,
                    "part": {"type": "summary_text", "text": ""},
                },
            )
            client._dispatch_notification(
                "item/reasoning/summaryTextDelta",
                {
                    **correlation,
                    "delta": "Checked the exact turn before answering.",
                },
            )
        client._dispatch_notification(
            "item/agentMessage/delta",
            {"threadId": thread_id, "delta": "One final answer."},
        )
        client._dispatch_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": native_turn_id, "status": "completed"},
            },
        )
        return {"turn": {"id": native_turn_id}}

    client._request = fake_request
    callback = summaries.append if request_summary else None
    result = client.run_turn(
        thread_id="native-thread-wor265",
        conversation_id="conversation-wor265",
        osia_turn_id="turn-wor265",
        client_request_id="request-wor265",
        final_response_message_id="message-wor265",
        model="gpt-5.6-sol",
        effort="high",
        text="Solve this test fixture.",
        on_reasoning_summary=callback,
    )

    assert result.text == "One final answer."
    assert (captured[0].get("summary") == "concise") is request_summary
    if request_summary:
        assert len(summaries) == 1
        assert summaries[0] == {
            "summary": "Checked the exact turn before answering.",
            "conversation_id": "conversation-wor265",
            "request_id": "request-wor265",
            "turn_id": "turn-wor265",
            "run_id": "turn-wor265",
            "provider_id": "chatgpt_account",
            "model_id": "gpt-5.6-sol",
            "native_thread_id": "native-thread-wor265",
            "native_turn_id": "native-turn-wor265",
        }
        assert result.public_reasoning_summary == summaries[0]["summary"]
    else:
        assert summaries == []
        assert result.public_reasoning_summary == ""


class _SummaryCallbackClient:
    def set_public_reasoning_summary_callback(self, callback):
        self.callback = callback


def test_provider_summary_reaches_ledger_only_with_exact_turn_correlation():
    client = _SummaryCallbackClient()
    agent = object.__new__(OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-sol"
    agent.client = client
    frames = []
    agent.configure_execution_trace(
        conversation_id="conversation-wor265",
        request_id="request-wor265",
        turn_id="turn-wor265",
        run_id="turn-wor265",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-sol",
        effective_reasoning="high",
        callback=frames.append,
    )
    payload = {
        "summary": "Checked the exact turn before answering.",
        "conversation_id": "conversation-wor265",
        "request_id": "request-wor265",
        "turn_id": "turn-wor265",
        "run_id": "turn-wor265",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-sol",
        "native_thread_id": "native-thread-wor265",
        "native_turn_id": "native-turn-wor265",
    }

    client.callback(payload)
    assert len(frames) == 1
    assert frames[0]["kind"] == "public_reasoning_summary"
    assert frames[0]["display"] == {
        "key": "execution.reasoning.summary",
        "args": {},
    }
    assert frames[0]["summary"] == payload["summary"]

    client.callback({**payload, "turn_id": "stale-turn"})
    client.callback("raw private reasoning")
    assert len(frames) == 1
