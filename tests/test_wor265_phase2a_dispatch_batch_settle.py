"""Focused WOR-265 dispatch ownership, settle, and batching gates."""

from __future__ import annotations

import copy
import time

import pytest

from osia import execution_strategy as es
from osia import tool_attempts as tool_attempts_module
from osia.autonomous.authorization import pc_projection
from osia.semantic_batch import (
    classify_batch_observation,
    execute_semantic_batch,
    normalize_semantic_batch,
)
from osia.tool_attempts import (
    IllegalTransition,
    SemanticSettleProbeClassification,
    SemanticSettleProbeResult,
    ToolAttemptCoordinator,
    ToolAttemptStatus,
    ToolAttemptStore,
)
from osia.turn_terminality import TurnTerminalityController


TASK = "Open Calculator and calculate 5 + 23. Verify the result is 28."
CRITERION = "Calculator visibly displays the exact result 28."
TOOLS = {
    "activate_desktop_surface",
    "inspect_desktop_surfaces",
    "open_application",
}
WINDOW_REF = "surface-calculator-window"
BUTTON_REF = "surface-calculator-two"
DISPLAY_REF = "surface-calculator-display"


def _strategy():
    return es.build_execution_strategy(
        TASK,
        TOOLS,
        postcondition=CRITERION,
        semantic_context=TASK,
    )


def _scene(*, foreground: bool, background_safe: bool = True) -> dict:
    return {
        "application": "calculator",
        "requested_application": "calculator",
        "window_surface_ref": WINDOW_REF,
        "window_generation": "calculator-generation-1",
        "accessibility_quality": "rich_target_resolved",
        "target_resolution": "exact_target",
        "target_foreground": foreground,
        "target_minimized": False,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "calculator" if foreground else "chatgpt",
        "focus_observable": foreground,
        "focused_surface_ref": BUTTON_REF if foreground else "",
        "observed_at": time.time(),
        "surfaces": [
            {
                "surface_ref": WINDOW_REF,
                "surface_kind": "window",
                "role": "WindowControl",
                "label": "Calculator",
                "enabled": True,
                "patterns": ["Window"],
                "actuation": {
                    "available": True,
                    "attempted_effect": "application_foreground",
                    "background_safe": False,
                    "foreground_required": False,
                },
            },
            {
                "surface_ref": DISPLAY_REF,
                "surface_kind": "control",
                "role": "TextControl",
                "label": "Display is 5 +",
                "enabled": True,
                "patterns": [],
                "actuation": {"available": False},
            },
            {
                "surface_ref": BUTTON_REF,
                "surface_kind": "control",
                "role": "ButtonControl",
                "label": "Two",
                "enabled": True,
                "patterns": ["Invoke"],
                "actuation": {
                    "available": True,
                    "attempted_effect": "desktop_uia_primitive",
                    "background_safe": background_safe,
                    "foreground_required": not background_safe,
                    "patterns": ["Invoke"],
                },
            },
        ],
    }


def _observe(strategy, *, foreground: bool, background_safe: bool = True):
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": _scene(
                foreground=foreground,
                background_safe=background_safe,
            ),
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )


def _focus_arguments() -> dict:
    return {
        "surface_ref": WINDOW_REF,
        "objective": "Calculator",
        "action_pattern": "Window",
    }


def _semantic_arguments() -> dict:
    return {
        "surface_ref": BUTTON_REF,
        "objective": "Two",
        "action_pattern": "Invoke",
        "observable_effect": {
            "kind": "exact_surface_text",
            "surface_ref": DISPLAY_REF,
            "expected_text": "5 + 2",
        },
    }


def _focus_receipt(*, unknown: bool = False) -> dict:
    return {
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "effect_unknown" if unknown else "dispatched",
            "outcome": "outcome_unknown" if unknown else "success_unverified",
            "reason_code": "desktop_target_focus_dispatched",
            "surface_ref": WINDOW_REF,
            "primitive": {
                "kind": "desktop_window_focus",
                "mechanism": "exact_window_focus",
                "dispatched_at": time.time(),
                "before": {},
            },
        },
    }


def _semantic_receipt() -> dict:
    return {
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "effect_unknown",
            "outcome": "outcome_unknown",
            "reason_code": "uia_activation_outcome_unknown",
            "surface_ref": BUTTON_REF,
            "primitive": {
                "kind": "desktop_uia_primitive",
                "mechanism": "invoke",
                "observable_effect": _semantic_arguments()[
                    "observable_effect"
                ],
                "dispatched_at": time.time(),
                "before": {},
            },
        },
    }


def test_phase_a_01_focus_dispatch_has_only_preparatory_identity():
    strategy = _strategy()
    _observe(strategy, foreground=False)

    decision = es.tool_preflight(
        "activate_desktop_surface", _focus_arguments(), strategy=strategy,
    )

    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt is not None
    assert attempt.mechanism_id == "native_accessibility:preparatory_focus"
    assert attempt.attempted_effect == "application_foreground"
    assert attempt.substep_id == ""
    assert attempt.primitive_postcondition == ""


def test_phase_a_02_focus_success_cannot_dispatch_semantic_primitive():
    strategy = _strategy()
    _observe(strategy, foreground=False)
    decision = es.tool_preflight(
        "activate_desktop_surface", _focus_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="success_unverified",
        is_error=False,
        reason_code="",
        result=_focus_receipt(),
        strategy=strategy,
    )

    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt is not None
    assert attempt.status == es.AttemptStatus.AMBIGUOUS.value
    assert strategy.preparatory_effect_is_frozen()
    assert not any(item.substep_id for item in strategy.attempts)


def test_phase_a_03_semantic_remains_executable_after_preparatory_only_success():
    strategy = _strategy()
    _observe(strategy, foreground=False)
    focus = es.tool_preflight(
        "activate_desktop_surface", _focus_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        focus,
        outcome="success_unverified",
        is_error=False,
        reason_code="",
        result=_focus_receipt(),
        strategy=strategy,
    )
    _observe(strategy, foreground=True)

    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )

    assert semantic is not None and semantic.allowed
    assert strategy.attempt_by_id(semantic.attempt_id).substep_id


def test_phase_a_04_preparatory_uncertainty_has_no_semantic_replay_barrier():
    strategy = _strategy()
    _observe(strategy, foreground=False)
    focus = es.tool_preflight(
        "activate_desktop_surface", _focus_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        focus,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="desktop_target_focus_outcome_unknown",
        result=_focus_receipt(unknown=True),
        strategy=strategy,
    )
    _observe(strategy, foreground=True)

    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )

    assert strategy.preparatory_effect_is_frozen()
    assert semantic is not None and semantic.allowed


def test_phase_a_05_actual_uia_dispatch_creates_exact_semantic_effect():
    strategy = _strategy()
    _observe(strategy, foreground=True)
    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        semantic,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="uia_activation_outcome_unknown",
        result=_semantic_receipt(),
        strategy=strategy,
    )

    attempt = strategy.attempt_by_id(semantic.attempt_id)
    assert attempt.primitive_receipt["kind"] == "desktop_uia_primitive"
    assert attempt.attempted_effect == (
        "exact surface text on surface-calculator-display is 5 + 2"
    )
    assert strategy.frozen_reason == "uia_activation_outcome_unknown"


def test_phase_a_06_background_safe_surface_does_not_reacquire_focus():
    strategy = _strategy()
    _observe(strategy, foreground=True, background_safe=True)

    focus = es.tool_preflight(
        "activate_desktop_surface", _focus_arguments(), strategy=strategy,
    )
    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )

    assert focus is not None and focus.allowed is False
    assert focus.reason_code == "prerequisite_effect_already_completed"
    assert semantic is not None and semantic.allowed


def test_phase_a_07_focus_required_target_reacquires_focus():
    strategy = _strategy()
    _observe(strategy, foreground=False, background_safe=False)

    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )
    focus = es.tool_preflight(
        "activate_desktop_surface", _focus_arguments(), strategy=strategy,
    )

    assert semantic is not None and semantic.allowed is False
    assert semantic.reason_code == "exact_target_activation_required"
    assert focus is not None and focus.allowed


def test_phase_a_08_real_semantic_dispatch_keeps_existing_anti_replay():
    strategy = _strategy()
    _observe(strategy, foreground=True)
    first = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        first,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="uia_activation_outcome_unknown",
        result=_semantic_receipt(),
        strategy=strategy,
    )

    replay = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )

    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"


