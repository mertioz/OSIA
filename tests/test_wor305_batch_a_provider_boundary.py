"""WOR-305 Batch A: provider-native execution and model identity boundaries."""

import json

import pytest

from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    AppServerError,
    _TurnAttempt,
)
from osia.tool_attempts import ToolAttemptStore


MODEL = "gpt-5.6-sol"


def _client(tmp_path, *, register=True):
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(
        base_dir=tmp_path / "codex",
        tool_attempt_store=store,
        compact_logs_on_startup=False,
    )
    attempt = _TurnAttempt(
        thread_id="provider-thread",
        turn_id="provider-turn",
        conversation_id="conversation",
        osia_turn_id="canonical-turn",
        model_id=MODEL,
        effective_model_id=MODEL,
        user_request="Print a harmless marker.",
    )
    if register:
        client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    client._send = sent.append
    client._send_request_no_wait = lambda method, params: interrupts.append(
        (method, params)
    )
    return client, attempt, store, sent, interrupts


@pytest.mark.parametrize(
    ("method", "item_id"),
    [
        ("item/commandExecution/requestApproval", "command-item"),
        ("item/fileChange/requestApproval", "file-item"),
    ],
)
def test_raw_native_physical_requests_are_correlated_and_declined(
    tmp_path, monkeypatch, method, item_id,
):
    client, attempt, store, sent, _interrupts = _client(tmp_path)

    def forbidden_attempt_store():
        pytest.fail("provider-native requests must not create ToolAttempts")

    monkeypatch.setattr(client, "_attempt_store", forbidden_attempt_store)
    client._handle_server_request({
        "id": "request-42",
        "method": method,
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": item_id,
            "command": "echo SHOULD_NOT_RUN",
        },
    })

    assert sent == [{
        "id": "request-42",
        "result": {"decision": "decline"},
    }]
    event = attempt.capability_events[-1]
    assert event.item_id == item_id
    assert event.identity == (
        "commandExecution" if "commandExecution" in method else "fileChange"
    )
    assert event.outcome == "rejected_before_execution"
    assert event.reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert attempt.events.empty()
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


@pytest.mark.parametrize(
    "item",
    [
        {
            "id": "unexpected-command",
            "type": "commandExecution",
            "status": "completed",
            "command": "echo SHOULD_NOT_BE_ADOPTED",
            "exitCode": 0,
            "aggregatedOutput": "SHOULD_NOT_BE_ADOPTED",
        },
        {
            "id": "unexpected-file",
            "type": "fileChange",
            "status": "completed",
            "changes": [{"path": "would-change.txt", "kind": "add"}],
        },
    ],
)
def test_completed_native_physical_item_stays_unknown_without_canonical_success(
    tmp_path, item,
):
    client, attempt, store, _sent, interrupts = _client(tmp_path)
    client._complete_native_item(attempt, item)

    event = attempt.capability_events[-1]
    assert event.item_id == item["id"]
    assert event.outcome == "outcome_unknown"
    assert event.reason_code == "UNEXPECTED_PROVIDER_NATIVE_PHYSICAL_EFFECT"
    assert event.tool_attempt_id == ""
    assert attempt.outcome_unknown is True
    assert attempt.native_effect_started is True
    assert attempt.unsafe_category == f"unbrokered_native_item:{item['type']}"
    assert len(interrupts) == 1
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_unapproved_native_item_start_interrupts_before_adoption(tmp_path):
    client, attempt, store, _sent, interrupts = _client(tmp_path)
    client._dispatch_notification("item/started", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "item": {
            "id": "unapproved-command",
            "type": "commandExecution",
            "status": "inProgress",
            "command": "echo SHOULD_NOT_RUN",
            "commandActions": [],
        },
    })

    event = attempt.capability_events[-1]
    assert event.item_id == "unapproved-command"
    assert event.phase == "started"
    assert event.outcome == "outcome_unknown"
    assert event.reason_code == "UNEXPECTED_PROVIDER_NATIVE_PHYSICAL_EFFECT"
    assert attempt.unsafe_category == "unbrokered_native_item:commandExecution"
    assert len(interrupts) == 1
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def _run_turn_with_events(
    client, *, model=MODEL, rerouted_model=None, handoff=None,
    image_input_present=False, observed_attempt=None,
    turn_status="completed",
):
    request_params = {}

    def request(method, params, **_kwargs):
        assert method == "turn/start"
        request_params.update(params)
        attempt = client._attempts[params["threadId"]]
        if observed_attempt is not None:
            observed_attempt["attempt"] = attempt
        attempt.turn_id = "provider-turn"
        attempt.image_input_present = image_input_present
        if rerouted_model is not None:
            client._dispatch_notification("model/rerouted", {
                "threadId": attempt.thread_id,
                "turnId": attempt.turn_id,
                "model": rerouted_model,
            })
        if handoff is not None:
            client._handle_server_request({
                "id": "dynamic-request",
                "method": "item/tool/call",
                "params": {
                    "threadId": attempt.thread_id,
                    "turnId": attempt.turn_id,
                    "callId": "dynamic-call",
                    "tool": handoff[0],
                    "arguments": handoff[1],
                },
            })
        attempt.events.put(("done", {
            "status": turn_status,
            "turnId": attempt.turn_id,
        }))
        return {"turn": {"id": attempt.turn_id}}

    client._request = request
    result = client.run_turn(
        thread_id="provider-thread",
        conversation_id="conversation",
        model=model,
        text="Print a harmless marker.",
    )
    return result, request_params


