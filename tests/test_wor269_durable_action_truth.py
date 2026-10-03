"""WOR-269 PASS A deterministic model tests.

These tests are intentionally written but not executed during PASS A.
"""

from __future__ import annotations

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
    ObserverStatus,
    ProvidedObservationAdapter,
    ReconciliationStatus,
    SettlePolicyKind,
    action_identity,
    canonical_effect_class,
    gate_action,
    legacy_action_truth,
    new_action_truth,
    retry_decision,
    semantic_effect_identity,
    settle_policy_for,
    transition_action_truth,
)
from osia.semantic_batch import classify_batch_observation, execute_semantic_batch
from osia.tool_attempts import ToolAttemptStore
from osia.turn_terminality import TurnTerminalityController
from tests.fixtures.wor269_action_truth import (
    FakeBrowser,
    FakeClock,
    FakeDesktop,
    MemoryActionStore,
    ScriptedObserver,
    make_attempt,
)


def _truth(
    effect_class: CanonicalEffectClass = CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
    *,
    operation: str = "invoke",
    target: dict | None = None,
    idempotency_identity: str = "",
) -> dict:
    semantic_effect_id = semantic_effect_identity(
        turn_id="turn_12345678",
        semantic_node_id="node_12345678",
        attempted_effect="calculator_add",
        operation=operation,
        target=target or {"kind": "desktop"},
    )
    return new_action_truth(
        action_id=action_identity(
            run_id="run_12345678",
            turn_id="turn_12345678",
            semantic_effect_id=semantic_effect_id,
        ),
        semantic_effect_id=semantic_effect_id,
        effect_class=effect_class,
        operation=operation,
        target=target or {"kind": "desktop"},
        goal_contract_ref="goal_12345678",
        semantic_node_id="node_12345678",
        attempted_effect="calculator_add",
        idempotency_identity=idempotency_identity,
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
        run_id="run_12345678",
        turn_id="turn_12345678",
        request_id="request_12345678",
        goal_contract_ref=truth["goal_contract_ref"],
        effect_class=truth["effect_class"],
        operation=truth["operation"],
        target=dict(truth["target"]),
        idempotency_identity=truth["idempotency_identity"],
        current_state=truth["current_state"],
        effect_truth=truth["effect_truth"],
        dependencies=list(truth["dependencies"]),
    )


def _coordinator(
    truth: dict, *observers, clock: FakeClock | None = None
) -> tuple[ActionTruthCoordinator, MemoryActionStore]:
    store = MemoryActionStore([make_attempt(truth)])
    return (
        ActionTruthCoordinator(store, adapters=observers, clock=clock or FakeClock()),
        store,
    )


def _desktop_binding(effect_role: str) -> dict:
    return {
        "effect_role": effect_role,
        "strategy_attempt_id": "strategy_12345678",
        "semantic_node_id": "node_12345678",
        "semantic_substep_id": "substep_12345678",
        "surface_ref": "surface_12345678",
        "attempted_effect": "application_foreground",
    }


def _create_store_attempt(
    store: ToolAttemptStore,
    *,
    primitive_binding: dict | None = None,
) -> dict:
    return store.create_attempt(
        conversation_id="conversation_12345678",
        turn_id="turn_12345678",
        request_id="request_12345678",
        tool_call_id="toolcall_12345678",
        tool_name="activate_desktop_surface",
        provider_id="provider_12345678",
        model_id="model_12345678",
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments({"target": "Calculator"}),
        side_effect="reversible_local",
        idempotence="conditionally_idempotent",
        retry_policy="never_after_dispatch",
        primitive_binding=primitive_binding,
        action_binding={
            "semantic_node_id": "node_12345678",
            "attempted_effect": (
                "application_foreground"
                if (primitive_binding or {}).get("effect_role") == "preparatory"
                else "calculator_add"
            ),
            "effect_role": (
                "preparatory"
                if (primitive_binding or {}).get("effect_role") == "preparatory"
                else "semantic"
            ),
            "operation": "activate_desktop_surface",
            "target": {
                "kind": "desktop",
                "surface_ref": "surface_12345678",
            },
        },
    )