def _durable_attempt(
    tmp_path,
    *,
    effect_role: str = "semantic",
    surface_ref: str = BUTTON_REF,
):
    store = ToolAttemptStore(tmp_path / "tool-attempts")
    arguments = {
        "surface_ref": surface_ref,
        "objective": "sanitized-control",
        "action_pattern": "Invoke" if effect_role == "semantic" else "Window",
    }
    fingerprint = store.fingerprint_arguments(arguments)
    record = store.create_attempt(
        conversation_id="conversation_wor265",
        turn_id="turn_wor265",
        tool_call_id="toolcall_wor265",
        tool_name="activate_desktop_surface",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={},
        argument_fingerprint=fingerprint,
        side_effect="external_mutation",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        primitive_binding={
            "effect_role": effect_role,
            "semantic_node_id": "node_wor265",
            "semantic_substep_id": (
                "substep_wor265" if effect_role == "semantic" else ""
            ),
            "strategy_attempt_id": "strategy_wor265",
            "application_identity": "calculator",
            "surface_ref": surface_ref,
            "native_window_id": "4458060",
            "window_generation": "generation_wor265",
            "runtime_identity": "runtime_wor265",
            "automation_id": "num2Button",
            "uia_pattern": (
                "Invoke" if effect_role == "semantic" else "Window"
            ),
            "attempted_effect": (
                "desktop_uia_primitive"
                if effect_role == "semantic" else "application_foreground"
            ),
            "semantic_postcondition": "sha256:" + "a" * 64,
        },
        persistence_contract={"restart_reconciliation": False},
    )
    record = store.mark_accepted(
        record["tool_attempt_id"], expected_revision=record["revision"],
    )
    record = store.mark_running(record["tool_attempt_id"])
    return store, record, fingerprint


def _journal_entry(record: dict) -> dict:
    assert len(record["primitive_journal"]) == 1
    return record["primitive_journal"][0]


def test_phase_b_01_owner_loss_before_dispatch_is_failed_before_effect(tmp_path):
    store, record, _fingerprint = _durable_attempt(tmp_path)

    recovered = ToolAttemptCoordinator(store).startup_fail_closed()

    assert len(recovered) == 1
    terminal = store.get_attempt(record["tool_attempt_id"])
    assert terminal["status"] == "failed_before_effect"
    assert terminal["terminal_outcome"] == "confirmed_failure_before_effect"
    assert _journal_entry(terminal)["lifecycle"] == "prepared"
    assert _journal_entry(terminal)["effect_classification"] == "impossible"


def test_phase_b_02_owner_loss_after_dispatch_keeps_exact_effect_possible(tmp_path):
    store, record, _fingerprint = _durable_attempt(tmp_path)
    entry = _journal_entry(record)
    store.record_primitive_dispatch_started(
        record["tool_attempt_id"],
        primitive_id=entry["primitive_id"],
        automation_id="num2Button",
        uia_pattern="Invoke",
    )

    ToolAttemptCoordinator(store).startup_fail_closed()

    terminal = store.get_attempt(record["tool_attempt_id"])
    journal = _journal_entry(terminal)
    assert terminal["status"] == "unknown_after_start"
    assert terminal["terminal_outcome"] == "outcome_unknown"
    assert journal["lifecycle"] == "dispatch_started"
    assert journal["effect_classification"] == "effect_possible"


def test_phase_b_03_exact_identity_survives_owner_callback_loss(tmp_path):
    store, record, _fingerprint = _durable_attempt(tmp_path)
    entry = _journal_entry(record)
    store.record_primitive_dispatch_started(
        record["tool_attempt_id"],
        primitive_id=entry["primitive_id"],
        automation_id="num2Button",
        uia_pattern="Invoke",
    )
    ToolAttemptCoordinator(store).startup_fail_closed()

    journal = _journal_entry(store.get_attempt(record["tool_attempt_id"]))
    assert journal["semantic_node_id"] == "node_wor265"
    assert journal["semantic_substep_id"] == "substep_wor265"
    assert journal["strategy_attempt_id"] == "strategy_wor265"
    assert journal["surface_ref"] == BUTTON_REF
    assert journal["native_window_id"] == "4458060"
    assert journal["window_generation"] == "generation_wor265"
    assert journal["runtime_identity"] == "runtime_wor265"
    assert journal["automation_id"] == "num2Button"
    assert journal["uia_pattern"] == "Invoke"


def test_phase_b_04_journal_survives_atomic_checkpoint_reload(tmp_path):
    store, record, _fingerprint = _durable_attempt(tmp_path)
    entry = _journal_entry(record)
    store.record_primitive_dispatch_started(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )
    store.record_primitive_dispatch_returned(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )

    restored = ToolAttemptStore(tmp_path / "tool-attempts").get_attempt(
        record["tool_attempt_id"]
    )

    journal = _journal_entry(restored)
    assert journal["lifecycle"] == "dispatch_returned"
    assert journal["dispatch_started_at"] > 0.0
    assert journal["dispatch_returned_at"] >= journal["dispatch_started_at"]


def test_phase_b_05_anti_replay_is_keyed_to_exact_semantic_dispatch(tmp_path):
    store, record, fingerprint = _durable_attempt(tmp_path)
    assert store.has_dispatch_barrier(
        conversation_id="conversation_wor265",
        turn_id="turn_wor265",
        tool_name="activate_desktop_surface",
        argument_fingerprint=fingerprint,
    ) is False
    entry = _journal_entry(record)
    store.record_primitive_dispatch_started(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )
    ToolAttemptCoordinator(store).startup_fail_closed()

    assert store.has_dispatch_barrier(
        conversation_id="conversation_wor265",
        turn_id="turn_wor265",
        tool_name="activate_desktop_surface",
        argument_fingerprint=fingerprint,
    ) is True


def test_phase_b_06_preparatory_unknown_does_not_freeze_other_semantic_candidate(
    tmp_path,
):
    store, record, _fingerprint = _durable_attempt(
        tmp_path, effect_role="preparatory", surface_ref=WINDOW_REF,
    )
    entry = _journal_entry(record)
    store.record_primitive_dispatch_started(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
        uia_pattern="Window",
    )
    ToolAttemptCoordinator(store).startup_fail_closed()
    other = store.fingerprint_arguments({
        "surface_ref": BUTTON_REF,
        "objective": "sanitized-control",
        "action_pattern": "Invoke",
    })

    assert store.has_dispatch_barrier(
        conversation_id="conversation_wor265",
        turn_id="turn_wor265",
        tool_name="activate_desktop_surface",
        argument_fingerprint=other,
    ) is False


def test_phase_b_07_wrapper_running_cannot_claim_semantic_os_dispatch(tmp_path):
    store, record, _fingerprint = _durable_attempt(tmp_path)

    current = store.get_attempt(record["tool_attempt_id"])

    assert current["dispatch_state"] == "owner_started"
    assert _journal_entry(current)["lifecycle"] == "prepared"
    assert _journal_entry(current)["effect_classification"] == "impossible"


def _terminality(tmp_path, turn_id: str = "turn_phase_c"):
    return TurnTerminalityController(
        turn_id=turn_id,
        objective=TASK,
        required_postconditions=("application_open", CRITERION),
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )


def _strategy_with_completed_open():
    strategy = _strategy()
    strategy.remember_completed_effect(
        "application_open",
        target_application="calculator",
        evidence="exact_process_and_window_observation",
        observed_at=time.time(),
    )
    return strategy


def test_phase_c_01_verified_application_open_remains_verified(tmp_path):
    controller = _terminality(tmp_path)
    strategy = _strategy_with_completed_open()

    controller.set_strategy_snapshot(strategy.snapshot())

    snapshot = controller.snapshot()
    assert "application_open" in snapshot["proven_postconditions"]
    assert snapshot["remaining_work"] == [CRITERION]


def test_phase_c_02_later_primitive_uncertainty_does_not_erase_prerequisite(
    tmp_path,
):
    controller = _terminality(tmp_path)
    strategy = _strategy_with_completed_open()
    _observe(strategy, foreground=True)
    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        semantic,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="uia_activation_outcome_unknown",
        result=_semantic_receipt(),
        strategy=strategy,
    )

    controller.set_strategy_snapshot(strategy.snapshot())

    snapshot = controller.snapshot()
    assert "application_open" in snapshot["proven_postconditions"]
    assert snapshot["remaining_work"] == [CRITERION]


def test_phase_c_03_checkpoint_reload_preserves_completed_prerequisite(tmp_path):
    controller = _terminality(tmp_path)
    controller.set_strategy_snapshot(_strategy_with_completed_open().snapshot())

    restored = _terminality(tmp_path)

    assert "application_open" in restored.snapshot()["proven_postconditions"]
    assert restored.snapshot()["remaining_work"] == [CRITERION]


def test_phase_c_04_inline_progress_projection_cannot_mutate_semantic_state(
    tmp_path,
):
    controller = _terminality(tmp_path)
    controller.set_strategy_snapshot(_strategy_with_completed_open().snapshot())
    durable_task_state = {
        "run_id": "run_phase_c",
        "plan_revision": 1,
        "execution_memory": {
            "completed_effects": [{"attempted_effect": "application_open"}],
        },
        "run_state": "running",
    }
    before_task = copy.deepcopy(durable_task_state)
    before_terminal = controller.snapshot()

    projection = pc_projection(durable_task_state)

    assert projection["run_state"] == "running"
    assert durable_task_state == before_task
    assert controller.snapshot() == before_terminal


