"""Deterministic WOR-260 Phase-4A adaptive-reasoning policy benchmark."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from statistics import fmean
from time import perf_counter_ns

from benchmarks.wor260.phase3 import Phase3BenchmarkReport, run_phase3_benchmark
from osia.adaptive_reasoning import (
    AmbiguityLevel,
    AuthoritativeReasoningCapability,
    DecisionConfidence,
    DifficultyClass,
    GoalComplexity,
    Irreversibility,
    OutcomeUnknownComplexity,
    ProgressSignal,
    ProviderParameterBehavior,
    REASONING_CONTEXT_SCHEMA_VERSION,
    ReasoningDecision,
    ReasoningDecisionContext,
    ReasoningPhase,
    SideEffectRisk,
    adapt_authoritative_reasoning_capability,
    capability_from_current_registry,
    determine_reasoning_decision,
    select_level_for_difficulty,
)


@dataclass(frozen=True)
class Phase4AReasoningScenario:
    scenario_id: str
    group: str
    context: ReasoningDecisionContext
    capability: AuthoritativeReasoningCapability
    acceptable_difficulties: tuple[DifficultyClass, ...]
    expected_direction: str = "steady"
    expected_selected_level: str = ""
    critic_candidate: bool = False


@dataclass(frozen=True)
class Phase4AReasoningOutcome:
    scenario_id: str
    group: str
    configurable: bool
    decision: ReasoningDecision
    acceptable_levels: tuple[str, ...]
    valid_supported_value: bool
    acceptable_effort: bool
    under_reasoning: bool
    unnecessary_high_effort: bool
    override_violation: bool
    provider_default_violation: bool
    escalation_correct: bool
    deescalation_correct: bool
    fixed_policy_acceptance: tuple[tuple[str, bool], ...]
    critic_candidate: bool


@dataclass(frozen=True)
class PolicyTiming:
    decision_count: int
    mean_microseconds: float
    p50_microseconds: float
    p95_microseconds: float
    max_microseconds: float


@dataclass(frozen=True)
class Phase4AMetrics:
    scenario_count: int
    configurable_pair_scenarios: int
    non_configurable_pair_scenarios: int
    valid_supported_value_rate: float
    unsupported_value_selection_count: int
    user_override_violation_count: int
    provider_default_violation_count: int
    acceptable_effort_rate: float
    under_reasoning_count: int
    unnecessary_high_effort_count: int
    escalation_correctness_rate: float
    deescalation_correctness_rate: float
    critical_reconciliation_high_effort_rate: float
    simple_task_low_or_default_rate: float
    unknown_pair_safe_default_rate: float
    adaptive_vs_fixed_policy_advantage_count: int
    adaptive_vs_fixed_policy_regression_count: int
    unresolved_policy_count: int
    critic_candidate_count: int
    critic_unnecessary_count: int


@dataclass(frozen=True)
class Phase4ABenchmarkReport:
    phase3_report: Phase3BenchmarkReport
    outcomes: tuple[Phase4AReasoningOutcome, ...]
    metrics: Phase4AMetrics
    timing: PolicyTiming

    @property
    def total_wor260_scenario_count(self) -> int:
        return self.phase3_report.metrics.total_scenario_count + self.metrics.scenario_count


def _chatgpt_account_snapshot() -> AuthoritativeReasoningCapability:
    raw = {
        "options": [
            {"id": "auto", "kwargs": {}, "default": True},
            *(
                {"id": value, "kwargs": {"effort": value}, "default": False}
                for value in ("low", "medium", "high", "xhigh", "max", "ultra")
            ),
        ],
        "provider_default": "low",
        "evidence": "chatgpt_app_server:model/list fixture",
    }
    return adapt_authoritative_reasoning_capability(
        "chatgpt_account", "gpt-5.6-sol", raw
    )


def _capability(provider_id: str, model_id: str) -> AuthoritativeReasoningCapability:
    if (provider_id, model_id) == ("chatgpt_account", "gpt-5.6-sol"):
        return _chatgpt_account_snapshot()
    return capability_from_current_registry(provider_id, model_id)


def _previous_high(
    capability: AuthoritativeReasoningCapability,
) -> ReasoningDecision:
    selected = select_level_for_difficulty(capability, DifficultyClass.HIGH)
    value = capability.value_for(selected)
    return ReasoningDecision(
        provider_id=capability.provider_id,
        model_id=capability.model_id,
        phase=ReasoningPhase.REPLANNING,
        difficulty_class=DifficultyClass.HIGH,
        requested_mode="auto",
        adaptive_allowed=True,
        selected_osia_level=selected,
        selected_native_value=value.native_value if value else None,
        provider_parameter_behavior=(
            ProviderParameterBehavior.EMIT_NATIVE_VALUE
            if value else ProviderParameterBehavior.OMIT_PROVIDER_DEFAULT
        ),
        escalation_reason_codes=("previous_complex_state",),
        deescalation_reason_codes=(),
        unsupported_reason="",
        confidence_class=DecisionConfidence.MEDIUM,
    )


def _base_context(
    capability: AuthoritativeReasoningCapability,
) -> ReasoningDecisionContext:
    return ReasoningDecisionContext(
        schema_version=REASONING_CONTEXT_SCHEMA_VERSION,
        provider_id=capability.provider_id,
        model_id=capability.model_id,
        user_reasoning_mode="auto",
        explicit_turn_reasoning=None,
        supported_reasoning_values=capability.supported_osia_values,
        provider_default_behavior=capability.provider_default_behavior,
        phase=ReasoningPhase.INITIAL_PLANNING,
        goal_complexity=GoalComplexity.SIMPLE,
        candidate_count=1,
        close_candidate_count=1,
        ambiguity_level=AmbiguityLevel.NONE,
        contradiction_count=0,
        unresolved_constraint_count=0,
        remaining_goal_node_count=1,
        replan_count=0,
        repeated_replan_count=0,
        stale_ref_count=0,
        outcome_unknown_count=0,
        outcome_unknown_complexity=OutcomeUnknownComplexity.NONE,
        verifier_status="verified",
        required_observation_count=0,
        evidence_strength="exact_postcondition",
        evidence_conflict=False,
        side_effect_risk=SideEffectRisk.LOW,
        irreversibility=Irreversibility.REVERSIBLE,
        cross_application_count=1,
        previous_reasoning_decision=None,
        progress_signal=ProgressSignal.CLEAR,
        exact_candidate_count=1,
        exact_direct_capability=True,
        semantic_effect_exact=True,
        surface_account_exact=True,
        unresolved_permission_count=0,
        unresolved_account_binding_count=0,
        dependency_count=0,
        incomplete_world_state_count=0,
    )


def reasoning_scenarios() -> tuple[Phase4AReasoningScenario, ...]:
    openai = _capability("openai", "gpt-5.5")
    groq = _capability("groq", "openai/gpt-oss-120b")
    deepseek = _capability("deepseek", "deepseek-flash")
    google = _capability("google", "gemini-3.5-flash")
    anthropic = _capability("anthropic", "claude-opus-4-8")
    account = _capability("chatgpt_account", "gpt-5.6-sol")
    mistral = _capability("mistral", "mistral-medium-3-5")
    qwen = _capability("groq", "qwen/qwen3.6-27b")
    scenarios: list[Phase4AReasoningScenario] = []

    simple_specs = (
        ("simple_direct_api", openai, {}),
        ("simple_exact_deep_link", account, {"phase": ReasoningPhase.CAPABILITY_SEARCH}),
        ("simple_exact_uia_target", groq, {"phase": ReasoningPhase.PRE_ACTION_VALIDATION}),
        ("simple_strong_structured_read", google, {"phase": ReasoningPhase.POST_ACTION_VERIFICATION}),
        ("simple_one_remaining_postcondition", anthropic, {"verifier_status": "unverified"}),
        ("simple_obvious_final_step", deepseek, {"progress_signal": ProgressSignal.CLEAR}),
    )
    for scenario_id, capability, changes in simple_specs:
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "simple",
            replace(_base_context(capability), **changes),
            capability,
            (DifficultyClass.TRIVIAL, DifficultyClass.LOW),
        ))

    moderate_specs = (
        ("moderate_two_candidates", openai, {"candidate_count": 2, "close_candidate_count": 2}),
        ("moderate_three_candidates", account, {"candidate_count": 3, "close_candidate_count": 2, "ambiguity_level": AmbiguityLevel.LOW}),
        ("moderate_single_app_multistep", groq, {"goal_complexity": GoalComplexity.MULTI_STEP, "dependency_count": 2}),
        ("moderate_observe_then_act", google, {"goal_complexity": GoalComplexity.MULTI_STEP, "required_observation_count": 2}),
        ("moderate_unresolved_target", anthropic, {"ambiguity_level": AmbiguityLevel.MEDIUM, "unresolved_constraint_count": 1}),
        ("moderate_multiple_postconditions", deepseek, {"remaining_goal_node_count": 2, "unresolved_constraint_count": 2}),
    )
    for scenario_id, capability, changes in moderate_specs:
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "moderate",
            replace(_base_context(capability), **changes),
            capability,
            (DifficultyClass.MEDIUM,),
            expected_direction="escalate",
        ))

    high_specs = (
        ("high_multi_app_mission", openai, {"cross_application_count": 3, "goal_complexity": GoalComplexity.DEPENDENCY_HEAVY, "dependency_count": 3}),
        ("high_close_candidates", account, {"candidate_count": 5, "close_candidate_count": 4, "ambiguity_level": AmbiguityLevel.HIGH, "unresolved_constraint_count": 2}),
        ("high_contradictory_observations", groq, {"contradiction_count": 2, "evidence_conflict": True, "verifier_status": "contradicted", "required_observation_count": 2}),
        ("high_repeated_replan", google, {"replan_count": 3, "repeated_replan_count": 2, "stale_ref_count": 2, "progress_signal": ProgressSignal.STALLED}),
        ("high_account_surface_ambiguity", anthropic, {"unresolved_account_binding_count": 1, "cross_application_count": 2, "ambiguity_level": AmbiguityLevel.HIGH}),
        ("high_stale_alternate_paths", deepseek, {"stale_ref_count": 3, "candidate_count": 4, "close_candidate_count": 3, "unresolved_constraint_count": 2}),
    )
    for index, (scenario_id, capability, changes) in enumerate(high_specs):
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "high",
            replace(_base_context(capability), **changes),
            capability,
            (DifficultyClass.HIGH,),
            expected_direction="escalate",
            critic_candidate=index in {1, 2, 3},
        ))

    critical_specs = (
        ("critical_equivalent_mutations", openai, {"phase": ReasoningPhase.RECONCILIATION, "outcome_unknown_count": 2, "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX}),
        ("critical_multiple_read_routes", account, {"phase": ReasoningPhase.RECONCILIATION, "outcome_unknown_count": 1, "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX, "required_observation_count": 3}),
        ("critical_contradictory_reconciliation", anthropic, {"phase": ReasoningPhase.RECONCILIATION, "outcome_unknown_count": 1, "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX, "contradiction_count": 2, "evidence_conflict": True}),
        ("critical_irreversible_review", google, {"phase": ReasoningPhase.TERMINAL_BLOCK_REVIEW, "verifier_status": "contradicted", "contradiction_count": 1, "irreversibility": Irreversibility.IRREVERSIBLE, "side_effect_risk": SideEffectRisk.CRITICAL}),
        ("critical_unknown_stale_reconciliation", groq, {"phase": ReasoningPhase.RECONCILIATION, "outcome_unknown_count": 2, "outcome_unknown_complexity": OutcomeUnknownComplexity.COMPLEX, "stale_ref_count": 2, "repeated_replan_count": 1}),
    )
    for scenario_id, capability, changes in critical_specs:
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "critical_reconciliation",
            replace(_base_context(capability), **changes),
            capability,
            (DifficultyClass.CRITICAL_RECONCILIATION,),
            expected_direction="escalate",
            critic_candidate=True,
        ))

    deescalation_specs = (
        ("deescalate_one_exact_candidate", openai, {}),
        ("deescalate_contradiction_resolved", account, {"phase": ReasoningPhase.REPLANNING}),
        ("deescalate_stale_ref_resolved", groq, {"phase": ReasoningPhase.REPLANNING}),
        ("deescalate_outcome_reconciled", anthropic, {"phase": ReasoningPhase.POST_ACTION_VERIFICATION}),
        ("deescalate_one_simple_node", google, {"phase": ReasoningPhase.REPLANNING}),
    )
    for scenario_id, capability, changes in deescalation_specs:
        context = replace(
            _base_context(capability),
            previous_reasoning_decision=_previous_high(capability),
            **changes,
        )
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "deescalation",
            context,
            capability,
            (DifficultyClass.TRIVIAL, DifficultyClass.LOW),
            expected_direction="deescalate",
        ))

    override_specs = (
        ("override_slash_low", openai, "low", "low"),
        ("override_slash_high", openai, "high", "high"),
        ("override_provider_setting", deepseek, None, "high"),
        ("override_supported_custom", account, "ultra", "ultra"),
        ("override_existing_none_alias", openai, "none", "off"),
    )
    for scenario_id, capability, turn_value, setting_value in override_specs:
        context = replace(
            _base_context(capability),
            explicit_turn_reasoning=turn_value,
            user_reasoning_mode=setting_value,
        )
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "explicit_override",
            context,
            capability,
            (DifficultyClass.TRIVIAL, DifficultyClass.LOW),
            expected_selected_level=setting_value,
        ))

    for scenario_id, provider_id, model_id in (
        ("unsupported_xai_model", "xai", "grok-3"),
        ("unsupported_ollama_model", "ollama", "llama3"),
        ("unknown_exact_pair", "unknown-provider", "unknown-model"),
        ("unknown_account_model", "chatgpt_account", "not-in-runtime"),
    ):
        capability = _capability(provider_id, model_id)
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "unsupported_control",
            _base_context(capability),
            capability,
            (DifficultyClass.TRIVIAL,),
            expected_selected_level="auto",
        ))

    partial_specs = (
        ("partial_deepseek_high_max", deepseek, DifficultyClass.HIGH, {"candidate_count": 3, "close_candidate_count": 2, "unresolved_constraint_count": 1}),
        ("partial_mistral_off_high", mistral, DifficultyClass.HIGH, {"candidate_count": 4, "close_candidate_count": 3, "contradiction_count": 1, "evidence_conflict": True}),
        ("partial_qwen_auto_off", qwen, DifficultyClass.HIGH, {"candidate_count": 4, "close_candidate_count": 3, "repeated_replan_count": 2}),
    )
    for scenario_id, capability, acceptable, changes in partial_specs:
        scenarios.append(Phase4AReasoningScenario(
            scenario_id,
            "partial_support",
            replace(_base_context(capability), **changes),
            capability,
            (acceptable,),
            expected_direction="escalate",
        ))

    if len(scenarios) != 40:
        raise AssertionError(f"phase4a_scenario_count:{len(scenarios)}")
    return tuple(scenarios)


def _acceptable_levels(scenario: Phase4AReasoningScenario) -> tuple[str, ...]:
    if scenario.expected_selected_level:
        return (scenario.expected_selected_level,)
    return tuple(dict.fromkeys(
        select_level_for_difficulty(scenario.capability, difficulty)
        for difficulty in scenario.acceptable_difficulties
    ))


def _selected_ordinal(
    capability: AuthoritativeReasoningCapability,
    level: str | None,
) -> int:
    if level in {None, "auto"}:
        return -1
    value = capability.value_for(level)
    return value.ordinal if value else -2


def _fixed_policy_levels(scenario: Phase4AReasoningScenario) -> dict[str, str]:
    context = scenario.context
    explicit = context.explicit_turn_reasoning
    requested = explicit if explicit is not None else context.user_reasoning_mode
    normalized = {"default": "auto", "none": "off"}.get(
        str(requested).strip().lower(), str(requested).strip().lower()
    )
    if normalized != "auto":
        return {name: normalized for name in (
            "provider_default", "lowest_fixed", "medium_fixed", "highest_fixed"
        )}
    capability = scenario.capability
    adaptive_values = capability.adaptive_values
    lowest = adaptive_values[0].osia_level if adaptive_values else "auto"
    highest = adaptive_values[-1].osia_level if adaptive_values else "auto"
    return {
        "provider_default": "auto",
        "lowest_fixed": lowest,
        "medium_fixed": select_level_for_difficulty(capability, DifficultyClass.MEDIUM),
        "highest_fixed": highest,
    }


def _run_scenario(scenario: Phase4AReasoningScenario) -> Phase4AReasoningOutcome:
    decision = determine_reasoning_decision(scenario.context, scenario.capability)
    acceptable_levels = _acceptable_levels(scenario)
    selected = decision.selected_osia_level
    if not scenario.capability.configurable:
        valid_supported = selected == "auto" and decision.selected_native_value is None
    else:
        valid_supported = bool(
            selected in scenario.capability.supported_osia_values
            and (
                selected == "auto"
                or scenario.capability.value_for(str(selected)) is not None
            )
        )
    acceptable = selected in acceptable_levels
    accepted_ordinals = tuple(
        _selected_ordinal(scenario.capability, item) for item in acceptable_levels
    )
    selected_ordinal = _selected_ordinal(scenario.capability, selected)
    under = bool(accepted_ordinals and selected_ordinal < min(accepted_ordinals))
    unnecessary = bool(accepted_ordinals and selected_ordinal > max(accepted_ordinals))
    explicit_requested = (
        scenario.context.explicit_turn_reasoning
        if scenario.context.explicit_turn_reasoning is not None
        else scenario.context.user_reasoning_mode
    )
    explicit_normalized = {"default": "auto", "none": "off"}.get(
        str(explicit_requested).strip().lower(),
        str(explicit_requested).strip().lower(),
    )
    override_violation = bool(
        explicit_normalized != "auto" and selected != explicit_normalized
    )
    provider_default_violation = bool(
        not scenario.capability.configurable
        and (
            selected != "auto"
            or decision.provider_parameter_behavior
            != ProviderParameterBehavior.OMIT_PROVIDER_DEFAULT
        )
    )
    escalation_correct = (
        acceptable if scenario.expected_direction == "escalate" else True
    )
    deescalation_correct = (
        acceptable if scenario.expected_direction == "deescalate" else True
    )
    fixed = _fixed_policy_levels(scenario)
    fixed_acceptance = tuple(
        (name, level in acceptable_levels) for name, level in fixed.items()
    )
    return Phase4AReasoningOutcome(
        scenario_id=scenario.scenario_id,
        group=scenario.group,
        configurable=scenario.capability.configurable,
        decision=decision,
        acceptable_levels=acceptable_levels,
        valid_supported_value=valid_supported,
        acceptable_effort=acceptable,
        under_reasoning=under,
        unnecessary_high_effort=unnecessary,
        override_violation=override_violation,
        provider_default_violation=provider_default_violation,
        escalation_correct=escalation_correct,
        deescalation_correct=deescalation_correct,
        fixed_policy_acceptance=fixed_acceptance,
        critic_candidate=scenario.critic_candidate,
    )


def _percentile(values: tuple[float, ...], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(math.ceil(percentile * len(ordered))) - 1)
    return ordered[max(0, index)]


def _measure_policy(
    scenarios: tuple[Phase4AReasoningScenario, ...],
    iterations: int,
) -> PolicyTiming:
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        for scenario in scenarios:
            started = perf_counter_ns()
            determine_reasoning_decision(scenario.context, scenario.capability)
            samples.append((perf_counter_ns() - started) / 1_000.0)
    frozen = tuple(samples)
    return PolicyTiming(
        decision_count=len(frozen),
        mean_microseconds=fmean(frozen),
        p50_microseconds=_percentile(frozen, 0.50),
        p95_microseconds=_percentile(frozen, 0.95),
        max_microseconds=max(frozen),
    )


def run_phase4a_benchmark(
    scenarios: tuple[Phase4AReasoningScenario, ...] | None = None,
    *,
    timing_iterations: int = 200,
) -> Phase4ABenchmarkReport:
    selected = scenarios or reasoning_scenarios()
    outcomes = tuple(_run_scenario(item) for item in selected)
    phase3 = run_phase3_benchmark()

    def count(field: str) -> int:
        return sum(bool(getattr(item, field)) for item in outcomes)

    escalations = tuple(
        item for item, scenario in zip(outcomes, selected)
        if scenario.expected_direction == "escalate"
    )
    deescalations = tuple(
        item for item, scenario in zip(outcomes, selected)
        if scenario.expected_direction == "deescalate"
    )
    critical = tuple(item for item in outcomes if item.group == "critical_reconciliation")
    simple = tuple(item for item in outcomes if item.group == "simple")
    unknown = tuple(
        item for item in outcomes if item.scenario_id in {
            "unknown_exact_pair", "unknown_account_model"
        }
    )
    advantage = sum(
        item.acceptable_effort
        and any(not accepted for _, accepted in item.fixed_policy_acceptance)
        for item in outcomes
    )
    regression = sum(
        not item.acceptable_effort
        and any(accepted for _, accepted in item.fixed_policy_acceptance)
        for item in outcomes
    )
    metrics = Phase4AMetrics(
        scenario_count=len(outcomes),
        configurable_pair_scenarios=sum(item.configurable for item in outcomes),
        non_configurable_pair_scenarios=sum(not item.configurable for item in outcomes),
        valid_supported_value_rate=fmean(item.valid_supported_value for item in outcomes),
        unsupported_value_selection_count=sum(not item.valid_supported_value for item in outcomes),
        user_override_violation_count=count("override_violation"),
        provider_default_violation_count=count("provider_default_violation"),
        acceptable_effort_rate=fmean(item.acceptable_effort for item in outcomes),
        under_reasoning_count=count("under_reasoning"),
        unnecessary_high_effort_count=count("unnecessary_high_effort"),
        escalation_correctness_rate=fmean(item.escalation_correct for item in escalations),
        deescalation_correctness_rate=fmean(item.deescalation_correct for item in deescalations),
        critical_reconciliation_high_effort_rate=fmean(item.acceptable_effort for item in critical),
        simple_task_low_or_default_rate=fmean(item.acceptable_effort for item in simple),
        unknown_pair_safe_default_rate=fmean(item.acceptable_effort for item in unknown),
        adaptive_vs_fixed_policy_advantage_count=advantage,
        adaptive_vs_fixed_policy_regression_count=regression,
        unresolved_policy_count=sum(bool(item.decision.unsupported_reason) for item in outcomes if item.configurable),
        critic_candidate_count=sum(item.critic_candidate for item in outcomes),
        critic_unnecessary_count=sum(not item.critic_candidate for item in outcomes),
    )
    return Phase4ABenchmarkReport(
        phase3_report=phase3,
        outcomes=outcomes,
        metrics=metrics,
        timing=_measure_policy(selected, timing_iterations),
    )


__all__ = [
    "Phase4ABenchmarkReport",
    "Phase4AMetrics",
    "Phase4AReasoningOutcome",
    "Phase4AReasoningScenario",
    "PolicyTiming",
    "reasoning_scenarios",
    "run_phase4a_benchmark",
]