# A. STATE MACHINE


def test_01_prepare_failure_never_becomes_effect_possible():
    truth = transition_action_truth(
        _truth(),
        state=ActionState.PRECONDITION_FAILED,
        reason_code="precondition_failed",
        occurred_at=101.0,
    )
    assert truth["current_state"] == ActionState.PRECONDITION_FAILED.value
    assert truth["effect_truth"] == EffectTruth.NOT_POSSIBLE.value
    assert truth["dispatch_started_at"] == 0.0


def test_02_semantic_dispatch_start_is_the_exact_effect_boundary():
    prepared = _truth()
    started = _started(prepared)
    assert prepared["effect_truth"] == EffectTruth.NOT_POSSIBLE.value
    assert started["effect_truth"] == EffectTruth.POSSIBLE.value
    assert started["dispatch_started_at"] == 101.0


def test_03_observer_failure_does_not_prove_effect_or_non_effect():
    failed = ObservationResult(
        ObservationClassification.FAILED,
        "uia_observer",
        "observer_failed",
    )
    coordinator, _store = _coordinator(
        _started(_truth()), ScriptedObserver(failed)
    )
    decision = coordinator.reconcile(_truth()["action_id"])
    record = coordinator.action_record(_truth()["action_id"])
    assert decision.effect_truth is EffectTruth.POSSIBLE
    assert decision.observer_status is ObserverStatus.FAILED
    assert record is not None
    assert record.effect_truth == EffectTruth.POSSIBLE.value


def test_04_exact_non_application_reopens_frontier():
    absent = ObservationResult(
        ObservationClassification.NON_APPLIED,
        "exact_state",
        "not_applied_verified",
        evidence_ref="sha256:" + "1" * 64,
        exact=True,
    )
    coordinator, _store = _coordinator(
        _started(_truth()), ScriptedObserver(absent)
    )
    decision = coordinator.reconcile(_truth()["action_id"])
    assert decision.state is ActionState.NOT_APPLIED_VERIFIED
    assert decision.replay_allowed is True


def test_05_verified_effect_is_a_fact_not_a_new_request_veto():
    match = ObservationResult(
        ObservationClassification.MATCH,
        "exact_state",
        "effect_verified",
        evidence_ref="sha256:" + "2" * 64,
        exact=True,
    )
    coordinator, _store = _coordinator(
        _started(_truth()), ScriptedObserver(match)
    )
    decision = coordinator.reconcile(_truth()["action_id"])
    retry = retry_decision(
        effect_class=CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
        state=decision.state,
        effect_truth=decision.effect_truth,
        dispatch_started=True,
    )
    assert decision.state is ActionState.EFFECT_VERIFIED
    assert decision.replay_allowed is True
    assert retry.allowed is True


def test_06_reconciliation_pending_is_not_an_effect_terminal():
    inconclusive = ObservationResult(
        ObservationClassification.INCONCLUSIVE,
        "exact_state",
        "observer_inconclusive",
        evidence_ref="sha256:" + "3" * 64,
    )
    coordinator, _store = _coordinator(
        _started(_truth()), ScriptedObserver(inconclusive)
    )
    decision = coordinator.reconcile(_truth()["action_id"])
    assert decision.reconciliation_status is ReconciliationStatus.PENDING
    assert decision.state is ActionState.EFFECT_POSSIBLE
    assert decision.safe_avenues_exhausted is False


# B. FACTUAL HISTORY — NEW REQUESTS ARE NOT VETOED


def test_07_unresolved_mutation_history_does_not_veto_a_fresh_request():
    unresolved = _record(_started(_truth()))
    decision = gate_action(
        ActionIntent(
            CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
            semantic_effect_id=unresolved.semantic_effect_id,
        ),
        [unresolved],
    )
    assert decision.allowed is True
    assert decision.reason_code == "new_request_allowed"


