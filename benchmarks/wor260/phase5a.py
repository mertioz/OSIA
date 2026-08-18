"""Deterministic WOR-260 Phase-5A controlled-integration benchmark.

The benchmark never calls a provider.  It compares request dictionaries at the
existing provider boundary and carries forward the accepted Phase-1--4B corpus.
"""

from __future__ import annotations

import math
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from statistics import fmean
from time import perf_counter_ns
from typing import Any, Iterator, Mapping
from unittest.mock import patch

from benchmarks.wor260.phase4a import reasoning_scenarios
from benchmarks.wor260.phase4b import Phase4BBenchmarkReport, run_phase4b_benchmark
from osia.adaptive_reasoning import (
    DifficultyClass,
    capability_from_current_registry,
    determine_reasoning_decision,
    select_level_for_difficulty,
)
from osia.adaptive_reasoning_runtime import (
    ADAPTIVE_REASONING_BINDING_SCHEMA,
    ADAPTIVE_REASONING_GATE_ENV,
    BoundReasoningContext,
    build_bound_reasoning_context,
    integrate_adaptive_reasoning,
)
from osia.execution_strategy import build_execution_strategy
from osia.reasoning import build_reasoning_request


PHASE3_GATE_ENV = "OSIA_ADAPTIVE_INTELLIGENCE_PHASE3"
_ACCOUNT_MODEL = {
    "id": "gpt-5.6-sol",
    "supportedReasoningEfforts": [
        {"id": item}
        for item in ("low", "medium", "high", "xhigh", "max", "ultra")
    ],
    "defaultReasoningEffort": "low",
}


@dataclass(frozen=True)
class Phase5AIntegrationScenario:
    scenario_id: str
    group: str
    policy_scenario_id: str
    provider_id: str
    model_id: str
    planner_gate_value: str | None = None
    reasoning_gate_value: str | None = "1"
    explicit_reasoning: str = "auto"
    binding_fault: str = ""
    expected_direction: str = "steady"


@dataclass(frozen=True)
class Phase5AIntegrationOutcome:
    scenario_id: str
    group: str
    planner_enabled: bool
    reasoning_enabled: bool
    applied: bool
    reason_code: str
    selected_reasoning: str
    valid_supported_value: bool
    explicit_override_violation: bool
    payload_diff_paths: tuple[str, ...]
    payload_diff_contained: bool
    provider_call_count_legacy: int
    provider_call_count_adaptive: int
    critic_call_count: int
    evaluator_call_count: int
    escalation_correct: bool
    deescalation_correct: bool
    context_isolation_correct: bool


@dataclass(frozen=True)
class TimingStats:
    operation_count: int
    mean_microseconds: float
    p50_microseconds: float
    p95_microseconds: float
    max_microseconds: float


@dataclass(frozen=True)
class CorrectnessMetrics:
    legacy_semantic_fidelity_rate: float
    adaptive_semantic_fidelity_rate: float
    false_success_count: int
    incorrect_blocked_count: int
    hard_violation_count: int
    permission_violation_count: int
    identity_violation_count: int
    replay_violation_count: int
    modality_violation_count: int


@dataclass(frozen=True)
class BehaviorMetrics:
    average_strategy_attempts_legacy: float
    average_strategy_attempts_adaptive: float
    average_plan_steps_legacy: float
    average_plan_steps_adaptive: float
    completed_step_replay_count: int
    redundant_observation_count: int
    unnecessary_fallback_count: int


@dataclass(frozen=True)
class ReasoningPolicyMetrics:
    supported_value_compliance_rate: float
    unsupported_value_emission_count: int
    explicit_override_violation_count: int
    acceptable_effort_rate: float
    unnecessary_high_effort_count: int
    under_reasoning_count: int
    escalation_correctness_rate: float
    deescalation_correctness_rate: float
    payload_containment_rate: float


@dataclass(frozen=True)
class TopologyMetrics:
    legacy_provider_call_count: int
    adaptive_provider_call_count: int
    additional_provider_call_count: int
    critic_call_count: int
    evaluator_call_count: int
    preflight_model_call_count: int


@dataclass(frozen=True)
class IntegrationTiming:
    context_construction: TimingStats
    adaptive_policy: TimingStats
    native_value_validation: TimingStats
    combined_integration: TimingStats


