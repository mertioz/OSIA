"""Fresh file calls remain model-led; attempt history stays factual."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import agent as agent_module
from osia.capability_federation import mutation_equivalence_arguments
from osia.tool_attempts import ToolAttemptStore
from osia.tool_contracts import ExecutionOutcome


@contextmanager
def _runtime(monkeypatch, tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(
        "osia.tool_attempts.get_tool_attempt_coordinator",
        lambda: SimpleNamespace(notify_terminal=lambda *_: None),
    )
    agent = agent_module.OSIAAgent()
    sequence = 0

    def dispatch(tool, arguments):
        nonlocal sequence
        sequence += 1
        context = {
            "enabled": True,
            "store": store,
            "conversation_id": "conversation_files",
            "turn_id": "turn_files",
            "request_id": "request_files",
            "tool_call_id": f"call_files_{sequence}",
            "provider_id": "ollama",
            "model_id": "fixture",
        }
        return agent_module._execute_tool_with_policy(
            tool, arguments, attempt_context=context,
            lang="en", allow_retry=False,
        )

    yield store, dispatch


def _physical_write(path, content):
    Path(path).write_text(content, encoding="utf-8")
    return "Successfully wrote file"


def test_repeated_file_calls_dispatch_as_distinct_attempts(monkeypatch, tmp_path):
    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", _physical_write)
    monkeypatch.setitem(
        agent_module.TOOL_MAP, "edit_file_lines", agent_module.tool_edit_file_lines,
    )
    path = tmp_path / "Main.java"
    with _runtime(monkeypatch, tmp_path) as (store, dispatch):
        first = dispatch("write_file", {"path": str(path), "content": "broken"})
        second = dispatch("write_file", {"path": str(path), "content": "fixed"})
        third = dispatch(
            "edit_file_lines",
            {"path": str(path), "start_line": 1, "end_line": 1,
             "new_content": "repaired"},
        )
        repeated = dispatch("write_file", {"path": str(path), "content": "fixed"})

        records = [first, second, third, repeated]
        assert all(item.dispatched for item in records)
        assert len({item.tool_attempt_id for item in records}) == len(records)
        assert path.read_text(encoding="utf-8") == "fixed"
        rows = [store.get_attempt(item.tool_attempt_id) for item in records]
        assert all(row["action_truth"]["dispatch_started_at"] for row in rows)


@pytest.mark.parametrize("next_tool", ["write_file", "edit_file_lines"])
def test_unknown_file_result_does_not_lock_path_or_payload(
    monkeypatch, tmp_path, next_tool,
):
    calls = []

    def unknown(**kwargs):
        calls.append(kwargs)
        return "ERROR: write result lost after dispatch"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", unknown)
    monkeypatch.setitem(
        agent_module.TOOL_MAP, "edit_file_lines",
        lambda **kwargs: calls.append(kwargs) or "edit result returned",
    )
    path = str(tmp_path / "Main.java")
    original = {"path": path, "content": "class Main { broken }"}
    changed = (
        {"path": path, "content": "class Main {}"}
        if next_tool == "write_file"
        else {"path": path, "start_line": 1, "end_line": 1,
              "new_content": "class Main {}"}
    )
    with _runtime(monkeypatch, tmp_path) as (store, dispatch):
        first = dispatch("write_file", original)
        second = dispatch("write_file", original)
        third = dispatch(next_tool, changed)

        assert first.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
        assert second.dispatched and third.dispatched
        assert len({first.tool_attempt_id, second.tool_attempt_id, third.tool_attempt_id}) == 3
        assert len(calls) == 3
        assert store.get_attempt(first.tool_attempt_id)["status"] == "unknown_after_start"


def test_legacy_fingerprint_barriers_do_not_veto_a_fresh_dispatch(monkeypatch, tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    original = {"path": str(tmp_path / "Main.java"), "content": "before"}
    changed = {**original, "content": "after"}
    equivalent = store.fingerprint_arguments(
        mutation_equivalence_arguments("write_file", original)
    )
    legacy = {
        "conversation_id": "conversation_files",
        "turn_id": "turn_files",
        "tool_name": "write_file",
        "argument_fingerprint": store.fingerprint_arguments(original),
        "equivalence_fingerprint": equivalent,
        "dispatch_state": "effect_confirmed",
        "status": "succeeded",
        "completed_at": 1.0,
    }
    monkeypatch.setattr(store, "_terminal_records_and_barriers", lambda: [legacy])

    assert not store.has_dispatch_barrier(
        conversation_id="conversation_files", turn_id="turn_files",
        tool_name="write_file",
        argument_fingerprint=store.fingerprint_arguments(changed),
        equivalence_fingerprint=equivalent,
        block_unknown_effect=False,
    )