def test_08_read_only_observer_remains_allowed():
    unresolved = _record(_started(_truth()))
    decision = gate_action(
        ActionIntent(CanonicalEffectClass.READ_ONLY),
        [unresolved],
    )
    assert decision.allowed is True


def test_09_explicitly_independent_safe_branch_remains_allowed():
    unresolved = _record(_started(_truth()))
    decision = gate_action(
        ActionIntent(
            CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
            semantic_effect_id="effect_independent_12345678",
            semantic_node_id="node_independent_12345678",
            independent_of_action_ids=(unresolved.action_id,),
        ),
        [unresolved],
    )
    assert decision.allowed is True


def test_10_dependency_history_does_not_veto_model_tool_choice():
    unresolved = _record(_started(_truth()))
    decision = gate_action(
        ActionIntent(
            CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
            semantic_effect_id="effect_dependent_12345678",
            dependency_action_ids=(unresolved.action_id,),
            independent_of_action_ids=(unresolved.action_id,),
        ),
        [unresolved],
    )
    assert decision.allowed is True
    assert decision.reason_code == "new_request_allowed"


# C. READ_ONLY


def test_11_lost_read_result_can_safely_retry():
    truth = _started(_truth(
        CanonicalEffectClass.READ_ONLY,
        operation="site_audit",
        target={"kind": "browser", "safe_url": "https://example.com"},
    ))
    coordinator, _store = _coordinator(truth, ScriptedObserver(
        ObservationResult(
            ObservationClassification.INCONCLUSIVE,
            "read_observer",
            "read_only_result_not_retained",
        )
    ))
    decision = retry_decision(
        effect_class=CanonicalEffectClass.READ_ONLY,
        state=ActionState.EFFECT_POSSIBLE,
        dispatch_started=True,
    )
    assert decision.allowed is True
    assert decision.reason_code == "new_request_allowed"
    assert coordinator.unresolved_actions(turn_id="turn_12345678")


def test_12_site_audit_read_retries_after_transport_uncertainty():
    decision = retry_decision(
        effect_class=canonical_effect_class("read_only", "idempotent"),
        state=ActionState.DISPATCH_STARTED,
        effect_truth=EffectTruth.POSSIBLE,
        dispatch_started=True,
    )
    assert decision.allowed is True


def test_13_read_reconciliation_does_not_consume_mutation_replay_truth():
    mutation = _started(_truth())
    before = dict(mutation)
    first = retry_decision(
        effect_class=CanonicalEffectClass.READ_ONLY,
        state=ActionState.EFFECT_POSSIBLE,
        dispatch_started=True,
    )
    second = retry_decision(
        effect_class=CanonicalEffectClass.READ_ONLY,
        state=ActionState.EFFECT_POSSIBLE,
        dispatch_started=True,
    )
    assert first.allowed and second.allowed
    assert mutation == before


# D. WEB


def test_14_lost_navigation_callback_is_reconciled_by_exact_url():
    browser = FakeBrowser("https://example.com/audit")
    truth = _started(_truth(
        operation="navigate_browser",
        target={"kind": "browser", "safe_url": "https://example.com/audit"},
    ))
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot)
    )
    decision = coordinator.reconcile(truth["action_id"])
    assert decision.state is ActionState.EFFECT_VERIFIED
    assert browser.read_count == 1


def test_15_exact_navigation_non_application_reopens_frontier():
    browser = FakeBrowser("https://example.com/previous")
    truth = _started(_truth(
        operation="navigate_browser",
        target={"kind": "browser", "safe_url": "https://example.com/audit"},
    ))
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot)
    )
    decision = coordinator.reconcile(
        truth["action_id"],
        {
            "browser_snapshot": browser.snapshot(),
            "browser_exact_non_application": True,
        },
    )
    assert decision.state is ActionState.NOT_APPLIED_VERIFIED
    assert decision.replay_allowed is True


