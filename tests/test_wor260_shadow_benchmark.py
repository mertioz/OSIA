from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from benchmarks.wor260.phase2 import (
    ShadowBenchmarkMetrics,
    benchmark_scenarios,
    run_shadow_benchmark,
)


@pytest.fixture(scope="module")
def report():
    return run_shadow_benchmark()


def test_phase2_executes_43_high_signal_scenarios_and_retains_164_target(report):
    assert len(benchmark_scenarios()) == 43
    assert report.metrics.scenario_count == 43
    assert report.target_scenario_count == 164
    assert len({item.scenario_id for item in report.outcomes}) == 43


def test_required_phase2_coverage_categories_are_present():
    categories = {
        category
        for scenario in benchmark_scenarios()
        for category in scenario.categories
    }
    assert {
        "direct_api", "mcp", "provider_native", "availability",
        "surface_fidelity", "personal_browser", "identity",
        "desktop", "uia", "vision", "keyboard", "deep_link",
        "stale_ref", "outcome_unknown", "reconciliation",
        "permissions", "multi_step", "two_apps", "ordering",
        "read_only", "partial_success", "retrieval", "top_k",
        "full_scan", "deterministic_route", "discover", "observe", "no_plan",
        "verification", "false_success",
    }.issubset(categories)


def test_shadow_metrics_prove_semantic_and_security_gates(report):
    metrics = report.metrics
    assert metrics.plan_found_rate == pytest.approx(37 / 43)
    assert metrics.semantic_fidelity_rate == 1.0
    assert metrics.false_success_rate == 0.0
    assert metrics.hard_constraint_violation_count == 0
    assert metrics.permission_violation_count == 0
    assert metrics.identity_violation_count == 0
    assert metrics.outcome_unknown_replay_violation_count == 0
    assert metrics.modality_violation_count == 0
    assert metrics.retrieval_false_negative_count == 0
    assert metrics.full_scan_recovery_count == 1
    assert metrics.average_initial_candidates > 0
    assert metrics.average_eligible_candidates > 0
    assert metrics.average_plan_steps > 0


def test_legacy_shadow_comparison_counts_and_lists_are_exhaustive(report):
    metrics = report.metrics
    assert metrics.legacy_shadow_agreement_rate == pytest.approx(23 / 43)
    assert metrics.plausibly_better_count == 11
    assert metrics.semantically_equivalent_divergence_count == 1
    assert metrics.worse_count == 0
    assert metrics.unresolved_count == 6
    assert metrics.impossible_to_compare_count == 2
    assert len(report.plausibly_better_scenario_ids) == metrics.plausibly_better_count
    assert len(report.semantically_equivalent_divergence_scenario_ids) == (
        metrics.semantically_equivalent_divergence_count
    )
    assert len(report.worse_scenario_ids) == metrics.worse_count
    assert len(report.unresolved_scenario_ids) == metrics.unresolved_count
    assert len(report.impossible_to_compare_scenario_ids) == (
        metrics.impossible_to_compare_count
    )


def test_every_plausibly_better_divergence_is_named_and_factual(report):
    assert report.plausibly_better_scenario_ids == (
        "direct_api_exact_vs_ui",
        "exact_mcp_vs_browser",
        "direct_unavailable_fallback",
        "wrong_account_direct_rejected",
        "steam_library_vs_store",
        "native_required_vs_web_surface",
        "exact_deep_link",
        "global_coordinates_last_resort",
        "outcome_unknown_blocks_mutation_replay",
        "read_only_reconciliation_preferred",
        "unavailable_direct_does_not_hide_fallback",
    )


def test_every_equivalent_worse_unresolved_and_impossible_divergence_is_named(report):
    assert report.semantically_equivalent_divergence_scenario_ids == (
        "provider_native_not_artificially_demoted",
    )
    assert report.worse_scenario_ids == ()
    assert report.unresolved_scenario_ids == (
        "personal_browser_wrong_tab",
        "personal_browser_unknown_authority",
        "target_missing_nonvision",
        "stale_ref_no_direct_actuation",
        "permission_disallowed",
        "no_plan_only_after_full_scan",
    )
    assert report.impossible_to_compare_scenario_ids == (
        "vision_observation_then_uia_action",
        "full_scan_recovery",
    )


def test_full_scan_occurs_before_no_plan_and_records_recovery(report):
    by_id = {item.scenario_id: item for item in report.outcomes}
    no_plan = by_id["no_plan_only_after_full_scan"].result
    assert no_plan.selected_plan is None
    assert all(item.full_scan_performed for item in no_plan.search_evidence)

    recovered = by_id["full_scan_recovery"].result
    assert recovered.selected_plan is not None
    assert any(
        item.full_scan_recovered_ids == ("opaque:7f3a",)
        for item in recovered.search_evidence
    )


