"""Physical work reaches OSIA only through dynamic-tool handoff."""

from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
from osia.tool_attempts import ToolAttemptStore


def _client(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(
        base_dir=tmp_path / "account",
        tool_attempt_store=store,
        compact_logs_on_startup=False,
    )
    attempt = _TurnAttempt(
        thread_id="provider-thread",
        turn_id="provider-turn",
        conversation_id="conversation",
        osia_turn_id="turn",
        model_id="gpt-5.6-sol",
        user_request="Print a harmless marker.",
    )
    client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    client._send = sent.append
    client._send_request_no_wait = lambda method, params: interrupts.append(
        (method, params)
    )
    return client, attempt, store, sent, interrupts


def test_raw_command_approval_never_enters_active_handoff(tmp_path):
    client, attempt, store, sent, _interrupts = _client(tmp_path)
    client._handle_server_request({
        "id": 17,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "native-command",
            "command": "echo SHOULD_NOT_RUN",
        },
    })

    assert sent == [{"id": 17, "result": {"decision": "decline"}}]
    assert attempt.native_tool_call is None
    assert not attempt.capability_events[-1].tool_attempt_id
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_dynamic_run_command_keeps_exact_canonical_handoff(tmp_path):
    client, attempt, store, sent, interrupts = _client(tmp_path)
    params = {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "callId": "canonical-call",
        "tool": "run_command",
        "arguments": {"command": "echo OSIA_CANONICAL"},
    }

    client._handle_server_request({
        "id": 18,
        "method": "item/tool/call",
        "params": params,
    })

    assert sent[0]["id"] == 18
    assert sent[0]["result"]["success"] is False
    assert "canonical tool pipeline" in sent[0]["result"]["contentItems"][0]["text"]
    assert attempt.native_tool_call == {
        "tool": "run_command",
        "arguments": params["arguments"],
        "call_id": "canonical-call",
    }
    assert attempt.unsafe_category == "canonical_tool:run_command"
    assert interrupts == [("turn/interrupt", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
    })]
    assert list(store.attempts_dir.glob("attempt_*.json")) == []