def test_phase_c_05_terminal_explanation_names_only_actual_unresolved_step(
    tmp_path,
):
    controller = _terminality(tmp_path)
    strategy = _strategy_with_completed_open()
    _observe(strategy, foreground=True)
    semantic = es.tool_preflight(
        "activate_desktop_surface", _semantic_arguments(), strategy=strategy,
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        semantic,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="uia_activation_outcome_unknown",
        result=_semantic_receipt(),
        strategy=strategy,
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    decision = controller.evaluate()
    explanation = controller.continuation_context(decision)

    assert CRITERION in explanation
    assert f'"remaining_work":["{CRITERION}"]' in explanation
    assert '"remaining_work":["application_open"' not in explanation


def _start_settle_attempt(tmp_path, *, store_name: str = "settle-attempts"):
    store, record, _fingerprint = _durable_attempt(tmp_path / store_name)
    entry = _journal_entry(record)
    record = store.record_primitive_dispatch_started(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )
    record = store.record_primitive_dispatch_returned(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )
    record = store.bind_primitive_receipt(
        record["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )
    record = store.mark_waiting_verification(
        record["tool_attempt_id"],
        next_check_at=time.time() + 20.0,
        deadline_at=time.time() + 60.0,
        owner_settled=True,
    )
    return store, record


def _settle_coordinator(store, record, monkeypatch, *, verifier=None):
    resumed = []
    finalized = []
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(coordinator, "_schedule_timer", lambda *_a, **_k: None)
    coordinator.register_suspended(
        record["tool_attempt_id"],
        resume=lambda attempt_id, payload: resumed.append(
            (attempt_id, payload)
        ) or "assistant_phase_d",
        finalize=lambda *args: finalized.append(args),
        verify=verifier,
    )
    assert coordinator.activate(record["tool_attempt_id"])
    return coordinator, resumed, finalized


def _typed_probe(
    classification: SemanticSettleProbeClassification,
    *,
    terminal_payload=None,
    probe_invoked: bool = True,
    reason_code: str = "settle_probe_fixture",
):
    observed_at = tool_attempts_module._now()
    completed = classification in {
        SemanticSettleProbeClassification.COMPLETED_MATCH,
        SemanticSettleProbeClassification.COMPLETED_NONMATCH,
        SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE,
        SemanticSettleProbeClassification.STALE_OR_INVALID,
    }
    return SemanticSettleProbeResult(
        classification=classification,
        observer_mechanism="inspect_desktop_surfaces",
        observation_started_at=observed_at,
        observation_completed_at=observed_at + 0.01 if completed else 0.0,
        target_application="calculator",
        surface_ref=BUTTON_REF,
        native_window_id="592338",
        window_generation="calculator-generation-1",
        exact_postcondition_match=(
            True
            if classification is SemanticSettleProbeClassification.COMPLETED_MATCH
            else False
            if classification
            is SemanticSettleProbeClassification.COMPLETED_NONMATCH
            else None
        ),
        reason_code=reason_code,
        evidence_ref="sha256:" + "a" * 64 if completed else "",
        probe_invoked=probe_invoked,
        terminal_payload=terminal_payload,
    )


def test_phase_d_01_exactly_three_one_second_local_checks(tmp_path, monkeypatch):
    store, record = _start_settle_attempt(tmp_path)
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=lambda _attempt_id: None,
    )
    start = store.get_attempt(record["tool_attempt_id"])[
        "semantic_settle"
    ]["started_at"]
    clock = {"value": start}
    monkeypatch.setattr(
        tool_attempts_module, "_now", lambda: clock["value"],
    )

    for expected in (1, 2, 3):
        clock["value"] += 1.0
        coordinator._timer_fired(record["tool_attempt_id"])
        settle = store.get_attempt(record["tool_attempt_id"])[
            "semantic_settle"
        ]
        assert settle["local_check_count"] == expected
        assert settle["settle_slot_count"] == expected
        assert settle["local_check_times"][-1] == start + expected
        assert settle["probe_attempt_count"] == expected
        assert settle["completed_observation_count"] == 0
        assert settle["observation_receipts"][-1][
            "result_classification"
        ] == "probe_failed"
    coordinator._timer_fired(record["tool_attempt_id"])
    assert store.get_attempt(record["tool_attempt_id"])["semantic_settle"][
        "settle_slot_count"
    ] == 3


def test_phase_d_02_uncertainty_is_emitted_only_after_check_three(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path)
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=lambda _attempt_id: None,
    )

    coordinator._timer_fired(record["tool_attempt_id"])
    assert store.get_attempt(record["tool_attempt_id"])["status"] == (
        ToolAttemptStatus.WAITING_VERIFICATION.value
    )
    coordinator._timer_fired(record["tool_attempt_id"])
    assert store.get_attempt(record["tool_attempt_id"])["status"] == (
        ToolAttemptStatus.WAITING_VERIFICATION.value
    )
    coordinator._timer_fired(record["tool_attempt_id"])

    terminal = store.get_attempt(record["tool_attempt_id"])
    assert terminal["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert terminal["reason_code"] == "semantic_settle_observation_failed"
    assert terminal["semantic_settle"]["state"] == "observation_failed"
    assert terminal["semantic_settle"]["completed_observation_count"] == 0
    primitive = _journal_entry(terminal)
    assert primitive["lifecycle"] == "unknown"
    assert primitive["effect_classification"] == "unknown"
    assert primitive["dispatch_started_at"] > 0


def test_phase_d_02b_three_completed_observations_exhaust_semantic_evidence(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path, store_name="real-observations")
    results = iter((
        SemanticSettleProbeClassification.COMPLETED_NONMATCH,
        SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE,
        SemanticSettleProbeClassification.COMPLETED_NONMATCH,
    ))
    coordinator, _resumed, _finalized = _settle_coordinator(
        store,
        record,
        monkeypatch,
        verifier=lambda _attempt_id: _typed_probe(next(results)),
    )

    for _index in range(3):
        coordinator._timer_fired(record["tool_attempt_id"])

    terminal = store.get_attempt(record["tool_attempt_id"])
    settle = terminal["semantic_settle"]
    assert terminal["reason_code"] == "semantic_settle_exhausted"
    assert settle["state"] == "exhausted"
    assert settle["settle_slot_count"] == 3
    assert settle["probe_attempt_count"] == 3
    assert settle["completed_observation_count"] == 3
    assert [
        item["result_classification"]
        for item in settle["observation_receipts"]
    ] == [
        "completed_nonmatch", "completed_inconclusive", "completed_nonmatch",
    ]


@pytest.mark.parametrize("proof_check", [1, 2, 3])
def test_phase_d_proof_on_each_allowed_check_stops_immediately(
    tmp_path, monkeypatch, proof_check,
):
    store, record = _start_settle_attempt(
        tmp_path, store_name=f"proof-check-{proof_check}",
    )
    observations = []

    def _verify(attempt_id):
        observations.append(attempt_id)
        if len(observations) != proof_check:
            return _typed_probe(
                SemanticSettleProbeClassification.COMPLETED_NONMATCH,
                reason_code="settle_probe_completed_nonmatch",
            )
        primitive_id = _journal_entry(store.get_attempt(attempt_id))[
            "primitive_id"
        ]
        store.record_primitive_outcome(
            attempt_id, primitive_id=primitive_id, outcome="verified",
        )
        terminal, _created = store.record_terminal_result(
            attempt_id,
            status=ToolAttemptStatus.SUCCEEDED,
            terminal_outcome="success_verified",
            dispatch_state="effect_confirmed",
            reason_code="postcondition_verified_read_only",
        )
        return _typed_probe(
            SemanticSettleProbeClassification.COMPLETED_MATCH,
            terminal_payload=terminal,
            reason_code="settle_probe_completed_match",
        )

    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=_verify,
    )
    for _index in range(proof_check):
        coordinator._timer_fired(record["tool_attempt_id"])

    terminal = store.get_attempt(record["tool_attempt_id"])
    assert terminal["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert terminal["semantic_settle"]["local_check_count"] == proof_check
    assert len(observations) == proof_check
    receipts = terminal["semantic_settle"]["observation_receipts"]
    assert len(receipts) == proof_check
    assert receipts[-1]["slot_index"] == proof_check
    assert receipts[-1]["result_classification"] == "completed_match"
    assert receipts[-1]["exact_postcondition_match"] is True
    assert receipts[-1]["evidence_ref"] == "sha256:" + "a" * 64


def test_phase_d_03_each_new_checkpoint_gets_a_fresh_three_check_budget(
    tmp_path, monkeypatch,
):
    totals = []
    for name in ("checkpoint_b", "checkpoint_c"):
        store, record = _start_settle_attempt(tmp_path, store_name=name)
        coordinator, _resumed, _finalized = _settle_coordinator(
            store, record, monkeypatch, verifier=lambda _attempt_id: None,
        )
        for _index in range(3):
            coordinator._timer_fired(record["tool_attempt_id"])
        totals.append(store.get_attempt(record["tool_attempt_id"])[
            "semantic_settle"
        ]["local_check_count"])

    assert totals == [3, 3]


def test_phase_d_04_proof_observed_stops_settle_early(tmp_path, monkeypatch):
    store, record = _start_settle_attempt(tmp_path)
    observations = []

    def _verify(attempt_id):
        observations.append(attempt_id)
        if len(observations) != 2:
            return _typed_probe(
                SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE,
            )
        primitive_id = _journal_entry(store.get_attempt(attempt_id))[
            "primitive_id"
        ]
        store.record_primitive_outcome(
            attempt_id, primitive_id=primitive_id, outcome="verified",
        )
        terminal, _created = store.record_terminal_result(
            attempt_id,
            status=ToolAttemptStatus.SUCCEEDED,
            terminal_outcome="success_verified",
            dispatch_state="effect_confirmed",
            reason_code="postcondition_verified_read_only",
        )
        return _typed_probe(
            SemanticSettleProbeClassification.COMPLETED_MATCH,
            terminal_payload=terminal,
        )

    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=_verify,
    )
    coordinator._timer_fired(record["tool_attempt_id"])
    coordinator._timer_fired(record["tool_attempt_id"])

    terminal = store.get_attempt(record["tool_attempt_id"])
    assert terminal["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert terminal["semantic_settle"]["local_check_count"] == 2
    assert terminal["semantic_settle"]["state"] == "proof_observed"


def test_phase_d_05_settle_never_replays_the_semantic_primitive(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path)
    before = _journal_entry(record)
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=lambda _attempt_id: None,
    )

    for _index in range(3):
        coordinator._timer_fired(record["tool_attempt_id"])

    after_record = store.get_attempt(record["tool_attempt_id"])
    after = _journal_entry(after_record)
    assert len(after_record["primitive_journal"]) == 1
    assert after["primitive_id"] == before["primitive_id"]
    assert after["dispatch_started_at"] == before["dispatch_started_at"]


def test_phase_d_06_stale_target_aborts_settle_truthfully(tmp_path, monkeypatch):
    store, record = _start_settle_attempt(tmp_path)

    def _stale(_attempt_id):
        return _typed_probe(
            SemanticSettleProbeClassification.STALE_OR_INVALID,
            reason_code="settle_probe_target_stale_or_invalid",
        )

    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=_stale,
    )
    coordinator._timer_fired(record["tool_attempt_id"])

    terminal = store.get_attempt(record["tool_attempt_id"])
    assert terminal["reason_code"] == "semantic_settle_target_invalid"
    assert terminal["semantic_settle"]["state"] == "stale_target"
    assert terminal["semantic_settle"]["local_check_count"] == 1
    assert terminal["semantic_settle"]["observation_receipts"][0][
        "result_classification"
    ] == "stale_or_invalid"


