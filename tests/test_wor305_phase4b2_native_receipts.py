"""Provider-native physical items never receive OSIA attempt receipts."""

from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
from osia.tool_attempts import ToolAttemptStore


def _setup(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(
        base_dir=tmp_path / "account",
        tool_attempt_store=store,
        compact_logs_on_startup=False,
    )
    attempt = _TurnAttempt(
        thread_id="thread-native",
        turn_id="turn-native",
        conversation_id="conversation",
        osia_turn_id="osia-turn",
        model_id="selected-model",
    )
    client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    client._send = sent.append
    client._send_request_no_wait = lambda method, params: interrupts.append(
        (method, params)
    )
    return client, attempt, store, sent, interrupts


def test_command_start_is_unknown_and_cannot_be_adopted(tmp_path):
    client, attempt, store, _sent, interrupts = _setup(tmp_path)
    item = {
        "id": "exec-unapproved",
        "type": "commandExecution",
        "status": "inProgress",
        "command": "echo SHOULD_NOT_RUN",
        "commandActions": [],
    }
    client._dispatch_notification("item/started", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "item": item,
    })
    client._dispatch_notification("item/completed", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "item": {
            **item,
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": "SHOULD_NOT_BE_ADOPTED",
        },
    })

    assert attempt.unsafe_category == "unbrokered_native_item:commandExecution"
    assert attempt.outcome_unknown is True
    assert not any(
        event.outcome == "success_verified"
        for event in attempt.capability_events
    )
    assert all(not event.tool_attempt_id for event in attempt.capability_events)
    assert len(interrupts) == 1
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_raw_file_change_approval_declines_without_attempt(tmp_path):
    client, attempt, store, sent, _interrupts = _setup(tmp_path)
    client._handle_server_request({
        "id": 23,
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "file-change",
        },
    })

    assert sent == [{"id": 23, "result": {"decision": "decline"}}]
    assert attempt.capability_events[-1].identity == "fileChange"
    assert attempt.capability_events[-1].reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert not attempt.capability_events[-1].tool_attempt_id
    assert list(store.attempts_dir.glob("attempt_*.json")) == []
