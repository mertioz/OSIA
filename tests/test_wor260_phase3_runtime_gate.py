from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from osia.adaptive_runtime import (
    AdaptiveContinuationProposal,
    PHASE3_GATE_ENV,
    adaptive_intelligence_phase3_enabled,
    phase3_continuation_context,
)
from osia.execution_strategy import build_execution_strategy


_ROOT = Path(__file__).resolve().parents[1]


def _snapshots(mission):
    node_id = mission.action_nodes[0].node_id
    strategy = {
        "mission": {
            "action_nodes": ({"node_id": node_id, "status": "pending"},),
        },
        "attempts": (), "mechanisms": (),
        "alternatives_still_applicable": ("api:read",),
    }
    terminality = {
        "turn_id": "turn-gate", "proven_postconditions": {},
        "typed_evidence": (), "active_attempts": {},
        "permission_state": "allowed", "evidence_revision": 1,
    }
    return terminality, strategy


def test_feature_gate_is_default_off_and_legacy_context_is_byte_exact(monkeypatch):
    monkeypatch.delenv(PHASE3_GATE_ENV, raising=False)
    legacy = "legacy continuation\nwith exact bytes: \u00e9"

    result = phase3_continuation_context(
        legacy,
        semantic_mission=object(), terminality_snapshot={},
        strategy_snapshot={}, legacy_next_strategy="legacy",
        legacy_postcondition="unchanged",
    )

    assert adaptive_intelligence_phase3_enabled() is False
    assert result == legacy
    assert result.encode("utf-8") == legacy.encode("utf-8")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_feature_gate_accepts_only_explicit_true_values(monkeypatch, value):
    monkeypatch.setenv(PHASE3_GATE_ENV, value)
    assert adaptive_intelligence_phase3_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "enabled", "phase3"])
def test_feature_gate_rejects_every_non_true_value(monkeypatch, value):
    monkeypatch.setenv(PHASE3_GATE_ENV, value)
    assert adaptive_intelligence_phase3_enabled() is False


def test_enabled_bridge_appends_bounded_advisory_only(monkeypatch):
    monkeypatch.setenv(PHASE3_GATE_ENV, "1")
    mission = build_execution_strategy("Read the current catalog item").mission
    terminality, strategy = _snapshots(mission)
    proposal = AdaptiveContinuationProposal(
        selected_plan=None,
        selected_next_capability="api:read",
        selected_strategy_family="direct_api",
        required_observation="",
        unresolved_constraints=(),
        fallback_to_legacy=False,
        explanation_codes=("fixture",),
        remaining_goal_node_ids=(mission.action_nodes[0].node_id,),
    )
    monkeypatch.setattr(
        "osia.adaptive_runtime.evaluate_adaptive_continuation",
        lambda *args, **kwargs: proposal,
    )
    legacy = "legacy continuation"

    result = phase3_continuation_context(
        legacy,
        semantic_mission=mission, terminality_snapshot=terminality,
        strategy_snapshot=strategy, legacy_next_strategy="api:read",
        legacy_postcondition="item is visible", provider="openai",
        model_id="fixture-model",
    )

    assert result.startswith(legacy + "\n")
    assert "adaptive_phase3_next_strategy_hint" in result
    assert "Advisory only" in result
    assert "api:read" in result
    assert "tool_call" not in result.casefold()


def test_enabled_bridge_returns_exact_legacy_on_internal_failure(monkeypatch):
    monkeypatch.setenv(PHASE3_GATE_ENV, "1")
    legacy = "legacy continuation"
    result = phase3_continuation_context(
        legacy,
        semantic_mission=object(), terminality_snapshot={},
        strategy_snapshot={}, legacy_next_strategy="legacy",
        legacy_postcondition="unchanged",
    )
    assert result == legacy


def test_agent_integration_is_confined_to_the_authorized_terminal_loop():
    path = _ROOT / "osia" / "agent.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_drive_terminal_turn"
    )
    segment = ast.get_source_segment(source, target) or ""

    assert "phase3_continuation_context(" not in segment
    assert "adaptive_success_eligible" not in segment
    # WOR-303: continuation binding is centralized in apply_continuation; the
    # canonical driver owns every same-turn continuation.
    assert segment.count("apply_continuation(") >= 8
    assert segment.count(
        "segment_input = continuation_context(decision)"
    ) == 2
    assert segment.count("while True:") == 1
    assert "controller.evaluate(" in segment
    assert "controller.mark_terminal" not in segment
    assert "dispatch" not in segment.casefold()


def test_phase3_sidecars_have_no_dispatch_or_terminality_authority_imports():
    for relative in (
        "osia/world_state.py", "osia/verifier.py", "osia/adaptive_runtime.py",
    ):
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            node.module or ""
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "osia.agent" not in imported
        assert "osia.execution_strategy" not in imported
        assert "osia.turn_terminality" not in imported


def test_gate_off_phase3_imports_preserve_provider_prompt_and_tool_identity():
    script = r'''
import hashlib
import json
from dataclasses import asdict
from osia.capability_federation import federated_capabilities
from osia.mcp_client.registry import registered_names
from osia.tool_contracts import STATIC_TOOL_REGISTRY, dynamic_tool_specs, static_tool_prompt_block

def snapshot():
    prompt = static_tool_prompt_block().encode("utf-8")
    return {
        "static": STATIC_TOOL_REGISTRY.names(),
        "dynamic": tuple(item.name for item in dynamic_tool_specs()),
        "mcp": tuple(registered_names()),
        "chat": tuple(asdict(item) for item in federated_capabilities("openai")),
        "account": tuple(asdict(item) for item in federated_capabilities("chatgpt_account")),
        "prompt_bytes": len(prompt),
        "prompt_lines": len(prompt.decode("utf-8").splitlines()),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
    }

before = snapshot()
import osia.world_state
import osia.verifier
import osia.adaptive_runtime
import benchmarks.wor260.phase3
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
    env = dict(os.environ)
    env.pop(PHASE3_GATE_ENV, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=_ROOT, env=env,
        check=True, capture_output=True, text=True,
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
