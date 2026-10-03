"""A past successful write is not proof of the resource's current version."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from osia import agent as agent_module
from osia.tool_contracts import ExecutionOutcome


@pytest.mark.parametrize("durable", [False, True])
def test_write_a_b_a_never_returns_stale_success(monkeypatch, tmp_path, capsys, durable):
    import osia.tool_attempts as attempts
    monkeypatch.setattr(attempts, "CONFIG_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(attempts, "_DEFAULT_STORE", None)
    monkeypatch.setattr(attempts, "_DEFAULT_COORDINATOR", None)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path / "runtime"))
    target = tmp_path / "subject.txt"
    responses = ["<tool_call>" + json.dumps({
        "tool": "write_file", "path": str(target), "content": content,
    }) + "</tool_call>" for content in ("A", "B", "A")]
    responses.append("Stopped after the scripted sequence.")

    class ScriptedClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.calls = 0

        def create(self, **_kwargs):
            if self.calls >= len(responses):
                raise RuntimeError("Bounded provider fixture exhausted")
            content = responses[self.calls]
            self.calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None))])

    agent = agent_module.OSIAAgent(lang="en")
    agent.provider, agent.model, agent.client = "ollama", "fixture", ScriptedClient()
    monkeypatch.setattr(agent, "_resolve_route", lambda _: ("medium", []))
    if durable:
        agent.configure_tool_attempts(conversation_id="aba", turn_id="aba",
            request_id="aba", final_response_message_id="aba-assistant")
    physical_writes, receipts = [], []
    write = agent_module.TOOL_MAP["write_file"]
    execute = agent_module._execute_tool_with_policy

    def tracked_write(**kwargs):
        physical_writes.append(kwargs["content"])
        return write(**kwargs)

    def tracked_execute(*args, **kwargs):
        record = execute(*args, **kwargs)
        receipts.append(record)
        return record

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", tracked_write)
    monkeypatch.setattr(agent_module, "_execute_tool_with_policy", tracked_execute)
    agent.run("Write A, then B, then A to the scratch file.", history=[], canonical_output=False)
    assert len(receipts) == 3, "An old cached receipt bypassed the third dispatch decision"
    assert "same-turn completed result reused: write_file" not in capsys.readouterr().out
    if target.read_text(encoding="utf-8") == "A":
        assert physical_writes == ["A", "B", "A"]
        assert receipts[-1].dispatched and not receipts[-1].is_error
    else:
        assert durable, "Non-durable writes should actually restore A"
        assert target.read_text(encoding="utf-8") == "B"
        assert physical_writes == ["A", "B"]
        assert receipts[-1].is_error and not receipts[-1].dispatched
        assert receipts[-1].outcome is ExecutionOutcome.REJECTED_BEFORE_EXECUTION
        assert "Successfully wrote" not in str(receipts[-1].result)


def test_outcome_metadata_tool_is_registered_without_external_success_or_count_cap():
    from osia.tool_contracts import (
        SideEffectClass, get_tool_spec, validate_tool_arguments,
    )
    from osia.chatgpt_account.app_server_client import _CANONICAL_NATIVE_TOOL_NAMES
    spec = get_tool_spec("declare_outcomes")
    assert spec is not None and spec.side_effect is SideEffectClass.READ_ONLY
    assert spec.verified_success is False and spec.postcondition_required is False
    assert "declare_outcomes" in agent_module._active_chat_tool_names()
    assert "declare_outcomes" in _CANONICAL_NATIVE_TOOL_NAMES
    rows = [{"id": str(index), "custom_outcome_metadata": True} for index in range(200)]
    assert validate_tool_arguments(spec, {"outcomes": rows})["outcomes"] == rows
