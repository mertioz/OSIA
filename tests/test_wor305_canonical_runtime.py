"""New Chat has one runtime; legacy V2 diagnostics are recovery-only."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from osia import agent as agents
from osia import legacy_v2_recovery as legacy_v2
from osia import turn_terminality


class CanonicalPathReached(Exception):
    pass


def _entry_agent():
    agent = agents.OSIAAgent()
    agent._bind_canonical_turn_execution_state = lambda _turn_id: None
    return agent


def _binding(turn_id):
    return {
        "conversation_id": "legacy-conversation",
        "request_id": "legacy-request",
        "turn_id": turn_id,
        "run_id": "legacy-run",
        "provider_id": "chatgpt_account",
        "model_id": "legacy-selected-model",
        "effective_reasoning": "auto",
    }


def test_private_v2_environment_cannot_divert_a_new_chat_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("OSIA_PRIVATE_SEMANTIC_V2_TEST", "1")
    monkeypatch.setattr(turn_terminality, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(legacy_v2, "CONFIG_DIR", str(tmp_path))
    calls = []

    def enter_canonical_driver(self, controller, strategy, *, initial_input, **_kwargs):
        calls.append((initial_input, strategy))
        assert strategy is None
        raise CanonicalPathReached

    monkeypatch.setattr(agents.OSIAAgent, "_drive_terminal_turn", enter_canonical_driver)
    with pytest.raises(CanonicalPathReached):
        _entry_agent().run_terminal_turn("create a project", turn_id="new-turn")
    assert calls == [("create a project", None)]


def test_exact_persisted_legacy_turn_does_not_fall_through_to_v1(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_terminality, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(legacy_v2, "CONFIG_DIR", str(tmp_path))
    path = legacy_v2._diagnostic_path("interrupted-v2")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "execution_runtime": legacy_v2.RUNTIME,
        "binding": _binding("interrupted-v2"),
        "status": "dispatch_claimed",
    }), encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy execution entered a new Chat path")

    monkeypatch.setattr(agents._execution_strategy, "build_execution_strategy", forbidden)
    with pytest.raises(RuntimeError, match="V1 fallback forbidden"):
        _entry_agent().run_terminal_turn(
            "original objective", turn_id="interrupted-v2",
        )
    assert path.is_file()


def test_legacy_reader_projects_only_an_unverified_interruption(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_terminality, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(legacy_v2, "CONFIG_DIR", str(tmp_path))
    path = legacy_v2._diagnostic_path("legacy-recovery")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "execution_runtime": legacy_v2.RUNTIME,
        "binding": _binding("legacy-recovery"),
        "boundary": "legacy_v2_interrupted",
    }), encoding="utf-8")

    controller = legacy_v2.recover_interrupted_v2_final(
        "legacy-recovery", "original objective", _binding("legacy-recovery"),
    )
    state = controller.snapshot()
    assert state["execution_runtime"] == legacy_v2.RUNTIME
    assert state["terminal_state"] == "blocked_verified"
    assert state["typed_evidence"] == []
    assert "unverified" in state["final_reservation"]["final_text"].lower()


def test_no_new_v2_runtime_admission_or_static_execution_loop():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "osia" / "main.py").read_text(encoding="utf-8"))
    calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "admit_local_desktop_text" not in calls
    source = inspect.getsource(agents.OSIAAgent.run_terminal_turn)
    assert "select_local_runtime" not in source
    assert "run_admitted_v2_turn" not in source
    assert "known_v2_turn" in source
    assert not hasattr(legacy_v2, "run_admitted_v2_turn")
    assert not hasattr(legacy_v2, "select_local_runtime")
