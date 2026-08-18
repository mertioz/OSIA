from __future__ import annotations

import ast
from pathlib import Path

from osia.agent import OSIAAgent
from osia.execution_strategy import build_execution_strategy


_ROOT = Path(__file__).resolve().parents[1]


class _Controller:
    def snapshot(self):
        return {
            "turn_id": "turn-count",
            "proven_postconditions": {},
            "typed_evidence": (),
            "active_attempts": {},
            "permission_state": "allowed",
            "evidence_revision": 1,
        }


class _BrokenController:
    def snapshot(self):
        raise RuntimeError("fixture snapshot failure")


class _ProviderBoundary:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return object()


def _agent():
    agent = object.__new__(OSIAAgent)
    agent.provider = "openai"
    agent.model = "gpt-5.5"
    agent._reasoning_override_for_turn = None
    agent._terminality_controller = _Controller()
    agent._terminality_strategy = build_execution_strategy(
        "Read the current catalog item"
    )
    agent._chatgpt_conversation_id = "conversation-count"
    return agent


def _invoke_existing_boundary(agent, boundary):
    payload = {
        "model": agent.model,
        "messages": [{"role": "user", "content": "fixture"}],
    }
    payload.update(agent._reasoning_request().get("kwargs") or {})
    boundary.create(**payload)


def test_reasoning_gate_does_not_add_provider_invocations(monkeypatch):
    counts = []
    for gate in (None, "1"):
        if gate is None:
            monkeypatch.delenv("OSIA_ADAPTIVE_REASONING_AUTO", raising=False)
        else:
            monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", gate)
        boundary = _ProviderBoundary()
        _invoke_existing_boundary(_agent(), boundary)
        counts.append(len(boundary.calls))

    assert counts == [1, 1]


def test_agent_bridge_error_returns_exact_explicit_legacy_request(monkeypatch):
    from osia.reasoning import build_reasoning_request

    agent = _agent()
    agent._terminality_controller = _BrokenController()
    agent._reasoning_override_for_turn = "high"
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    expected = build_reasoning_request(
        agent.provider,
        agent.model,
        settings={"default": "high", "providers": {}, "models": {}},
    )

    assert agent._reasoning_request() == expected


def test_runtime_bridge_has_no_provider_client_or_critic_dependency():
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

    assert "openai" not in imported
    assert "osia.adaptive_critic" not in imported
    assert all("provider" not in name or name == "osia.capability_adapters" for name in imported)


def test_existing_stream_and_nonstream_provider_boundaries_are_unchanged():
    path = _ROOT / "osia" / "agent.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_reasoning_request"
    ]

    assert len(calls) == 2
    assert source.count(
        "create_kwargs.update(reasoning_request.get(\"kwargs\") or {})"
    ) == 2


def test_benchmark_topology_has_zero_extra_critic_or_evaluator_calls():
    from benchmarks.wor260.phase5a import run_phase5a_benchmark

    topology = run_phase5a_benchmark(timing_iterations=1).metrics.topology
    assert topology.legacy_provider_call_count == topology.adaptive_provider_call_count
    assert topology.additional_provider_call_count == 0
    assert topology.critic_call_count == 0
    assert topology.evaluator_call_count == 0
    assert topology.preflight_model_call_count == 0