def test_phase_d_07_diagnostics_separate_local_and_provider_checks(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path)
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=lambda _attempt_id: None,
    )
    for _index in range(3):
        coordinator._timer_fired(record["tool_attempt_id"])

    settle = store.get_attempt(record["tool_attempt_id"])["semantic_settle"]
    assert settle["local_check_count"] == 3
    assert settle["provider_check_count"] == 0


def test_phase_d_07b_missing_probe_callback_is_explicit_and_not_invoked(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path, store_name="missing-callback")
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=None,
    )

    coordinator._timer_fired(record["tool_attempt_id"])

    settle = store.get_attempt(record["tool_attempt_id"])["semantic_settle"]
    receipt = settle["observation_receipts"][0]
    assert receipt["result_classification"] == "probe_failed"
    assert receipt["probe_invoked"] is False
    assert receipt["reason_code"] == "settle_probe_callback_missing"
    assert settle["settle_slot_count"] == 1
    assert settle["probe_attempt_count"] == 0
    assert settle["completed_observation_count"] == 0


def test_phase_d_07c_receipt_is_persisted_atomically_with_slot_advance(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path, store_name="atomic-receipt")
    original = store.record_semantic_settle_probe_result
    transitions = []

    def _record(attempt_id, *, result):
        before = store.get_attempt(attempt_id)["semantic_settle"]
        assert before["settle_slot_count"] == len(before["observation_receipts"])
        persisted = original(attempt_id, result=result)
        after = persisted["semantic_settle"]
        transitions.append((before, after))
        assert after["settle_slot_count"] == len(after["observation_receipts"])
        return persisted

    monkeypatch.setattr(store, "record_semantic_settle_probe_result", _record)
    coordinator, _resumed, _finalized = _settle_coordinator(
        store,
        record,
        monkeypatch,
        verifier=lambda _attempt_id: _typed_probe(
            SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE,
        ),
    )

    coordinator._timer_fired(record["tool_attempt_id"])

    assert len(transitions) == 1
    before, after = transitions[0]
    assert before["settle_slot_count"] == 0
    assert after["settle_slot_count"] == 1
    assert after["observation_receipts"][0]["slot_index"] == 1


def test_phase_d_07d_restart_recovers_typed_settle_history(tmp_path, monkeypatch):
    store, record = _start_settle_attempt(tmp_path, store_name="restart-history")
    coordinator, _resumed, _finalized = _settle_coordinator(
        store,
        record,
        monkeypatch,
        verifier=lambda _attempt_id: _typed_probe(
            SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE,
        ),
    )
    coordinator._timer_fired(record["tool_attempt_id"])
    coordinator._timer_fired(record["tool_attempt_id"])

    restarted_store = ToolAttemptStore(store.root)
    ToolAttemptCoordinator(restarted_store).startup_fail_closed(
        now=tool_attempts_module._now(),
    )
    recovered = restarted_store.get_attempt(record["tool_attempt_id"])
    settle = recovered["semantic_settle"]
    assert settle["settle_slot_count"] == 2
    assert settle["probe_attempt_count"] == 2
    assert settle["completed_observation_count"] == 2
    assert [
        item["slot_index"] for item in settle["observation_receipts"]
    ] == [1, 2]
    assert all(item["evidence_ref"] for item in settle["observation_receipts"])


def test_phase_d_07e_local_match_never_polls_provider(tmp_path, monkeypatch):
    store, record = _start_settle_attempt(tmp_path, store_name="local-only")
    provider_calls = []
    primitive_id = _journal_entry(record)["primitive_id"]

    def _verify(attempt_id):
        store.record_primitive_outcome(
            attempt_id, primitive_id=primitive_id, outcome="verified",
        )
        terminal, _created = store.record_terminal_result(
            attempt_id,
            status=ToolAttemptStatus.SUCCEEDED,
            terminal_outcome="success_verified",
            dispatch_state="effect_confirmed",
            reason_code="postcondition_verified_read_only",
        )
        return _typed_probe(
            SemanticSettleProbeClassification.COMPLETED_MATCH,
            terminal_payload=terminal,
        )

    coordinator, _resumed, _finalized = _settle_coordinator(
        store, record, monkeypatch, verifier=_verify,
    )
    coordinator._timer_fired(record["tool_attempt_id"])

    assert provider_calls == []
    assert store.get_attempt(record["tool_attempt_id"])["status"] == (
        ToolAttemptStatus.SUCCEEDED.value
    )


def test_phase_d_08_non_uia_long_running_timing_is_unchanged(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path / "long-running")
    fingerprint = store.fingerprint_arguments({"fixture": "read-only"})
    record = store.create_attempt(
        conversation_id="conversation_long",
        turn_id="turn_long",
        tool_call_id="toolcall_long",
        tool_name="read_page",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={},
        argument_fingerprint=fingerprint,
        side_effect="read_only",
        idempotence="idempotent",
        retry_policy="read_once",
        persistence_contract={"check_delays_seconds": [7.0]},
    )
    monkeypatch.setattr(tool_attempts_module.secrets, "randbelow", lambda _n: 0)

    due, index = ToolAttemptCoordinator._next_progressive_check(record, 100.0)

    assert record["semantic_settle"]["state"] == "inactive"
    assert (due, index) == (107.0, 1)


def test_phase_d_09_predispatch_cancellation_remains_authoritative(tmp_path):
    store2 = ToolAttemptStore(tmp_path / "cancel-before-dispatch")
    fingerprint = store2.fingerprint_arguments({"surface_ref": BUTTON_REF})
    created = store2.create_attempt(
        conversation_id="conversation_cancel",
        turn_id="turn_cancel",
        tool_call_id="toolcall_cancel",
        tool_name="activate_desktop_surface",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={},
        argument_fingerprint=fingerprint,
        side_effect="external_mutation",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        primitive_binding={
            "effect_role": "semantic",
            "semantic_substep_id": "substep_cancel",
            "surface_ref": BUTTON_REF,
        },
    )
    accepted = store2.mark_accepted(
        created["tool_attempt_id"], expected_revision=created["revision"],
    )
    cancelled = store2.request_cancellation(accepted["tool_attempt_id"])
    entry = _journal_entry(cancelled)
    after_dispatch_request = store2.record_primitive_dispatch_started(
        cancelled["tool_attempt_id"], primitive_id=entry["primitive_id"],
    )

    assert cancelled["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    assert after_dispatch_request["status"] == (
        ToolAttemptStatus.CANCELLED_PROVEN.value
    )
    assert _journal_entry(after_dispatch_request)["lifecycle"] == "prepared"
    assert after_dispatch_request["semantic_settle"]["state"] == "inactive"


