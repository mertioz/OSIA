"""The isolated ChatGPT Account server preserves Code Mode without native tools."""

from pathlib import Path

from osia.chatgpt_account.app_server_client import AppServerClient
from osia.tool_contracts import get_tool_spec


def test_isolated_app_server_command_disables_native_surfaces_generically(tmp_path):
    executable = tmp_path / "codex.exe"
    client = AppServerClient(base_dir=tmp_path / "codex")

    command = client._app_server_command(executable)
    argv = "\0".join(command)

    assert command[:4] == [
        str(executable), "app-server", "--stdio", "--strict-config",
    ]
    assert "--enable\0code_mode" in argv
    assert "--enable\0code_mode_host" in argv
    for feature in (
        "code_mode_only",
        "shell_tool",
        "unified_exec",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "apps",
        "plugins",
        "standalone_web_search",
        "in_app_browser",
        "in_app_local_automation",
    ):
        assert f"--disable\0{feature}" in argv
    assert '--config\0web_search="disabled"' in argv
    assert "--config\0tools.web_search=false" in argv
    assert not any(arg.startswith("gpt-") for arg in command)
    assert "max_tool_rounds" not in argv


def test_thread_start_keeps_read_file_in_the_osia_dynamic_tool_list(tmp_path):
    client = AppServerClient(base_dir=tmp_path / "codex")
    spec = get_tool_spec("read_file")
    assert spec is not None
    dynamic_tool = {
        "type": "function",
        "name": spec.name,
        "description": spec.capability,
        "inputSchema": spec.input_schema,
    }
    requests = []

    def request(method, params, **_kwargs):
        requests.append((method, params))
        return {"thread": {"id": "thread-read-file"}}

    client._request = request
    thread_id = client.start_or_resume_conversation(
        thread_id=None,
        model="fixture-model",
        base_instructions="base",
        developer_instructions="developer",
        dynamic_tools=[dynamic_tool],
    )

    assert thread_id == "thread-read-file"
    assert requests == [("thread/start", {
        "model": "fixture-model",
        "cwd": str(client.workspace_dir),
        "approvalPolicy": "on-request",
        "sandbox": "read-only",
        "ephemeral": True,
        "serviceName": "osia_pc",
        "baseInstructions": "base",
        "developerInstructions": "developer",
        "dynamicTools": [dynamic_tool],
    })]
    assert "run_command" not in [
        item["name"] for item in requests[0][1]["dynamicTools"]
    ]
