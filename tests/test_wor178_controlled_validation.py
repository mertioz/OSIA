from __future__ import annotations

import json
import tempfile
import types
from pathlib import Path

from osia import action_policy
from osia import agent as agent_module
from osia.agent import OSIAAgent
from osia.mcp_client import registry as mcp_registry


def test_real_read_only_tools_flow_through_one_canonical_osia_turn(capsys):
    with tempfile.TemporaryDirectory(prefix="osia-wor178-read-") as temp_dir:
        fixture = Path(temp_dir) / "fixture.txt"
        fixture.write_text("synthetic read-only fixture", encoding="utf-8")
        agent = OSIAAgent(lang="en")
        rounds = [0]

        def fake_stream(self, callback, *, on_first_token=None, guard=None):
            if rounds[0] == 0:
                rounds[0] += 1
                return (
                    '<tool_call>{"tool":"get_system_info"}</tool_call>'
                    '<tool_call>{"tool":"list_running_processes","limit":3}</tool_call>'
                    '<tool_call>{"tool":"get_file_info","path":'
                    + json.dumps(str(fixture))
                    + "}</tool_call>"
                )
            feedback = "\n".join(str(message.get("content")) for message in self.messages)
            assert "Tool get_system_info result:" in feedback
            assert "Tool list_running_processes result:" in feedback
            assert "Tool get_file_info result:" in feedback
            rounds[0] += 1
            return "The isolated read-only checks completed."

        agent._stream_with_sentences = types.MethodType(fake_stream, agent)
        visible = []
        final = agent.run(
            "Inspect the isolated local fixture.",
            history=[],
            on_sentence=visible.append,
        )
        captured = capsys.readouterr()
        assert final == "The isolated read-only checks completed."
        assert visible == [final]
        assert str(fixture) not in captured.out
        assert str(fixture) not in captured.err
        assert str(fixture) not in str(agent.exported_history())
    assert not Path(temp_dir).exists()


def test_reversible_local_file_lifecycle_is_confirmed_verified_and_cleaned(
    monkeypatch,
):
    prompts = []
    monkeypatch.setattr(
        mcp_registry,
        "_setting_bool",
        lambda key, default: True if key == "mcp_confirm_writes" else False,
    )
    monkeypatch.setattr(
        mcp_registry,
        "_confirmation_provider",
        lambda server, tool, summary: prompts.append((tool, summary)) or True,
    )
    with tempfile.TemporaryDirectory(prefix="osia-wor178-files-") as temp_dir:
        root = Path(temp_dir)
        original = root / "original.txt"
        copied = root / "copied.txt"
        renamed = root / "renamed.txt"

        assert not agent_module.tool_write_file(str(original), "alpha").startswith("ERROR:")
        assert original.read_text(encoding="utf-8") == "alpha"
        assert not agent_module.tool_edit_file_lines(
            str(original), 1, 1, "beta",
        ).startswith("ERROR:")
        assert original.read_text(encoding="utf-8") == "beta\n"
        assert not agent_module.tool_copy_file(str(original), str(copied)).startswith("ERROR:")
        assert copied.read_text(encoding="utf-8") == "beta\n"
        assert not agent_module.tool_rename_file(str(copied), str(renamed)).startswith("ERROR:")
        assert renamed.exists() and not copied.exists()

        assert action_policy.guard_tool_call(
            "Delete the isolated copied fixture.",
            "delete_file",
            {"path": str(renamed)},
        ) is None
        assert not agent_module.tool_delete_file(str(renamed)).startswith("ERROR:")
        assert not renamed.exists()

        nested = root / "nested"
        assert not agent_module.tool_create_directory(str(nested)).startswith("ERROR:")
        assert action_policy.guard_tool_call(
            "Delete the isolated empty fixture directory.",
            "delete_directory",
            {"path": str(nested), "recursive": False},
        ) is None
        assert not agent_module.tool_delete_directory(
            str(nested), recursive=False,
        ).startswith("ERROR:")
        assert not nested.exists()

        assert prompts == [
            ("delete_file", "Delete one local item"),
            ("delete_directory", "Delete one local item"),
        ]
        assert all(str(root) not in summary for _tool, summary in prompts)
    assert not Path(temp_dir).exists()
