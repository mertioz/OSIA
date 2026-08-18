from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from benchmarks.wor260.phase4a import reasoning_scenarios
from benchmarks.wor260.phase5a import run_phase5a_benchmark
from osia.adaptive_reasoning_runtime import (
    ADAPTIVE_REASONING_BINDING_SCHEMA,
    BoundReasoningContext,
    integrate_adaptive_reasoning,
    public_structure_field_names,
)
from osia.reasoning import build_reasoning_request


_ROOT = Path(__file__).resolve().parents[1]


def _high_context():
    scenario = next(
        item for item in reasoning_scenarios()
        if item.scenario_id == "critical_equivalent_mutations"
    )
    bound = BoundReasoningContext(
        schema=ADAPTIVE_REASONING_BINDING_SCHEMA,
        turn_id="turn-adversarial",
        revision=9,
        goal_fingerprint="goal-adversarial",
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        conversation_id="conversation-adversarial",
        source="phase3_planner",
        decision_context=scenario.context,
    )
    return scenario, bound


def test_high_complexity_has_no_permission_identity_or_dispatch_output(monkeypatch):
    scenario, bound = _high_context()
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": "auto", "providers": {}, "models": {}},
    )
    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=bound,
        turn_id=bound.turn_id,
        revision=bound.revision,
        conversation_id=bound.conversation_id,
    )

    serialized = repr(result.request).casefold()
    assert result.applied is True
    assert all(token not in serialized for token in (
        "permission_grant", "identity_override", "dispatch_action",
        "terminal_state", "vision_enabled", "outcome_replay",
    ))


def test_adaptive_policy_error_returns_exact_legacy(monkeypatch):
    scenario, bound = _high_context()
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": "auto", "providers": {}, "models": {}},
    )
    monkeypatch.setattr(
        "osia.adaptive_reasoning_runtime.determine_reasoning_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fixture")),
    )

    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=bound,
        turn_id=bound.turn_id,
        revision=bound.revision,
        conversation_id=bound.conversation_id,
    )

    assert result.applied is False
    assert result.reason_code == "adaptive_error_legacy_fallback"
    assert result.request == legacy


def test_malformed_gate_and_unsupported_explicit_value_fail_safe(monkeypatch):
    scenario, bound = _high_context()
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": "ultra", "providers": {}, "models": {}},
    )
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "malformed")

    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=bound,
        turn_id=bound.turn_id,
        revision=bound.revision,
        conversation_id=bound.conversation_id,
    )

    assert result.applied is False
    assert result.request == legacy
    assert result.request["kwargs"] == {}


def test_context_schema_has_no_private_reasoning_or_persistence_fields():
    names = set().union(*map(set, public_structure_field_names().values()))
    forbidden = {
        "prompt", "messages", "chain_of_thought", "cot", "raw_reasoning",
        "database", "config_path", "migration", "provider_response",
    }
    assert names.isdisjoint(forbidden)


def test_sidecar_imports_no_authority_persistence_or_critic_modules():
    path = _ROOT / "osia" / "adaptive_reasoning_runtime.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {
        node.module or ""
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = {
        "osia.adaptive_critic", "osia.permission_policy", "osia.permission_store",
        "osia.turn_terminality", "osia.execution_strategy", "sqlite3",
    }
    assert imported.isdisjoint(forbidden)


def test_all_security_and_authority_benchmark_gates_are_zero():
    report = run_phase5a_benchmark(timing_iterations=1)
    correctness = report.metrics.correctness
    topology = report.metrics.topology

    assert not any((
        correctness.false_success_count,
        correctness.incorrect_blocked_count,
        correctness.hard_violation_count,
        correctness.permission_violation_count,
        correctness.identity_violation_count,
        correctness.replay_violation_count,
        correctness.modality_violation_count,
        report.metrics.reasoning_policy.unsupported_value_emission_count,
        report.metrics.reasoning_policy.explicit_override_violation_count,
        report.metrics.context_isolation_failure_count,
        report.metrics.gate_off_parity_failure_count,
        topology.additional_provider_call_count,
        topology.critic_call_count,
        topology.evaluator_call_count,
        topology.preflight_model_call_count,
    ))
    assert report.metrics.classification == "READY_FOR_CONTROLLED_PHYSICAL_PILOT"


def test_stale_context_cannot_be_repaired_by_high_reasoning(monkeypatch):
    scenario, bound = _high_context()
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    legacy = build_reasoning_request(
        scenario.capability.provider_id,
        scenario.capability.model_id,
        settings={"default": "auto", "providers": {}, "models": {}},
    )

    result = integrate_adaptive_reasoning(
        legacy,
        provider_id=scenario.capability.provider_id,
        model_id=scenario.capability.model_id,
        bound_context=replace(bound, revision=8),
        turn_id=bound.turn_id,
        revision=9,
        conversation_id=bound.conversation_id,
    )

    assert result.applied is False
    assert result.reason_code == "context_binding_mismatch"
    assert result.request == legacy
