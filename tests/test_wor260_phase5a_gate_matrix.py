from __future__ import annotations

import ast
from pathlib import Path

import pytest

from benchmarks.wor260.phase5a import run_phase5a_benchmark
from osia.adaptive_reasoning_runtime import adaptive_reasoning_auto_enabled


_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_reasoning_gate_accepts_only_explicit_true_values(monkeypatch, value):
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", value)
    assert adaptive_reasoning_auto_enabled() is True


@pytest.mark.parametrize(
    "value", [None, "", "0", "false", "enabled", "adaptive", "malformed"],
)
def test_reasoning_gate_absent_false_or_malformed_is_off(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OSIA_ADAPTIVE_REASONING_AUTO", raising=False)
    else:
        monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", value)
    assert adaptive_reasoning_auto_enabled() is False


def test_gate_matrix_isolated_behavior_is_deterministic():
    report = run_phase5a_benchmark(timing_iterations=1)
    by_id = {item.scenario_id: item for item in report.outcomes}

    assert by_id["off_off_exact_legacy"].payload_diff_paths == ()
    assert by_id["planner_on_reasoning_off"].payload_diff_paths == ()
    assert by_id["planner_off_reasoning_on"].reasoning_enabled is True
    assert by_id["planner_off_reasoning_on"].applied is True
    assert by_id["planner_on_reasoning_on"].planner_enabled is True
    assert by_id["planner_on_reasoning_on"].applied is True


def test_production_default_is_not_persisted_or_exposed_in_ui():
    for relative in ("config", "osia/gui", "osia/server"):
        root = _ROOT / relative
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json", ".html", ".js"}:
                assert "OSIA_ADAPTIVE_REASONING_AUTO" not in path.read_text(
                    encoding="utf-8", errors="ignore",
                )


def test_agent_changes_are_confined_to_two_authorized_functions():
    path = _ROOT / "osia" / "agent.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "OSIA_ADAPTIVE_REASONING_AUTO" in functions["_reasoning_request"]
    assert "OSIA_ADAPTIVE_REASONING_AUTO" in functions["_drive_terminal_turn"]
    assert source.count('"OSIA_ADAPTIVE_REASONING_AUTO"') == 2
    assert functions["_drive_terminal_turn"].count(
        "phase3_continuation_context("
    ) == 1
    assert functions["_drive_terminal_turn"].count(
        "segment_input = continuation_context(decision)"
    ) == 3
