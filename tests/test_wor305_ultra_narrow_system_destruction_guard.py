"""Final WOR-305 guard audit: ordinary commands pass; only proven host wipes block."""
from __future__ import annotations

import ntpath
import posixpath
from types import SimpleNamespace

import pytest

from osia import system_resources
from osia.action_policy import guard_tool_call, system_mutation_barrier


def _normalize(platform: str, target: str) -> str:
    if platform == "windows":
        return ntpath.normcase(ntpath.normpath(str(target)))
    return posixpath.normpath(str(target))


class _EvidenceRegistry:
    """Pure fixture: only listed, normalized targets are proven host resources."""

    def __init__(self, platform: str, active_targets: set[str]):
        self.platform_family = platform
        self.active_targets = {_normalize(platform, target) for target in active_targets}
        self.current_os_installation_identity = f"mock:{platform}"
        self.calls: list[tuple[str, object]] = []

    def classify_path(self, target, *, operation, follow_leaf=False):
        del follow_leaf
        normalized = _normalize(self.platform_family, str(target))
        self.calls.append((normalized, operation))
        return SimpleNamespace(blocks_destruction=normalized in self.active_targets)


def _install_registry(monkeypatch, platform: str, active_targets: set[str]):
    registry = _EvidenceRegistry(platform, active_targets)
    monkeypatch.setattr(
        system_resources,
        "get_default_system_resource_registry",
        lambda: registry,
    )
    return registry


_ORDINARY_COMMANDS = (
    # Android and build commands.
    "adb devices -l",
    "adb shell getprop ro.product.model",
    r"adb install -r C:\Temp\acceptance.apk",
    "adb uninstall com.example.application",
    "adb shell am start -n com.example.application/.MainActivity",
    "gradle assembleDebug",
    "gradle clean",
    r".\gradlew.bat assembleDebug",
    r"Set-Location C:\src\osia",
    # Registry, process, service, environment, and package operations.
    r"reg add HKLM\SYSTEM\CurrentControlSet\Services\Example /v Start /d 3 /f",
    r"Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\Example' -Name Start -Value 3",
    "Stop-Process -Name adb -Force",
    "taskkill /F /IM adb.exe",
    "Restart-Service Spooler",
    "setx OSIA_BUILD_MODE test",
    "$env:OSIA_BUILD_MODE='test'",
    "winget install Google.PlatformTools",
    "winget uninstall Google.PlatformTools",
    "choco uninstall adb -y",
    "apt-get remove example-package",
    # Filesystem, disk, partition, and read-only storage inspection.
    r"Get-ChildItem -LiteralPath C:\Windows\System32 -Force",
    "Get-Disk",
    "Get-Partition -DiskNumber 0",
    "Get-Volume -DriveLetter C",
    "Get-PhysicalDisk",
    "Get-CimInstance Win32_DiskDrive",
    "fsutil fsinfo drives",
    "lsblk -f",
    "df -h",
    "findmnt",
    r"mountvol C:\ /L",
    r"Get-Content C:\Windows\System32\drivers\etc\hosts",
    r"Get-Item C:\Windows\System32\kernel32.dll",
    # Chaining, pipelines, redirection, scripts, and inert keyword mentions.
    r"Set-Location C:\src\osia; gradle assembleDebug",
    "Get-Disk; Get-Partition",
    "Get-Disk | Format-Table",
    r"Write-Output 'format C:' > C:\Temp\format-notes.txt",
    r"Write-Output 'wipe /dev/system' | Set-Content C:\Temp\notes.txt",
    r"powershell -File C:\Temp\inspect-storage.ps1",
    r"powershell -Command \"Write-Output 'format C:'\"",
    "bash -lc \"echo 'format C:'\"",
    "Write-Output 'format C:'",
    "Write-Output 'wipe /dev/system'",
    r"Get-Item 'C:\Temp\system-format-wipe.txt'",
    r"Get-Content 'C:\Windows\System32\format-wipe-system.log'",
    r"diskpart /s C:\Temp\inspect-partitions.txt",
    # Formatter names with help, dry-run, ambiguous, or non-host targets.
    "format /? C:",
    "format /help C:",
    "format C: /?",
    "format D:",
    "Format-Volume -DriveLetter C -WhatIf",
    "Format-Volume -DriveLetter C -WhatIf:$true",
    "Format-Volume -DriveLetter C -What",
    "Format-Volume -DriveLetter D -DriveLetter C",
    "mkfs.ext4 -n /dev/system",
    "mkfs.ext4 --help /dev/system",
    "mkfs.ext4 /dev/system 100",
    "mkfs.ext4 /dev/data",
    "wipefs --no-act -a /dev/system",
    "wipefs -a /dev/system /dev/data",
    "dd if=/dev/zero of=/dev/system count=0",
    "dd --help of=/dev/system",
    "sudo mkfs.ext4 /dev/system",
    "bash -c \"mkfs.ext4 /dev/system\"",
    "cmd /c format C:",
)

_POSIX_COMMANDS = frozenset({
    "apt-get remove example-package",
    "lsblk -f",
    "df -h",
    "findmnt",
    "bash -lc \"echo 'format C:'\"",
    "mkfs.ext4 -n /dev/system",
    "mkfs.ext4 --help /dev/system",
    "mkfs.ext4 /dev/system 100",
    "mkfs.ext4 /dev/data",
    "wipefs --no-act -a /dev/system",
    "wipefs -a /dev/system /dev/data",
    "dd if=/dev/zero of=/dev/system count=0",
    "dd --help of=/dev/system",
    "sudo mkfs.ext4 /dev/system",
    "bash -c \"mkfs.ext4 /dev/system\"",
})