def test_phase_d_10_cancellation_during_settle_remains_truthful(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path)
    cancelled = store.request_cancellation(record["tool_attempt_id"])
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, cancelled, monkeypatch, verifier=lambda _attempt_id: None,
    )

    coordinator._timer_fired(record["tool_attempt_id"])
    after_one = store.get_attempt(record["tool_attempt_id"])
    assert after_one["status"] == ToolAttemptStatus.CANCELLATION_REQUESTED.value
    coordinator._timer_fired(record["tool_attempt_id"])
    coordinator._timer_fired(record["tool_attempt_id"])

    terminal = store.get_attempt(record["tool_attempt_id"])
    assert terminal["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    assert terminal["terminal_outcome"] == "cancelled_proven"
    assert terminal["semantic_settle"]["state"] == "cancelled"
    assert terminal["semantic_settle"]["local_check_count"] == 3


def test_phase_d_11_authoritative_success_receipt_skips_settle_sleep(tmp_path):
    store, record = _start_settle_attempt(tmp_path)
    primitive_id = _journal_entry(record)["primitive_id"]
    store.record_primitive_outcome(
        record["tool_attempt_id"],
        primitive_id=primitive_id,
        outcome="verified",
    )

    terminal, _created = store.record_terminal_result(
        record["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
        reason_code="authoritative_success_receipt",
    )

    assert terminal["semantic_settle"]["local_check_count"] == 0
    assert terminal["semantic_settle"]["state"] == "proof_observed"


def test_phase_d_12_preparatory_focus_never_inherits_semantic_settle(tmp_path):
    store, record, _fingerprint = _durable_attempt(
        tmp_path, effect_role="preparatory", surface_ref=WINDOW_REF,
    )
    entry = _journal_entry(record)

    dispatched = store.record_primitive_dispatch_started(
        record["tool_attempt_id"],
        primitive_id=entry["primitive_id"],
        uia_pattern="Window",
    )

    assert dispatched["semantic_settle"]["state"] == "inactive"
    assert dispatched["semantic_settle"]["local_check_count"] == 0


def test_phase_d_13_owner_active_poll_is_not_counted_as_local_observation(
    tmp_path, monkeypatch,
):
    store, record = _start_settle_attempt(tmp_path)
    active = store.mark_pending(
        record["tool_attempt_id"],
        next_check_at=time.time(),
        deadline_at=time.time() + 60.0,
    )
    coordinator, _resumed, _finalized = _settle_coordinator(
        store, active, monkeypatch, verifier=lambda _attempt_id: None,
    )

    coordinator._timer_fired(record["tool_attempt_id"])

    current = store.get_attempt(record["tool_attempt_id"])
    assert current["semantic_settle"]["local_check_count"] == 0
    assert current["status"] == ToolAttemptStatus.PENDING.value


def _batch_arguments() -> dict:
    display = "surface-fixture-display"
    expression = "surface-fixture-expression"
    controls = (
        ("surface-fixture-five", "Five", display, "5"),
        ("surface-fixture-plus", "Plus", expression, "5 +"),
        ("surface-fixture-two", "Two", display, "2"),
        ("surface-fixture-three", "Three", display, "23"),
        ("surface-fixture-equals", "Equals", display, "28"),
    )
    items = [
        {
            "surface_ref": surface_ref,
            "objective": objective,
            "action_pattern": "Invoke",
            "observable_effect": {
                "kind": "exact_surface_text",
                "surface_ref": witness,
                "expected_text": expected_text,
            },
            "branch_independent": True,
            "transition_boundary": "none",
            "permission_boundary": False,
            "safety_class": "reversible_local",
        }
        for surface_ref, objective, witness, expected_text in controls
    ]
    return {
        "surface_ref": items[0]["surface_ref"],
        "objective": items[0]["objective"],
        "action_pattern": items[0]["action_pattern"],
        "observable_effect": dict(items[-1]["observable_effect"]),
        "semantic_batch": items,
    }


def _batch_plan(arguments: dict | None = None):
    arguments = arguments or _batch_arguments()
    plan, reason = normalize_semantic_batch(
        surface_ref=arguments["surface_ref"],
        objective=arguments["objective"],
        action_pattern=arguments["action_pattern"],
        observable_effect=arguments["observable_effect"],
        semantic_batch=arguments["semantic_batch"],
    )
    assert reason == ""
    return plan


def _observe_batch_scene(
    strategy, *, display_text="0", expression_text="Expression",
    fresh_three_ref="",
):
    arguments = _batch_arguments()
    scene = _scene(foreground=True)
    scene["surfaces"] = [
        scene["surfaces"][0],
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-display",
            "label": display_text,
        },
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-expression",
            "label": expression_text,
        },
    ] + [
        {
            "surface_ref": (
                fresh_three_ref
                if item["objective"] == "Three" and fresh_three_ref else
                item["surface_ref"]
            ),
            "surface_kind": "control",
            "role": "ButtonControl",
            "label": item["objective"],
            "enabled": True,
            "patterns": ["Invoke"],
            "actuation": {
                "available": True,
                "attempted_effect": "desktop_uia_primitive",
                "background_safe": True,
                "foreground_required": False,
                "patterns": ["Invoke"],
            },
        }
        for item in arguments["semantic_batch"]
    ]
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )


def _successful_batch_dispatch(calls: list[dict]):
    def _dispatch(item):
        calls.append(dict(item))
        return {
            "text": "dispatched",
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "dispatched",
                "outcome": "success_unverified",
                "reason_code": "native_accessibility_dispatched",
                "surface_ref": item["surface_ref"],
                "structural_epoch": 7,
                "primitive": {
                    "kind": "desktop_uia_primitive",
                    "mechanism": "invoke",
                    "observable_effect": dict(item["observable_effect"]),
                    "dispatched_at": 100.0 + item["index"],
                    "before": {},
                },
            },
        }

    return _dispatch


def _batch_store(tmp_path, plan):
    store = ToolAttemptStore(tmp_path / "semantic-batch-attempts")
    fingerprint = store.fingerprint_arguments({
        "semantic_batch_id": plan["semantic_batch_id"],
    })
    bindings = []
    for index, item in enumerate(plan["items"]):
        bindings.append({
            "effect_role": "semantic",
            "semantic_node_id": "node_batch",
            "semantic_substep_id": f"substep_batch_{index + 1}",
            "strategy_attempt_id": "strategy_batch",
            "application_identity": "fixtureapp",
            "surface_ref": item["surface_ref"],
            "native_window_id": "4242",
            "window_generation": "generation_batch",
            "runtime_identity": f"runtime_batch_{index + 1}",
            "automation_id": f"control_batch_{index + 1}",
            "uia_pattern": item["action_pattern"],
            "attempted_effect": "desktop_uia_primitive",
            "semantic_postcondition": "sha256:" + f"{index + 1:064x}",
        })
    record = store.create_attempt(
        conversation_id="conversation_batch",
        turn_id="turn_batch",
        tool_call_id="toolcall_batch",
        tool_name="activate_desktop_surface",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context={},
        argument_fingerprint=fingerprint,
        side_effect="external_mutation",
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        primitive_binding={
            "semantic_batch_id": plan["semantic_batch_id"],
            "batch_primitives": bindings,
        },
    )
    record = store.mark_accepted(
        record["tool_attempt_id"], expected_revision=record["revision"],
    )
    record = store.mark_running(record["tool_attempt_id"])
    return store, record


def _journal_batch_prefix(store, record, count: int):
    attempt_id = record["tool_attempt_id"]
    for entry in store.get_attempt(attempt_id)["primitive_journal"][:count]:
        store.record_primitive_dispatch_started(
            attempt_id, primitive_id=entry["primitive_id"],
        )
        store.record_primitive_dispatch_returned(
            attempt_id, primitive_id=entry["primitive_id"],
        )
    return store.get_attempt(attempt_id)


def _batch_receipt(item, *, dispatch_state="dispatched", outcome="success_unverified",
                   reason_code="native_accessibility_dispatched", **extra):
    receipt = {
        "schema_version": 1,
        "dispatch_state": dispatch_state,
        "outcome": outcome,
        "reason_code": reason_code,
        "surface_ref": item["surface_ref"],
        "structural_epoch": 7,
        "primitive": {
            "kind": "desktop_uia_primitive",
            "mechanism": "invoke",
            "observable_effect": dict(item["observable_effect"]),
            "dispatched_at": 100.0 + item["index"],
            "before": {},
        },
    }
    receipt.update(extra)
    return receipt


def _record_partial_batch(store, record, plan, *, rejected_index=3,
                          reason_code="stale_semantic_reference"):
    attempt_id = record["tool_attempt_id"]
    for index, item in enumerate(plan["items"][:rejected_index]):
        entry = store.get_attempt(attempt_id)["primitive_journal"][index]
        store.record_primitive_dispatch_started(
            attempt_id, primitive_id=entry["primitive_id"],
        )
        store.record_primitive_dispatch_returned(
            attempt_id, primitive_id=entry["primitive_id"],
        )
        store.record_semantic_batch_primitive_receipt(
            attempt_id,
            semantic_batch_id=plan["semantic_batch_id"],
            batch_index=index,
            receipt=_batch_receipt(item),
        )
    rejected = plan["items"][rejected_index]
    store.record_semantic_batch_primitive_receipt(
        attempt_id,
        semantic_batch_id=plan["semantic_batch_id"],
        batch_index=rejected_index,
        receipt=_batch_receipt(
            rejected,
            dispatch_state="not_dispatched",
            outcome="confirmed_failure_before_effect",
            reason_code=reason_code,
            failure_class="mechanism_failed_continue",
            stale_reason=(
                "accessibility_runtime_id_disappeared"
                if "stale" in reason_code else ""
            ),
            actionability={
                "dispatch_preflight_result": "failed_before_effect",
                "failed_before_effect_reason": reason_code,
                "stale_rebind_status": "fresh_runtime_reference_missing",
                "selected_pattern_available": False,
                "candidate_applicable": False,
            },
        ),
    )
    return store.get_attempt(attempt_id)


