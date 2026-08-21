"""WOR-262 physical replan/frontier drift regressions."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from osia.autonomous import state
from osia.autonomous.orchestrator import Orchestrator
from osia.chatgpt_account.app_server_client import _canonical_native_tools
from osia.execution_strategy import (
    StrategyLayer,
    StrategyMechanism,
    build_execution_strategy,
    export_execution_memory,
    execution_strategy_scope,
    rebind_execution_memory,
    record_tool_outcome,
    tool_applicability,
    tool_preflight,
)


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."
OPEN_CRITERION = "A Calculator window is visibly open."
CALC_CRITERION = "Calculator visibly displays the exact result 10063."
TOOLS = {
    "activate_desktop_surface",
    "desktop_click",
    "desktop_press",
    "desktop_type",
    "inspect_desktop_surfaces",
    "open_application",
    "query_document",
    "request_visual_observation",
    "run_command",
    "take_screenshot",
}


def _desktop_strategy(
    objective: str = "Enter 347 × 29 and evaluate it.",
    criterion: str = CALC_CRITERION,
):
    return build_execution_strategy(
        objective,
        TOOLS,
        postcondition=criterion,
        semantic_context=TASK,
    )


def _observation(*, observed_at: float, foreground: bool = True, rich: bool = True):
    return {
        "application": "calculatorapp",
        "requested_application": "calculator",
        "window_surface_ref": "surface-calculator-window",
        "window_generation": "calculator-generation-4",
        "accessibility_quality": (
            "rich_target_resolved" if rich else "poor_target_resolved"
        ),
        "target_resolution": "exact_target",
        "target_foreground": foreground,
        "target_identity_evidence": "uia_content_application:calculatorapp",
        "foreground_application": "calculatorapp" if foreground else "roblox",
        "observed_at": observed_at,
    }


def _open_unknown_memory(
    *, observed_at: float, foreground: bool = True, rich: bool = True,
):
    legacy = build_execution_strategy(
        "Open Calculator.",
        {"run_command"},
        postcondition=OPEN_CRITERION,
        semantic_context=TASK,
    )
    decision = tool_preflight(
        "run_command",
        {"command": "Start-Process Calculator"},
        strategy=legacy,
    )
    assert decision is not None and decision.allowed
    record_tool_outcome(
        "run_command",
        decision,
        outcome="outcome_unknown",
        is_error=True,
        reason_code="provider_failed_after_start",
        result={"dispatch_started": True},
        strategy=legacy,
    )
    legacy._latest_desktop_observation = _observation(
        observed_at=observed_at,
        foreground=foreground,
        rich=rich,
    )
    legacy._evidence_revision = 4
    return export_execution_memory(legacy)


def _calculation_unknown_memory(*, observed_at: float):
    legacy = _desktop_strategy()
    calculation = legacy.goal
    direct = StrategyMechanism(
        "direct:run_command",
        StrategyLayer.DIRECT_CAPABILITY,
        "provider-native calculation",
        application="calculator",
        covers_goal=False,
        mutation=True,
        node_id=calculation.node_id,
    )
    decision = legacy.select(
        direct, {"command": "provider-native calculate 347 x 29"},
    )
    assert decision.allowed
    legacy.record(
        decision,
        "ambiguous",
        failure_reason="provider_failed_after_start",
    )
    legacy._latest_desktop_observation = _observation(
        observed_at=observed_at,
    )
    legacy._evidence_revision = 4
    return export_execution_memory(legacy)


def _rebound_open(memory: dict, *, now: float):
    rebuilt = build_execution_strategy(
        "Open Calculator.",
        TOOLS,
        postcondition="Calculator is visibly available.",
        semantic_context=TASK,
    )
    return rebind_execution_memory(rebuilt, [memory], now=now)


def _frontier(strategy):
    arguments = {
        "query_document": {},
        "take_screenshot": {},
        "run_command": {},
        "inspect_desktop_surfaces": {"target": "Calculator"},
        "activate_desktop_surface": {
            "surface_ref": "surface-calculator-window",
            "action_pattern": "Window",
        },
    }
    return {
        name: tool_applicability(strategy, name, values)
        for name, values in arguments.items()
    }


def _phase(objective: str, criterion: str):
    planned = state.new_task_state(
        TASK,
        "chatgpt_account",
        "6-terra",
        [{
            "name": "Replanned Calculator work",
            "tasks": [{
                "objective": objective,
                "success_criterion": criterion,
            }],
        }],
        language="en",
    )
    return planned["phases"]


class _Agent:
    provider = "chatgpt_account"
    model = "6-terra"

    def reset(self):
        return None

    def run(self, _message, **_kwargs):
        return ""


def _physical_state(memory: dict):
    st = state.new_task_state(
        TASK,
        "chatgpt_account",
        "6-terra",
        [{
            "name": "Initial Calculator plan",
            "tasks": [{
                "objective": "Open Calculator.",
                "success_criterion": OPEN_CRITERION,
            }],
        }],
        language="en",
    )
    task = st["phases"][0]["tasks"][0]
    task["last_error"] = "provider_failed_after_start"
    task["execution_memory"] = memory
    return st, task


def _prepare_replan(st, **_kwargs):
    st["plan_revision"] = int(st.get("plan_revision", 0)) + 1
    st["run_state"] = "ready"
    return st


def test_01_sanitized_physical_fixture_reproduces_legacy_frontier_drift():
    legacy = build_execution_strategy(
        "Enter 347 × 29 and evaluate it.",
        TOOLS,
    )
    query_allowed, _reason = tool_applicability(
        legacy, "query_document", {},
    )
    assert legacy.goal.required_application == ""
    assert query_allowed is True


def test_02_fixed_physical_fixture_excludes_query_document():
    allowed, reason = tool_applicability(
        _desktop_strategy(), "query_document", {},
    )
    assert allowed is False
    assert reason == "tool_capability_not_applicable_to_semantic_node"


def test_03_fixed_physical_fixture_excludes_vision_for_rich_target():
    strategy = _desktop_strategy()
    strategy._latest_desktop_observation = _observation(observed_at=time.time())
    allowed, reason = tool_applicability(strategy, "take_screenshot", {})
    assert allowed is False
    assert reason == "structured_uia_evidence_sufficient"


def test_04_fixed_physical_fixture_excludes_generic_run_command():
    decision = tool_preflight(
        "run_command",
        {"command": "Add-Type; send arbitrary keys"},
        strategy=_desktop_strategy(),
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "generic_command_not_applicable_to_desktop_node"


def test_05_fixed_physical_fixture_retains_calculator_target():
    strategy = _desktop_strategy()
    assert strategy.goal.required_application == "calculator"
    memory = export_execution_memory(strategy)
    assert memory["effects"][0]["target_application"] == "calculator"
    assert memory["effects"][0]["semantic_domain"] == "desktop"


def test_06_replan_rebind_keeps_desktop_semantic_domain():
    now = time.time()
    memory = _open_unknown_memory(observed_at=now)
    rebound = _rebound_open(memory, now=now + 1)
    assert export_execution_memory(rebound)["effects"][0]["semantic_domain"] == "desktop"


def test_07_replan_rebind_keeps_calculator_application():
    now = time.time()
    rebound = _rebound_open(
        _open_unknown_memory(observed_at=now), now=now + 1,
    )
    assert rebound.goal.required_application == "calculator"


def test_08_replan_rebind_preserves_retired_run_command():
    now = time.time()
    rebound = _rebound_open(
        _open_unknown_memory(observed_at=now), now=now + 1,
    )
    assert rebound._scope_retirement_reason(
        "direct:run_command", rebound.goal.node_id,
    ) == "outcome_unknown"


def test_09_replan_rebind_preserves_unknown_effect():
    now = time.time()
    rebound = _rebound_open(
        _open_unknown_memory(observed_at=now), now=now + 1,
    )
    assert rebound.frozen_reason == "provider_failed_after_start"
    assert rebound._frozen_node_id == rebound.goal.node_id


def test_10_replan_rebind_preserves_completed_postcondition_evidence():
    original = build_execution_strategy(
        "Open Calculator.", TOOLS,
        postcondition=OPEN_CRITERION,
        semantic_context=TASK,
    )
    assert original.verify_postcondition(
        "exact independent Calculator window evidence",
        postcondition=original.goal.observable_success_postcondition,
    )
    rebuilt = build_execution_strategy(
        "Open Calculator.", TOOLS,
        postcondition="The Calculator window is exactly observable.",
        semantic_context=TASK,
    )
    rebind_execution_memory(rebuilt, [export_execution_memory(original)])
    assert rebuilt.succeeded is True
    assert rebuilt.goal.observable_success_postcondition in rebuilt.proven_postconditions


def test_11_stale_surface_ref_is_cleared_but_target_identity_survives():
    memory = _open_unknown_memory(observed_at=100.0)
    rebound = _rebound_open(memory, now=200.0)
    observed = rebound._latest_desktop_observation
    assert observed["requested_application"] == "calculator"
    assert observed["window_surface_ref"] == ""
    assert observed["window_generation"] == ""
    assert observed["target_resolution"] == "stale_target_requires_refresh"


def test_12_fresh_rich_uia_evidence_survives_replan():
    now = time.time()
    rebound = _rebound_open(
        _open_unknown_memory(observed_at=now), now=now + 1,
    )
    assert rebound._latest_desktop_observation["window_surface_ref"]
    assert rebound._latest_desktop_observation["accessibility_quality"] == (
        "rich_target_resolved"
    )


def test_13_desktop_calculation_does_not_expose_document_query():
    assert tool_applicability(
        _desktop_strategy(), "query_document", {},
    )[0] is False


def test_14_document_mission_keeps_document_query_available():
    strategy = build_execution_strategy(
        "Query the provided PDF document for the invoice total.",
        {"query_document"},
    )
    assert tool_applicability(strategy, "query_document", {}) == (True, "")


def test_15_generic_run_command_without_semantic_match_fails_closed():
    strategy = build_execution_strategy(
        "Open Calculator.", TOOLS,
        postcondition=OPEN_CRITERION,
        semantic_context=TASK,
    )
    before = len(strategy.attempts)
    decision = tool_preflight(
        "run_command", {"command": "whoami"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "generic_command_not_applicable_to_desktop_node"
    assert len(strategy.attempts) == before


def test_16_exact_shell_goal_still_allows_run_command():
    strategy = build_execution_strategy(
        "Execute the shell command Get-Date.", {"run_command"},
    )
    decision = tool_preflight(
        "run_command", {"command": "Get-Date"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed is True


def test_17_rich_calculator_target_blocks_screenshot():
    strategy = _desktop_strategy()
    strategy._latest_desktop_observation = _observation(observed_at=time.time())
    assert tool_applicability(strategy, "take_screenshot", {}) == (
        False, "structured_uia_evidence_sufficient",
    )


def test_18_unrelated_poor_foreground_does_not_enable_target_vision():
    strategy = _desktop_strategy()
    observed = _observation(observed_at=time.time())
    observed["foreground_application"] = "roblox"
    strategy._latest_desktop_observation = observed
    assert tool_applicability(strategy, "take_screenshot", {}) == (
        False, "structured_uia_evidence_sufficient",
    )


def test_19_exact_poor_target_keeps_targeted_vision_available():
    strategy = _desktop_strategy()
    strategy._latest_desktop_observation = _observation(
        observed_at=time.time(), rich=False,
    )
    assert tool_applicability(
        strategy, "request_visual_observation", {},
    ) == (False, "keyboard_frontier_not_exhausted")


def test_20_provider_dynamic_frontier_is_node_scoped():
    desktop = _desktop_strategy()
    document = build_execution_strategy(
        "Query the provided PDF document for the invoice total.",
        {"query_document"},
    )
    available = [SimpleNamespace(
        identity="query_document", available=True, source="osia",
    )]
    with patch(
        "osia.capability_federation.federated_capabilities",
        return_value=available,
    ):
        desktop_names = {item["name"] for item in _canonical_native_tools(desktop)}
        document_names = {item["name"] for item in _canonical_native_tools(document)}
    assert "query_document" not in desktop_names
    assert "query_document" in document_names


def test_21_provider_ineligible_tool_call_is_rejected_before_effect():
    strategy = _desktop_strategy()
    before = len(strategy.attempts)
    decision = tool_preflight(
        "query_document", {"query": "10063"}, strategy=strategy,
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason_code == "tool_capability_not_applicable_to_semantic_node"
    assert len(strategy.attempts) == before


def test_22_provider_rejection_does_not_consume_mutating_attempt():
    strategy = _desktop_strategy()
    before = len(strategy.attempts)
    decision = tool_preflight(
        "run_command",
        {"command": "generic desktop automation"},
        strategy=strategy,
    )
    assert decision is not None and decision.allowed is False
    assert len(strategy.attempts) == before


def test_23_real_autonomous_replan_reconstructs_filtered_physical_frontier():
    now = time.time()
    st, old_task = _physical_state(
        _open_unknown_memory(observed_at=now),
    )
    orchestrator = Orchestrator(_Agent())
    orchestrator._state = st
    orchestrator._active_task = old_task
    replanned = _phase(
        "Open Calculator.",
        "The Calculator target is visibly available.",
    )
    with (
        patch(
            "osia.autonomous.orchestrator.planner.plan_objective",
            return_value=replanned,
        ),
        patch(
            "osia.autonomous.orchestrator.authorization.prepare_replan",
            side_effect=_prepare_replan,
        ),
        patch.object(orchestrator, "_safe_save"),
    ):
        assert orchestrator._replan(st) is True

    phase_index = st["cursor"]["phase"]
    task = st["phases"][phase_index]["tasks"][0]
    strategy = orchestrator._strategy_for_task(st, task)
    frontier = _frontier(strategy)
    assert strategy.goal.required_application == "calculator"
    assert export_execution_memory(strategy)["effects"][0]["semantic_domain"] == "desktop"
    assert frontier["query_document"][0] is False
    assert frontier["take_screenshot"] == (
        False, "structured_uia_evidence_sufficient",
    )
    assert frontier["run_command"][0] is False
    assert frontier["inspect_desktop_surfaces"][0] is True
    assert frontier["activate_desktop_surface"] == (
        False, "ambiguous_effect_requires_reconciliation",
    )
    replay = tool_preflight(
        "run_command",
        {"command": "Start-Process Calculator"},
        strategy=strategy,
    )
    assert replay is not None and replay.allowed is False
    assert replay.reason_code == "ambiguous_effect_requires_reconciliation"
    assert strategy.succeeded is False


def test_24_background_calculator_target_persists_across_two_real_replans():
    now = time.time()
    st, old_task = _physical_state(_open_unknown_memory(
        observed_at=now,
        foreground=False,
        rich=True,
    ))
    orchestrator = Orchestrator(_Agent())
    orchestrator._state = st
    orchestrator._active_task = old_task
    replans = iter((
        _phase("Open Calculator.", "Calculator is available after replan one."),
        _phase("Open Calculator.", "Calculator is available after replan two."),
    ))

    def next_plan(*_args, **_kwargs):
        return next(replans)

    with (
        patch(
            "osia.autonomous.orchestrator.planner.plan_objective",
            side_effect=next_plan,
        ),
        patch(
            "osia.autonomous.orchestrator.authorization.prepare_replan",
            side_effect=_prepare_replan,
        ),
        patch.object(orchestrator, "_safe_save"),
    ):
        assert orchestrator._replan(st) is True
        first_index = st["cursor"]["phase"]
        first_task = st["phases"][first_index]["tasks"][0]
        first = orchestrator._strategy_for_task(st, first_task)
        orchestrator._active_task = first_task
        first_task["last_error"] = "target_focus_pending"
        assert orchestrator._replan(st) is True

    second_index = st["cursor"]["phase"]
    second_task = st["phases"][second_index]["tasks"][0]
    second = orchestrator._strategy_for_task(st, second_task)
    observed = second._latest_desktop_observation
    assert st["budgets"]["replan_count"] == 2
    assert second.goal.required_application == "calculator"
    assert observed["requested_application"] == "calculator"
    assert observed["foreground_application"] == "roblox"
    assert observed["accessibility_quality"] == "rich_target_resolved"
    assert observed["target_foreground"] is False
    assert tool_applicability(
        second,
        "inspect_desktop_surfaces",
        {"target": "Calculator"},
    )[0] is True
    assert tool_applicability(
        second,
        "activate_desktop_surface",
        {
            "surface_ref": observed["window_surface_ref"],
            "action_pattern": "Window",
        },
    ) == (False, "ambiguous_effect_requires_reconciliation")
    assert tool_applicability(second, "take_screenshot", {}) == (
        False, "target_focus_verification_required_before_vision",
    )
    assert second.frozen_reason == "provider_failed_after_start"
    assert second.succeeded is False


def test_25_physical_calculation_unknown_rebinds_to_exact_result_verifier():
    now = time.time()
    st = state.new_task_state(
        TASK,
        "chatgpt_account",
        "6-terra",
        [{
            "name": "Physical failed calculation",
            "tasks": [{
                "objective": "Enter 347 × 29 and evaluate it.",
                "success_criterion": CALC_CRITERION,
            }],
        }],
        language="en",
    )
    old_task = st["phases"][0]["tasks"][0]
    old_task["last_error"] = "provider_failed_after_start"
    old_task["execution_memory"] = _calculation_unknown_memory(
        observed_at=now,
    )
    orchestrator = Orchestrator(_Agent())
    orchestrator._state = st
    orchestrator._active_task = old_task
    replanned = _phase(
        "Verify the displayed result.",
        "The exact displayed result is 10063.",
    )
    with (
        patch(
            "osia.autonomous.orchestrator.planner.plan_objective",
            return_value=replanned,
        ),
        patch(
            "osia.autonomous.orchestrator.authorization.prepare_replan",
            side_effect=_prepare_replan,
        ),
        patch.object(orchestrator, "_safe_save"),
    ):
        assert orchestrator._replan(st) is True

    phase_index = st["cursor"]["phase"]
    task = st["phases"][phase_index]["tasks"][0]
    strategy = orchestrator._strategy_for_task(st, task)
    frontier = _frontier(strategy)
    assert strategy.goal.required_application == "calculator"
    assert strategy.frozen_reason == "provider_failed_after_start"
    assert strategy._scope_retirement_reason(
        "direct:run_command", strategy.goal.node_id,
    ) == "outcome_unknown"
    assert frontier["query_document"][0] is False
    assert frontier["take_screenshot"] == (
        False, "structured_uia_evidence_sufficient",
    )
    assert frontier["run_command"][0] is False
    assert frontier["inspect_desktop_surfaces"][0] is True
    assert strategy.succeeded is False


def test_26_execution_memory_checkpoints_only_opaque_evidence_tokens():
    strategy = build_execution_strategy(
        "Open Calculator.", TOOLS,
        postcondition=OPEN_CRITERION,
        semantic_context=TASK,
    )
    private_evidence = r"C:\private\desktop\calculator-proof.png"
    assert strategy.verify_postcondition(
        private_evidence,
        postcondition=strategy.goal.observable_success_postcondition,
    )
    serialized = json.dumps(export_execution_memory(strategy), sort_keys=True)
    assert private_evidence not in serialized
    assert "evidence_sha256:" in serialized


def test_27_regular_provider_tool_catalogue_uses_same_node_frontier():
    from osia.tool_contracts import effective_static_tool_specs

    desktop = _desktop_strategy()
    with execution_strategy_scope(TASK, strategy=desktop):
        desktop_names = {spec.name for spec in effective_static_tool_specs()}
    document = build_execution_strategy(
        "Query the provided PDF document for the invoice total.",
        {"query_document"},
    )
    with execution_strategy_scope(
        document.mission.objective, strategy=document,
    ):
        document_names = {spec.name for spec in effective_static_tool_specs()}

    assert "query_document" not in desktop_names
    assert "take_screenshot" not in desktop_names
    assert "inspect_desktop_surfaces" in desktop_names
    assert "query_document" in document_names


def test_28_explicit_shell_goal_remains_in_regular_provider_catalogue():
    from osia.tool_contracts import effective_static_tool_specs

    strategy = build_execution_strategy(
        "Execute the shell command Get-Date.", {"run_command"},
    )
    with execution_strategy_scope(
        strategy.mission.objective, strategy=strategy,
    ):
        names = {spec.name for spec in effective_static_tool_specs()}
    assert "run_command" in names
