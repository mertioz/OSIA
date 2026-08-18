from __future__ import annotations

import json
import subprocess
import sys

from benchmarks.wor260.phase4b import (
    CriticUsefulness,
    EvaluatorFixture,
    critic_scenarios,
    run_phase4b_benchmark,
)


def test_phase4b_expands_accepted_benchmark_to_164_scenarios() -> None:
    report = run_phase4b_benchmark(timing_iterations=1)
    assert report.metrics.phase4b_scenario_count == 40
    assert report.metrics.total_wor260_scenario_count == 164
    assert report.phase4a_report.metrics.scenario_count == 40
    assert report.phase4a_report.phase3_report.metrics.total_scenario_count == 84


def test_independent_usefulness_oracle_split_is_explicit() -> None:
    scenarios = critic_scenarios()
    assert sum(item.usefulness == CriticUsefulness.REQUIRED for item in scenarios) == 8
    assert sum(item.usefulness == CriticUsefulness.OPTIONAL for item in scenarios) == 2
    assert sum(item.usefulness == CriticUsefulness.UNNECESSARY for item in scenarios) == 30


def test_trigger_quality_is_perfect_on_the_deterministic_oracle() -> None:
    metrics = run_phase4b_benchmark(timing_iterations=1).metrics
    assert (metrics.trigger_true_positive, metrics.trigger_true_negative) == (10, 30)
    assert (metrics.trigger_false_positive, metrics.trigger_false_negative) == (0, 0)
    assert metrics.trigger_precision == 1.0
    assert metrics.trigger_recall == 1.0
    assert metrics.trigger_specificity == 1.0
    assert metrics.invocation_rate == 0.25
    assert metrics.unnecessary_invocation_rate == 0.0
    assert metrics.missed_useful_critic_rate == 0.0


def test_shadow_evaluator_fixtures_cover_required_failure_modes() -> None:
    fixtures = {item.fixture for item in critic_scenarios()}
    assert {
        EvaluatorFixture.HELPFUL,
        EvaluatorFixture.NO_OP,
        EvaluatorFixture.WRONG_CANDIDATE,
        EvaluatorFixture.STALE,
        EvaluatorFixture.MALICIOUS,
        EvaluatorFixture.OVERCONFIDENT,
        EvaluatorFixture.INSUFFICIENT_EVIDENCE,
    }.issubset(fixtures)


def test_validation_and_utility_metrics_have_no_regression() -> None:
    metrics = run_phase4b_benchmark(timing_iterations=1).metrics
    assert metrics.helpful_evaluation_acceptance_rate == 1.0
    assert metrics.no_benefit_suppression_rate == 1.0
    assert metrics.harmful_evaluation_rejection_rate == 1.0
    assert metrics.stale_evaluation_rejection_rate == 1.0
    assert metrics.malformed_evaluation_rejection_rate == 1.0
    assert metrics.hard_constraint_override_rejection_rate == 1.0
    assert metrics.candidate_selection_improvement_count == 5
    assert metrics.candidate_selection_regression_count == 0
    assert metrics.observation_request_improvement_count == 5
    assert metrics.unnecessary_observation_count == 0


def test_rare_adaptive_architecture_dominates_always_invoked_calls() -> None:
    comparisons = {
        item.architecture: item
        for item in run_phase4b_benchmark(timing_iterations=1).architecture_comparison
    }
    assert comparisons["no_critic"].theoretical_extra_provider_call_count == 0
    assert comparisons["critic_always"].theoretical_extra_provider_call_count == 40
    assert comparisons["rare_adaptive_critic"].theoretical_extra_provider_call_count == 10
    assert comparisons["critic_always"].unnecessary_invocation_count == 30
    assert comparisons["rare_adaptive_critic"].unnecessary_invocation_count == 0
    assert comparisons["rare_adaptive_critic"].useful_evaluation_coverage_count == 10
    assert comparisons["rare_adaptive_critic"].final_advisory_decision_correct_count == 40


def test_trigger_and_validation_timing_are_in_memory_only() -> None:
    report = run_phase4b_benchmark(timing_iterations=2)
    assert report.trigger_timing.operation_count == 80
    assert report.validation_timing.operation_count == 80
    assert report.trigger_timing.mean_microseconds > 0
    assert report.validation_timing.mean_microseconds > 0


def test_importing_phase4b_preserves_prompt_tools_and_reasoning_behavior() -> None:
    script = r'''
import hashlib
import json
from dataclasses import asdict
from osia.capability_federation import federated_capabilities
from osia.reasoning import build_reasoning_request
from osia.tool_contracts import dynamic_tool_specs, static_tool_prompt_block

def snapshot():
    prompt = static_tool_prompt_block().encode("utf-8")
    return {
        "chat": tuple(asdict(item) for item in federated_capabilities("openai")),
        "account": tuple(asdict(item) for item in federated_capabilities("chatgpt_account")),
        "dynamic": tuple(item.name for item in dynamic_tool_specs()),
        "prompt_bytes": len(prompt),
        "prompt_lines": len(prompt.decode("utf-8").splitlines()),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "explicit": build_reasoning_request(
            "openai", "gpt-5.5",
            {"default": "auto", "providers": {}, "models": {"gpt-5.5": "high"}},
        ),
        "auto": build_reasoning_request(
            "openai", "gpt-5.5",
            {"default": "auto", "providers": {}, "models": {}},
        ),
        "unsupported": build_reasoning_request(
            "xai", "grok-3",
            {"default": "high", "providers": {}, "models": {}},
        ),
    }

before = snapshot()
import osia.adaptive_critic
import benchmarks.wor260.phase4b
after = snapshot()
print(json.dumps({
    "equal": before == after,
    "chat_count": len(after["chat"]),
    "account_count": len(after["account"]),
    "dynamic_count": len(after["dynamic"]),
    "prompt_bytes": after["prompt_bytes"],
    "prompt_lines": after["prompt_lines"],
    "prompt_sha256": after["prompt_sha256"],
}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "equal": True,
        "chat_count": 95,
        "account_count": 98,
        "dynamic_count": 0,
        "prompt_bytes": 11_609,
        "prompt_lines": 97,
        "prompt_sha256": "7bc8974fe969caedfb5a48387c0326d5b1d021be60442f77ac4f51ed4769170a",
    }
