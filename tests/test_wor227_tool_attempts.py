import json
import threading
import time

import pytest

from osia import conversations
from osia import tool_attempts
from osia.tool_attempts import (
    CorruptAttemptError,
    IllegalTransition,
    RevisionConflict,
    ToolAttemptCoordinator,
    ToolAttemptStatus,
    ToolAttemptStore,
)


def _create(store: ToolAttemptStore, *, conversation_id: str = "conv_fixture"):
    fingerprint = store.fingerprint_arguments({"private": "synthetic-value"})
    return store.create_attempt(
        conversation_id=conversation_id,
        turn_id="turn_fixture",
        request_id="request_fixture",
        tool_call_id="call_fixture",
        tool_name="activate_browser_element",
        provider_id="provider_fixture",
        model_id="provider/model-fixture",
        turn_context={
            "language": "en",
            "source": "fixture",
            "turn_ref": "turn_ref_fixture",
            "allowed_languages": ["en"],
        },
        argument_fingerprint=fingerprint,
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        deadline_at=2_000_000_000.0,
        postcondition_type="popup_creation",
        final_response_message_id="assistant_fixture",
    )


def _running(store: ToolAttemptStore, *, conversation_id: str = "conv_fixture"):
    created = _create(store, conversation_id=conversation_id)
    accepted = store.mark_accepted(
        created["tool_attempt_id"], expected_revision=created["revision"],
    )
    running = store.mark_running(
        created["tool_attempt_id"], expected_revision=accepted["revision"],
    )
    return running


def test_valid_state_transitions_and_startup_recovery(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _running(store)
    pending = store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=1_900_000_000.0,
        deadline_at=2_000_000_000.0,
    )
    waiting = store.mark_waiting_verification(
        pending["tool_attempt_id"],
        next_check_at=1_900_000_010.0,
        deadline_at=2_000_000_000.0,
    )
    assert waiting["status"] == ToolAttemptStatus.WAITING_VERIFICATION.value
    assert [item["tool_attempt_id"] for item in store.list_recoverable_attempts()] == [
        waiting["tool_attempt_id"]
    ]
    terminal, created = store.record_terminal_result(
        waiting["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
        reason_code="postcondition_verified",
    )
    assert created is True
    assert terminal["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert store.list_recoverable_attempts() == []


def test_terminal_cannot_return_to_running(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _running(store)
    store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
    )
    with pytest.raises(IllegalTransition, match="immutable"):
        store.mark_running(running["tool_attempt_id"])


def test_atomic_revision_conflict(tmp_path):
    store = ToolAttemptStore(tmp_path)
    created = _create(store)
    accepted = store.mark_accepted(
        created["tool_attempt_id"], expected_revision=created["revision"],
    )
    with pytest.raises(RevisionConflict):
        store.mark_running(
            accepted["tool_attempt_id"], expected_revision=created["revision"],
        )


def test_duplicate_callback_and_timer_after_callback_are_noops(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _running(store)
    first, first_created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )
    duplicate, duplicate_created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )
    timer = store.record_progress(running["tool_attempt_id"], phase="timer_check")
    assert first_created is True
    assert duplicate_created is False
    assert duplicate["revision"] == first["revision"]
    assert timer["revision"] == first["revision"]


def test_corrupt_record_fails_closed_and_is_not_recoverable(tmp_path):
    store = ToolAttemptStore(tmp_path)
    store.attempts_dir.mkdir(parents=True)
    path = store.attempts_dir / "attempt_corrupt.json"
    path.write_text('{"schema_version":1,"status":"running"}', encoding="utf-8")
    with pytest.raises(CorruptAttemptError):
        store.get_attempt("attempt_corrupt")
    assert store.list_recoverable_attempts() == []


def test_cleanup_preserves_anti_replay_barrier(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _running(store)
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
    )
    claim = store.claim_continuation(running["tool_attempt_id"])
    assert claim
    assert store.complete_continuation(
        running["tool_attempt_id"],
        claim,
        final_response_message_id="assistant_fixture",
    )
    outcome = store.cleanup(
        now=float(terminal["completed_at"]) + 20,
        retention_seconds=10,
        barrier_retention_seconds=100,
    )
    assert outcome == {"compacted": 1, "removed": 0}
    assert store.get_attempt(running["tool_attempt_id"]) is None
    assert store.has_terminal_barrier(
        conversation_id=terminal["conversation_id"],
        turn_id=terminal["turn_id"],
        tool_name=terminal["tool_name"],
        argument_fingerprint=terminal["argument_fingerprint"],
    )


def test_cleanup_keeps_terminal_receipt_until_continuation_is_complete(tmp_path):
    store = ToolAttemptStore(tmp_path)
    running = _running(store)
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )

    outcome = store.cleanup(
        now=float(terminal["completed_at"]) + 20,
        retention_seconds=10,
        barrier_retention_seconds=100,
    )

    assert outcome == {"compacted": 0, "removed": 0}
    assert store.get_attempt(running["tool_attempt_id"]) is not None


