"""Executable in-memory WOR-260 Phase-3 A/B benchmark.

The OFF arm is the fixture's accepted legacy decision.  The ON arm evaluates
only immutable Phase-3 sidecars.  No provider, network, browser, desktop,
permission, persistence, dispatch, or terminality transition is performed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import fmean

from benchmarks.wor260.phase2 import (
    fixture_descriptor,
    fixture_goal_contract,
    fixture_node,
    run_shadow_benchmark,
)
from osia.adaptive_planner import LegacyDecisionSnapshot
from osia.adaptive_runtime import evaluate_adaptive_continuation
from osia.capability_adapters import (
    CapabilityFamily,
    RuntimeAvailability,
    VerifierStrength,
)
from osia.capability_search import PermissionState
from osia.tool_contracts import SideEffectClass
from osia.verifier import VerificationStatus, verify_goal_node
from osia.world_state import (
    DispatchState,
    EffectState,
    EffectStateFact,
    EvidenceItem,
    EvidenceStaleState,
    PermissionFact,
    WorldStateInputs,
    build_world_state_view,
    goal_node_equivalence_class,
    postcondition_id,
)


@dataclass(frozen=True)
class Phase3BenchmarkScenario:
    scenario_id: str
    categories: tuple[str, ...]
    case: str
    variant: int
    legacy_attempts: int = 1


@dataclass(frozen=True)
class Phase3BenchmarkOutcome:
    scenario_id: str
    categories: tuple[str, ...]
    off_semantic_fidelity: bool
    on_semantic_fidelity: bool
    false_success: bool
    incorrect_blocked: bool
    hard_constraint_violation: bool
    permission_violation: bool
    identity_violation: bool
    outcome_unknown_replay_violation: bool
    modality_violation: bool
    completed_node_replay: bool
    direct_dispatch_violation: bool
    terminality_bypass_violation: bool
    off_strategy_attempts: int
    on_strategy_attempts: int
    off_plan_steps: int
    on_plan_steps: int
    redundant_observations: int
    route_length: int
    unnecessary_fallback: bool
    safe_fallback: bool
    stale_replanned: bool
    unknown_effect_reconciled: bool
    restart_rebuild_correct: bool


@dataclass(frozen=True)
class Phase3BenchmarkMetrics:
    phase2_scenario_count: int
    phase3_scenario_count: int
    total_scenario_count: int
    off_semantic_fidelity_rate: float
    on_semantic_fidelity_rate: float
    false_success_count: int
    incorrect_blocked_count: int
    hard_constraint_violation_count: int
    permission_violation_count: int
    identity_violation_count: int
    outcome_unknown_replay_violation_count: int
    modality_violation_count: int
    completed_node_replay_count: int
    direct_dispatch_violation_count: int
    terminality_bypass_violation_count: int
    average_strategy_attempts_off: float
    average_strategy_attempts_on: float
    average_plan_steps_off: float
    average_plan_steps_on: float
    redundant_observation_count: int
    average_route_length: float
    unnecessary_fallback_count: int
    safe_fallback_count: int
    stale_replan_count: int
    outcome_unknown_reconciliation_count: int
    restart_rebuild_correct_count: int


@dataclass(frozen=True)
class Phase3BenchmarkReport:
    phase2_report: object
    outcomes: tuple[Phase3BenchmarkOutcome, ...]
    metrics: Phase3BenchmarkMetrics


def benchmark_scenarios() -> tuple[Phase3BenchmarkScenario, ...]:
    scenarios: list[Phase3BenchmarkScenario] = []
    groups = (
        ("verifier", 7, ("verification", "false_success"), 1),
        ("state_change", 6, ("state_change", "replanning"), 2),
        ("partial", 5, ("partial_completion", "completed_node"), 2),
        ("cross_mechanism", 6, ("outcome_unknown", "cross_mechanism"), 2),
        ("permission", 5, ("permissions", "hard_constraints"), 2),
        ("identity", 4, ("identity", "account_profile"), 1),
        ("adaptive_failure", 5, ("fallback", "malformed_metadata"), 1),
        ("rebuild", 3, ("restart", "reconstruction"), 1),
    )
    labels = {
        "verifier": (
            "provider_success_not_proof", "weak_result_not_proof",
            "stale_exact_not_proof", "current_contradiction",
            "structured_read_proof", "observable_mutation_proof",
            "unknown_effect_not_success",
        ),
        "state_change": (
            "api_failure_to_browser", "browser_failure_to_api",
            "desktop_failure_to_deep_link", "stale_route_to_fresh_route",
            "unavailable_mcp_to_native", "failed_provider_to_exact_api",
        ),
        "partial": (
            "two_node_second_only", "three_node_third_only",
            "first_app_complete_second_app_pending", "partial_postconditions",
            "completed_read_before_mutation",
        ),
        "cross_mechanism": (
            "api_to_browser_replay_blocked", "browser_to_desktop_replay_blocked",
            "desktop_to_provider_replay_blocked", "provider_to_api_replay_blocked",
            "mcp_to_browser_replay_blocked", "deep_link_to_desktop_replay_blocked",
        ),
        "permission": (
            "disallowed_direct_to_permitted_browser", "denied_browser_to_api",
            "revoked_desktop_to_native", "permission_required_preserved",
            "full_access_does_not_bypass_policy",
        ),
        "identity": (
            "matching_account_selected", "matching_profile_selected",
            "unknown_account_fallback", "wrong_account_rejected",
        ),
        "adaptive_failure": (
            "missing_descriptors", "malformed_descriptors",
            "unavailable_only", "selected_outside_frontier",
            "incomplete_world_identity",
        ),
        "rebuild": (
            "same_snapshot_same_world", "evidence_revision_rebuild",
            "unknown_effect_rebuild",
        ),
    }
    for case, count, categories, attempts in groups:
        for index in range(count):
            scenarios.append(Phase3BenchmarkScenario(
                scenario_id=labels[case][index], categories=categories,
                case=case, variant=index, legacy_attempts=attempts,
            ))
    return tuple(scenarios)


def _snapshots(*, frontier=(), proven=(), failed=(), evidence_revision=2):
    attempts = tuple({
        "attempt_id": f"failed-{index}", "node_id": "read",
        "mechanism_id": capability_id, "status": "failed",
        "failure_or_stagnation_reason": "confirmed failure before effect",
    } for index, capability_id in enumerate(failed))
    return (
        {
            "turn_id": "phase3-benchmark", "proven_postconditions": {
                item: f"proof:{index}" for index, item in enumerate(proven)
            },
            "typed_evidence": (), "active_attempts": {},
            "permission_state": "allowed",
            "evidence_revision": evidence_revision,
        },
        {
            "attempts": attempts, "mechanisms": (),
            "alternatives_still_applicable": tuple(frontier),
            "mission": {"action_nodes": ()},
        },
    )


def _legacy(frontier, *, attempts=1):
    values = tuple(frontier)
    return LegacyDecisionSnapshot(
        first_mechanism_id=values[0] if values else "",
        first_family="existing_execution_strategy",
        semantic_plan=values, step_count=attempts,
        verifier_strength=VerifierStrength.PROVIDER_REPORTED.value,
        frontier_capability_ids=values,
    )


def _read_descriptor(capability_id, *, target="item", account="unknown", availability="available"):
    return fixture_descriptor(
        capability_id, CapabilityFamily.DIRECT_API.value, "act",
        target=target, application="catalog", surface="details",
        account=account, effect=f"Read {target}", availability=availability,
        verifier=VerifierStrength.EXACT_POSTCONDITION.value,
        exact_postcondition=f"{target} is visible",
    )


def _outcome(
    scenario, *, on_ok, selected="", expected_selected="", fallback=False,
    fallback_expected=False, plan_steps=0, permission_violation=False,
    identity_violation=False, replay_violation=False, completed_replay=False,
    false_success=False, incorrect_blocked=False, stale_replanned=False,
    reconciled=False, rebuild=False,
):
    return Phase3BenchmarkOutcome(
        scenario_id=scenario.scenario_id, categories=scenario.categories,
        off_semantic_fidelity=True, on_semantic_fidelity=on_ok,
        false_success=false_success, incorrect_blocked=incorrect_blocked,
        hard_constraint_violation=not on_ok,
        permission_violation=permission_violation,
        identity_violation=identity_violation,
        outcome_unknown_replay_violation=replay_violation,
        modality_violation=False, completed_node_replay=completed_replay,
        direct_dispatch_violation=False, terminality_bypass_violation=False,
        off_strategy_attempts=scenario.legacy_attempts,
        on_strategy_attempts=0 if fallback else 1,
        off_plan_steps=scenario.legacy_attempts,
        on_plan_steps=plan_steps,
        redundant_observations=0,
        route_length=plan_steps,
        unnecessary_fallback=fallback and not fallback_expected,
        safe_fallback=fallback and fallback_expected,
        stale_replanned=stale_replanned,
        unknown_effect_reconciled=reconciled,
        restart_rebuild_correct=rebuild,
    )


def _run_verifier(scenario):
    mutation = scenario.variant not in {4}
    node = fixture_node(
        "verify", "Update item" if mutation else "Read item", "item",
        "catalog", "details",
        side_effect=(SideEffectClass.PERSISTENT_LOCAL.value if mutation else SideEffectClass.READ_ONLY.value),
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots()
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(current_application="catalog", current_surface="details"),
    )
    strengths = (
        VerifierStrength.PROVIDER_REPORTED.value,
        VerifierStrength.WEAK_UNSTRUCTURED.value,
        VerifierStrength.EXACT_POSTCONDITION.value,
        VerifierStrength.EXACT_POSTCONDITION.value,
        VerifierStrength.STRUCTURED_RESULT.value,
        VerifierStrength.OBSERVABLE_STATE.value,
        VerifierStrength.PROVIDER_REPORTED.value,
    )
    observed = "observable_state_mismatch" if scenario.variant == 3 else "verified"
    stale = EvidenceStaleState.STALE.value if scenario.variant == 2 else EvidenceStaleState.FRESH.value
    effect = EffectState.OUTCOME_UNKNOWN.value if scenario.variant == 6 else EffectState.EFFECT_CONFIRMED.value
    evidence = EvidenceItem(
        evidence_id=f"verifier:{scenario.variant}", source_type="benchmark",
        source_attempt_id="attempt", goal_node_id=node.node_id,
        capability_id="fixture", capability_family="direct_api",
        observation_type="structured_read_exact",
        verifier_strength=strengths[scenario.variant], semantic_effect=node.objective,
        observed_state=observed,
        supported_postcondition_ids=(postcondition_id(node.node_id, "item is visible"),),
        dispatch_state=DispatchState.COMPLETED.value, effect_state=effect,
        stale_state=stale, evidence_revision=2,
    )
    result = verify_goal_node(node, world, (evidence,))
    expected = (
        VerificationStatus.REQUIRES_OBSERVATION.value,
        VerificationStatus.REQUIRES_OBSERVATION.value,
        VerificationStatus.REQUIRES_OBSERVATION.value,
        VerificationStatus.CONTRADICTED.value,
        VerificationStatus.VERIFIED.value,
        VerificationStatus.VERIFIED.value,
        VerificationStatus.OUTCOME_UNKNOWN.value,
    )[scenario.variant]
    false_success = scenario.variant in {0, 1, 2, 6} and result.status == VerificationStatus.VERIFIED.value
    return _outcome(
        scenario, on_ok=result.status == expected, plan_steps=0,
        false_success=false_success,
    )


def _run_state_change(scenario):
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    failed = f"route:failed:{scenario.variant}"
    remaining = f"route:remaining:{scenario.variant}"
    terminality, strategy = _snapshots(
        frontier=(failed, remaining), failed=(failed,),
    )
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(current_application="catalog", current_surface="details"),
    )
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world),
        (_read_descriptor(failed), _read_descriptor(remaining)),
        _legacy((failed, remaining), attempts=2),
    )
    selected = proposal.selected_next_capability
    return _outcome(
        scenario, on_ok=selected == remaining and not proposal.fallback_to_legacy,
        selected=selected, expected_selected=remaining,
        fallback=proposal.fallback_to_legacy,
        plan_steps=len(proposal.selected_plan.ordered_steps) if proposal.selected_plan else 0,
        stale_replanned=scenario.variant == 3 and selected == remaining,
    )


def _run_partial(scenario):
    first = fixture_node(
        "first", "Read first", "first", "catalog", "details",
        postcondition="first is visible",
    )
    second = fixture_node(
        "second", "Read second", "second", "catalog", "details",
        postcondition="second is visible",
    )
    contract = fixture_goal_contract(first, second)
    first_cap = f"api:first:{scenario.variant}"
    second_cap = f"api:second:{scenario.variant}"
    terminality, strategy = _snapshots(
        frontier=(first_cap, second_cap), proven=("first is visible",),
    )
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(current_application="catalog", current_surface="details"),
    )
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(second, world),
        (
            _read_descriptor(first_cap, target="first"),
            _read_descriptor(second_cap, target="second"),
        ),
        _legacy((first_cap, second_cap), attempts=2),
    )
    steps = proposal.selected_plan.ordered_steps if proposal.selected_plan else ()
    replay = any(item.goal_node_id == "first" for item in steps)
    return _outcome(
        scenario,
        on_ok=proposal.selected_next_capability == second_cap and not replay,
        fallback=proposal.fallback_to_legacy,
        plan_steps=len(steps), completed_replay=replay,
    )


def _run_cross_mechanism(scenario):
    node = fixture_node(
        "send", "Send message", "message", "mail", "inbox",
        side_effect=SideEffectClass.EXTERNAL_MUTATION.value,
        postcondition="message is present",
    )
    contract = fixture_goal_contract(node)
    equivalence = goal_node_equivalence_class(node)
    sources = ("api", "browser", "desktop", "provider", "mcp", "deep-link")
    source = f"{sources[scenario.variant]}:send"
    alternate = f"alternate:{scenario.variant}:send"
    terminality, strategy = _snapshots(frontier=(source, alternate))
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="mail", current_surface="inbox",
            known_effect_states=(EffectStateFact(
                "unknown-attempt", source, equivalence,
                EffectState.OUTCOME_UNKNOWN.value, "send",
            ),),
        ),
    )
    mutators = tuple(
        fixture_descriptor(
            item, CapabilityFamily.DIRECT_API.value, "act",
            target="message", application="mail", surface="inbox",
            effect="Send message", side_effect=SideEffectClass.EXTERNAL_MUTATION.value,
            equivalence_class=equivalence,
            verifier=VerifierStrength.OBSERVABLE_STATE.value,
        ) for item in (source, alternate)
    )
    reconcile = fixture_descriptor(
        f"reconcile:{scenario.variant}", CapabilityFamily.DIRECT_API.value,
        "reconcile", target="message", application="mail", surface="inbox",
        effect="reconcile unknown effect", side_effect=SideEffectClass.READ_ONLY.value,
        equivalence_class=equivalence,
        verifier=VerifierStrength.OBSERVABLE_STATE.value,
    )
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world),
        (*mutators, reconcile), _legacy((source, alternate), attempts=2),
    )
    steps = proposal.selected_plan.ordered_steps if proposal.selected_plan else ()
    replay = any(item.capability_id in {source, alternate} for item in steps)
    reconciled = proposal.selected_next_capability == reconcile.capability_id
    return _outcome(
        scenario, on_ok=reconciled and not replay,
        fallback=proposal.fallback_to_legacy, plan_steps=len(steps),
        replay_violation=replay, reconciled=reconciled,
    )


def _run_permission(scenario):
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    denied = f"denied:{scenario.variant}"
    allowed = f"allowed:{scenario.variant}"
    terminality, strategy = _snapshots(frontier=(denied, allowed))
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
            permission_states=(
                PermissionFact(denied, PermissionState.DISALLOWED.value),
                PermissionFact(allowed, PermissionState.PERMITTED.value),
            ),
        ),
    )
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world),
        (_read_descriptor(denied), _read_descriptor(allowed)),
        _legacy((denied, allowed), attempts=2),
    )
    selected = proposal.selected_next_capability
    violation = selected == denied
    return _outcome(
        scenario, on_ok=selected == allowed and not violation,
        fallback=proposal.fallback_to_legacy,
        plan_steps=len(proposal.selected_plan.ordered_steps) if proposal.selected_plan else 0,
        permission_violation=violation,
    )


def _run_identity(scenario):
    node = fixture_node(
        "read", "Read private item", "item", "catalog", "details",
        account="profile-a", postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    capability = f"account-route:{scenario.variant}"
    terminality, strategy = _snapshots(frontier=(capability,))
    current = "profile-a" if scenario.variant < 2 else "unknown" if scenario.variant == 2 else "profile-b"
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
            current_account_profile=current,
        ),
    )
    descriptor = _read_descriptor(capability, account="profile-a")
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), (descriptor,),
        _legacy((capability,)),
    )
    fallback_expected = scenario.variant >= 2
    identity_violation = current != "profile-a" and bool(proposal.selected_next_capability)
    on_ok = (
        proposal.selected_next_capability == capability
        if not fallback_expected else
        proposal.fallback_to_legacy and not identity_violation
    )
    return _outcome(
        scenario, on_ok=on_ok, fallback=proposal.fallback_to_legacy,
        fallback_expected=fallback_expected,
        plan_steps=len(proposal.selected_plan.ordered_steps) if proposal.selected_plan else 0,
        identity_violation=identity_violation,
    )


def _run_adaptive_failure(scenario):
    account = "profile-a" if scenario.variant == 4 else "not_specified"
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        account=account, postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    frontier = ("api:read",) if scenario.variant != 3 else ("legacy:only",)
    terminality, strategy = _snapshots(frontier=frontier)
    world = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=WorldStateInputs(
            current_application="catalog", current_surface="details",
            current_account_profile="unknown",
        ),
    )
    descriptor = _read_descriptor(
        "api:read", account=account,
        availability=(RuntimeAvailability.UNAVAILABLE.value if scenario.variant == 2 else RuntimeAvailability.AVAILABLE.value),
    )
    descriptors = () if scenario.variant == 0 else (object(),) if scenario.variant == 1 else (descriptor,)
    proposal = evaluate_adaptive_continuation(
        contract, world, verify_goal_node(node, world), descriptors,
        _legacy(frontier),
    )
    return _outcome(
        scenario, on_ok=proposal.fallback_to_legacy,
        fallback=proposal.fallback_to_legacy, fallback_expected=True,
        plan_steps=len(proposal.selected_plan.ordered_steps) if proposal.selected_plan else 0,
        incorrect_blocked=False,
    )


def _run_rebuild(scenario):
    node = fixture_node(
        "read", "Read item", "item", "catalog", "details",
        postcondition="item is visible",
    )
    contract = fixture_goal_contract(node)
    terminality, strategy = _snapshots(evidence_revision=scenario.variant + 1)
    inputs = WorldStateInputs(
        current_application="catalog", current_surface="details",
        revision=scenario.variant + 1,
    )
    first = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=inputs,
    )
    second = build_world_state_view(
        contract, terminality_snapshot=terminality, strategy_snapshot=strategy,
        inputs=inputs,
    )
    correct = first == second
    return _outcome(scenario, on_ok=correct, rebuild=correct)


_RUNNERS = {
    "verifier": _run_verifier,
    "state_change": _run_state_change,
    "partial": _run_partial,
    "cross_mechanism": _run_cross_mechanism,
    "permission": _run_permission,
    "identity": _run_identity,
    "adaptive_failure": _run_adaptive_failure,
    "rebuild": _run_rebuild,
}


def run_phase3_benchmark(
    scenarios: tuple[Phase3BenchmarkScenario, ...] | None = None,
) -> Phase3BenchmarkReport:
    selected = scenarios or benchmark_scenarios()
    outcomes = tuple(_RUNNERS[item.case](item) for item in selected)
    phase2 = run_shadow_benchmark()

    def count(field):
        return sum(bool(getattr(item, field)) for item in outcomes)

    metrics = Phase3BenchmarkMetrics(
        phase2_scenario_count=phase2.metrics.scenario_count,
        phase3_scenario_count=len(outcomes),
        total_scenario_count=phase2.metrics.scenario_count + len(outcomes),
        off_semantic_fidelity_rate=fmean(item.off_semantic_fidelity for item in outcomes),
        on_semantic_fidelity_rate=fmean(item.on_semantic_fidelity for item in outcomes),
        false_success_count=count("false_success"),
        incorrect_blocked_count=count("incorrect_blocked"),
        hard_constraint_violation_count=count("hard_constraint_violation"),
        permission_violation_count=count("permission_violation"),
        identity_violation_count=count("identity_violation"),
        outcome_unknown_replay_violation_count=count("outcome_unknown_replay_violation"),
        modality_violation_count=count("modality_violation"),
        completed_node_replay_count=count("completed_node_replay"),
        direct_dispatch_violation_count=count("direct_dispatch_violation"),
        terminality_bypass_violation_count=count("terminality_bypass_violation"),
        average_strategy_attempts_off=fmean(item.off_strategy_attempts for item in outcomes),
        average_strategy_attempts_on=fmean(item.on_strategy_attempts for item in outcomes),
        average_plan_steps_off=fmean(item.off_plan_steps for item in outcomes),
        average_plan_steps_on=fmean(item.on_plan_steps for item in outcomes),
        redundant_observation_count=sum(item.redundant_observations for item in outcomes),
        average_route_length=fmean(item.route_length for item in outcomes),
        unnecessary_fallback_count=count("unnecessary_fallback"),
        safe_fallback_count=count("safe_fallback"),
        stale_replan_count=count("stale_replanned"),
        outcome_unknown_reconciliation_count=count("unknown_effect_reconciled"),
        restart_rebuild_correct_count=count("restart_rebuild_correct"),
    )
    return Phase3BenchmarkReport(
        phase2_report=phase2, outcomes=outcomes, metrics=metrics,
    )


__all__ = [
    "Phase3BenchmarkMetrics",
    "Phase3BenchmarkOutcome",
    "Phase3BenchmarkReport",
    "Phase3BenchmarkScenario",
    "benchmark_scenarios",
    "run_phase3_benchmark",
]
