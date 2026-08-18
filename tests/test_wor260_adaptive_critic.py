from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, is_dataclass, replace
from pathlib import Path

import pytest

from benchmarks.wor260.phase4b import critic_scenarios
from osia.adaptive_critic import (
    CriticEvaluation,
    CriticEvaluationValidation,
    CriticRequest,
    CriticTriggerContext,
    CriticTriggerDecision,
    determine_critic_trigger,
    public_structure_field_names,
)


def test_phase4b_public_structures_are_immutable_dataclasses() -> None:
    structures = (
        CriticTriggerContext,
        CriticTriggerDecision,
        CriticRequest,
        CriticEvaluation,
        CriticEvaluationValidation,
    )
    assert all(is_dataclass(item) for item in structures)
    assert all(item.__dataclass_params__.frozen for item in structures)


def test_trigger_context_is_actually_frozen() -> None:
    context = critic_scenarios()[0].context
    with pytest.raises(FrozenInstanceError):
        context.goal_node_id = "different"  # type: ignore[misc]


def test_context_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="counts_must_be_non_negative"):
        replace(critic_scenarios()[0].context, replan_count=-1)


def test_context_rejects_impossible_close_candidate_count() -> None:
    with pytest.raises(ValueError, match="close_candidates_exceed"):
        replace(
            critic_scenarios()[0].context,
            planner_candidate_count=1,
            close_candidate_count=2,
        )


def test_context_and_decision_have_no_private_reasoning_fields() -> None:
    names = public_structure_field_names()
    forbidden = ("chain_of_thought", "private_reasoning", "hidden_reasoning", "cot")
    flattened = " ".join(
        field_name for fields in names.values() for field_name in fields
    ).lower()
    assert not any(item in flattened for item in forbidden)


def test_phase4b_module_has_no_provider_network_or_dispatch_calls() -> None:
    path = Path(__file__).parents[1] / "osia" / "adaptive_critic.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_imports = {
        "requests",
        "httpx",
        "openai",
        "anthropic",
        "socket",
        "osia.agent",
        "osia.server.server",
    }
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert imports.isdisjoint(forbidden_imports)
    assert calls.isdisjoint(
        {"dispatch", "call_model", "provider_critic", "evaluator_provider"}
    )


def test_no_production_module_imports_adaptive_critic() -> None:
    root = Path(__file__).parents[1] / "osia"
    importers = []
    for path in root.rglob("*.py"):
        if path.name == "adaptive_critic.py":
            continue
        if "adaptive_critic" in path.read_text(encoding="utf-8", errors="ignore"):
            importers.append(path)
    assert importers == []


def test_trigger_is_deterministic() -> None:
    for scenario in critic_scenarios():
        assert determine_critic_trigger(scenario.context) == determine_critic_trigger(
            scenario.context
        )
