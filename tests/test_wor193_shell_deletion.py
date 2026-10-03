from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from osia import action_policy
from osia import agent as agent_module
from osia import deletion_plan as deletion_module
from osia import system_resources as system_module
from osia.deletion_plan import DeletionPlanError, parse_shell_deletion
from osia.mcp_client import registry as mcp_registry
from osia.system_resources import SystemResourceRegistry, WindowsRuntimeAdapter


@pytest.mark.parametrize(
    ("command", "family", "count", "recursive"),
    [
        ('Remove-Item -LiteralPath "C:\\Temp\\one"', "windows", 1, False),
        ('Remove-Item -LiteralPath "C:\\Temp\\one" -Recurse', "windows", 1, True),
        ("Remove-Item -LiteralPath 'C:\\Temp\\one','C:\\Temp\\two'", "windows", 2, False),
        ('cmd /c del /q "C:\\Temp\\one.txt"', "windows", 1, False),
        ('cmd /c rmdir /s /q "C:\\Temp\\tree"', "windows", 1, True),
        ("rm -- '/tmp/one' '/tmp/two'", "posix", 2, False),
        ("rm -rf -- '/tmp/tree'", "posix", 1, True),
        ("unlink '/tmp/one'", "posix", 1, False),
        ("rmdir '/tmp/empty'", "posix", 1, False),
    ],
)
def test_exact_literal_shell_forms_are_enumerable(command, family, count, recursive):
    spec = parse_shell_deletion(command, platform_family=family)
    assert spec is not None
    assert len(spec.targets) == count
    assert spec.recursive is recursive


@pytest.mark.parametrize(
    "command",
    [
        "rm *.txt",
        "rm $TARGET",
        "rm $(Get-Content target.txt)",
        "rm one | Out-Null",
        "rm one; echo done",
        "find . -delete",
        "find . -print0 | xargs -0 rm",
        "for x in a b; do rm $x; done",
        "Remove-Item -Path C:\\Temp\\one",
    ],
)
def test_dynamic_complex_or_raw_deletion_shell_is_rejected(command):
    with pytest.raises(DeletionPlanError):
        parse_shell_deletion(command, platform_family="windows")


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Partition -DiskNumber 0 -PartitionNumber 1",
        "mkfs.ext4 /dev/sda1",
        "Write-Output 'format C:'",
    ],
)
def test_storage_syntax_is_not_a_deletion_plan_classifier(command):
    assert parse_shell_deletion(command, platform_family="windows") is None


def _safe_registry(tmp_path: Path) -> SystemResourceRegistry:
    unused = tmp_path / "never-system"
    return SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(unused),
            system_directory=str(unused / "System32"),
            system_windows_directory=str(unused),
            wrp_probe=lambda _path: False,
            storage_rows=(),
        ),
    )


@pytest.fixture(autouse=True)
def _policy(monkeypatch, tmp_path):
    monkeypatch.setattr(
        deletion_module,
        "get_default_system_resource_registry",
        lambda: _safe_registry(tmp_path),
    )
    safe = _safe_registry(tmp_path)
    monkeypatch.setattr(
        system_module,
        "get_default_system_resource_registry",
        lambda: safe,
    )
    mcp_registry.set_confirmation_provider(None)
    yield
    mcp_registry.set_confirmation_provider(None)


def test_literal_shell_deletion_runs_plan_instead_of_raw_shell(tmp_path, monkeypatch):
    target = tmp_path / "literal.txt"
    target.write_text("delete", encoding="utf-8")
    raw_calls = []
    monkeypatch.setattr(
        agent_module,
        "_run_windows_powershell",
        lambda *_args: raw_calls.append(True) or subprocess.CompletedProcess([], 0, "", ""),
    )
    mcp_registry.set_confirmation_provider(lambda *_args: True)
    command = f'rm "{target}"'
    arguments = {"command": command}
    assert action_policy.guard_tool_call(
        "Delete the isolated fixture.", "run_command", arguments,
    ) is None
    result = agent_module.tool_run_command(command)
    assert result.startswith("Deleted: SUCCESS_VERIFIED")
    assert not target.exists()
    assert raw_calls == []


def test_raw_shell_call_cannot_bypass_missing_plan(tmp_path):
    target = tmp_path / "bypass.txt"
    target.write_text("stay", encoding="utf-8")
    result = agent_module.tool_bash(f'rm "{target}"')
    assert result.startswith("ERROR:")
    assert target.exists()


def test_complex_deletion_is_blocked_before_shell_launch(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(
        agent_module,
        "_run_windows_powershell",
        lambda *_args: launched.append(True),
    )
    result = action_policy.guard_tool_call(
        "Delete matching fixtures.",
        "bash",
        {"command": f'rm "{tmp_path}\\*.txt"'},
    )
    assert result.startswith("ERROR: DELETION_PREFLIGHT_REQUIRED")
    assert launched == []


def test_non_deletion_shell_behavior_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        agent_module,
        "_run_windows_powershell",
        lambda *_args: subprocess.CompletedProcess([], 0, "ordinary output\n", ""),
    )
    assert agent_module.tool_bash("Get-Date") == "ordinary output"


def test_non_deletion_shell_may_mention_deletion_words(monkeypatch):
    calls = []
    monkeypatch.setattr(
        agent_module,
        "_run_windows_powershell",
        lambda command, timeout: calls.append((command, timeout))
        or subprocess.CompletedProcess([], 0, "remove rm unlink\n", ""),
    )
    assert agent_module.tool_bash("echo remove rm unlink") == "remove rm unlink"
    assert calls == [("echo remove rm unlink", 300.0)]