def test_16_dead_browser_page_is_typed_as_target_stale():
    browser = FakeBrowser("", stale=True)
    truth = _started(_truth(
        operation="navigate_browser",
        target={"kind": "browser", "safe_url": "https://example.com/audit"},
    ))
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot)
    )
    decision = coordinator.reconcile(truth["action_id"])
    assert decision.state is ActionState.TARGET_STALE
    assert decision.observer_status is ObserverStatus.TARGET_STALE


def test_17_browser_state_inspection_is_allowed_during_uncertainty():
    unresolved = _record(_started(_truth(
        operation="navigate_browser",
        target={"kind": "browser", "safe_url": "https://example.com"},
    )))
    assert gate_action(
        ActionIntent(CanonicalEffectClass.READ_ONLY), [unresolved]
    ).allowed


def test_18_site_audit_can_continue_after_navigation_reconciliation():
    browser = FakeBrowser("https://example.com/audit")
    truth = _started(_truth(
        operation="navigate_browser",
        target={"kind": "browser", "safe_url": "https://example.com/audit"},
    ))
    coordinator, _store = _coordinator(
        truth, BrowserReconciliationAdapter(browser.snapshot)
    )
    assert coordinator.reconcile(
        truth["action_id"]
    ).state is ActionState.EFFECT_VERIFIED
    audit_gate = gate_action(ActionIntent(CanonicalEffectClass.READ_ONLY), [])
    assert audit_gate.allowed is True
    assert retry_decision(
        effect_class=CanonicalEffectClass.READ_ONLY,
        state=ActionState.DISPATCH_STARTED,
        dispatch_started=True,
    ).allowed


# E. DESKTOP


def test_19_dispatched_invoke_is_verified_by_uia_match():
    desktop = FakeDesktop(
        outcome="confirmed_success",
        reason_code="exact_desktop_primitive_state_change",
    )
    truth = _started(_truth(target={
        "kind": "desktop", "application": "calculator",
    }))
    coordinator, _store = _coordinator(
        truth, DesktopReconciliationAdapter(desktop.snapshot)
    )
    assert coordinator.reconcile(
        truth["action_id"]
    ).state is ActionState.EFFECT_VERIFIED


def test_20_dispatched_invoke_is_verified_by_vision_match():
    vision = ObservationResult(
        ObservationClassification.MATCH,
        "vision_image_observer",
        "vision_postcondition_verified",
        evidence_ref="sha256:" + "4" * 64,
        exact=True,
    )
    truth = _started(_truth())
    coordinator, _store = _coordinator(
        truth, ProvidedObservationAdapter()
    )
    decision = coordinator.reconcile(
        truth["action_id"], {"observer_results": [vision]}
    )
    assert decision.state is ActionState.EFFECT_VERIFIED


def test_21_observer_failure_is_factual_and_does_not_veto_a_new_request():
    truth = _started(_truth())
    coordinator, _store = _coordinator(
        truth,
        ScriptedObserver(ObservationResult(
            ObservationClassification.FAILED,
            "uia_observer",
            "observer_failed",
        )),
    )
    decision = coordinator.reconcile(truth["action_id"])
    retry = retry_decision(
        effect_class=CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
        state=decision.state,
        effect_truth=decision.effect_truth,
        dispatch_started=True,
    )
    assert decision.observer_status is ObserverStatus.FAILED
    assert decision.replay_allowed is True
    assert retry.allowed is True


def test_22_exact_desktop_nonmatch_reopens_frontier():
    desktop = FakeDesktop(
        outcome="confirmed_absence",
        reason_code="exact_final_state_observation",
        no_partial_effect=True,
    )
    truth = _started(_truth(target={
        "kind": "desktop", "application": "calculator",
    }))
    coordinator, _store = _coordinator(
        truth, DesktopReconciliationAdapter(desktop.snapshot)
    )
    decision = coordinator.reconcile(truth["action_id"])
    assert decision.state is ActionState.NOT_APPLIED_VERIFIED
    assert decision.replay_allowed