def test_store_never_persists_raw_arguments(tmp_path):
    store = ToolAttemptStore(tmp_path)
    _create(store)
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*.json")
    )
    assert "synthetic-value" not in persisted
    assert "private" not in persisted


def test_only_one_active_suspended_turn_per_conversation(tmp_path):
    store = ToolAttemptStore(tmp_path)
    _create(store)
    with pytest.raises(IllegalTransition, match="active_tool_attempt"):
        _create(store)


def test_startup_recovery_persists_one_correlated_final_without_replay(
    tmp_path, monkeypatch,
):
    conversation_dir = tmp_path / "conversations"
    store = ToolAttemptStore(tmp_path / "tool-attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", conversation_dir)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)

    conversation = conversations.create_conversation()
    conversations.commit_message(
        conversation["id"],
        "user",
        "Perform the controlled fixture action",
        metadata={
            "_request_id": "request_fixture",
            "_client_request_id": "request_fixture",
            "_turn_id": "turn_fixture",
            "_message_id": "user_fixture",
            "_operation_id": "user_fixture",
        },
    )
    running = _running(store, conversation_id=conversation["id"])
    store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=1_900_000_000.0,
        deadline_at=2_000_000_000.0,
    )

    recovered = tool_attempts.recover_orphaned_tool_attempts()
    after_first = conversations.get_conversation(conversation["id"])
    assert len(recovered) == 1
    assert [item["role"] for item in after_first["messages"]] == [
        "user", "assistant",
    ]
    assert "did not repeat it" in after_first["messages"][-1]["content"]
    terminal = store.get_attempt(running["tool_attempt_id"])
    assert terminal["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert terminal["continuation_state"] == "complete"

    assert tool_attempts.recover_orphaned_tool_attempts() == []
    after_second = conversations.get_conversation(conversation["id"])
    assert len(after_second["messages"]) == 2


def test_startup_recovery_targets_actual_unverified_mutation_without_redispatch(
    tmp_path, monkeypatch,
):
    from osia import agent as agent_module
    from osia.tool_contracts import ExecutionOutcome
    from osia.turn_context import fixed_turn_context

    store = ToolAttemptStore(tmp_path / "tool-attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(
        conversations, "CONVERSATIONS_DIR", tmp_path / "conversations",
    )
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    conversation = conversations.create_conversation()
    conversations.commit_message(
        conversation["id"],
        "user",
        "Activate the controlled fixture once",
        metadata={
            "_request_id": "request_restart_fixture",
            "_client_request_id": "request_restart_fixture",
            "_turn_id": "turn_restart_fixture",
            "_message_id": "user_restart_fixture",
            "_operation_id": "user_restart_fixture",
        },
    )
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "find_and_click",
        lambda text: dispatches.append(text) or "controlled click accepted",
    )
    context = {
        "enabled": True,
        "store": store,
        "conversation_id": conversation["id"],
        "turn_id": "turn_restart_fixture",
        "request_id": "request_restart_fixture",
        "tool_call_id": "toolcall_restart_fixture",
        "provider_id": "provider_restart_fixture",
        "model_id": "model_restart_fixture",
        "turn_context": fixed_turn_context(
            "en", source="fixture", reference="turn_restart_fixture",
        ),
        "final_response_message_id": "assistant_restart_fixture",
        "terminal_callback": coordinator.notify_terminal,
    }

    pending = agent_module._execute_tool_with_policy(
        "find_and_click",
        {"text": "Activate controlled fixture"},
        lang="en",
        attempt_context=context,
    )

    assert pending.outcome == ExecutionOutcome.PENDING
    assert dispatches == ["Activate controlled fixture"]
    before = store.get_attempt(pending.tool_attempt_id)
    assert before["tool_name"] == "find_and_click"
    assert before["status"] == ToolAttemptStatus.WAITING_VERIFICATION.value
    assert before["provider_id"] == "provider_restart_fixture"
    assert before["model_id"] == "model_restart_fixture"
    assert before["turn_id"] == "turn_restart_fixture"
    assert len(list(store.attempts_dir.glob("attempt_*.json"))) == 1

    recovered = tool_attempts.recover_orphaned_tool_attempts()

    assert recovered == []
    waiting = store.get_attempt(pending.tool_attempt_id)
    assert waiting["status"] == ToolAttemptStatus.WAITING_VERIFICATION.value
    assert waiting["reason_code"] == "restart_reconciliation_pending"
    assert conversations.get_conversation(conversation["id"])["messages"][-1][
        "role"
    ] == "user"

    store.mark_waiting_verification(
        pending.tool_attempt_id,
        next_check_at=time.time() - 1.0,
        deadline_at=time.time() - 0.5,
        reason_code="restart_reconciliation_pending",
        owner_settled=True,
    )
    recovered = tool_attempts.recover_orphaned_tool_attempts()
    assert len(recovered) == 1
    assert recovered[0]["tool_attempt_id"] == pending.tool_attempt_id
    assert dispatches == ["Activate controlled fixture"]
    terminal = store.get_attempt(pending.tool_attempt_id)
    assert terminal["tool_name"] == "find_and_click"
    assert terminal["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert terminal["reason_code"] == "reconciliation_hard_deadline"
    assert terminal["dispatch_state"] == "effect_unknown"
    assert terminal["provider_id"] == "provider_restart_fixture"
    assert terminal["model_id"] == "model_restart_fixture"
    assert terminal["turn_id"] == "turn_restart_fixture"
    assert terminal["continuation_state"] == "complete"
    assert len(list(store.attempts_dir.glob("attempt_*.json"))) == 1
    stored = conversations.get_conversation(conversation["id"])["messages"]
    assert [(item["role"], item["content"]) for item in stored] == [
        ("user", "Activate the controlled fixture once"),
        (
            "assistant",
            "Read-only verification reached its hard limit. OSIA did not "
            "repeat the action.",
        ),
    ]


def test_terminal_callback_after_completed_continuation_is_discarded(tmp_path):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    running = _running(store)
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )
    claim = store.claim_continuation(running["tool_attempt_id"])
    assert claim
    assert store.complete_continuation(
        running["tool_attempt_id"],
        claim,
        final_response_message_id="assistant_fixture",
    )

    coordinator.notify_terminal(running["tool_attempt_id"], terminal)

    assert running["tool_attempt_id"] not in coordinator._early_terminal