def test_same_selected_model_continues_normally(tmp_path):
    client, _attempt, _store, _sent, _interrupts = _client(
        tmp_path, register=False,
    )
    result, request_params = _run_turn_with_events(
        client, rerouted_model=f" {MODEL} ",
    )

    assert request_params["model"] == MODEL
    assert result.effective_model_id == f" {MODEL} "
    assert result.model_rerouted is True
    assert result.status == "completed"


def test_text_turn_fails_closed_on_identity_changing_reroute(tmp_path):
    client, _attempt, _store, _sent, _interrupts = _client(
        tmp_path, register=False,
    )

    with pytest.raises(AppServerError) as raised:
        _run_turn_with_events(client, rerouted_model="gpt-5.6-terra")

    assert raised.value.category == "MODEL_IDENTITY_CHANGED"
    assert raised.value.allow_fallback is False


def test_identity_change_remains_the_error_when_provider_turn_is_interrupted(tmp_path):
    client, _attempt, _store, _sent, _interrupts = _client(
        tmp_path, register=False,
    )

    with pytest.raises(AppServerError) as raised:
        _run_turn_with_events(
            client,
            rerouted_model="gpt-5.6-terra",
            turn_status="interrupted",
        )

    assert raised.value.category == "MODEL_IDENTITY_CHANGED"
    assert raised.value.allow_fallback is False


def test_changed_model_cannot_handoff_run_command_or_create_receipt(tmp_path):
    client, _attempt, store, sent, _interrupts = _client(
        tmp_path, register=False,
    )
    observed = {}

    with pytest.raises(AppServerError) as raised:
        _run_turn_with_events(
            client,
            rerouted_model="gpt-5.6-terra",
            handoff=("run_command", {"command": "echo SHOULD_NOT_RUN"}),
            observed_attempt=observed,
        )

    assert raised.value.category == "MODEL_IDENTITY_CHANGED"
    assert raised.value.allow_fallback is False
    attempt = observed["attempt"]
    assert attempt.native_tool_call is None
    assert not any(
        event.tool_attempt_id for event in attempt.capability_events
        if event.reason_code == "MODEL_IDENTITY_CHANGED"
    )
    response = next(item for item in sent if item.get("id") == "dynamic-request")
    response_payload = json.loads(response["result"]["contentItems"][0]["text"])
    assert response_payload["reason_code"] == "MODEL_IDENTITY_CHANGED"
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_existing_image_turn_reroute_protection_is_preserved(tmp_path):
    client, _attempt, _store, _sent, _interrupts = _client(
        tmp_path, register=False,
    )

    with pytest.raises(AppServerError) as raised:
        _run_turn_with_events(
            client, rerouted_model=MODEL, image_input_present=True,
        )

    assert raised.value.category == "MODEL_IDENTITY_CHANGED"
    assert raised.value.allow_fallback is False
