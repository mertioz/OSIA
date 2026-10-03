"""WOR-264 Normal Chat Desktop continuation and terminality regressions.

WOR-301: normal chat runs with permanent unconditional Full Access; what this
file still validates is the same-turn desktop continuation, strategy
terminality and the fact that no permission permit exists to gate a final
commit.
"""

from __future__ import annotations

from types import SimpleNamespace

from osia.action_policy import guard_tool_call
from osia.agent import OSIAAgent
from osia import desktop_scene
from osia.desktop_surface import (
    DesktopSurfaceRegistry,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
)
from osia.execution_strategy import (
    AttemptStatus,
    StrategyAttempt,
    StrategyLayer,
    build_execution_strategy,
    execution_strategy_scope,
    record_tool_observation,
    record_tool_outcome,
    select_local_reconciliation_probe,
    tool_preflight,
)
from osia.permission_outcome import PermissionOutcome, PermissionResult
from osia.tool_contracts import (
    ConfirmationPolicy,
    ExecutionOutcome,
    SideEffectClass,
    ToolExecutionRecord,
    effective_contract,
    get_tool_spec,
)
from osia.turn_terminality import TurnTerminalityController
from tools.delivery_provenance import RuntimeRecord, assess_runtime


TASK = "ouvre calculatrice et calcule 5+23"
TOOLS = {
    "activate_desktop_surface",
    "desktop_click",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
}


def _unknown_localized_open():
    strategy = build_execution_strategy(TASK, TOOLS)
    decision = tool_preflight(
        "open_application",
        {"name": "Calculatrice"},
        strategy=strategy,
    )
    assert decision is not None and decision.allowed
    record_tool_outcome(
        "open_application",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="owner_still_running",
        result={"dispatch_started": True},
        strategy=strategy,
    )
    return strategy, decision


def _reconcile_localized_open(strategy, decision) -> None:
    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt is not None
    observed_at = attempt.dispatched_at + 1.0
    result = {
        "text": "exact Calculator application surface observed",
        "scene": {
            "application": "calculatorapp",
            "requested_application": "calculator",
            "window_surface_ref": "surface-calculator-window",
            "window_generation": "calculator-generation-1",
            "structural_epoch": 4,
            "target_resolution": "exact_target",
            "target_foreground": False,
            "foreground_application": "applicationframehost",
            "observed_at": observed_at,
        },
        "receipt": {
            "schema_version": 1,
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "dispatch_state": "effect_confirmed",
            "outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "semantic_postcondition": "application_open",
            "postcondition": "application_open",
            "observed_target": "calculator",
            "observed_application": "calculatorapp",
            "observed_at": observed_at,
        },
    }
    assert record_tool_observation(
        "inspect_desktop_surfaces",
        result,
        is_error=False,
        strategy=strategy,
    )


def test_01_normal_chat_localized_calculator_uses_canonical_windows_identity():
    strategy, decision = _unknown_localized_open()
    open_node, calculation_node = strategy.mission.action_nodes
    probe = select_local_reconciliation_probe(
        strategy,
        {"inspect_desktop_surfaces"},
        user_request=TASK,
    )

    assert open_node.required_application == "calculator"
    assert calculation_node.required_application == "calculator"
    assert calculation_node.requested_content == "5+23"
    assert decision.mechanism_id == "direct:open_application:calculator"
    assert probe is not None
    assert probe["arguments"] == {"target": "calculator"}


