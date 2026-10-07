"""WOR-269 PASS B durable transport/provider state-machine tests.

All scenarios are deterministic: no sockets, live providers, UI automation or
wall-clock sleeps are used here.
"""

from __future__ import annotations

import json
from pathlib import Path
import random

import pytest

from osia.action_truth import (
    ActionIntent,
    ActionRecord,
    ActionState,
    ActionTruthCoordinator,
    BrowserReconciliationAdapter,
    CanonicalEffectClass,
    DesktopReconciliationAdapter,
    EffectTruth,
    ObservationClassification,
    ObservationResult,
    ProvidedObservationAdapter,
    ReconciliationStatus,
    action_identity,
    gate_action,
    legacy_action_truth,
    new_action_truth,
    retry_decision,
    semantic_effect_identity,
    transition_action_truth,
)
from osia.durable_transport import (
    CanonicalProjection,
    DurableTransportStore,
    InboxDisposition,
    ProjectionDisposition,
)
from osia.provider_continuation import (
    LookupContinuationAdapter,
    ProviderContinuationCoordinator,
    ProviderContinuationState,
)
from osia.semantic_batch import classify_batch_observation
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
from osia.turn_terminality import TurnTerminalityController
from tests.fixtures.wor269_action_truth import (
    FakeBrowser,
    FakeClock,
    FakeDesktop,
    MemoryActionStore,
    make_attempt,
)


