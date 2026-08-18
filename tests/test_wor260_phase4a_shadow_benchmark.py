from pathlib import Path
import json
import subprocess
import sys

import pytest

from benchmarks.wor260.phase4a import run_phase4a_benchmark


@pytest.fixture(scope="module")
def report():
    return run_phase4a_benchmark(timing_iterations=25)


def test_benchmark_preserves_phase3_and_expands_total_to_124(report):
    assert report.phase3_report.metrics.total_scenario_count == 84
    assert report.metrics.scenario_count == 40
    assert report.total_wor260_scenario_count == 124
    assert report.phase3_report.metrics.off_semantic_fidelity_rate == 1.0
    assert report.phase3_report.metrics.on_semantic_fidelity_rate == 1.0


def test_policy_metrics_are_clean_and_deterministic(report):
    metrics = report.metrics
    assert metrics.configurable_pair_scenarios == 36
    assert metrics.non_configurable_pair_scenarios == 4
    assert metrics.valid_supported_value_rate == 1.0
    assert metrics.unsupported_value_selection_count == 0
    assert metrics.user_override_violation_count == 0
    assert metrics.provider_default_violation_count == 0
    assert metrics.acceptable_effort_rate == 1.0
    assert metrics.under_reasoning_count == 0
    assert metrics.unnecessary_high_effort_count == 0
    assert metrics.escalation_correctness_rate == 1.0
    assert metrics.deescalation_correctness_rate == 1.0
    assert metrics.unresolved_policy_count == 0


def test_simple_critical_and_unknown_pair_rates_are_exact(report):
    metrics = report.metrics
    assert metrics.critical_reconciliation_high_effort_rate == 1.0
    assert metrics.simple_task_low_or_default_rate == 1.0
    assert metrics.unknown_pair_safe_default_rate == 1.0


def test_adaptive_policy_beats_fixed_policies_without_policy_regression(report):
    metrics = report.metrics
    assert metrics.adaptive_vs_fixed_policy_advantage_count > 0
    assert metrics.adaptive_vs_fixed_policy_regression_count == 0


def test_policy_timing_is_measured_in_memory_without_artificial_gate(report):
    timing = report.timing
    assert timing.decision_count == 1000
    assert 0 < timing.p50_microseconds <= timing.p95_microseconds <= timing.max_microseconds
    assert timing.mean_microseconds > 0


def test_critic_candidates_are_rare_and_phase4b_is_not_implemented(report):
    assert report.metrics.critic_candidate_count == 8
    assert report.metrics.critic_unnecessary_count == 32
    assert report.metrics.critic_candidate_count < report.metrics.critic_unnecessary_count


def test_adaptive_reasoning_imports_are_confined_to_phase5a_bridge():
    offenders = []
    for path in Path("osia").rglob("*.py"):
        if path.name == "adaptive_reasoning.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "adaptive_reasoning" in text:
            offenders.append(path.as_posix())
    assert sorted(offenders) == [
        "osia/adaptive_reasoning_runtime.py",
        "osia/agent.py",
    ]
    agent_text = Path("osia/agent.py").read_text(encoding="utf-8")
    assert "OSIA_ADAPTIVE_REASONING_AUTO" in agent_text


def test_importing_phase4a_preserves_prompt_tools_and_production_reasoning_bytes():
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
import osia.adaptive_reasoning
import benchmarks.wor260.phase4a
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
