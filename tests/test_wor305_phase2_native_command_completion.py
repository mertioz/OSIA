"""Provider-native physical items never become canonical process receipts."""

from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
from osia.tool_attempts import ToolAttemptStore


def _setup(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(
        base_dir=tmp_path / "native",
        tool_attempt_store=store,
        compact_logs_on_startup=False,
    )
    attempt = _TurnAttempt(
        thread_id="provider-thread",
        turn_id="provider-turn",
        conversation_id="conversation",
        osia_turn_id="turn",
        model_id="selected-model",
        user_request="Run a build check",
    )
    client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    client._send = sent.append
    client._send_request_no_wait = lambda method, params: interrupts.append(
        (method, params)
    )
    return client, attempt, store, sent, interrupts


def test_raw_command_approval_is_declined_with_exact_correlation(tmp_path):
    client, attempt, store, sent, _interrupts = _setup(tmp_path)
    client._handle_server_request({
        "id": "request-7",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "native-command",
            "command": "echo SHOULD_NOT_RUN",
        },
    })

    assert sent == [{
        "id": "request-7",
        "result": {"decision": "decline"},
    }]
    event = attempt.capability_events[-1]
    assert (event.identity, event.item_id) == (
        "commandExecution", "native-command",
    )
    assert event.reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert event.tool_attempt_id == ""
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_unexpected_command_completion_cannot_create_process_success(tmp_path):
    client, attempt, store, _sent, interrupts = _setup(tmp_path)
    client._dispatch_notification("item/completed", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "item": {
            "id": "unapproved-command",
            "type": "commandExecution",
            "status": "completed",
            "command": "echo SHOULD_NOT_BE_ADOPTED",
            "commandActions": [],
            "exitCode": 0,
            "aggregatedOutput": "SHOULD_NOT_BE_ADOPTED",
        },
    })

    event = attempt.capability_events[-1]
    assert event.outcome == "outcome_unknown"
    assert event.process_facts is None
    assert event.tool_attempt_id == ""
    assert attempt.outcome_unknown is True
    assert attempt.native_action is True
    assert len(interrupts) == 1
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_unexpected_file_change_completion_cannot_be_adopted(tmp_path):
    client, attempt, store, _sent, interrupts = _setup(tmp_path)
    client._dispatch_notification("item/completed", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "item": {
            "id": "unapproved-file-change",
            "type": "fileChange",
            "status": "completed",
            "changes": [{
                "path": "would-change.txt",
                "kind": "add",
                "diff": "+must-not-be-adopted",
            }],
        },
    })

    event = attempt.capability_events[-1]
    assert event.outcome == "outcome_unknown"
    assert event.reason_code == "UNEXPECTED_PROVIDER_NATIVE_PHYSICAL_EFFECT"
    assert event.tool_attempt_id == ""
    assert attempt.unsafe_category == "unbrokered_native_item:fileChange"
    assert len(interrupts) == 1
    assert list(store.attempts_dir.glob("attempt_*.json")) == []
