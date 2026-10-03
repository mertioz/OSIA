"""The system-destruction guard only rejects direct host-volume destruction."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from osia.action_policy import guard_tool_call, system_mutation_barrier
from osia import system_resources


def _policy_registry(platform: str, blocked_targets: set[str]):
    class Registry:
        platform_family = platform

        def classify_path(self, path, *, operation, follow_leaf=False):
            del operation, follow_leaf
            return SimpleNamespace(blocks_destruction=str(path) in blocked_targets)

    return Registry()


def test_normal_android_build_file_and_workspace_commands_are_allowed(tmp_path):
    cleanup = tmp_path / "temporary-workspace"
    cleanup.mkdir()
    (cleanup / "generated.txt").write_text("fixture", encoding="utf-8")
    temporary = str(tmp_path)
    commands = (
        "adb devices -l",
        "adb shell getprop ro.product.model",
        f"Set-Location '{temporary}'; gradle assembleDebug",
        f"Set-Location '{temporary}'",
        f"New-Item -ItemType File -Path '{temporary}\\new.txt'",
        f"Remove-Item -LiteralPath '{temporary}\\new.txt' -Force",
        f"$env:TEMP='{temporary}'; $env:TMP='{temporary}'; gradle assembleDebug",
        "adb install -r 'C:\\Temp\\acceptance.apk'",
        "adb shell am start -n com.example/.MainActivity",
        "Get-Process adb,python",
        f"Get-ChildItem -LiteralPath '{temporary}' -Force",
        f"Remove-Item -LiteralPath '{cleanup}' -Recurse -Force",
        f"Set-Location '{temporary}'; adb devices -l; gradle assembleDebug",
        "Write-Output 'format C:'; Get-Process; Get-ChildItem C:\\Windows",
    )
    for command in commands:
        assert system_mutation_barrier("run_command", {"command": command}) is None
        assert guard_tool_call("inspect or build", "run_command", {"command": command}) is None


def test_project_file_create_edit_rename_and_delete_are_not_system_destruction(tmp_path):
    existing = tmp_path / "project-note.txt"
    existing.write_text("fixture", encoding="utf-8")
    renamed = tmp_path / "project-note-renamed.txt"

    assert system_mutation_barrier(
        "write_file", {"path": str(tmp_path / "new.txt"), "content": "x"},
    ) is None
    assert system_mutation_barrier(
        "edit_file_lines", {"path": str(existing), "new_content": "x"},
    ) is None
    assert system_mutation_barrier(
        "rename_file", {"old_path": str(existing), "new_path": str(renamed)},
    ) is None
    assert guard_tool_call(
        "write a project file", "write_file",
        {"path": str(tmp_path / "new.txt"), "content": "x"},
    ) is None
    assert guard_tool_call(
        "delete a project file", "delete_file", {"path": str(existing)},
    ) is None


def test_system_looking_file_paths_and_uncertain_evidence_do_not_block(tmp_path):
    windows_file = r"C:\Windows\System32\drivers\etc\hosts"
    assert system_mutation_barrier(
        "write_file", {"path": windows_file, "content": "fixture"},
    ) is None
    assert system_mutation_barrier(
        "run_command", {"command": r'echo fixture > "C:\Windows\System32\drivers\etc\hosts"'},
    ) is None

    registry = system_resources.SystemResourceRegistry(
        platform_family="windows",
        windows=system_resources.WindowsRuntimeAdapter(
            windows_directory=str(tmp_path),
            system_directory=str(tmp_path / "System32"),
            system_windows_directory=str(tmp_path),
            wrp_probe=lambda _path: None,
            storage_rows=(),
        ),
    )
    evidence = registry.indeterminate_destructive_target("mock_target_uncertain")
    assert evidence.blocks_destruction is False


def test_mocked_catastrophic_volume_patterns_block_only_the_action(monkeypatch):
    windows_registry = _policy_registry("windows", {"C:\\"})
    monkeypatch.setattr(
        system_resources,
        "get_default_system_resource_registry",
        lambda: windows_registry,
    )
    assert guard_tool_call(
        "format the host volume", "run_command", {"command": "format C:"},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert guard_tool_call(
        "format the host volume", "run_command",
        {"command": "Format-Volume -DriveLetter C"},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    # The block is local to that action; a following harmless request is allowed.
    assert guard_tool_call(
        "discover Android", "run_command", {"command": "adb devices -l"},
    ) is None
    assert guard_tool_call(
        "run a composed diagnostic", "run_command",
        {"command": "format C:; Write-Output ready"},
    ) is None
    assert guard_tool_call(
        "format another volume", "run_command", {"command": "format D:"},
    ) is None

    linux_registry = _policy_registry("linux", {"/dev/system"})
    monkeypatch.setattr(
        system_resources,
        "get_default_system_resource_registry",
        lambda: linux_registry,
    )
    assert guard_tool_call(
        "format a mocked host device", "run_command",
        {"command": "mkfs.ext4 /dev/system"},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert guard_tool_call(
        "wipe a mocked host device", "run_command",
        {"command": "wipefs -a /dev/system"},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert guard_tool_call(
        "write to a mocked host device", "run_command",
        {"command": "dd if=/dev/zero of=/dev/system bs=1M"},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert guard_tool_call(
        "format a data device", "run_command",
        {"command": "mkfs.ext4 /dev/data"},
    ) is None


def test_unavailable_classifier_does_not_become_destruction_evidence(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("mock registry unavailable")

    monkeypatch.setattr(
        system_resources,
        "get_default_system_resource_registry",
        unavailable,
    )
    assert system_mutation_barrier(
        "run_command", {"command": "adb devices -l"},
    ) is None
    assert system_mutation_barrier(
        "write_file", {"path": "notes.txt", "content": "x"},
    ) is None