def test_23_focus_preparation_cannot_contaminate_semantic_effect(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    created = _create_store_attempt(
        store,
        primitive_binding=_desktop_binding("preparatory"),
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    store.mark_running(created["tool_attempt_id"])
    store.arm_owner_dispatch_boundary(created["tool_attempt_id"])
    store.claim_owner_dispatch(created["tool_attempt_id"])
    record = store.record_primitive_dispatch_started(
        created["tool_attempt_id"],
        primitive_id=accepted["primitive_journal"][0]["primitive_id"],
    )
    semantic_action = _truth()
    assert record["primitive_journal"][0]["effect_classification"] == "effect_possible"
    assert record["action_truth"]["effect_role"] == "preparatory"
    assert record["action_truth"]["effect_truth"] == EffectTruth.POSSIBLE.value
    assert record["action_truth"]["semantic_effect_id"] != semantic_action[
        "semantic_effect_id"
    ]
    assert semantic_action["effect_truth"] == EffectTruth.NOT_POSSIBLE.value


# F. BATCHING


def _batch_plan() -> dict:
    return {
        "semantic_batch_id": "batch_12345678",
        "items": [
            {
                "index": 0,
                "observable_effect": {
                    "kind": "exact_surface_text",
                    "surface_ref": "surface_first_12345678",
                    "expected_text": "first",
                },
            },
            {
                "index": 1,
                "observable_effect": {
                    "kind": "exact_surface_text",
                    "surface_ref": "surface_second_12345678",
                    "expected_text": "second",
                },
            },
        ],
    }


def _partial_batch_result() -> dict:
    receipts = iter([
        {
            "receipt": {
                "dispatch_state": "dispatched",
                "outcome": "success_unverified",
                "reason_code": "semantic_dispatch_returned",
                "structural_epoch": 7,
            }
        },
        {
            "receipt": {
                "dispatch_state": "not_dispatched",
                "outcome": "confirmed_failure_before_effect",
                "reason_code": "target_stale",
                "structural_epoch": 7,
            }
        },
    ])
    return execute_semantic_batch(_batch_plan(), lambda _item: next(receipts))


def test_24_partial_batch_retains_every_primitive_receipt():
    result = _partial_batch_result()
    receipts = result["receipt"]["semantic_batch"]["primitive_receipts"]
    assert len(receipts) == 2
    assert receipts[0]["dispatch_state"] == "dispatched"
    assert receipts[1]["dispatch_state"] == "not_dispatched"


def test_25_rejected_next_primitive_reason_survives_aggregation():
    batch = _partial_batch_result()["receipt"]["semantic_batch"]
    assert batch["rejected_primitive_index"] == 1
    assert batch["rejection_reason"] == "target_stale"


def test_26_exact_applied_prefix_resumes_from_proven_frontier():
    plan = _batch_plan()
    observed = [dict(plan["items"][0]["observable_effect"])]
    classified = classify_batch_observation(plan, observed)
    assert classified["state"] == "exact_prefix"
    assert classified["applied_count"] == 1
    assert classified["resume_from_index"] == 1
    discontinuous = classify_batch_observation(
        plan, [dict(plan["items"][1]["observable_effect"])]
    )
    assert discontinuous["state"] == "outcome_unknown"
    assert discontinuous["resume_from_index"] is None


# G. DURABILITY


def test_27_action_and_attempt_journal_survive_simulated_restart(tmp_path):
    root = tmp_path / "journal"
    store = ToolAttemptStore(root)
    created = _create_store_attempt(store)
    reloaded = ToolAttemptStore(root).get_attempt(created["tool_attempt_id"])
    assert reloaded is not None
    assert reloaded["action_truth"]["action_id"]
    assert reloaded["action_truth"]["semantic_effect_id"]


def test_28_reconciliation_history_survives_reload():
    clock = FakeClock()
    failed = ObservationResult(
        ObservationClassification.FAILED,
        "fault_injected_observer",
        "observer_failed",
    )
    truth = _started(_truth())
    coordinator, store = _coordinator(
        truth, ScriptedObserver(failed), clock=clock
    )
    coordinator.reconcile(truth["action_id"])
    restarted = ActionTruthCoordinator(store.restarted(), adapters=[])
    record = restarted.action_record(truth["action_id"])
    assert record is not None
    assert record.reconciliation_history
    assert record.observers[-1]["classification"] == ObserverStatus.FAILED.value


def test_29_legacy_unknown_loads_without_false_success_upgrade():
    truth = legacy_action_truth({
        "tool_attempt_id": "attempt_legacy_12345678",
        "tool_name": "navigate_browser",
        "side_effect": "external_mutation",
        "idempotence": "non_idempotent",
        "dispatch_state": "effect_unknown",
        "terminal_outcome": "outcome_unknown",
        "status": "unknown_after_start",
        "created_at": 90.0,
        "updated_at": 91.0,
    })
    assert truth["legacy"] is True
    assert truth["effect_truth"] == EffectTruth.POSSIBLE.value
    assert truth["current_state"] != ActionState.EFFECT_VERIFIED.value
    legacy_unverified_read = legacy_action_truth({
        "tool_attempt_id": "attempt_legacy_read_12345678",
        "tool_name": "site_audit",
        "side_effect": "read_only",
        "idempotence": "idempotent",
        "dispatch_state": "not_dispatched",
        "terminal_outcome": "success_unverified",
        "status": "completed",
        "created_at": 90.0,
        "updated_at": 91.0,
    })
    assert legacy_unverified_read["effect_truth"] == EffectTruth.POSSIBLE.value
    assert legacy_unverified_read["current_state"] != ActionState.EFFECT_VERIFIED.value
    legacy_unverified_mutation = legacy_action_truth({
        "tool_attempt_id": "attempt_legacy_write_12345678",
        "tool_name": "send_message",
        "side_effect": "external_mutation",
        "idempotence": "non_idempotent",
        "dispatch_state": "not_dispatched",
        "terminal_outcome": "success_unverified",
        "status": "completed",
        "created_at": 90.0,
        "updated_at": 91.0,
    })
    legacy_retry = retry_decision(
        effect_class=legacy_unverified_mutation["effect_class"],
        state=legacy_unverified_mutation["current_state"],
        effect_truth=legacy_unverified_mutation["effect_truth"],
        dispatch_started=False,
    )
    assert legacy_retry.allowed is True
    legacy_coordinator, _legacy_store = _coordinator(
        legacy_unverified_mutation,
        ScriptedObserver(ObservationResult(
            ObservationClassification.INCONCLUSIVE,
            "legacy_observer",
            "observer_inconclusive",
        )),
    )
    legacy_decision = legacy_coordinator.reconcile(
        legacy_unverified_mutation["action_id"]
    )
    assert legacy_decision.effect_truth is EffectTruth.POSSIBLE
    assert legacy_decision.replay_allowed is True
    legacy_exact_coordinator, _legacy_exact_store = _coordinator(
        legacy_unverified_mutation,
        ScriptedObserver(ObservationResult(
            ObservationClassification.NON_APPLIED,
            "legacy_exact_observer",
            "not_applied_verified",
            exact=True,
        )),
    )
    legacy_exact_decision = legacy_exact_coordinator.reconcile(
        legacy_unverified_mutation["action_id"]
    )
    assert legacy_exact_decision.effect_truth is EffectTruth.NOT_APPLIED
    assert legacy_exact_decision.replay_allowed is True


# H. TERMINALITY


def test_30_reconciliation_pending_is_nonterminal_for_turn(tmp_path):
    controller = TurnTerminalityController(
        turn_id="turn_reconcile_12345678",
        objective="perform the requested action",
        required_postconditions=("effect is verified",),
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"dispatch": "started"},
        attempt_id="attempt_12345678",
        reason_code="reconciliation_pending",
    )
    decision = controller.evaluate()
    assert decision.action in {"continue", "wait"}
    assert decision.terminal_state == ""


def test_31_unknown_remains_nonterminal_after_reconciliation_frontier_exhausted(tmp_path):
    controller = TurnTerminalityController(
        turn_id="turn_exhausted_12345678",
        objective="perform the requested action",
        required_postconditions=("effect is verified",),
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"dispatch": "started"},
        attempt_id="attempt_12345678",
        reason_code="reconciliation_pending",
    )
    assert controller.evaluate().action != "terminal"
    assert controller.mark_reconciliation_exhausted(
        "attempt_12345678",
        evidence={"exact_non_application": True},
    ) is True
    controller.set_strategy_frontier(())
    decision = controller.evaluate()
    assert decision.action in {"continue", "wait"}
    assert decision.terminal_state == ""
    assert not controller.reserve_final(
        "blocked_verified",
        reason_code=decision.reason_code,
    )


