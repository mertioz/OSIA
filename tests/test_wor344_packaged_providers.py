from pathlib import Path

from osia.chatgpt_account.app_server_client import AppServerClient
from osia.provider_smoke import run_claude_messages_smoke


def test_launch_gates_match_reviewed_runtime_versions(tmp_path):
    for version, enabled in [("0.144.1", False), ("0.156.1", True)]:
        client = AppServerClient(base_dir=tmp_path, runtime_version_override="codex-cli " + version)
        command = client._app_server_command(Path("codex.exe"))
        assert ("in_app_local_automation" in command) is enabled
        assert command[1:4] == ["app-server", "--stdio", "--strict-config"]
        for flag in ["shell_tool", "unified_exec", "browser_use", "computer_use"]:
            assert command[command.index(flag) - 1] == "--disable"


def test_offline_frozen_claude_probe_runs_actual_tool(monkeypatch):
    monkeypatch.setattr("osia.config.get_setting", lambda name, default=None: default)
    run_claude_messages_smoke()