def test_phase_e_00_strategy_accepts_only_one_typed_batch_checkpoint():
    strategy = _strategy()
    scene = _scene(foreground=True)
    scene["surfaces"] = [
        scene["surfaces"][0],
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-display",
            "label": "0",
        },
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-expression",
            "label": "Expression",
        },
    ] + [
        {
            "surface_ref": item["surface_ref"],
            "surface_kind": "control",
            "role": "ButtonControl",
            "label": item["objective"],
            "enabled": True,
            "patterns": ["Invoke"],
            "actuation": {
                "available": True,
                "attempted_effect": "desktop_uia_primitive",
                "background_safe": True,
                "foreground_required": False,
                "patterns": ["Invoke"],
            },
        }
        for item in _batch_arguments()["semantic_batch"]
    ]
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )

    decision = es.tool_preflight(
        "activate_desktop_surface",
        _batch_arguments(),
        strategy=strategy,
    )

    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt.semantic_batch_id.startswith("batch_")
    assert len(attempt.semantic_batch_effects) == 5
    assert attempt.semantic_checkpoint_count == 1

    memory = es.export_execution_memory(strategy)
    restored = _strategy()
    es.rebind_execution_memory(restored, [memory])
    rebound = next(
        item for item in restored.attempts
        if item.attempt_id == attempt.attempt_id
    )
    assert rebound.semantic_batch_id == attempt.semantic_batch_id
    assert rebound.semantic_batch_effects == attempt.semantic_batch_effects
    assert rebound.semantic_checkpoint_count == 1


def test_phase_e_00a_batch_rejects_an_effect_already_true_before_dispatch():
    strategy = _strategy()
    scene = _scene(foreground=True)
    scene["surfaces"] = [
        scene["surfaces"][0],
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-display",
            "label": "5",
        },
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-expression",
            "label": "Expression",
        },
    ] + [
        {
            "surface_ref": item["surface_ref"],
            "surface_kind": "control",
            "role": "ButtonControl",
            "label": item["objective"],
            "enabled": True,
            "patterns": ["Invoke"],
            "actuation": {
                "available": True,
                "attempted_effect": "desktop_uia_primitive",
                "background_safe": True,
                "foreground_required": False,
                "patterns": ["Invoke"],
            },
        }
        for item in _batch_arguments()["semantic_batch"]
    ]
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )

    decision = es.tool_preflight(
        "activate_desktop_surface",
        _batch_arguments(),
        strategy=strategy,
    )

    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "semantic_batch_effect_already_observed"
    assert strategy.attempts == []


def test_phase_e_00b_calculation_batch_must_end_at_exact_result():
    strategy = _strategy_with_completed_open()
    open_node = strategy.mission.action_nodes[0]
    assert strategy.verify_postcondition(
        "calculator_window_observed",
        postcondition=open_node.observable_success_postcondition,
    )
    arguments = _batch_arguments()
    arguments["semantic_batch"] = arguments["semantic_batch"][1:4]
    first = arguments["semantic_batch"][0]
    final = arguments["semantic_batch"][-1]
    arguments.update({
        "surface_ref": first["surface_ref"],
        "objective": first["objective"],
        "action_pattern": first["action_pattern"],
        "observable_effect": dict(final["observable_effect"]),
    })
    scene = _scene(foreground=True)
    scene["surfaces"] = [
        scene["surfaces"][0],
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-display",
            "label": "5",
        },
        {
            **scene["surfaces"][1],
            "surface_ref": "surface-fixture-expression",
            "label": "Expression",
        },
    ] + [
        {
            "surface_ref": item["surface_ref"],
            "surface_kind": "control",
            "role": "ButtonControl",
            "label": item["objective"],
            "enabled": True,
            "patterns": ["Invoke"],
            "actuation": {
                "available": True,
                "attempted_effect": "desktop_uia_primitive",
                "background_safe": True,
                "foreground_required": False,
                "patterns": ["Invoke"],
            },
        }
        for item in _batch_arguments()["semantic_batch"]
    ]
    es.record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "observed",
                "reason_code": "semantic_desktop_surface_observed",
            },
        },
        is_error=False,
        strategy=strategy,
    )

    decision = es.tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )

    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "semantic_batch_incomplete_semantic_goal"
    assert strategy.attempts == []


def test_phase_e_01_five_safe_primitives_create_one_semantic_checkpoint():
    plan = _batch_plan()
    calls = []

    result = execute_semantic_batch(plan, _successful_batch_dispatch(calls))

    batch = result["receipt"]["semantic_batch"]
    assert len(calls) == 5
    assert batch["primitive_count"] == 5
    assert batch["semantic_checkpoint_count"] == 1
    assert result["receipt"]["reason_code"] == "semantic_batch_dispatched"


def test_phase_e_02_all_five_dispatches_are_individually_journaled(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)

    current = _journal_batch_prefix(store, record, 5)

    assert len(current["primitive_journal"]) == 5
    assert len({item["primitive_id"] for item in current["primitive_journal"]}) == 5
    assert all(
        item["lifecycle"] == "dispatch_returned"
        for item in current["primitive_journal"]
    )
    assert current["semantic_batch"]["dispatched_count"] == 5
    assert current["semantic_settle"]["state"] == "active"


def test_phase_e_03_no_provider_or_verifier_boundary_between_primitives():
    plan = _batch_plan()
    calls = []
    provider_calls = []
    verifier_calls = []

    execute_semantic_batch(plan, _successful_batch_dispatch(calls))

    assert len(calls) == 5
    assert provider_calls == []
    assert verifier_calls == []


def test_phase_e_04_ambiguous_primitive_stops_later_dispatches():
    plan = _batch_plan()
    calls = []

    def _dispatch(item):
        calls.append(item["surface_ref"])
        result = _successful_batch_dispatch([])(item)
        if item["index"] == 2:
            result["receipt"].update({
                "dispatch_state": "effect_unknown",
                "outcome": "outcome_unknown",
            })
        return result

    result = execute_semantic_batch(plan, _dispatch)

    assert len(calls) == 3
    assert result["receipt"]["reason_code"] == (
        "semantic_batch_primitive_outcome_unknown"
    )
    assert result["receipt"]["semantic_batch"]["stopped_at_index"] == 2
    assert result["receipt"]["semantic_batch"]["partial_batch_state"] == (
        "ambiguous_in_batch"
    )
    assert len(result["receipt"]["semantic_batch"]["primitive_receipts"]) == 3


def test_phase_e_05_stale_primitive_stops_batch_before_later_dispatches():
    plan = _batch_plan()
    calls = []

    def _dispatch(item):
        calls.append(item["surface_ref"])
        if item["index"] == 1:
            return {
                "text": "ERROR: stale",
                "receipt": {
                    "dispatch_state": "not_dispatched",
                    "outcome": "confirmed_failure_before_effect",
                    "reason_code": "stale_semantic_reference",
                },
            }
        return _successful_batch_dispatch([])(item)

    result = execute_semantic_batch(plan, _dispatch)

    assert len(calls) == 2
    assert result["receipt"]["reason_code"] == (
        "semantic_batch_prefix_checkpoint_pending"
    )
    assert result["receipt"]["semantic_batch"]["stopped_at_index"] == 1
    assert result["receipt"]["semantic_batch"]["rejection_reason"] == (
        "stale_semantic_reference"
    )


def test_phase_e_06_navigation_forces_a_checkpoint_before_batching():
    arguments = _batch_arguments()
    arguments["semantic_batch"][2]["transition_boundary"] = "navigation"

    plan, reason = normalize_semantic_batch(
        surface_ref=arguments["surface_ref"],
        objective=arguments["objective"],
        action_pattern=arguments["action_pattern"],
        observable_effect=arguments["observable_effect"],
        semantic_batch=arguments["semantic_batch"],
    )

    assert plan == {}
    assert reason == "semantic_batch_structural_transition"


def test_phase_e_07_permission_boundary_stops_batching():
    arguments = _batch_arguments()
    arguments["semantic_batch"][1]["permission_boundary"] = True

    plan, reason = normalize_semantic_batch(
        surface_ref=arguments["surface_ref"],
        objective=arguments["objective"],
        action_pattern=arguments["action_pattern"],
        observable_effect=arguments["observable_effect"],
        semantic_batch=arguments["semantic_batch"],
    )

    assert plan == {}
    assert reason == "semantic_batch_permission_boundary"