# Additional policy/settle coverage required by PASS A.


def test_32_idempotency_identity_does_not_gate_a_new_model_request():
    truth = _started(_truth(
        CanonicalEffectClass.IDEMPOTENT_WRITE,
        idempotency_identity="operation_key_12345678",
    ))
    unresolved = _record(truth)
    allowed = gate_action(
        ActionIntent(
            CanonicalEffectClass.IDEMPOTENT_WRITE,
            semantic_effect_id=truth["semantic_effect_id"],
            idempotency_identity="operation_key_12345678",
        ),
        [unresolved],
    )
    wrong_key = gate_action(
        ActionIntent(
            CanonicalEffectClass.IDEMPOTENT_WRITE,
            semantic_effect_id=truth["semantic_effect_id"],
            idempotency_identity="operation_key_different_12345678",
        ),
        [unresolved],
    )
    assert allowed.allowed is True
    assert wrong_key.allowed is True
    assert canonical_effect_class(
        "reversible_local",
        "conditionally_idempotent",
        idempotency_identity=truth["semantic_effect_id"],
    ) is CanonicalEffectClass.IDEMPOTENT_WRITE


def test_33_unknown_mutation_contract_fails_closed_as_high_risk():
    assert canonical_effect_class(
        "dynamically_classified", "unknown"
    ) is CanonicalEffectClass.DESTRUCTIVE_OR_HIGH_RISK