@pytest.mark.parametrize("command,platform", [
    (command, "linux" if command in _POSIX_COMMANDS else "windows")
    for command in _ORDINARY_COMMANDS
])
def test_ordinary_command_matrix_never_emits_system_destruction(
    monkeypatch, command, platform,
):
    active_targets = {"C:\\"} if platform == "windows" else {"/dev/system"}
    _install_registry(monkeypatch, platform, active_targets)
    assert system_mutation_barrier("run_command", {"command": command}) is None
    assert guard_tool_call("inspect or run a normal command", "run_command", {
        "command": command,
    }) is None


@pytest.mark.parametrize("dirname", [
    "project-cleanup", "temporary-tree", "outside-repository",
])
def test_recursive_project_temporary_and_outside_repo_deletions_are_not_host_destruction(
    monkeypatch, tmp_path, dirname,
):
    _install_registry(monkeypatch, "windows", {"C:\\"})
    target = tmp_path / dirname
    target.mkdir()
    (target / "fixture.txt").write_text("fixture", encoding="utf-8")
    assert guard_tool_call(
        "clean a directory", "delete_directory",
        {"path": str(target), "recursive": True},
    ) is None
    assert (target / "fixture.txt").read_text(encoding="utf-8") == "fixture"


def test_single_windows_system_file_operations_are_not_os_root_destruction(
    monkeypatch, tmp_path,
):
    system_root = tmp_path / "Windows"
    system_dir = system_root / "System32"
    system_dir.mkdir(parents=True)
    system_file = system_dir / "host-fixture.dll"
    system_file.write_text("fixture", encoding="utf-8")
    _install_registry(monkeypatch, "windows", {str(system_root)})

    assert guard_tool_call("write one system file", "write_file", {
        "path": str(system_file), "content": "updated fixture",
    }) is None
    assert guard_tool_call("edit one system file", "edit_file_lines", {
        "path": str(system_file), "start_line": 1, "end_line": 1,
        "new_content": "updated fixture",
    }) is None
    assert guard_tool_call("rename one system file", "rename_file", {
        "old_path": str(system_file), "new_path": str(system_dir / "renamed.dll"),
    }) is None
    assert guard_tool_call("delete one system file", "delete_file", {
        "path": str(system_file),
    }) is None
    # Policy inspection never performed any of the operations above.
    assert system_file.read_text(encoding="utf-8") == "fixture"


@pytest.mark.parametrize("command,platform,target", [
    ("format C:", "windows", "C:\\"),
    ("format C: /Q", "windows", "C:\\"),
    ("Format-Volume -DriveLetter C", "windows", "C:\\"),
    ("Format-Volume -DriveLetter C -WhatIf:$false", "windows", "C:\\"),
    ("mkfs.ext4 /dev/system", "linux", "/dev/system"),
    ("mkfs.ext4 -F /dev/system", "linux", "/dev/system"),
    ("wipefs -a /dev/system", "linux", "/dev/system"),
    ("dd if=/dev/zero of=/dev/system bs=1M", "linux", "/dev/system"),
])
def test_direct_catastrophic_primitive_and_mocked_active_target_block_before_dispatch(
    monkeypatch, command, platform, target,
):
    registry = _install_registry(monkeypatch, platform, {target})
    blocked = guard_tool_call("run the command", "run_command", {"command": command})
    assert blocked == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert registry.calls == [
        (_normalize(platform, target), system_resources.DestructiveOperation.VOLUME_DELETE),
    ]

    from osia import agent
    from osia.tool_contracts import ExecutionOutcome

    dispatched = []
    monkeypatch.setitem(
        agent.TOOL_MAP,
        "run_command",
        lambda **kwargs: dispatched.append(kwargs) or "unexpected dispatch",
    )
    result = agent._execute_tool_with_policy(
        "run_command", {"command": command}, preflight_error=blocked,
    )
    assert result.outcome is ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert result.is_error and not result.dispatched
    assert result.result == blocked
    assert dispatched == []


def test_unknown_linux_device_role_is_uncertain_and_allowed(monkeypatch):
    registry = system_resources.SystemResourceRegistry(
        platform_family="linux",
        linux=system_resources.LinuxRuntimeAdapter(
            mountinfo="", os_release={}, lsblk={"blockdevices": []},
        ),
        path_resolver=lambda path, _follow_leaf: posixpath.normpath(path),
    )
    evidence = registry.classify_path(
        "/dev/not-an-active-osia-device",
        operation=system_resources.DestructiveOperation.FORMAT,
    )
    assert evidence.classification is (
        system_resources.SystemResourceClassification.INDETERMINATE_PROTECTED_TARGET
    )
    assert not evidence.blocks_destruction
    monkeypatch.setattr(
        system_resources, "get_default_system_resource_registry", lambda: registry,
    )
    command = "mkfs.ext4 /dev/not-an-active-osia-device"
    assert system_mutation_barrier("run_command", {"command": command}) is None
    assert guard_tool_call("format an unknown device", "run_command", {
        "command": command,
    }) is None


def test_registry_or_parser_exception_is_not_destruction_evidence(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("mock registry unavailable")

    monkeypatch.setattr(
        system_resources, "get_default_system_resource_registry", unavailable,
    )
    command = "format C:"
    assert system_mutation_barrier("run_command", {"command": command}) is None
    assert guard_tool_call("format a volume", "run_command", {
        "command": command,
    }) is None