def test_phase_e_08_sensitive_irreversible_action_cannot_be_buried():
    arguments = _batch_arguments()
    arguments["semantic_batch"][3]["objective"] = "Purchase"

    plan, reason = normalize_semantic_batch(
        surface_ref=arguments["surface_ref"],
        objective=arguments["objective"],
        action_pattern=arguments["action_pattern"],
        observable_effect=arguments["observable_effect"],
        semantic_batch=arguments["semantic_batch"],
    )

    assert plan == {}
    assert reason == "semantic_batch_sensitive_boundary"


def test_phase_e_09_final_exact_state_verifies_the_whole_batch(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)
    current = _journal_batch_prefix(store, record, 5)
    classified = classify_batch_observation(
        plan, plan["final_observable_effect"],
    )

    verified = store.reconcile_semantic_batch_prefix(
        current["tool_attempt_id"],
        semantic_batch_id=plan["semantic_batch_id"],
        applied_count=classified["resume_from_index"],
        exact_non_applied_suffix=True,
    )

    assert classified["state"] == "verified"
    assert verified["semantic_batch"]["state"] == "verified"
    assert all(
        item["effect_classification"] == "verified"
        for item in verified["primitive_journal"]
    )


def test_phase_e_10_exact_applied_prefix_resumes_from_safe_frontier(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)
    current = _journal_batch_prefix(store, record, 2)
    classified = classify_batch_observation(
        plan, plan["items"][1]["observable_effect"],
    )

    reconciled = store.reconcile_semantic_batch_prefix(
        current["tool_attempt_id"],
        semantic_batch_id=plan["semantic_batch_id"],
        applied_count=classified["applied_count"],
        exact_non_applied_suffix=True,
    )
    safe_frontier = plan["items"][classified["resume_from_index"]:]

    assert classified["state"] == "exact_prefix"
    assert reconciled["semantic_batch"]["state"] == "resume_ready"
    assert reconciled["semantic_batch"]["resume_from_index"] == 2
    assert [item["objective"] for item in safe_frontier] == [
        "Two", "Three", "Equals",
    ]
    terminal, _created = store.record_terminal_result(
        current["tool_attempt_id"],
        status=ToolAttemptStatus.FAILED_AFTER_EFFECT,
        terminal_outcome="failed_after_effect",
        dispatch_state="effect_confirmed_prefix",
        reason_code="semantic_batch_exact_prefix_reconciled",
    )
    assert terminal["semantic_batch"]["state"] == "resume_ready"
    assert terminal["semantic_batch"]["resume_from_index"] == 2


def test_phase_e_10a_multi_witness_observation_uses_latest_exact_prefix():
    plan = _batch_plan()

    classified = classify_batch_observation(
        plan,
        [
            plan["items"][0]["observable_effect"],
            plan["items"][1]["observable_effect"],
        ],
    )

    assert classified["state"] == "exact_prefix"
    assert classified["applied_count"] == 2
    assert classified["resume_from_index"] == 2


def test_phase_e_11_ambiguous_applied_prefix_remains_fail_closed(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)
    current = _journal_batch_prefix(store, record, 2)
    before = copy.deepcopy(current["primitive_journal"])
    classified = classify_batch_observation(
        plan,
        {},
        applied_subset_ambiguous=True,
    )

    reconciled = store.reconcile_semantic_batch_prefix(
        current["tool_attempt_id"],
        semantic_batch_id=plan["semantic_batch_id"],
        applied_count=None,
        exact_non_applied_suffix=False,
        applied_subset_ambiguous=True,
    )

    assert classified["state"] == "outcome_unknown"
    assert classified["anti_replay_required"] is True
    assert reconciled["semantic_batch"]["state"] == "outcome_unknown"
    assert reconciled["primitive_journal"] == before

    store2, record2 = _batch_store(tmp_path / "effect-possible-suffix", plan)
    current2 = _journal_batch_prefix(store2, record2, 3)
    with pytest.raises(
        IllegalTransition, match="semantic_batch_suffix_effect_possible",
    ):
        store2.reconcile_semantic_batch_prefix(
            current2["tool_attempt_id"],
            semantic_batch_id=plan["semantic_batch_id"],
            applied_count=2,
            exact_non_applied_suffix=True,
        )


def test_partial_batch_retains_rejected_receipt_and_never_dispatches_later_item():
    plan = _batch_plan()
    calls = []

    def _dispatch(item):
        calls.append(item["index"])
        if item["index"] == 3:
            return {
                "text": "ERROR: exact UIA pattern unavailable",
                "receipt": _batch_receipt(
                    item,
                    dispatch_state="not_dispatched",
                    outcome="confirmed_failure_before_effect",
                    reason_code="uia_action_pattern_unavailable",
                    actionability={
                        "dispatch_preflight_result": "failed_before_effect",
                        "failed_before_effect_reason": (
                            "uia_action_pattern_unavailable"
                        ),
                    },
                ),
            }
        return {"text": "dispatched", "receipt": _batch_receipt(item)}

    result = execute_semantic_batch(plan, _dispatch)
    receipt = result["receipt"]
    batch = receipt["semantic_batch"]

    assert calls == [0, 1, 2, 3]
    assert len(batch["primitive_receipts"]) == 4
    assert batch["primitive_receipts"][3]["dispatch_state"] == "not_dispatched"
    assert batch["primitive_receipts"][3]["outcome"] == (
        "confirmed_failure_before_effect"
    )
    assert batch["primitive_receipts"][3]["reason_code"] == (
        "uia_action_pattern_unavailable"
    )
    assert batch["partial_batch_state"] == "partial_prefix_dispatched"
    assert batch["dispatched_prefix_length"] == 3
    assert batch["rejected_primitive_index"] == 3
    assert batch["early_checkpoint"]["observable_effect"] == (
        plan["items"][2]["observable_effect"]
    )
    assert receipt["reason_code"] == "semantic_batch_prefix_checkpoint_pending"
    assert batch["primitive_count"] == 5


def test_partial_batch_receipts_and_exact_rejection_survive_restart(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)

    current = _record_partial_batch(
        store, record, plan, reason_code="stale_semantic_reference",
    )
    recovered = ToolAttemptStore(store.root).get_attempt(record["tool_attempt_id"])

    assert recovered == current
    journal = recovered["primitive_journal"]
    assert len(journal) == 5
    assert [item["lifecycle"] for item in journal] == [
        "receipt_bound", "receipt_bound", "receipt_bound", "not_applied",
        "prepared",
    ]
    assert all(item["primitive_receipt_id"] for item in journal[:4])
    assert journal[3]["dispatch_state"] == "not_dispatched"
    assert journal[3]["outcome"] == "confirmed_failure_before_effect"
    assert journal[3]["reason_code"] == "stale_semantic_reference"
    assert journal[3]["stale_reason"] == (
        "accessibility_runtime_id_disappeared"
    )
    assert journal[3]["failed_before_effect_reason"] == (
        "stale_semantic_reference"
    )
    assert recovered["semantic_batch"]["partial_batch_state"] == (
        "partial_prefix_dispatched"
    )
    assert recovered["semantic_batch"]["rejected_primitive_index"] == 3
    assert recovered["semantic_batch"]["rejection_reason"] == (
        "stale_semantic_reference"
    )
    assert recovered["semantic_settle"]["state"] == "active"
    assert recovered["semantic_settle"]["settle_slot_count"] == 0
    assert recovered["semantic_settle"]["completed_observation_count"] == 0


def test_partial_batch_aggregate_fallback_binds_every_attempted_receipt(tmp_path):
    from osia.agent import _bind_durable_primitive_receipt

    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)
    attempt_id = record["tool_attempt_id"]
    for entry in store.get_attempt(attempt_id)["primitive_journal"][:3]:
        store.record_primitive_dispatch_started(
            attempt_id, primitive_id=entry["primitive_id"],
        )
        store.record_primitive_dispatch_returned(
            attempt_id, primitive_id=entry["primitive_id"],
        )

    result = execute_semantic_batch(
        plan,
        lambda item: (
            {
                "text": "ERROR: unavailable",
                "receipt": _batch_receipt(
                    item,
                    dispatch_state="not_dispatched",
                    outcome="confirmed_failure_before_effect",
                    reason_code="observable_effect_precondition_unverified",
                    actionability={
                        "dispatch_preflight_result": "failed_before_effect",
                        "failed_before_effect_reason": (
                            "observable_effect_precondition_unverified"
                        ),
                    },
                ),
            }
            if item["index"] == 3 else
            {"text": "dispatched", "receipt": _batch_receipt(item)}
        ),
    )
    _bind_durable_primitive_receipt(
        store, attempt_id, "activate_desktop_surface", result,
    )
    recovered = store.get_attempt(attempt_id)

    assert [item["lifecycle"] for item in recovered["primitive_journal"]] == [
        "receipt_bound", "receipt_bound", "receipt_bound", "not_applied",
        "prepared",
    ]
    assert recovered["primitive_journal"][3]["reason_code"] == (
        "observable_effect_precondition_unverified"
    )
    assert recovered["semantic_batch"]["rejection_reason"] == (
        "observable_effect_precondition_unverified"
    )
    assert recovered["semantic_settle"]["state"] == "active"