@dataclass(frozen=True)
class Phase5AMetrics:
    phase5a_scenario_count: int
    total_wor260_scenario_count: int
    correctness: CorrectnessMetrics
    behavior: BehaviorMetrics
    reasoning_policy: ReasoningPolicyMetrics
    topology: TopologyMetrics
    context_isolation_failure_count: int
    gate_off_parity_failure_count: int
    classification: str


@dataclass(frozen=True)
class Phase5ABenchmarkReport:
    phase4b_report: Phase4BBenchmarkReport
    outcomes: tuple[Phase5AIntegrationOutcome, ...]
    metrics: Phase5AMetrics
    timing: IntegrationTiming


def integration_scenarios() -> tuple[Phase5AIntegrationScenario, ...]:
    """Return 52 non-duplicative controlled-integration cases."""

    specs = (
        # Gate parity and the full A/B/C/D matrix.
        ("off_off_exact_legacy", "gate", "high_repeated_replan", "openai", "gpt-5.5", None, None, "auto", "", "steady"),
        ("planner_on_reasoning_off", "gate", "high_repeated_replan", "openai", "gpt-5.5", "1", None, "auto", "", "steady"),
        ("planner_off_reasoning_on", "gate", "moderate_two_candidates", "openai", "gpt-5.5", None, "1", "auto", "", "escalate"),
        ("planner_on_reasoning_on", "gate", "high_repeated_replan", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        ("reasoning_environment_absent", "gate", "high_close_candidates", "openai", "gpt-5.5", None, None, "auto", "", "steady"),
        ("reasoning_environment_false", "gate", "high_close_candidates", "openai", "gpt-5.5", None, "false", "auto", "", "steady"),
        ("reasoning_environment_malformed", "gate", "high_close_candidates", "openai", "gpt-5.5", None, "enabled", "auto", "", "steady"),
        # Explicit turn and stored selection precedence.
        ("slash_reasoning_low", "explicit", "critical_equivalent_mutations", "openai", "gpt-5.5", None, "1", "low", "", "steady"),
        ("slash_reasoning_high", "explicit", "simple_direct_api", "openai", "gpt-5.5", None, "1", "high", "", "steady"),
        ("explicit_supported_xhigh", "explicit", "simple_direct_api", "openai", "gpt-5.5", None, "1", "xhigh", "", "steady"),
        ("explicit_unsupported_ultra", "explicit", "simple_direct_api", "openai", "gpt-5.5", None, "1", "ultra", "", "steady"),
        ("saved_model_reasoning_high", "explicit", "simple_direct_api", "deepseek", "deepseek-v4-pro", None, "1", "high", "", "steady"),
        ("explicit_with_planner_on", "explicit", "high_repeated_replan", "groq", "openai/gpt-oss-120b", "1", "1", "medium", "", "steady"),
        # Simple Auto cases.
        ("auto_exact_direct_api", "simple", "simple_direct_api", "openai", "gpt-5.5", None, "1", "auto", "", "steady"),
        ("auto_exact_deep_link", "simple", "simple_exact_deep_link", "chatgpt_account", "gpt-5.6-sol", "1", "1", "auto", "", "steady"),
        ("auto_one_exact_uia_control", "simple", "simple_exact_uia_target", "groq", "openai/gpt-oss-120b", "1", "1", "auto", "", "steady"),
        ("auto_strong_structured_read", "simple", "simple_strong_structured_read", "google", "gemini-3.5-flash", None, "1", "auto", "", "steady"),
        ("auto_one_remaining_node", "simple", "simple_one_remaining_postcondition", "anthropic", "claude-opus-4-8", None, "1", "auto", "", "steady"),
        # Complex Auto cases.
        ("auto_close_candidates", "complex", "high_close_candidates", "chatgpt_account", "gpt-5.6-sol", "1", "1", "auto", "", "escalate"),
        ("auto_contradiction", "complex", "high_contradictory_observations", "groq", "openai/gpt-oss-120b", "1", "1", "auto", "", "escalate"),
        ("auto_multi_app", "complex", "high_multi_app_mission", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        ("auto_repeated_replanning", "complex", "high_repeated_replan", "google", "gemini-3.5-flash", "1", "1", "auto", "", "escalate"),
        ("auto_complex_stale_state", "complex", "high_stale_alternate_paths", "deepseek", "deepseek-v4-pro", "1", "1", "auto", "", "escalate"),
        ("auto_difficult_outcome_unknown", "complex", "critical_equivalent_mutations", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        # De-escalation cases carry a prior high decision in Phase 4A.
        ("deescalate_ambiguity_resolved", "deescalation", "deescalate_contradiction_resolved", "chatgpt_account", "gpt-5.6-sol", "1", "1", "auto", "", "deescalate"),
        ("deescalate_reconciliation_complete", "deescalation", "deescalate_outcome_reconciled", "anthropic", "claude-opus-4-8", "1", "1", "auto", "", "deescalate"),
        ("deescalate_to_one_candidate", "deescalation", "deescalate_one_exact_candidate", "openai", "gpt-5.5", "1", "1", "auto", "", "deescalate"),
        ("deescalate_stale_ref_resolved", "deescalation", "deescalate_stale_ref_resolved", "groq", "openai/gpt-oss-120b", "1", "1", "auto", "", "deescalate"),
        ("deescalate_final_obvious_step", "deescalation", "deescalate_one_simple_node", "google", "gemini-3.5-flash", "1", "1", "auto", "", "deescalate"),
        # Exact-pair capability behavior.
        ("capability_configurable_pair", "capability", "moderate_two_candidates", "openai", "gpt-5.5", None, "1", "auto", "", "escalate"),
        ("capability_partial_value_set", "capability", "partial_deepseek_high_max", "deepseek", "deepseek-v4-pro", None, "1", "auto", "", "escalate"),
        ("capability_non_configurable", "capability", "unsupported_xai_model", "xai", "grok-3", None, "1", "auto", "", "steady"),
        ("capability_unknown_pair", "capability", "unknown_exact_pair", "unknown-provider", "unknown-model", None, "1", "auto", "", "steady"),
        ("capability_provider_default", "capability", "simple_direct_api", "openai", "gpt-5.5", None, "1", "auto", "", "steady"),
        ("capability_chatgpt_metadata", "capability", "moderate_three_candidates", "chatgpt_account", "gpt-5.6-sol", None, "1", "auto", "", "escalate"),
        # Context isolation.
        ("context_cross_turn", "isolation", "high_multi_app_mission", "openai", "gpt-5.5", None, "1", "auto", "turn", "steady"),
        ("context_provider_switch", "isolation", "high_multi_app_mission", "openai", "gpt-5.5", None, "1", "auto", "provider", "steady"),
        ("context_model_switch", "isolation", "high_multi_app_mission", "openai", "gpt-5.5", None, "1", "auto", "model", "steady"),
        ("context_stale_revision", "isolation", "high_multi_app_mission", "openai", "gpt-5.5", None, "1", "auto", "revision", "steady"),
        ("context_different_conversation", "isolation", "high_multi_app_mission", "openai", "gpt-5.5", None, "1", "auto", "conversation", "steady"),
        # Call topology.
        ("topology_same_call_count", "topology", "high_multi_app_mission", "openai", "gpt-5.5", None, "1", "auto", "", "escalate"),
        ("topology_no_critic_call", "topology", "critical_equivalent_mutations", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        ("topology_no_evaluator_call", "topology", "high_contradictory_observations", "groq", "openai/gpt-oss-120b", "1", "1", "auto", "", "escalate"),
        ("topology_no_preflight_call", "topology", "high_repeated_replan", "google", "gemini-3.5-flash", "1", "1", "auto", "", "escalate"),
        # Authority-separation adversarial cases.
        ("adversarial_wrong_surface", "adversarial", "high_account_surface_ambiguity", "anthropic", "claude-opus-4-8", "1", "1", "auto", "", "escalate"),
        ("adversarial_wrong_account", "adversarial", "high_account_surface_ambiguity", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        ("adversarial_hard_rejected_capability", "adversarial", "critical_irreversible_review", "google", "gemini-3.5-flash", "1", "1", "auto", "", "escalate"),
        ("adversarial_outcome_unknown_replay", "adversarial", "critical_unknown_stale_reconciliation", "groq", "openai/gpt-oss-120b", "1", "1", "auto", "", "escalate"),
        ("adversarial_unsupported_vision", "adversarial", "high_multi_app_mission", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        ("adversarial_permission_unchanged", "adversarial", "critical_irreversible_review", "openai", "gpt-5.5", "1", "1", "auto", "", "escalate"),
        ("adversarial_terminality_unchanged", "adversarial", "critical_contradictory_reconciliation", "anthropic", "claude-opus-4-8", "1", "1", "auto", "", "escalate"),
        ("adversarial_dispatch_unchanged", "adversarial", "high_repeated_replan", "deepseek", "deepseek-v4-pro", "1", "1", "auto", "", "escalate"),
    )
    scenarios = tuple(Phase5AIntegrationScenario(*item) for item in specs)
    if len(scenarios) != 52:
        raise AssertionError(f"phase5a_scenario_count:{len(scenarios)}")
    return scenarios


@contextmanager
def _gates(planner: str | None, reasoning: str | None) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in (PHASE3_GATE_ENV, ADAPTIVE_REASONING_GATE_ENV)}
    try:
        for name, value in (
            (PHASE3_GATE_ENV, planner),
            (ADAPTIVE_REASONING_GATE_ENV, reasoning),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _account_metadata(provider_id: str) -> Iterator[None]:
    if provider_id != "chatgpt_account":
        yield
        return
    with patch(
        "osia.chatgpt_account.provider.get_cached_account_models",
        return_value=[_ACCOUNT_MODEL],
    ):
        yield


def _policy_fixture(identity: str):
    return next(item for item in reasoning_scenarios() if item.scenario_id == identity)


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, Mapping):
            result.update(_flatten(child, path))
        else:
            result[path] = child
    return result


def _diff_paths(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    left = _flatten(before)
    right = _flatten(after)
    return tuple(sorted(
        path for path in set(left) | set(right) if left.get(path) != right.get(path)
    ))


def _run_scenario(scenario: Phase5AIntegrationScenario) -> Phase5AIntegrationOutcome:
    fixture = _policy_fixture(scenario.policy_scenario_id)
    context = replace(
        fixture.context,
        provider_id=scenario.provider_id,
        model_id=scenario.model_id,
    )
    bound = BoundReasoningContext(
        schema=ADAPTIVE_REASONING_BINDING_SCHEMA,
        turn_id="turn-phase5a",
        revision=7,
        goal_fingerprint="goal-phase5a",
        provider_id=scenario.provider_id,
        model_id=scenario.model_id,
        conversation_id="conversation-phase5a",
        source=(
            "phase3_planner"
            if str(scenario.planner_gate_value or "").casefold() in {"1", "true", "yes", "on"}
            else "canonical_snapshots"
        ),
        decision_context=context,
    )
    if scenario.binding_fault == "turn":
        bound = replace(bound, turn_id="other-turn")
    elif scenario.binding_fault == "provider":
        bound = replace(bound, provider_id="other-provider")
    elif scenario.binding_fault == "model":
        bound = replace(bound, model_id="other-model")
    elif scenario.binding_fault == "revision":
        bound = replace(bound, revision=6)
    elif scenario.binding_fault == "conversation":
        bound = replace(bound, conversation_id="other-conversation")

    settings = {
        "default": scenario.explicit_reasoning,
        "providers": {},
        "models": {},
    }
    invariant_payload = {
        "model": scenario.model_id,
        "messages": (("system", "stable"), ("user", "fixture")),
        "tools": (("tool-a", 1), ("tool-b", 2)),
        "temperature": 0,
    }
    with _account_metadata(scenario.provider_id), _gates(
        scenario.planner_gate_value, scenario.reasoning_gate_value,
    ):
        legacy = build_reasoning_request(
            scenario.provider_id, scenario.model_id, settings=settings,
        )
        integration = integrate_adaptive_reasoning(
            legacy,
            provider_id=scenario.provider_id,
            model_id=scenario.model_id,
            bound_context=bound,
            turn_id="turn-phase5a",
            revision=7,
            conversation_id="conversation-phase5a",
        )
        capability = capability_from_current_registry(
            scenario.provider_id, scenario.model_id,
        )

    before = {**invariant_payload, **dict(legacy.get("kwargs") or {})}
    after = {**invariant_payload, **dict(integration.request.get("kwargs") or {})}
    differences = _diff_paths(before, after)
    diagnostic = dict(integration.request.get("diagnostic") or {})
    selected = str(diagnostic.get("effective_reasoning") or "")
    allowed = {
        str(item.get("name") or "")
        for item in diagnostic.get("provider_parameters") or ()
        if isinstance(item, Mapping)
    }
    contained = all(
        (path.removeprefix("extra_body.") in allowed)
        or any(
            path.removeprefix("extra_body.").startswith(name + ".")
            for name in allowed
        )
        for path in differences
    )
    valid_supported = bool(
        not integration.applied
        or selected in capability.supported_osia_values
        and capability.value_for(selected) is not None
    )
    explicit = scenario.explicit_reasoning.casefold() != "auto"
    override_violation = bool(integration.applied and explicit)
    decision = integration.decision
    escalation_correct = bool(
        scenario.expected_direction != "escalate"
        or decision is not None
        and decision.difficulty_class in {
            DifficultyClass.MEDIUM,
            DifficultyClass.HIGH,
            DifficultyClass.CRITICAL_RECONCILIATION,
        }
    )
    deescalation_correct = bool(
        scenario.expected_direction != "deescalate"
        or decision is not None
        and decision.difficulty_class in {DifficultyClass.TRIVIAL, DifficultyClass.LOW}
    )
    isolation_correct = bool(
        not scenario.binding_fault
        or not integration.applied
        and integration.reason_code == "context_binding_mismatch"
        and not differences
    )
    return Phase5AIntegrationOutcome(
        scenario_id=scenario.scenario_id,
        group=scenario.group,
        planner_enabled=str(scenario.planner_gate_value or "").casefold() in {"1", "true", "yes", "on"},
        reasoning_enabled=str(scenario.reasoning_gate_value or "").casefold() in {"1", "true", "yes", "on"},
        applied=integration.applied,
        reason_code=integration.reason_code,
        selected_reasoning=selected,
        valid_supported_value=valid_supported,
        explicit_override_violation=override_violation,
        payload_diff_paths=differences,
        payload_diff_contained=contained,
        provider_call_count_legacy=1,
        provider_call_count_adaptive=1,
        critic_call_count=0,
        evaluator_call_count=0,
        escalation_correct=escalation_correct,
        deescalation_correct=deescalation_correct,
        context_isolation_correct=isolation_correct,
    )


def _timing(samples: list[float]) -> TimingStats:
    ordered = sorted(samples)
    if not ordered:
        return TimingStats(0, 0.0, 0.0, 0.0, 0.0)
    percentile = lambda value: ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * value) - 1))]
    return TimingStats(
        len(ordered), fmean(ordered), percentile(0.50), percentile(0.95), ordered[-1],
    )


def _measure(iterations: int) -> IntegrationTiming:
    strategy = build_execution_strategy("Read the current catalog item")
    terminality = {
        "turn_id": "phase5a-timing",
        "proven_postconditions": {},
        "typed_evidence": (),
        "active_attempts": {},
        "permission_state": "allowed",
        "evidence_revision": 1,
    }
    strategy_snapshot = strategy.snapshot()
    legacy = build_reasoning_request(
        "openai", "gpt-5.5",
        settings={"default": "auto", "providers": {}, "models": {}},
    )
    construction: list[float] = []
    policy: list[float] = []
    validation: list[float] = []
    combined: list[float] = []
    with _gates(None, "1"):
        bound = build_bound_reasoning_context(
            semantic_mission=strategy.mission,
            terminality_snapshot=terminality,
            strategy_snapshot=strategy_snapshot,
            provider_id="openai",
            model_id="gpt-5.5",
            conversation_id="timing",
        )
        if bound is None:
            raise AssertionError("phase5a_timing_context_unavailable")
        capability = capability_from_current_registry("openai", "gpt-5.5")
        for _ in range(max(1, iterations)):
            started = perf_counter_ns()
            build_bound_reasoning_context(
                semantic_mission=strategy.mission,
                terminality_snapshot=terminality,
                strategy_snapshot=strategy_snapshot,
                provider_id="openai",
                model_id="gpt-5.5",
                conversation_id="timing",
            )
            construction.append((perf_counter_ns() - started) / 1_000.0)

            started = perf_counter_ns()
            decision = determine_reasoning_decision(
                bound.decision_context, capability,
            )
            policy.append((perf_counter_ns() - started) / 1_000.0)

            started = perf_counter_ns()
            value = capability.value_for(
                decision.selected_osia_level or "",
            )
            bool(
                value is None
                or value.native_value == decision.selected_native_value
            )
            validation.append((perf_counter_ns() - started) / 1_000.0)

            started = perf_counter_ns()
            combined_candidate = build_bound_reasoning_context(
                semantic_mission=strategy.mission,
                terminality_snapshot=terminality,
                strategy_snapshot=strategy_snapshot,
                provider_id="openai",
                model_id="gpt-5.5",
                conversation_id="timing",
            )
            integrate_adaptive_reasoning(
                legacy,
                provider_id="openai",
                model_id="gpt-5.5",
                bound_context=combined_candidate,
                turn_id="phase5a-timing",
                revision=1,
                conversation_id="timing",
            )
            combined.append((perf_counter_ns() - started) / 1_000.0)
    return IntegrationTiming(
        _timing(construction), _timing(policy), _timing(validation), _timing(combined),
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def run_phase5a_benchmark(*, timing_iterations: int = 200) -> Phase5ABenchmarkReport:
    scenarios = integration_scenarios()
    outcomes = tuple(_run_scenario(item) for item in scenarios)
    phase4b = run_phase4b_benchmark(timing_iterations=1)
    phase3 = phase4b.phase4a_report.phase3_report.metrics
    phase4a_outcomes = {
        item.scenario_id: item
        for item in phase4b.phase4a_report.outcomes
    }
    policy_outcomes = tuple(
        item for item in outcomes
        if item.reasoning_enabled and item.group not in {"explicit", "isolation"}
    )
    applicable = tuple(item for item in policy_outcomes if item.applied)
    acceptable = 0
    unnecessary_high = 0
    under_reasoning = 0
    for scenario, outcome in zip(scenarios, outcomes):
        if outcome not in applicable:
            continue
        reference = phase4a_outcomes[scenario.policy_scenario_id]
        with _account_metadata(scenario.provider_id):
            capability = capability_from_current_registry(
                scenario.provider_id, scenario.model_id,
            )
        expected = select_level_for_difficulty(
            capability, reference.decision.difficulty_class,
        )
        acceptable += int(outcome.selected_reasoning == expected)
        selected_value = capability.value_for(outcome.selected_reasoning)
        expected_value = capability.value_for(expected)
        if selected_value is not None and expected_value is not None:
            unnecessary_high += int(selected_value.ordinal > expected_value.ordinal)
            under_reasoning += int(selected_value.ordinal < expected_value.ordinal)
    escalation = tuple(item for item in outcomes if any(
        scenario.scenario_id == item.scenario_id and scenario.expected_direction == "escalate"
        for scenario in scenarios
    ))
    deescalation = tuple(item for item in outcomes if any(
        scenario.scenario_id == item.scenario_id and scenario.expected_direction == "deescalate"
        for scenario in scenarios
    ))
    gate_off = tuple(item for item in outcomes if not item.reasoning_enabled)
    topology = TopologyMetrics(
        legacy_provider_call_count=sum(item.provider_call_count_legacy for item in outcomes),
        adaptive_provider_call_count=sum(item.provider_call_count_adaptive for item in outcomes),
        additional_provider_call_count=sum(
            item.provider_call_count_adaptive - item.provider_call_count_legacy
            for item in outcomes
        ),
        critic_call_count=sum(item.critic_call_count for item in outcomes),
        evaluator_call_count=sum(item.evaluator_call_count for item in outcomes),
        preflight_model_call_count=0,
    )
    reasoning = ReasoningPolicyMetrics(
        supported_value_compliance_rate=_rate(
            sum(item.valid_supported_value for item in policy_outcomes),
            len(policy_outcomes),
        ),
        unsupported_value_emission_count=sum(
            not item.valid_supported_value for item in policy_outcomes
        ),
        explicit_override_violation_count=sum(
            item.explicit_override_violation for item in outcomes
        ),
        acceptable_effort_rate=_rate(acceptable, len(applicable)),
        unnecessary_high_effort_count=unnecessary_high,
        under_reasoning_count=under_reasoning,
        escalation_correctness_rate=_rate(
            sum(item.escalation_correct for item in escalation), len(escalation),
        ),
        deescalation_correctness_rate=_rate(
            sum(item.deescalation_correct for item in deescalation), len(deescalation),
        ),
        payload_containment_rate=_rate(
            sum(item.payload_diff_contained for item in outcomes), len(outcomes),
        ),
    )
    correctness = CorrectnessMetrics(
        legacy_semantic_fidelity_rate=phase3.off_semantic_fidelity_rate,
        adaptive_semantic_fidelity_rate=phase3.on_semantic_fidelity_rate,
        false_success_count=phase3.false_success_count,
        incorrect_blocked_count=phase3.incorrect_blocked_count,
        hard_violation_count=phase3.hard_constraint_violation_count,
        permission_violation_count=phase3.permission_violation_count,
        identity_violation_count=phase3.identity_violation_count,
        replay_violation_count=phase3.outcome_unknown_replay_violation_count,
        modality_violation_count=phase3.modality_violation_count,
    )
    behavior = BehaviorMetrics(
        average_strategy_attempts_legacy=phase3.average_strategy_attempts_off,
        average_strategy_attempts_adaptive=phase3.average_strategy_attempts_on,
        average_plan_steps_legacy=phase3.average_plan_steps_off,
        average_plan_steps_adaptive=phase3.average_plan_steps_on,
        completed_step_replay_count=phase3.completed_node_replay_count,
        redundant_observation_count=phase3.redundant_observation_count,
        unnecessary_fallback_count=phase3.unnecessary_fallback_count,
    )
    isolation_failures = sum(
        not item.context_isolation_correct
        for item in outcomes if item.group == "isolation"
    )
    off_failures = sum(bool(item.payload_diff_paths) for item in gate_off)
    ready = bool(
        correctness.adaptive_semantic_fidelity_rate
        >= correctness.legacy_semantic_fidelity_rate
        and not any((
            correctness.false_success_count,
            correctness.incorrect_blocked_count,
            correctness.hard_violation_count,
            correctness.permission_violation_count,
            correctness.identity_violation_count,
            correctness.replay_violation_count,
            correctness.modality_violation_count,
            reasoning.unsupported_value_emission_count,
            reasoning.explicit_override_violation_count,
            topology.additional_provider_call_count,
            topology.critic_call_count,
            topology.evaluator_call_count,
            topology.preflight_model_call_count,
            isolation_failures,
            off_failures,
        ))
        and reasoning.payload_containment_rate == 1.0
        and reasoning.acceptable_effort_rate == 1.0
        and reasoning.escalation_correctness_rate == 1.0
        and reasoning.deescalation_correctness_rate == 1.0
    )
    metrics = Phase5AMetrics(
        phase5a_scenario_count=len(outcomes),
        total_wor260_scenario_count=phase4b.metrics.total_wor260_scenario_count + len(outcomes),
        correctness=correctness,
        behavior=behavior,
        reasoning_policy=reasoning,
        topology=topology,
        context_isolation_failure_count=isolation_failures,
        gate_off_parity_failure_count=off_failures,
        classification=(
            "READY_FOR_CONTROLLED_PHYSICAL_PILOT"
            if ready else "NOT_READY_FOR_PHYSICAL_PILOT"
        ),
    )
    return Phase5ABenchmarkReport(
        phase4b_report=phase4b,
        outcomes=outcomes,
        metrics=metrics,
        timing=_measure(timing_iterations),
    )


__all__ = [
    "BehaviorMetrics",
    "CorrectnessMetrics",
    "IntegrationTiming",
    "Phase5ABenchmarkReport",
    "Phase5AIntegrationOutcome",
    "Phase5AIntegrationScenario",
    "Phase5AMetrics",
    "ReasoningPolicyMetrics",
    "TimingStats",
    "TopologyMetrics",
    "integration_scenarios",
    "run_phase5a_benchmark",
]