def test_timer_recovers_durable_terminal_when_callback_was_lost(tmp_path):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    running = _running(store)
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )
    resumed = []
    finalized = threading.Event()

    coordinator.register_suspended(
        running["tool_attempt_id"],
        resume=lambda attempt_id, payload: resumed.append(
            (attempt_id, payload["status"])
        ) or "fixture final",
        finalize=lambda *_args: finalized.set() or True,
    )
    assert coordinator.activate(running["tool_attempt_id"])
    assert finalized.wait(2.0)

    assert resumed == [
        (running["tool_attempt_id"], terminal["status"]),
    ]
    deadline = time.monotonic() + 2.0
    completed = store.get_attempt(running["tool_attempt_id"])
    while completed["continuation_state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.01)
        completed = store.get_attempt(running["tool_attempt_id"])
    assert completed["continuation_state"] == "complete"


def test_fingerprint_key_is_stable_across_store_instances(tmp_path):
    first = ToolAttemptStore(tmp_path)
    second = ToolAttemptStore(tmp_path)
    arguments = {"fixture": "same-private-value"}

    assert first.fingerprint_arguments(arguments) == second.fingerprint_arguments(
        arguments
    )
    assert len((tmp_path / ".fingerprint.key").read_bytes()) == 32


def test_startup_recovers_terminal_receipt_left_before_continuation(
    tmp_path, monkeypatch,
):
    conversation_dir = tmp_path / "conversations"
    store = ToolAttemptStore(tmp_path / "tool-attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", conversation_dir)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    conversation = conversations.create_conversation()
    conversations.commit_message(
        conversation["id"],
        "user",
        "Run the synthetic action",
        metadata={
            "_request_id": "request_fixture",
            "_client_request_id": "request_fixture",
            "_turn_id": "turn_fixture",
            "_message_id": "user_fixture",
            "_operation_id": "user_fixture",
        },
    )
    running = _running(store, conversation_id=conversation["id"])
    store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )

    recovered = tool_attempts.recover_orphaned_tool_attempts()

    assert len(recovered) == 1
    stored = conversations.get_conversation(conversation["id"])["messages"]
    assert len(stored) == 2
    assert "receipt was recorded" in stored[-1]["content"]
    assert store.get_attempt(running["tool_attempt_id"])[
        "continuation_state"
    ] == "complete"


@pytest.mark.parametrize("finalized, expected_state", [
    (True, "complete"),
    (False, "not_claimed"),
])
def test_continuation_claim_tracks_final_persistence(
    tmp_path, finalized, expected_state,
):
    store = ToolAttemptStore(tmp_path)
    coordinator = ToolAttemptCoordinator(store)
    running = _running(store)
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="confirmed_success",
        dispatch_state="effect_confirmed",
    )
    finished = threading.Event()

    def resume(*_args):
        if finalized:
            raise RuntimeError("synthetic continuation failure")
        return "unpersisted final"

    coordinator.register_suspended(
        running["tool_attempt_id"],
        resume=resume,
        finalize=lambda *_args: finished.set() or finalized,
    )
    coordinator.notify_terminal(running["tool_attempt_id"], terminal)
    assert coordinator.activate(running["tool_attempt_id"])
    assert finished.wait(2.0)
    deadline = time.monotonic() + 2.0
    current = store.get_attempt(running["tool_attempt_id"])
    while current["continuation_state"] != expected_state and time.monotonic() < deadline:
        time.sleep(0.01)
        current = store.get_attempt(running["tool_attempt_id"])
    assert current["continuation_state"] == expected_state