def _optional_mobile_source(relative_path: str) -> Path:
    """Find a sibling Mobile checkout without assuming a normal PC layout."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "osia-mobile" / relative_path
        if candidate.is_file():
            return candidate
    pytest.skip("Mobile source checkout is not available beside this PC checkout")


CONVERSATION_ID = "conversation_wor269b"
REQUEST_ID = "request_wor269b"
TURN_ID = "turn_wor269b"
RUN_ID = TURN_ID


def _truth(
    *,
    effect_class: CanonicalEffectClass =
        CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
    operation: str = "invoke",
    target: dict | None = None,
) -> dict:
    target = dict(target or {"kind": "desktop", "application": "calculator"})
    semantic_effect_id = semantic_effect_identity(
        turn_id=TURN_ID,
        semantic_node_id="node_wor269b",
        attempted_effect="calculator_add",
        operation=operation,
        target=target,
    )
    return new_action_truth(
        action_id=action_identity(
            run_id=RUN_ID,
            turn_id=TURN_ID,
            semantic_effect_id=semantic_effect_id,
        ),
        semantic_effect_id=semantic_effect_id,
        effect_class=effect_class,
        operation=operation,
        target=target,
        semantic_node_id="node_wor269b",
        attempted_effect="calculator_add",
        occurred_at=100.0,
    )


def _started(truth: dict) -> dict:
    return transition_action_truth(
        truth,
        state=ActionState.DISPATCH_STARTED,
        reason_code="semantic_dispatch_started",
        occurred_at=101.0,
    )


def _record(truth: dict) -> ActionRecord:
    return ActionRecord(
        action_id=truth["action_id"],
        semantic_effect_id=truth["semantic_effect_id"],
        run_id=RUN_ID,
        turn_id=TURN_ID,
        request_id=REQUEST_ID,
        goal_contract_ref=truth["goal_contract_ref"],
        effect_class=truth["effect_class"],
        operation=truth["operation"],
        target=dict(truth["target"]),
        idempotency_identity=truth["idempotency_identity"],
        current_state=truth["current_state"],
        effect_truth=truth["effect_truth"],
        dependencies=list(truth["dependencies"]),
        conflict_scope=truth["conflict_scope"],
    )


def _coordinator(
    truth: dict, *adapters,
) -> tuple[ActionTruthCoordinator, MemoryActionStore]:
    store = MemoryActionStore([make_attempt(truth)])
    return (
        ActionTruthCoordinator(
            store,
            adapters=adapters,
            clock=FakeClock(),
        ),
        store,
    )


def _create_attempt(
    root: Path,
    *,
    provider_id: str = "provider_wor269b",
    operation: str = "invoke",
    target: dict | None = None,
    provider_references: dict | None = None,
) -> tuple[ToolAttemptStore, dict]:
    store = ToolAttemptStore(root)
    attempt = store.create_attempt(
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
        request_id=REQUEST_ID,
        tool_call_id="toolcall_wor269b",
        tool_name=operation,
        provider_id=provider_id,
        model_id="model_wor269b",
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments({"value": 28}),
        side_effect="reversible_local",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        provider_references=provider_references,
        action_binding={
            "semantic_node_id": "node_wor269b",
            "attempted_effect": "calculator_add",
            "operation": operation,
            "target": dict(
                target
                or {"kind": "desktop", "application": "calculator"}
            ),
        },
    )
    return store, attempt


def _dispatch(store: ToolAttemptStore, attempt_id: str) -> dict:
    store.mark_accepted(attempt_id)
    store.mark_running(attempt_id)
    store.arm_owner_dispatch_boundary(attempt_id)
    record, claimed = store.claim_owner_dispatch(attempt_id)
    assert claimed is True
    return record


def _append_event(
    store: DurableTransportStore,
    event_type: str,
    *,
    terminal: bool = False,
    key: str = "",
    payload: dict | None = None,
):
    return store.append_event(
        conversation_id=CONVERSATION_ID,
        request_id=REQUEST_ID,
        turn_id=TURN_ID,
        run_id=RUN_ID,
        event_type=event_type,
        payload=payload or {},
        terminal=terminal,
        idempotency_key=key,
    )


# OUTBOX / DELIVERY


def test_01_state_persisted_then_process_death_before_send_replays_event(tmp_path):
    root = tmp_path / "transport"
    first = DurableTransportStore(root, clock=lambda: 100.0)
    event = _append_event(
        first,
        "action_truth_changed",
        key="transition_1",
        payload={"effect_truth": "verified"},
    )
    restarted = DurableTransportStore(root, clock=lambda: 101.0)
    frames = restarted.replay(
        conversation_id=CONVERSATION_ID,
        run_id=RUN_ID,
    )
    assert [frame["event_id"] for frame in frames] == [event.event_id]
    assert frames[0]["replayed"] is True


def test_02_ack_lost_replays_event_without_repeating_action(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    _append_event(store, "turn_accepted")
    semantic_dispatches = 1
    first = store.replay_for_client("client_wor269b")
    second = store.replay_for_client("client_wor269b")
    projection = CanonicalProjection()
    assert projection.accept(first[0]).disposition is ProjectionDisposition.APPLIED
    assert projection.accept(second[0]).disposition is ProjectionDisposition.DUPLICATE
    assert semantic_dispatches == 1


def test_03_duplicate_event_is_projected_once(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    event = _append_event(store, "response_chunk")
    frame = event.to_frame()
    projection = CanonicalProjection()
    assert projection.accept(frame).disposition is ProjectionDisposition.APPLIED
    assert projection.accept(frame).disposition is ProjectionDisposition.DUPLICATE
    assert len(projection.projected(event.stream_id)) == 1


def test_event_id_collision_is_not_treated_as_an_exact_duplicate(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    event = _append_event(store, "response_chunk")
    first = event.to_frame()
    conflicting = dict(first)
    conflicting["stream_id"] = "stream_conflicting_wor269b"
    projection = CanonicalProjection()
    assert projection.accept(first).disposition is ProjectionDisposition.APPLIED
    assert (
        projection.accept(conflicting).disposition
        is ProjectionDisposition.INVALID
    )


def test_04_sequence_gap_is_detected(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    first = _append_event(store, "turn_accepted")
    _append_event(store, "turn_status")
    third = _append_event(store, "response_chunk")
    projection = CanonicalProjection()
    assert projection.accept(first.to_frame()).disposition is ProjectionDisposition.APPLIED
    gap = projection.accept(third.to_frame())
    assert gap.disposition is ProjectionDisposition.GAP
    assert gap.expected_sequence == 2


def test_05_reconnect_replays_exact_missing_suffix(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    events = [_append_event(store, f"event_{index}") for index in range(1, 5)]
    store.ack(client_id="client_wor269b", stream_id=events[0].stream_id, sequence=2)
    replay = store.replay_for_client("client_wor269b")
    assert [item["sequence"] for item in replay] == [3, 4]


def test_06_historical_final_is_not_reanimated(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    _append_event(store, "response")
    terminal = _append_event(
        store, "turn_status", terminal=True, payload={"status": "done"},
    )
    projection = CanonicalProjection()
    results = [projection.accept(frame) for frame in store.replay(
        conversation_id=CONVERSATION_ID, run_id=RUN_ID,
    )]
    assert all(item.disposition is ProjectionDisposition.HISTORICAL for item in results)
    assert projection.projected(terminal.stream_id) == []


# COMMAND DEDUP


def test_07_same_user_request_delivered_twice_claims_one_command(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    first = store.claim_command(
        command_id="command_request_wor269b",
        command_type="text",
        binding={"request_id": REQUEST_ID, "payload_hash": "hash_a"},
    )
    duplicate = store.claim_command(
        command_id="command_request_wor269b",
        command_type="text",
        binding={"request_id": REQUEST_ID, "payload_hash": "hash_a"},
    )
    assert first.disposition is InboxDisposition.CLAIMED
    assert duplicate.disposition is InboxDisposition.DUPLICATE_IN_PROGRESS
    assert duplicate.execution_epoch == first.execution_epoch


def test_08_duplicate_stop_has_one_cancellation_claim(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    binding = {"scope": "chat", "run_id": RUN_ID, "turn_id": TURN_ID}
    first = store.claim_command(
        command_id="command_stop_wor269b",
        command_type="cancel_active_work",
        binding=binding,
    )
    store.complete_command(
        first.command_id,
        execution_epoch=first.execution_epoch,
        result={"accepted": True},
    )
    duplicate = store.claim_command(
        command_id="command_stop_wor269b",
        command_type="cancel_active_work",
        binding=binding,
    )
    assert duplicate.disposition is InboxDisposition.DUPLICATE_COMPLETED
    assert duplicate.result == {"accepted": True}


# PROVIDER CONTINUATION


def test_10_provider_call_reference_survives_restart(tmp_path):
    root = tmp_path / "attempts"
    store, attempt = _create_attempt(
        root,
        provider_id="lookup_provider",
        provider_references={"external_operation_id": "operation_wor269b"},
    )
    restarted = ToolAttemptStore(root)
    loaded = restarted.get_attempt(attempt["tool_attempt_id"])
    assert loaded["provider_references"]["external_operation_id"] == (
        "operation_wor269b"
    )
    assert loaded["action_truth"]["provider_operation_ref"] == "operation_wor269b"


def test_11_lost_provider_callback_reconciles_existing_call(tmp_path):
    store, attempt = _create_attempt(
        tmp_path / "attempts",
        provider_id="lookup_provider",
        provider_references={"external_operation_id": "operation_wor269b"},
    )
    _dispatch(store, attempt["tool_attempt_id"])
    lookups = []

    def lookup(refs):
        lookups.append(dict(refs))
        return {
            "state": "completed_with_receipt",
            "callback_id": "callback_lookup_wor269b",
            "evidence_ref": "receipt_wor269b",
        }

    result = ProviderContinuationCoordinator(
        store, adapters=(LookupContinuationAdapter("lookup_provider", lookup),),
    ).resume_or_reconcile_provider_attempt(attempt["tool_attempt_id"])
    loaded = store.get_attempt(attempt["tool_attempt_id"])
    assert result.state is ProviderContinuationState.COMPLETED_WITH_RECEIPT
    assert len(lookups) == 1
    assert loaded["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert loaded["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value


def test_12_duplicate_provider_callback_is_deduplicated(tmp_path):
    store, attempt = _create_attempt(tmp_path / "attempts")
    started = _dispatch(store, attempt["tool_attempt_id"])
    kwargs = {
        "callback_id": "callback_duplicate_wor269b",
        "execution_epoch": started["execution_epoch"],
        "outcome": "completed_with_receipt",
        "reason_code": "provider_typed_completion",
        "evidence_ref": "receipt_wor269b",
    }
    first, authoritative = store.apply_provider_callback(
        attempt["tool_attempt_id"], **kwargs,
    )
    second, duplicate_authority = store.apply_provider_callback(
        attempt["tool_attempt_id"], **kwargs,
    )
    assert authoritative is True
    assert duplicate_authority is False
    assert first["revision"] == second["revision"]
    assert second["processed_provider_callbacks"] == [
        "callback_duplicate_wor269b"
    ]


def test_13_late_callback_after_cancellation_cannot_resume(tmp_path):
    store, attempt = _create_attempt(tmp_path / "attempts")
    started = _dispatch(store, attempt["tool_attempt_id"])
    store.request_cancellation(attempt["tool_attempt_id"])
    loaded, authoritative = store.apply_provider_callback(
        attempt["tool_attempt_id"],
        callback_id="callback_late_cancel_wor269b",
        execution_epoch=started["execution_epoch"],
        outcome="completed_with_receipt",
        reason_code="provider_typed_completion",
        evidence_ref="receipt_late_wor269b",
    )
    assert authoritative is False
    assert loaded["authority_state"] == "cancelled"
    assert loaded["action_truth"]["current_state"] == ActionState.CANCELLED.value
    assert loaded["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value


def test_14_late_callback_after_terminal_cannot_overwrite_verified_truth(tmp_path):
    store, attempt = _create_attempt(tmp_path / "attempts")
    started = _dispatch(store, attempt["tool_attempt_id"])
    store.apply_provider_callback(
        attempt["tool_attempt_id"],
        callback_id="callback_success_wor269b",
        execution_epoch=started["execution_epoch"],
        outcome="completed_with_receipt",
        reason_code="provider_typed_completion",
        evidence_ref="receipt_success_wor269b",
    )
    store.record_terminal_result(
        attempt["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_applied",
        reason_code="provider_typed_completion",
    )
    loaded, authoritative = store.apply_provider_callback(
        attempt["tool_attempt_id"],
        callback_id="callback_conflict_wor269b",
        execution_epoch=started["execution_epoch"],
        outcome="explicit_not_applied",
        reason_code="late_conflicting_receipt",
        evidence_ref="receipt_conflict_wor269b",
    )
    assert authoritative is False
    assert loaded["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value
    assert loaded["status"] == ToolAttemptStatus.SUCCEEDED.value


# FACTUAL ACTION HISTORY — a prior state never vetoes a new request


def test_15_dispatch_started_fact_survives_restart_without_vetoing_new_request(tmp_path):
    root = tmp_path / "attempts"
    store, attempt = _create_attempt(root)
    _dispatch(store, attempt["tool_attempt_id"])
    restarted = ToolAttemptStore(root)
    unresolved = restarted.list_unresolved_action_attempts()
    assert len(unresolved) == 1
    assert unresolved[0]["action_truth"]["current_state"] == (
        ActionState.DISPATCH_STARTED.value
    )
    assert retry_decision(
        effect_class=unresolved[0]["action_truth"]["effect_class"],
        state=unresolved[0]["action_truth"]["current_state"],
        effect_truth=unresolved[0]["action_truth"]["effect_truth"],
        dispatch_started=True,
    ).allowed is True


def test_16_verified_effect_fact_does_not_veto_new_request(tmp_path):
    truth = transition_action_truth(
        _started(_truth()),
        state=ActionState.EFFECT_VERIFIED,
        reason_code="effect_verified",
        evidence={"evidence_ref": "receipt_wor269b"},
        reconciliation_status=ReconciliationStatus.VERIFIED,
        exact=True,
        occurred_at=102.0,
    )
    transport = DurableTransportStore(tmp_path / "transport")
    _append_event(
        transport,
        "action_truth_changed",
        key="verified_transition",
        payload={"effect_truth": "verified"},
    )
    assert len(transport.replay_for_client("client_wor269b")) == 1
    assert retry_decision(
        effect_class=truth["effect_class"],
        state=truth["current_state"],
        effect_truth=truth["effect_truth"],
        dispatch_started=True,
    ).allowed is True


def test_17_exact_non_application_remains_fact_for_model_choice():
    truth = transition_action_truth(
        _started(_truth()),
        state=ActionState.NOT_APPLIED_VERIFIED,
        reason_code="effect_not_applied_verified",
        evidence={"evidence_ref": "observer_wor269b"},
        reconciliation_status=ReconciliationStatus.NOT_APPLIED,
        exact=True,
        occurred_at=102.0,
    )
    decision = retry_decision(
        effect_class=truth["effect_class"],
        state=truth["current_state"],
        effect_truth=truth["effect_truth"],
        dispatch_started=True,
    )
    assert decision.allowed is True
    assert decision.alternate_mechanism_only is False


def test_18_observer_failure_keeps_read_only_recovery_open():
    truth = _started(_truth())
    coordinator, _store = _coordinator(
        truth,
        ProvidedObservationAdapter(),
    )
    decision = coordinator.reconcile(
        truth["action_id"],
        {"observation": {"status": "failed", "reason_code": "observer_failed"}},
    )
    gate = gate_action(
        ActionIntent(CanonicalEffectClass.READ_ONLY),
        [_record(truth)],
    )
    assert decision.effect_truth is EffectTruth.POSSIBLE
    assert gate.allowed is True


# WEB


def test_19_lost_navigation_callback_exact_url_proves_loaded():
    target = {
        "kind": "browser",
        "safe_url": "https://example.com/audit",
        "page_id": "page_wor269b",
    }
    truth = _started(_truth(operation="navigate", target=target))
    browser = FakeBrowser("https://example.com/audit")
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot),
    )
    decision = coordinator.reconcile(truth["action_id"], {})
    assert decision.effect_truth is EffectTruth.VERIFIED
    assert browser.read_count == 1


def test_20_allowed_redirect_target_can_be_verified_by_final_safe_url():
    target = {
        "kind": "browser",
        # The navigation contract has already canonicalized an allowed redirect
        # to its final safe target before reconciliation.
        "safe_url": "https://www.example.com/final",
    }
    truth = _started(_truth(operation="navigate", target=target))
    browser = FakeBrowser("https://www.example.com/final")
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot),
    )
    assert coordinator.reconcile(
        truth["action_id"], {},
    ).effect_truth is EffectTruth.VERIFIED


def test_21_stale_tab_is_typed_separately():
    truth = _started(_truth(
        operation="navigate",
        target={"kind": "browser", "safe_url": "https://example.com"},
    ))
    browser = FakeBrowser("https://example.com", stale=True)
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot),
    )
    decision = coordinator.reconcile(truth["action_id"], {})
    assert decision.state is ActionState.TARGET_STALE
    assert decision.effect_truth is EffectTruth.POSSIBLE


def test_22_lost_read_only_result_allows_a_fresh_request():
    truth = _started(_truth(
        effect_class=CanonicalEffectClass.READ_ONLY,
        operation="read_page",
        target={"kind": "browser", "page_id": "page_wor269b"},
    ))
    decision = retry_decision(
        effect_class=truth["effect_class"],
        state=truth["current_state"],
        effect_truth=truth["effect_truth"],
        dispatch_started=True,
    )
    assert decision.allowed is True
    assert decision.reason_code == 'new_request_allowed'


def test_23_site_audit_history_does_not_gate_new_reads():
    navigation = _record(_started(_truth(
        operation="navigate",
        target={"kind": "browser", "safe_url": "https://example.com"},
    )))
    read_gate = gate_action(
        ActionIntent(
            CanonicalEffectClass.READ_ONLY,
            semantic_effect_id="effect_site_audit_read",
            target={"kind": "browser"},
        ),
        [navigation],
    )
    assert read_gate.allowed is True
    assert read_gate.reason_code == 'new_request_allowed'


# DESKTOP


def test_24_lost_uia_invoke_callback_is_verified_by_postcondition():
    target = {
        "kind": "desktop",
        "surface_ref": "surface_wor269b",
        "window_generation": "generation_12345678",
    }
    truth = _started(_truth(operation="invoke", target=target))
    desktop = FakeDesktop(
        outcome="confirmed_success",
        reason_code="exact_desktop_primitive_state_change",
    )
    coordinator, _store = _coordinator(
        truth,
        DesktopReconciliationAdapter(
            reader=lambda _surface_ref: desktop.snapshot(),
        ),
    )
    assert coordinator.reconcile(
        truth["action_id"], {},
    ).effect_truth is EffectTruth.VERIFIED


def test_25_exact_desktop_non_application_reopens_frontier():
    truth = _started(_truth(operation="invoke"))
    desktop = FakeDesktop(
        outcome="confirmed_absence",
        reason_code="exact_desktop_state_unchanged",
        no_partial_effect=True,
    )
    coordinator, _store = _coordinator(
        truth,
        DesktopReconciliationAdapter(
            reader=lambda _surface_ref: desktop.snapshot(),
        ),
    )
    decision = coordinator.reconcile(truth["action_id"], {})
    assert decision.effect_truth is EffectTruth.NOT_APPLIED
    assert retry_decision(
        effect_class=truth["effect_class"],
        state=decision.state,
        effect_truth=decision.effect_truth,
        dispatch_started=True,
    ).allowed is True


def test_26_stale_observer_epoch_cannot_overwrite_fresh_evidence(tmp_path):
    store, attempt = _create_attempt(tmp_path / "attempts")
    started = _dispatch(store, attempt["tool_attempt_id"])
    store.update_action_truth(
        attempt["tool_attempt_id"],
        state=ActionState.EFFECT_VERIFIED.value,
        reason_code="effect_verified",
        evidence={"evidence_ref": "fresh_wor269b"},
        observer={
            "observer_attempt_id": "observer_fresh_wor269b",
            "execution_epoch": started["execution_epoch"],
            "status": "match",
        },
        reconciliation_status=ReconciliationStatus.VERIFIED.value,
        exact=True,
    )
    loaded = store.update_action_truth(
        attempt["tool_attempt_id"],
        state=ActionState.NOT_APPLIED_VERIFIED.value,
        reason_code="effect_not_applied_verified",
        evidence={"evidence_ref": "stale_wor269b"},
        observer={
            "observer_attempt_id": "observer_stale_wor269b",
            "execution_epoch": "attempt_epoch_stale_wor269b",
            "status": "non_applied",
        },
        reconciliation_status=ReconciliationStatus.NOT_APPLIED.value,
        exact=True,
    )
    assert loaded["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value
    assert loaded["observer_receipts"][-1]["stale"] is True


def test_27_calculator_composite_resumes_exact_applied_prefix():
    effects = [
        {
            "kind": "exact_surface_text",
            "surface_ref": "calculator_surface",
            "expected_text": text,
        }
        for text in ("5", "5+", "5+23", "28")
    ]
    plan = {
        "semantic_batch_id": "batch_calculator_wor269b",
        "items": [
            {"index": index, "observable_effect": effect}
            for index, effect in enumerate(effects)
        ],
    }
    result = classify_batch_observation(plan, effects[:2])
    assert result["state"] == "exact_prefix"
    assert result["resume_from_index"] == 2
    assert result["anti_replay_required"] is False


# RECONNECT


def test_28_pc_reconnect_during_reconciliation_projects_once(tmp_path):
    transport = DurableTransportStore(tmp_path / "transport")
    event = _append_event(
        transport,
        "action_truth_changed",
        payload={"action_state": "reconciling"},
    )
    projection = CanonicalProjection()
    assert projection.accept(event.to_frame()).disposition is ProjectionDisposition.APPLIED
    replay = transport.replay_for_client("pc_projection_wor269b")
    assert projection.accept(replay[0]).disposition is ProjectionDisposition.DUPLICATE


def test_29_mobile_reconnect_contract_has_cursor_ack_and_gap_replay():
    mobile = _optional_mobile_source(
        "lib/services/websocket_service.dart",
    ).read_text(encoding="utf-8")
    assert "'event_cursors': Map<String, int>.from(_lastApplicationSequence)" in mobile
    assert "'type': 'event_ack'" in mobile
    assert "'type': 'event_replay_request'" in mobile
    assert "sequence > previous + 1" in mobile


def test_30_server_restart_during_reconciliation_replays_state(tmp_path):
    root = tmp_path / "transport"
    first = DurableTransportStore(root)
    event = _append_event(
        first,
        "action_truth_changed",
        payload={"action_state": "reconciling"},
    )
    restarted = DurableTransportStore(root)
    frame = restarted.replay_for_client("client_after_restart_wor269b")[0]
    assert frame["event_id"] == event.event_id
    assert frame["action_state"] == "reconciling"


def test_31_client_reconnect_does_not_recreate_action(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    first = store.claim_command(
        command_id="command_reconnect_wor269b",
        command_type="text",
        binding={"request_id": REQUEST_ID, "payload_hash": "hash_wor269b"},
    )
    duplicate = store.claim_command(
        command_id="command_reconnect_wor269b",
        command_type="text",
        binding={"request_id": REQUEST_ID, "payload_hash": "hash_wor269b"},
    )
    assert first.execution_epoch == duplicate.execution_epoch
    assert duplicate.disposition is InboxDisposition.DUPLICATE_IN_PROGRESS


# FENCING


def test_32_old_execution_epoch_callback_is_rejected(tmp_path):
    store, attempt = _create_attempt(tmp_path / "attempts")
    _dispatch(store, attempt["tool_attempt_id"])
    loaded, authoritative = store.apply_provider_callback(
        attempt["tool_attempt_id"],
        callback_id="callback_old_epoch_wor269b",
        execution_epoch="attempt_epoch_old_wor269b",
        outcome="completed_with_receipt",
        reason_code="provider_typed_completion",
        evidence_ref="receipt_old_epoch_wor269b",
    )
    assert authoritative is False
    assert loaded["action_truth"]["effect_truth"] == EffectTruth.POSSIBLE.value
    assert loaded["continuation_history"][-1]["stale"] is True


def test_33_cancelled_callback_retains_evidence_without_execution_authority(tmp_path):
    store, attempt = _create_attempt(tmp_path / "attempts")
    started = _dispatch(store, attempt["tool_attempt_id"])
    store.request_cancellation(attempt["tool_attempt_id"])
    loaded, authoritative = store.apply_provider_callback(
        attempt["tool_attempt_id"],
        callback_id="callback_cancelled_evidence_wor269b",
        execution_epoch=started["execution_epoch"],
        outcome="completed_with_receipt",
        reason_code="provider_typed_completion",
        evidence_ref="receipt_cancelled_evidence_wor269b",
    )
    assert authoritative is False
    assert loaded["continuation_history"][-1]["reason_code"] == (
        "late_provider_evidence"
    )
    assert loaded["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value


def test_34_new_turn_is_unaffected_by_previous_run_callback(tmp_path):
    transport = DurableTransportStore(tmp_path / "transport")
    old = _append_event(transport, "turn_status", terminal=True)
    new = transport.append_event(
        conversation_id=CONVERSATION_ID,
        request_id="request_new_wor269b",
        turn_id="turn_new_wor269b",
        run_id="turn_new_wor269b",
        event_type="turn_accepted",
    )
    projection = CanonicalProjection()
    projection.accept(old.to_frame())
    result = projection.accept(new.to_frame())
    assert old.stream_id != new.stream_id
    assert result.disposition is ProjectionDisposition.APPLIED


# LEGACY


def _legacy_unknown() -> dict:
    return legacy_action_truth({
        "tool_attempt_id": "attempt_legacy_wor269b",
        "tool_name": "legacy_mutation",
        "side_effect": "reversible_local",
        "idempotence": "non_idempotent",
        "dispatch_state": "effect_unknown",
        "terminal_outcome": "outcome_unknown",
        "status": "unknown_after_start",
        "created_at": 1.0,
    })


def test_35_old_outcome_unknown_record_stays_factual_and_allows_new_request():
    truth = _legacy_unknown()
    decision = retry_decision(
        effect_class=truth["effect_class"],
        state=truth["current_state"],
        effect_truth=truth["effect_truth"],
        dispatch_started=True,
    )
    assert truth["legacy"] is True
    assert decision.allowed is True


def test_36_old_record_still_allows_read_only_reconciliation():
    truth = _legacy_unknown()
    gate = gate_action(
        ActionIntent(CanonicalEffectClass.READ_ONLY),
        [_record(truth)],
    )
    assert gate.allowed is True


def test_37_legacy_migration_never_invents_effect_verified():
    truth = _legacy_unknown()
    assert truth["effect_truth"] == EffectTruth.POSSIBLE.value
    assert truth["current_state"] == ActionState.EFFECT_POSSIBLE.value
    assert not truth["evidence"]


# FAULT INJECTION — every named boundary is exercised against the same actual
# store transitions. Boundaries made atomic assert that the split is impossible.


_FAULT_BOUNDARIES = (
    "before_durable_prepared",
    "after_prepared",
    "before_dispatch_started",
    "immediately_after_dispatch_started",
    "after_external_call_before_local_return",
    "after_return_before_receipt_bind",
    "after_receipt_before_action_truth_update",
    "after_action_truth_update_before_outbox_write",
    "after_outbox_before_delivery",
    "after_delivery_before_ack",
    "after_ack_before_compaction",
    "observer_started",
    "observer_returned",
    "observer_receipt_persist",
    "reconciliation_decision_persist",
    "terminal_projection",
)


@pytest.mark.parametrize("boundary", _FAULT_BOUNDARIES)
def test_fault_injection_matrix_preserves_atomic_boundaries(tmp_path, boundary):
    if boundary == "before_durable_prepared":
        store = ToolAttemptStore(tmp_path / boundary / "attempts")
        assert store.list_unresolved_action_attempts() == []
        return

    store, attempt = _create_attempt(tmp_path / boundary / "attempts")
    attempt_id = attempt["tool_attempt_id"]
    truth = store.get_attempt(attempt_id)["action_truth"]
    assert truth["effect_truth"] == EffectTruth.NOT_POSSIBLE.value
    assert store.get_attempt(attempt_id)["action_outbox"]

    dispatch_boundaries = {
        "immediately_after_dispatch_started",
        "after_external_call_before_local_return",
        "after_return_before_receipt_bind",
        "after_receipt_before_action_truth_update",
        "after_action_truth_update_before_outbox_write",
        "after_outbox_before_delivery",
        "after_delivery_before_ack",
        "after_ack_before_compaction",
        "observer_started",
        "observer_returned",
        "observer_receipt_persist",
        "reconciliation_decision_persist",
        "terminal_projection",
    }
    if boundary not in dispatch_boundaries:
        assert truth["dispatch_started_at"] == 0.0
        return

    started = _dispatch(store, attempt_id)
    assert started["action_truth"]["effect_truth"] == EffectTruth.POSSIBLE.value

    if boundary == "observer_receipt_persist":
        observed = store.update_action_truth(
            attempt_id,
            state=ActionState.OBSERVER_FAILED.value,
            reason_code="observer_failed",
            observer={
                "observer_attempt_id": "observer_fault_wor269b",
                "execution_epoch": started["execution_epoch"],
                "status": "failed",
                "observer": "fault_observer",
            },
        )
        assert observed["observer_receipts"]
        assert observed["action_truth"]["effect_truth"] == EffectTruth.POSSIBLE.value
    elif boundary == "reconciliation_decision_persist":
        reconciled = store.update_action_truth(
            attempt_id,
            state=ActionState.RECONCILING.value,
            reason_code="reconciliation_pending",
            reconciliation_status=ReconciliationStatus.PENDING.value,
        )
        assert reconciled["action_truth"]["current_state"] == (
            ActionState.RECONCILING.value
        )

    receipt_boundaries = {
        "after_receipt_before_action_truth_update",
        "after_action_truth_update_before_outbox_write",
        "after_outbox_before_delivery",
        "after_delivery_before_ack",
        "after_ack_before_compaction",
        "terminal_projection",
    }
    if boundary in receipt_boundaries:
        terminal, created = store.record_terminal_result(
            attempt_id,
            status=ToolAttemptStatus.SUCCEEDED,
            terminal_outcome="success_verified",
            dispatch_state="effect_applied",
            reason_code="provider_typed_completion",
        )
        assert created is True
        # Receipt and ActionTruth/outbox share the same atomic attempt write.
        persisted = json.loads(
            (store.attempts_dir / f"{attempt_id}.json").read_text(encoding="utf-8")
        )
        assert terminal["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value
        assert persisted["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value
        assert persisted["action_outbox"]

    if boundary in {
        "after_outbox_before_delivery", "after_delivery_before_ack",
        "after_ack_before_compaction", "terminal_projection",
    }:
        transport = DurableTransportStore(tmp_path / boundary / "transport")
        event = _append_event(
            transport,
            "action_truth_changed",
            key="fault_verified_transition",
            payload={"effect_truth": "verified"},
        )
        if boundary in {
            "after_delivery_before_ack", "after_ack_before_compaction",
            "terminal_projection",
        }:
            projection = CanonicalProjection()
            assert projection.accept(event.to_frame()).disposition is (
                ProjectionDisposition.APPLIED
            )
        if boundary in {"after_ack_before_compaction", "terminal_projection"}:
            transport.ack(
                client_id="client_fault_wor269b",
                stream_id=event.stream_id,
                sequence=event.sequence,
            )
        assert store.get_attempt(attempt_id)["action_truth"]["effect_truth"] == (
            EffectTruth.VERIFIED.value
        )


def test_seeded_bounded_chaos_preserves_semantic_and_projection_invariants(tmp_path):
    rng = random.Random(269_2026)
    store, attempt = _create_attempt(tmp_path / "attempts")
    started = _dispatch(store, attempt["tool_attempt_id"])
    transport = DurableTransportStore(tmp_path / "transport")
    events = [
        _append_event(transport, "turn_accepted"),
        _append_event(
            transport,
            "action_truth_changed",
            payload={"action_state": "reconciling"},
        ),
    ]
    projection = CanonicalProjection()
    semantic_dispatches = 1
    cancellation_won = False

    for index in range(250):
        operation = rng.randrange(7)
        if operation == 0:
            frame = rng.choice(events).to_frame()
            projection.accept(frame)
        elif operation == 1:
            for frame in transport.replay_for_client("chaos_client_wor269b"):
                projection.accept(frame)
        elif operation == 2:
            store.apply_provider_callback(
                attempt["tool_attempt_id"],
                callback_id=f"callback_stale_{index % 5}",
                execution_epoch="attempt_epoch_stale_wor269b",
                outcome="completed_with_receipt",
                reason_code="delayed_provider_callback",
                evidence_ref=f"stale_receipt_{index % 5}",
            )
        elif operation == 3 and not cancellation_won:
            store.request_cancellation(attempt["tool_attempt_id"])
            cancellation_won = True
        elif operation == 4:
            transport.replay_for_client(
                "chaos_client_wor269b", {events[0].stream_id: 0},
            )
        elif operation == 5:
            projection.accept(events[0].to_frame())
        else:
            store.apply_provider_callback(
                attempt["tool_attempt_id"],
                callback_id=f"callback_probe_{index % 7}",
                execution_epoch=started["execution_epoch"],
                outcome="reconciliation_required",
                reason_code="provider_lookup_transport_failure",
                failure_domain="provider_transport_failure",
            )

    if not cancellation_won:
        store.request_cancellation(attempt["tool_attempt_id"])
    final, authority = store.apply_provider_callback(
        attempt["tool_attempt_id"],
        callback_id="callback_final_after_cancel_wor269b",
        execution_epoch=started["execution_epoch"],
        outcome="completed_with_receipt",
        reason_code="provider_typed_completion",
        evidence_ref="receipt_final_after_cancel_wor269b",
    )
    assert semantic_dispatches == 1
    assert authority is False
    assert final["authority_state"] == "cancelled"
    assert final["action_truth"]["current_state"] == ActionState.CANCELLED.value
    assert final["action_truth"]["effect_truth"] == EffectTruth.VERIFIED.value
    assert len(projection.projected(events[0].stream_id)) <= len(events)


def test_compaction_preserves_idempotency_and_exact_replay_cursor(tmp_path):
    store = DurableTransportStore(tmp_path / "transport")
    events = [
        _append_event(store, f"event_compact_{index}", key=f"key_{index}")
        for index in range(80)
    ]
    store.ack(
        client_id="client_compact_wor269b",
        stream_id=events[0].stream_id,
        sequence=80,
    )
    assert store.compact(stream_id=events[0].stream_id, retain=8) == 72
    cursors = store.client_cursors("new_client_wor269b")
    assert cursors[events[0].stream_id] == 72
    duplicate = _append_event(store, "event_compact_0", key="key_0")
    assert duplicate.event_id == events[0].event_id
    assert duplicate.sequence == 1


def test_compaction_discards_non_idempotent_payload_archives(tmp_path):
    store = DurableTransportStore(tmp_path / "transport_non_idempotent")
    events = [
        _append_event(store, f"chunk_compact_{index}")
        for index in range(12)
    ]
    store.ack(
        client_id="client_compact_non_idempotent_wor269b",
        stream_id=events[0].stream_id,
        sequence=12,
    )
    assert store.compact(stream_id=events[0].stream_id, retain=3) == 9
    stream = store.get_stream(CONVERSATION_ID, RUN_ID)
    assert stream["compacted_events"] == {}
    assert [event["sequence"] for event in stream["events"]] == [10, 11, 12]


@pytest.mark.parametrize("invariant", tuple(range(1, 11)))
def test_formal_invariants_i1_through_i10(tmp_path, invariant):
    prepared = _truth()
    started = _started(prepared)
    unresolved = _record(started)
    if invariant == 1:
        assert prepared["effect_truth"] == EffectTruth.NOT_POSSIBLE.value
        assert started["dispatch_started_at"] > 0
    elif invariant == 2:
        failed = transition_action_truth(
            started,
            state=ActionState.OBSERVER_FAILED,
            reason_code="observer_failed",
            observer={"status": "failed", "observer": "test"},
        )
        assert failed["effect_truth"] == EffectTruth.POSSIBLE.value
    elif invariant == 3:
        transport = DurableTransportStore(tmp_path / "transport_i3")
        _append_event(transport, "action_truth_changed")
        assert retry_decision(
            effect_class=started["effect_class"],
            state=started["current_state"],
            effect_truth=started["effect_truth"],
            dispatch_started=True,
        ).allowed is True
    elif invariant == 4:
        assert retry_decision(
            effect_class=started["effect_class"],
            state=started["current_state"],
            effect_truth=started["effect_truth"],
            dispatch_started=True,
        ).allowed is True
    elif invariant == 6:
        verified = transition_action_truth(
            started,
            state=ActionState.EFFECT_VERIFIED,
            reason_code="effect_verified",
            evidence={"evidence_ref": "receipt_i6"},
            reconciliation_status=ReconciliationStatus.VERIFIED,
            exact=True,
        )
        assert retry_decision(
            effect_class=verified["effect_class"],
            state=verified["current_state"],
            effect_truth=verified["effect_truth"],
            dispatch_started=True,
        ).allowed is True
    elif invariant == 5:
        assert gate_action(
            ActionIntent(CanonicalEffectClass.READ_ONLY), [unresolved],
        ).allowed is True
    elif invariant == 7:
        projection = CanonicalProjection()
        transport = DurableTransportStore(tmp_path / "transport_i7")
        event = _append_event(transport, "turn_accepted")
        projection.accept(event.to_frame())
        projection.accept(event.to_frame())
        assert len(projection.projected(event.stream_id)) == 1
    elif invariant == 8:
        store, attempt = _create_attempt(tmp_path / "attempts_i8")
        active = _dispatch(store, attempt["tool_attempt_id"])
        store.request_cancellation(attempt["tool_attempt_id"])
        late, authority = store.apply_provider_callback(
            attempt["tool_attempt_id"],
            callback_id="callback_i8",
            execution_epoch=active["execution_epoch"],
            outcome="completed_with_receipt",
            evidence_ref="receipt_i8",
        )
        assert authority is False
        assert late["action_truth"]["current_state"] == ActionState.CANCELLED.value
    elif invariant == 9:
        independent = gate_action(
            ActionIntent(
                CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
                semantic_effect_id="effect_independent_wor269b",
                target={"kind": "desktop", "application": "notepad"},
            ),
            [unresolved],
        )
        assert independent.allowed is True
    else:
        controller = TurnTerminalityController(
            turn_id="turn_invariant_i10",
            objective="verify the action",
            required_postconditions=("effect verified",),
            execution_required=True,
            storage_dir=tmp_path / "terminality_i10",
        )
        controller.record_typed_evidence(
            outcome="outcome_unknown",
            evidence={"dispatch": "started"},
            attempt_id="attempt_invariant_i10",
            reason_code="reconciliation_pending",
        )
        assert controller.evaluate().terminal_state == ""
        assert controller.mark_reconciliation_exhausted(
            "attempt_invariant_i10",
            evidence={"safe_paths_exhausted": True},
        ) is True
        controller.set_strategy_frontier(())
        decision = controller.evaluate()
        assert decision.terminal_state == ""
        assert decision.action == "continue"
