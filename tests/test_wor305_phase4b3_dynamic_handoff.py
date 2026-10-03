"""The exact dynamic call must finish its reply and hand off once."""

from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt


def test_canonical_dynamic_tool_reply_handoffs_without_reader_failure(tmp_path):
    client = AppServerClient(base_dir=tmp_path / "native")
    attempt = _TurnAttempt(thread_id="provider_thread", turn_id="provider_turn")
    client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    client._send = sent.append
    client._send_request_no_wait = lambda method, params: interrupts.append((method, params))
    params = {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "callId": "call-write",
        "tool": "write_file",
        "arguments": {"path": str(tmp_path / "project.txt"), "content": "project"},
    }

    client._handle_server_request({"id": 41, "method": "item/tool/call", "params": params})
    client._handle_server_request({"id": 42, "method": "item/tool/call", "params": params})

    assert [message["result"]["success"] for message in sent] == [False, False]
    assert attempt.native_tool_call == {
        "tool": "write_file",
        "arguments": params["arguments"],
        "call_id": "call-write",
    }
    assert attempt.unsafe_category == "canonical_tool:write_file"
    assert attempt.events.qsize() == 1
    assert interrupts == [("turn/interrupt", {
        "threadId": attempt.thread_id, "turnId": attempt.turn_id,
    })]


def test_foreign_turn_dynamic_tool_cannot_handoff(tmp_path):
    client = AppServerClient(base_dir=tmp_path / "native")
    attempt = _TurnAttempt(thread_id="provider_thread", turn_id="provider_turn")
    client._attempts[attempt.thread_id] = attempt
    sent = []
    client._send = sent.append
    client._send_request_no_wait = lambda _method, _params: None

    client._handle_server_request({"id": 43, "method": "item/tool/call", "params": {
        "threadId": attempt.thread_id,
        "turnId": "stale_turn",
        "callId": "call-stale",
        "tool": "write_file",
        "arguments": {"path": str(tmp_path / "foreign.txt"), "content": "foreign"},
    }})

    assert sent[0]["result"]["success"] is False
    assert not attempt.native_action
    assert attempt.native_tool_call is None
    assert attempt.events.empty()