def test_adversarial_scenarios_all_pass_deterministic_grader(report):
    required = {
        "steam_library_vs_store",
        "global_coordinates_last_resort",
        "provider_success_needs_exact_verify",
        "wrong_account_direct_rejected",
        "permission_unknown",
        "unknown_availability_vs_proven_available",
        "outcome_unknown_blocks_mutation_replay",
        "target_missing_nonvision",
        "personal_browser_unknown_authority",
        "stale_ref_no_direct_actuation",
        "no_plan_only_after_full_scan",
    }
    outcomes = {item.scenario_id: item for item in report.outcomes}
    assert required.issubset(outcomes)
    assert all(outcomes[item].semantic_fidelity for item in required)
    assert not any(outcomes[item].false_success for item in required)


def test_metrics_do_not_invent_latency_token_or_provider_cost_savings():
    names = {item.name for item in fields(ShadowBenchmarkMetrics)}
    assert {
        "latency_savings", "token_savings", "provider_cost_savings",
        "reliability_improvement",
    }.isdisjoint(names)


def test_no_production_runtime_imports_or_calls_wor260_sidecars():
    repo = Path(__file__).resolve().parents[1]
    sidecars = {
        repo / "osia" / "goal_contract.py",
        repo / "osia" / "capability_adapters.py",
        repo / "osia" / "capability_search.py",
        repo / "osia" / "adaptive_planner.py",
        repo / "osia" / "adaptive_reasoning_runtime.py",
    }
    forbidden_modules = {
        "osia.goal_contract",
        "osia.capability_adapters",
        "osia.capability_search",
        "osia.adaptive_planner",
    }
    violations: list[str] = []
    for path in (repo / "osia").rglob("*.py"):
        if path in sidecars:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        if imports.intersection(forbidden_modules):
            violations.append(str(path.relative_to(repo)))
    assert violations == []


def test_importing_phase2_sidecars_is_observational_in_fresh_process():
    repo = Path(__file__).resolve().parents[1]
    script = r'''
import hashlib
import json
from dataclasses import asdict, fields
from osia.capability_federation import CapabilityDescriptor, federated_capabilities
from osia.mcp_client.registry import registered_names
from osia.tool_contracts import STATIC_TOOL_REGISTRY, dynamic_tool_specs, static_tool_prompt_block

def snapshot():
    prompt = static_tool_prompt_block().encode("utf-8")
    openai = federated_capabilities("openai")
    account = federated_capabilities("chatgpt_account")
    return {
        "static_names": STATIC_TOOL_REGISTRY.names(),
        "dynamic_names": tuple(item.name for item in dynamic_tool_specs()),
        "mcp_names": tuple(registered_names()),
        "openai": tuple(asdict(item) for item in openai),
        "account": tuple(asdict(item) for item in account),
        "descriptor_fields": tuple(item.name for item in fields(CapabilityDescriptor)),
        "prompt_bytes": len(prompt),
        "prompt_lines": len(prompt.decode("utf-8").splitlines()),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
    }

before = snapshot()
import osia.capability_search
import osia.adaptive_planner
import benchmarks.wor260.phase2
after = snapshot()
print(json.dumps({
    "equal": before == after,
    "chat_count": len(after["openai"]),
    "account_count": len(after["account"]),
    "dynamic_count": len(after["dynamic_names"]),
    "prompt_bytes": after["prompt_bytes"],
    "prompt_lines": after["prompt_lines"],
    "prompt_sha256": after["prompt_sha256"],
}))
'''
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "equal": True,
        "chat_count": 95,
        "account_count": 98,
        "dynamic_count": 0,
        "prompt_bytes": 11_609,
        "prompt_lines": 97,
        "prompt_sha256": "7bc8974fe969caedfb5a48387c0326d5b1d021be60442f77ac4f51ed4769170a",
    }


def test_phase2_removal_requires_no_feature_flag_or_migration():
    repo = Path(__file__).resolve().parents[1]
    sources = "\n".join(
        (repo / relative).read_text(encoding="utf-8")
        for relative in (
            "osia/capability_search.py",
            "osia/adaptive_planner.py",
            "benchmarks/wor260/phase2.py",
        )
    )
    assert "FEATURE_FLAG" not in sources
    assert "feature_flag" not in sources
    assert "migration_required" not in sources
    assert "config/settings" not in sources
