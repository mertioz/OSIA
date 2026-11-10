"""Exact critical targets block before dispatch; neighbouring targets remain usable."""

import json
from types import SimpleNamespace

import pytest

from osia import deletion_plan, runtime, system_resources


def test_partition_guard_uses_exact_current_system_identity():
    class RemovableDisk(system_resources.WindowsRuntimeAdapter):
        is_system = True

        def storage_rows(self, *, refresh=False):
            return [
                {"DiskNumber": 5, "PartitionNumber": 2, "IsSystem": self.is_system and refresh},
                {"DiskNumber": 5, "PartitionNumber": 3, "IsSystem": False},
            ]

    adapter = RemovableDisk(storage_rows=[])
    registry = system_resources.SystemResourceRegistry(platform_family="windows", windows=adapter)

    def blocked(command):
        evidence = system_resources.classify_non_deletion_system_mutation(
            "run_command", {"command": command}, registry=registry,
        )
        return bool(evidence and evidence.blocks_destruction)

    command = "Remove-Partition -PartitionNumber 2 -DiskNumber 5"
    assert blocked(command)
    assert not blocked(command + " -WhatIf")
    assert not blocked(command + " -CimSession remote-fixture")
    assert not blocked("Remove-Partition -PartitionNumber 3 -DiskNumber 5")
    adapter.is_system = False  # The same numeric target has a different role now.
    assert not blocked(command)


def test_disk_clear_and_drive_letter_partition_use_proven_local_targets(monkeypatch):
    adapter = system_resources.WindowsRuntimeAdapter(storage_rows=[
        {"DiskNumber": 7, "PartitionNumber": 1, "IsSystem": True},
        {"DiskNumber": 8, "PartitionNumber": 1, "IsSystem": False},
    ])
    registry = system_resources.SystemResourceRegistry(platform_family="windows", windows=adapter)
    drive_checks = []
    monkeypatch.setattr(
        registry, "classify_path",
        lambda target, **_: drive_checks.append(target),
    )

    def classify(command):
        return system_resources.classify_non_deletion_system_mutation(
            "run_command", {"command": command}, registry=registry,
        )

    assert classify("Clear-Disk -Number 7 -RemoveData").blocks_destruction
    assert not classify("Clear-Disk -Number 8 -RemoveData").blocks_destruction
    assert classify("Clear-Disk -Number 7 -RemoveData -wi") is None
    assert classify("Clear-Disk -Number 7 -RemoveData -Session remote-fixture") is None
    classify("Remove-Partition -DriveLetter R")
    assert drive_checks == ["R:\\"]


def test_linux_packaged_runtime_is_protected_but_old_release_is_not(tmp_path, monkeypatch):
    active = tmp_path / "releases" / "active"
    inactive = tmp_path / "releases" / "inactive"
    active.mkdir(parents=True)
    inactive.mkdir()
    (active / "metadata.json").write_text(
        json.dumps({"installerId": "space.osia.desktop.linux.v1"}), encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runtime, "resource_root", lambda: active / "app")
    monkeypatch.setattr(runtime, "application_executable", lambda: active / "python" / "bin" / "python3.12")
    registry = system_resources.SystemResourceRegistry()
    with monkeypatch.context() as guard:
        guard.setattr(deletion_plan, "_enumerate_root", lambda *_: pytest.fail("critical tree was walked"))
        for target in (active, active.parent):
            with pytest.raises(deletion_plan.DeletionPlanError, match="system_destruction"):
                deletion_plan.prepare_deletion_call(
                    "delete_directory", {"path": str(target), "recursive": True}, registry=registry,
                )
    plan = deletion_plan.prepare_deletion_call(
        "delete_directory", {"path": str(inactive), "recursive": True}, registry=registry,
    ).plan
    assert not plan.system_destruction
    classifier = system_resources.classify_non_deletion_system_mutation
    assert classifier("write_file", {"path": str(active / "bin" / "osia")}, registry=registry).blocks_destruction
    assert classifier("rename_file", {
        "old_path": str(active.parent), "new_path": str(tmp_path / "moved"),
    }, registry=registry).blocks_destruction
    assert classifier("write_file", {"path": str(inactive / "bin" / "osia")}, registry=registry) is None

    # Merely naming a source directory "app" doesn't confer runtime protection.
    monkeypatch.setattr(runtime, "application_executable", lambda: tmp_path / "outside" / "python3")
    assert runtime.critical_install_paths() == ()