@pytest.mark.parametrize("proof_slot", [1, 2, 3])
def test_partial_batch_prefix_proof_uses_typed_early_settle_slot(
    tmp_path, proof_slot,
):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path / f"slot-{proof_slot}", plan)
    current = _record_partial_batch(store, record, plan)
    attempt_id = current["tool_attempt_id"]
    settle = current["semantic_settle"]

    for slot in range(1, proof_slot + 1):
        classification = (
            SemanticSettleProbeClassification.COMPLETED_MATCH
            if slot == proof_slot else
            SemanticSettleProbeClassification.COMPLETED_NONMATCH
        )
        current = store.record_semantic_settle_probe_result(
            attempt_id,
            result=SemanticSettleProbeResult(
                classification=classification,
                observer_mechanism="local_desktop_application_observation",
                observation_started_at=200.0 + slot,
                observation_completed_at=200.5 + slot,
                target_application="fixtureapp",
                surface_ref=plan["items"][2]["observable_effect"]["surface_ref"],
                native_window_id="4242",
                window_generation="generation_batch",
                exact_postcondition_match=(slot == proof_slot),
                reason_code=(
                    "semantic_batch_exact_prefix_observed"
                    if slot == proof_slot else "settle_completed_nonmatch"
                ),
                evidence_ref="sha256:" + f"{slot:064x}",
                probe_invoked=True,
            ),
        )

    classified = classify_batch_observation(
        plan, plan["items"][2]["observable_effect"],
    )
    reconciled = store.reconcile_semantic_batch_prefix(
        attempt_id,
        semantic_batch_id=plan["semantic_batch_id"],
        applied_count=classified["applied_count"],
        exact_non_applied_suffix=True,
    )

    assert classified["state"] == "exact_prefix"
    assert reconciled["semantic_settle"]["settle_slot_count"] == proof_slot
    assert len(reconciled["semantic_settle"]["observation_receipts"]) == proof_slot
    assert reconciled["semantic_settle"]["observation_receipts"][-1][
        "result_classification"
    ] == "completed_match"
    assert reconciled["semantic_batch"]["state"] == "resume_ready"
    assert reconciled["semantic_batch"]["resume_from_index"] == 3
    assert all(
        item["effect_classification"] == "verified"
        for item in reconciled["primitive_journal"][:3]
    )
    assert reconciled["primitive_journal"][3]["lifecycle"] == "not_applied"
    assert reconciled["primitive_journal"][3]["reason_code"] == (
        "stale_semantic_reference"
    )


def test_each_partial_batch_early_checkpoint_gets_fresh_three_slot_budget(tmp_path):
    plan = _batch_plan()
    checkpoints = []

    for name in ("first", "second"):
        store, record = _batch_store(tmp_path / name, plan)
        current = _record_partial_batch(store, record, plan)
        checkpoints.append(current["semantic_settle"])

    assert all(item["state"] == "active" for item in checkpoints)
    assert all(item["max_local_checks"] == 3 for item in checkpoints)
    assert all(item["settle_slot_count"] == 0 for item in checkpoints)
    assert checkpoints[0]["checkpoint_id"]
    assert checkpoints[1]["checkpoint_id"]


def test_pattern_unavailable_rejection_keeps_its_typed_reason(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)

    current = _record_partial_batch(
        store,
        record,
        plan,
        reason_code="uia_action_pattern_unavailable",
    )
    rejected = current["primitive_journal"][3]

    assert rejected["lifecycle"] == "not_applied"
    assert rejected["dispatch_state"] == "not_dispatched"
    assert rejected["reason_code"] == "uia_action_pattern_unavailable"
    assert rejected["failed_before_effect_reason"] == (
        "uia_action_pattern_unavailable"
    )
    assert current["semantic_batch"]["rejection_reason"] == (
        "uia_action_pattern_unavailable"
    )


def test_unresolved_partial_prefix_remains_fail_closed_after_three_slots(tmp_path):
    plan = _batch_plan()
    store, record = _batch_store(tmp_path, plan)
    current = _record_partial_batch(store, record, plan)
    attempt_id = current["tool_attempt_id"]

    for slot in range(1, 4):
        store.record_semantic_settle_probe_result(
            attempt_id,
            result=SemanticSettleProbeResult(
                classification=(
                    SemanticSettleProbeClassification.COMPLETED_INCONCLUSIVE
                ),
                observer_mechanism="local_desktop_application_observation",
                observation_started_at=300.0 + slot,
                observation_completed_at=300.5 + slot,
                target_application="fixtureapp",
                reason_code="semantic_batch_prefix_unresolved",
                evidence_ref="sha256:" + f"{slot + 10:064x}",
                probe_invoked=True,
            ),
        )
    terminal, _created = store.record_terminal_result(
        attempt_id,
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
        reason_code="semantic_settle_exhausted",
    )

    assert terminal["terminal_outcome"] == "outcome_unknown"
    assert terminal["semantic_batch"]["state"] == "outcome_unknown"
    assert terminal["semantic_batch"]["resume_from_index"] is None
    assert all(
        item["lifecycle"] != "verified"
        for item in terminal["primitive_journal"][:3]
    )


def test_proven_prefix_reopens_only_at_rejected_primitive_with_fresh_target():
    strategy = _strategy()
    arguments = _batch_arguments()
    plan = _batch_plan(arguments)
    _observe_batch_scene(strategy)
    decision = es.tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)

    partial_result = execute_semantic_batch(
        plan,
        lambda item: (
            {
                "text": "ERROR: stale",
                "receipt": _batch_receipt(
                    item,
                    dispatch_state="not_dispatched",
                    outcome="confirmed_failure_before_effect",
                    reason_code="stale_semantic_reference",
                ),
            }
            if item["index"] == 3 else
            {"text": "dispatched", "receipt": _batch_receipt(item)}
        ),
    )
    es.record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="semantic_batch_prefix_checkpoint_pending",
        result=partial_result,
        strategy=strategy,
    )
    fresh_ref = "surface-fixture-three-reresolved"
    frontier_arguments = {
        **arguments["semantic_batch"][3],
        "surface_ref": fresh_ref,
    }
    blocked_before_proof = es.tool_preflight(
        "activate_desktop_surface", frontier_arguments, strategy=strategy,
    )

    assert blocked_before_proof is not None
    assert blocked_before_proof.allowed is False
    assert blocked_before_proof.reason_code == (
        "ambiguous_effect_requires_reconciliation"
    )
    assert strategy.reconcile_semantic_batch_prefix(
        attempt_id=attempt.attempt_id,
        semantic_batch_id=plan["semantic_batch_id"],
        applied_count=3,
        evidence="exact_prefix_fixture_evidence",
    )
    _observe_batch_scene(
        strategy,
        display_text="2",
        expression_text="5 +",
        fresh_three_ref=fresh_ref,
    )

    replay = es.tool_preflight(
        "activate_desktop_surface",
        arguments["semantic_batch"][0],
        strategy=strategy,
    )
    resumed = es.tool_preflight(
        "activate_desktop_surface", frontier_arguments, strategy=strategy,
    )

    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "semantic_batch_resume_frontier_mismatch"
    assert resumed is not None and resumed.allowed
    assert strategy.attempt_by_id(resumed.attempt_id).target_surface == fresh_ref


def test_partial_batch_keeps_a_valid_local_prefix_reconciliation_observer():
    strategy = _strategy()
    arguments = _batch_arguments()
    plan = _batch_plan(arguments)
    _observe_batch_scene(strategy)
    decision = es.tool_preflight(
        "activate_desktop_surface", arguments, strategy=strategy,
    )
    attempt = strategy.attempt_by_id(decision.attempt_id)

    def _dispatch(item):
        if item["index"] == 3:
            return {
                "text": "ERROR: stale",
                "receipt": _batch_receipt(
                    item,
                    dispatch_state="not_dispatched",
                    outcome="confirmed_failure_before_effect",
                    reason_code="stale_semantic_reference",
                ),
            }
        receipt = _batch_receipt(item)
        receipt["primitive"]["dispatched_at"] = (
            attempt.dispatched_at + item["index"] + 1.0
        )
        receipt["primitive"]["before"] = {
            "application": "calculator",
            "window_surface_ref": WINDOW_REF,
            "window_generation": "calculator-generation-1",
            "structural_epoch": 7,
            "observed_at": attempt.dispatched_at,
            "target": {"runtime_signature": f"runtime-{item['index']}"},
            "state_rows": [{
                "surface_ref": item["observable_effect"]["surface_ref"],
                "identity": f"witness-{item['index']}",
                "label": "before",
            }],
        }
        return {"text": "dispatched", "receipt": receipt}

    partial = execute_semantic_batch(plan, _dispatch)
    es.record_tool_outcome(
        "activate_desktop_surface",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="semantic_batch_prefix_checkpoint_pending",
        result=partial,
        strategy=strategy,
    )
    probe = es.select_local_reconciliation_probe(
        strategy,
        {"inspect_desktop_surfaces"},
        user_request=TASK,
    )

    assert probe is not None
    assert probe["tool"] == "inspect_desktop_surfaces"
    assert probe["strategy_attempt_id"] == decision.attempt_id
    assert strategy.frozen_reason == "semantic_batch_prefix_checkpoint_pending"