def test_02_full_access_normal_chat_continues_same_turn_after_open(
    tmp_path,
):
    strategy, decision = _unknown_localized_open()
    _reconcile_localized_open(strategy, decision)
    controller = TurnTerminalityController(
        turn_id="wor264-normal-chat-continuation",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    continuation = controller.evaluate()

    assert continuation.action == "continue"
    assert continuation.reason_code == "safe_semantic_strategy_remains"
    assert controller.continuation_context(continuation)
    assert "continue_same_user_turn" in controller.continuation_context(
        continuation
    )


def test_03_calculator_5_plus_23_reaches_verified_28(tmp_path):
    strategy, decision = _unknown_localized_open()
    _reconcile_localized_open(strategy, decision)
    calculation = strategy.mission.action_nodes[1]
    exact_result = {
        "scene": {
            "application": "calculatorapp",
            "requested_application": "calculator",
            "window_surface_ref": "surface-calculator-window",
            "target_resolution": "exact_target",
            "target_foreground": True,
            "foreground_application": "calculatorapp",
        },
        "receipt": {
            "outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "postcondition": calculation.observable_success_postcondition,
            "observed_target": "28",
        },
    }

    assert record_tool_observation(
        "inspect_desktop_surfaces",
        exact_result,
        is_error=False,
        strategy=strategy,
    )
    controller = TurnTerminalityController(
        turn_id="wor264-exact-result",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "terminality-result",
    )
    controller.set_strategy_snapshot(strategy.snapshot())

    assert calculation.observable_success_postcondition in (
        strategy.proven_postconditions
    )
    assert strategy.succeeded is True
    assert controller.evaluate().terminal_state == "success_verified"


def test_04_preparatory_open_does_not_complete_parent_calculation():
    strategy, decision = _unknown_localized_open()
    _reconcile_localized_open(strategy, decision)
    open_node, calculation_node = strategy.mission.action_nodes

    assert open_node.status == "succeeded"
    assert calculation_node.status == "pending"
    assert calculation_node.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
    assert strategy.succeeded is False


def test_05_uncorrelated_denial_token_cannot_fabricate_permission_refusal():
    text = OSIAAgent(model="wor264", lang="en")._terminal_final_text(
        "blocked_verified",
        "internal candidate",
        reason_code="safe_semantic_frontier_exhausted",
        snapshot={
            "permission_state": "internal_failure",
            "typed_evidence": [{
                "reason_code": "permission_user_denied",
                "evidence": {"correlation_id": ""},
            }],
        },
    )

    assert "declined" not in text
    assert "internal permission-system failure" in text


def test_06_correlated_user_denial_still_projects_as_refusal():
    denial = PermissionResult(
        PermissionOutcome.USER_DENIED,
        "permission-request-wor264",
    )
    text = OSIAAgent(model="wor264", lang="en")._terminal_final_text(
        "blocked_verified",
        "internal candidate",
        reason_code="safe_semantic_frontier_exhausted",
        snapshot={
            "permission_state": denial.outcome.value,
            "typed_evidence": [{
                "reason_code": "permission_user_denied",
                "evidence": {"correlation_id": denial.correlation_id},
            }],
        },
    )

    assert denial.correlation_id
    assert "permission was declined" in text


def test_07_final_send_is_allowed_without_a_permission_permit(
    tmp_path,
):
    arguments = {"key": "enter"}
    spec = get_tool_spec("desktop_press")
    contract = effective_contract(
        spec,
        arguments,
        "Envoie le message déjà préparé dans Signal.",
    )
    request_text = "Envoie le message déjà préparé dans Signal."

    assert contract.side_effect is SideEffectClass.IRREVERSIBLE_EXTERNAL
    assert contract.confirmation is ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION
    # WOR-301: a recognized final commit dispatches with no permit; only an
    # integrity guard (e.g. system destruction) may refuse it.
    assert guard_tool_call(request_text, "desktop_press", arguments) is None


def test_08_outcome_unknown_still_blocks_replay():
    strategy, decision = _unknown_localized_open()

    blocked = tool_preflight(
        "open_application",
        {"name": "Calculatrice"},
        strategy=strategy,
    )

    assert blocked is not None and blocked.allowed is False
    assert blocked.reason_code == "ambiguous_effect_requires_reconciliation"
    assert sum(
        attempt.attempt_id == decision.attempt_id
        and attempt.status == "ambiguous_effect"
        for attempt in strategy.attempts
    ) == 1


def test_09_chat_and_scheduled_share_strategy_and_terminality(
    tmp_path,
):
    # WOR-303: one canonical execution runtime.  The Scheduled temporal trigger
    # and Chat share the same strategy and terminality semantics.
    chat = build_execution_strategy(TASK, TOOLS)
    scheduled = build_execution_strategy(TASK, TOOLS)

    assert chat.mission.public_dict() == scheduled.mission.public_dict()
    assert [item.mechanism_id for item in chat._mechanisms.values()] == [
        item.mechanism_id for item in scheduled._mechanisms.values()
    ]

    controllers = []
    for name, strategy in (("chat", chat), ("scheduled", scheduled)):
        controller = TurnTerminalityController(
            turn_id="wor264-" + name,
            objective=TASK,
            required_postconditions=strategy.required_postconditions,
            execution_required=True,
            storage_dir=tmp_path / name,
        )
        controller.set_strategy_snapshot(strategy.snapshot())
        controllers.append(controller)

    assert type(controllers[0]) is type(controllers[1])
    assert controllers[0].evaluate() == controllers[1].evaluate()


def test_10_stale_runtime_provenance_remains_fail_closed(tmp_path):
    stale = RuntimeRecord(
        "desktop",
        264,
        str(tmp_path / "old-runtime" / "osia" / "main.py"),
        started_at=100.0,
    )
    checks = assess_runtime(
        [stale],
        pc_repo=tmp_path,
        head_updated_at=200.0,
    )

    source = next(check for check in checks if check.name.endswith(".source"))
    fresh = next(check for check in checks if check.name.endswith(".fresh"))
    assert source.ok is False
    assert fresh.ok is False


def test_11_stateful_uia_text_keeps_automation_identity_across_runtime_churn():
    before = desktop_scene._runtime_signature(
        (42, 100, 4, 9),
        "TextControl",
        "calculator-display",
        "CalculatorExpression",
    )
    after = desktop_scene._runtime_signature(
        (42, 200, 4, 9),
        "TextControl",
        "calculator-display",
        "CalculatorExpression",
    )
    unrelated = desktop_scene._runtime_signature(
        (42, 200, 4, 9),
        "TextControl",
        "calculator-display",
        "CalculatorResults",
    )

    assert before == after
    assert before != unrelated


def test_12_exact_final_calculation_primitive_completes_parent():
    from tests.test_wor262_desktop_calculator_stabilization import (
        _observe_stepwise_primitive,
        _start_stepwise_primitive,
    )

    strategy = build_execution_strategy(TASK, TOOLS)
    open_node, calculation = strategy.mission.action_nodes
    assert strategy.verify_postcondition(
        "exact Calculator open evidence",
        postcondition=open_node.observable_success_postcondition,
    )
    _start_stepwise_primitive(
        strategy,
        "Est \u00e9gal \u00e0",
        before_result="23",
        expected_result="28",
    )
    observed, reconciled = _observe_stepwise_primitive(
        strategy,
        "28",
        target="reconcile",
    )

    assert reconciled is True
    assert observed["receipt"]["reason_code"] == (
        "exact_desktop_observable_effect_change"
    )
    assert calculation.observable_success_postcondition.endswith(
        "the exact result 28"
    )
    assert calculation.observable_success_postcondition in (
        strategy.proven_postconditions
    )
    assert calculation.status == "succeeded"
    assert strategy.succeeded is True


def test_13_resumed_primitive_receipt_does_not_claim_parent_result():
    strategy = build_execution_strategy(TASK, TOOLS)
    calculation = strategy.mission.action_nodes[1]
    primitive = "exact surface text on display-ref is L’affichage est 5"
    strategy.attempts.append(StrategyAttempt(
        attempt_id="strategy-wor264-primitive",
        mechanism_id="native_accessibility:" + calculation.node_id,
        layer=int(StrategyLayer.NATIVE_ACCESSIBILITY),
        mechanism="native_accessibility",
        reason_selected="fresh native accessibility target",
        action_signature="wor264-five",
        status=AttemptStatus.PROGRESSED.value,
        node_id=calculation.node_id,
        attempted_effect=primitive,
        dispatched_at=1.0,
        composite_id=calculation.node_id,
        substep_id="substep-wor264-five",
        substep_index=1,
        primitive_postcondition=primitive,
    ))

    resumed = OSIAAgent._resumed_tool_postcondition(strategy)

    assert resumed == primitive
    assert resumed != calculation.observable_success_postcondition


def test_14_verified_calculation_rejects_unrelated_final_candidate():
    strategy = build_execution_strategy(TASK, TOOLS)
    for node in strategy.mission.action_nodes:
        for postcondition in node.required_postconditions:
            strategy.proven_postconditions[postcondition] = "exact-wor264-proof"
        node.status = "succeeded"
    strategy.succeeded = True
    snapshot = {
        "strategy_snapshot": strategy.snapshot(),
        "proven_postconditions": dict(strategy.proven_postconditions),
    }

    final_text = OSIAAgent(model="wor264", lang="en")._terminal_final_text(
        "success_verified",
        "There is no information to store.",
        snapshot=snapshot,
    )

    assert final_text == "Calculation complete: 5+23 = 28."


def test_15_read_only_observation_does_not_claim_unproven_parent(tmp_path):
    strategy = build_execution_strategy(TASK, TOOLS)
    controller = TurnTerminalityController(
        turn_id="wor264-observation-projection",
        objective=TASK,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "observation-projection",
    )
    arguments = {"target": "calculator"}
    contract = effective_contract(
        get_tool_spec("inspect_desktop_surfaces"),
        arguments,
        TASK,
    )
    record = ToolExecutionRecord(
        {"scene": {"application": "calculatorapp"}},
        False,
        ExecutionOutcome.SUCCESS_VERIFIED,
        1,
        True,
        False,
        contract,
        "attempt-wor264-read-only",
    )
    agent = SimpleNamespace(
        _terminality_controller=controller,
        _unfinished_execution=None,
        _emit_execution_trace=lambda *_args, **_kwargs: None,
    )

    with execution_strategy_scope(TASK, strategy=strategy):
        OSIAAgent._record_unfinished_execution(
            agent,
            "inspect_desktop_surfaces",
            arguments,
            record,
            None,
        )

    evidence = controller.snapshot()["typed_evidence"][-1]
    assert evidence["outcome"] == "success_verified"
    assert evidence["postcondition"] == ""
    assert OSIAAgent._resumed_tool_postcondition(strategy, record) == ""
    assert strategy.mission.action_nodes[1].observable_success_postcondition not in (
        controller.snapshot()["proven_postconditions"]
    )


def test_16_stable_uia_text_ref_survives_parent_ref_churn():
    registry = DesktopSurfaceRegistry()
    common = {
        "application_id": "calculatorapp",
        "process_ref": "calculator-process",
        "generation": "calculator-window-generation",
        "surface_kind": SurfaceKind.CONTROL,
        "role": "TextControl",
        "selected": False,
        "focused": False,
        "enabled": True,
        "bounds": SurfaceBounds(100, 100, 300, 140),
        "runtime_signature": "stable-calculator-expression",
        "structural_epoch": 0,
        "scene_signature": "calculator-scene",
        "stable_identity": "stable-calculator-expression",
        "patterns": (),
    }
    before = registry.register(
        **common,
        accessible_label="L’expression est",
        parent_ref="volatile-parent-before",
        native=NativeSurfaceIdentity(1, 1.0, 10, (1, 2, 3)),
    )
    after = registry.register(
        **common,
        accessible_label="L’expression est 5 +",
        parent_ref="volatile-parent-after",
        native=NativeSurfaceIdentity(1, 1.0, 10, (1, 9, 3)),
    )

    assert after.ref_id == before.ref_id
    assert after.accessible_label == "L’expression est 5 +"
    assert after.parent_ref == "volatile-parent-after"


def test_17_preparatory_digit_matching_result_cannot_complete_calculation():
    from tests.test_wor262_desktop_calculator_stabilization import (
        _observe_stepwise_primitive,
        _start_stepwise_primitive,
    )

    strategy = build_execution_strategy(
        "ouvre calculatrice et calcule 3+0",
        TOOLS,
    )
    open_node, calculation = strategy.mission.action_nodes
    assert strategy.verify_postcondition(
        "exact Calculator open evidence",
        postcondition=open_node.observable_success_postcondition,
    )
    _start_stepwise_primitive(
        strategy,
        "Trois",
        before_result="0",
        expected_result="3",
    )
    _observed, reconciled = _observe_stepwise_primitive(
        strategy,
        "3",
        target="reconcile",
    )

    assert reconciled is True
    assert calculation.observable_success_postcondition.endswith(
        "the exact result 3"
    )
    assert calculation.observable_success_postcondition not in (
        strategy.proven_postconditions
    )
    assert calculation.status == "pending"
    assert strategy.succeeded is False