def test_34_settle_policy_is_effect_aware_and_event_driven_where_needed():
    local = settle_policy_for(
        operation="desktop_invoke",
        family="system",
        effect_class=CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
    )
    navigation = settle_policy_for(
        operation="navigate_browser",
        family="browser",
        effect_class=CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
    )
    network = settle_policy_for(
        operation="site_audit",
        family="browser",
        effect_class=CanonicalEffectClass.READ_ONLY,
    )
    preparatory = new_action_truth(
        action_id="action_preparatory_12345678",
        semantic_effect_id="effect_preparatory_12345678",
        effect_class=CanonicalEffectClass.IDEMPOTENT_WRITE,
        operation="activate_desktop_surface",
        target={"kind": "desktop"},
        effect_role="preparatory",
        occurred_at=100.0,
    )
    assert local.kind is SettlePolicyKind.LOCAL_UI_PROPAGATING
    assert navigation.kind is SettlePolicyKind.NAVIGATION
    assert navigation.event_driven is True
    assert network.kind is SettlePolicyKind.NETWORK_READ
    assert preparatory["settle_policy"]["kind"] == (
        SettlePolicyKind.LOCAL_UI_IMMEDIATE.value
    )


def test_35_disjoint_desktop_application_branch_is_locally_independent():
    unresolved_truth = _started(_truth(target={
        "kind": "desktop",
        "application": "calculator",
        "surface_ref": "surface_calculator_12345678",
    }))
    unresolved = _record(unresolved_truth)
    decision = gate_action(
        ActionIntent(
            CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
            semantic_effect_id="effect_notepad_12345678",
            target={
                "kind": "desktop",
                "application": "notepad",
                "surface_ref": "surface_notepad_12345678",
            },
        ),
        [unresolved],
    )
    assert decision.allowed is True
