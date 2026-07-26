from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from osia.system_resources import (
    DestructiveOperation,
    SystemResourceClassification as Classification,
    SystemResourceEvidence,
    SystemResourceRegistry,
    WindowsRuntimeAdapter,
    classify_non_deletion_system_mutation,
)


class _WindowsFixture(WindowsRuntimeAdapter):
    def __init__(self, *args, volume_root="D:\\", **kwargs):
        super().__init__(*args, **kwargs)
        self._fixture_volume_root = volume_root

    def volume_root(self, path: str) -> str:
        del path
        return self._fixture_volume_root


def _registry(*, wrp=False, rows=(), volume_root="D:\\"):
    adapter = _WindowsFixture(
        windows_directory="D:\\OS\\Windows",
        system_directory="D:\\OS\\Windows\\System32",
        system_windows_directory="D:\\OS\\Windows",
        wrp_probe=lambda _path: wrp,
        wrp_key_probe=lambda _root, _subkey: False,
        storage_rows=rows,
        volume_root=volume_root,
    )
    return SystemResourceRegistry(platform_family="windows", windows=adapter)


def test_windows_directory_is_runtime_derived_and_wrp_protected():
    registry = _registry(wrp=True)
    evidence = registry.classify_path("D:\\OS\\Windows\\System32\\kernel32.dll")
    assert evidence.classification is Classification.PROTECTED_SYSTEM_RESOURCE
    assert evidence.reason_code == "windows_resource_protection"
    assert "D:\\" not in repr(evidence)
    assert "target" not in evidence.public_metadata()


def test_windows_wrp_unavailable_is_typed_and_fail_closed():
    registry = SystemResourceRegistry(
        platform_family="windows",
        windows=_WindowsFixture(
            windows_directory="D:\\OS\\Windows",
            system_directory="D:\\OS\\Windows\\System32",
            system_windows_directory="D:\\OS\\Windows",
            wrp_probe=lambda _path: None,
            storage_rows=(),
        ),
    )
    evidence = registry.classify_path("D:\\OS\\Windows\\unknown.dll")
    assert evidence.classification is Classification.INDETERMINATE_PROTECTED_TARGET
    assert evidence.blocks_destruction is True


@pytest.mark.parametrize(
    "gpt_type",
    [
        "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
        "e3c9e316-0b5c-4db8-817d-f92df00215ae",
        "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
    ],
)
def test_esp_msr_and_winre_are_protected_volumes(gpt_type):
    registry = _registry(rows=({"DriveLetter": "D", "GptType": gpt_type},))
    evidence = registry.classify_path("D:\\")
    assert evidence.classification is Classification.PROTECTED_SYSTEM_VOLUME


def test_unrelated_basic_data_partition_is_not_protected_by_type_alone():
    registry = _registry(
        rows=({
            "DriveLetter": "E",
            "GptType": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
            "IsBoot": False,
            "IsSystem": False,
        },),
        volume_root="E:\\",
    )
    evidence = registry.classify_path("E:\\")
    assert evidence.classification is Classification.NOT_SYSTEM_RESOURCE


def test_active_windows_volume_uses_installation_identity_not_gpt_type():
    evidence = _registry(rows=(), volume_root="D:\\").classify_path("D:\\")
    assert evidence.classification is Classification.PROTECTED_SYSTEM_VOLUME
    assert evidence.reason_code == "active_windows_volume"


def test_boot_configuration_and_runtime_boot_partition_are_protected():
    registry = _registry(
        rows=({"DriveLetter": "E", "IsBoot": True, "IsSystem": False},),
        volume_root="E:\\",
    )
    assert registry.classify_path("E:\\").classification is Classification.PROTECTED_SYSTEM_VOLUME
    assert _registry().classify_path("D:\\Boot\\BCD").blocks_destruction


def test_ordinary_user_data_remains_outside_the_windows_registry():
    evidence = _registry().classify_path("D:\\Users\\fixture\\document.txt")
    assert evidence.classification is Classification.NOT_SYSTEM_RESOURCE


def test_registry_and_redirect_semantics_block_only_system_targets():
    registry = _registry()
    protected_registry = classify_non_deletion_system_mutation(
        "run_command",
        {"command": r"reg delete HKLM\SYSTEM\Fixture /f"},
        registry=registry,
    )
    ordinary_registry = classify_non_deletion_system_mutation(
        "run_command",
        {"command": r"reg delete HKCU\Software\Fixture /f"},
        registry=registry,
    )
    protected_redirect = classify_non_deletion_system_mutation(
        "run_command",
        {"command": r'echo empty > "D:\OS\Windows\system.ini"'},
        registry=registry,
    )
    ordinary_redirect = classify_non_deletion_system_mutation(
        "run_command",
        {"command": r'echo data > "D:\Users\fixture\ordinary.txt"'},
        registry=registry,
    )
    assert protected_registry.blocks_destruction
    assert ordinary_registry.classification is Classification.NOT_SYSTEM_RESOURCE
    assert protected_redirect.blocks_destruction
    assert ordinary_redirect.classification is Classification.NOT_SYSTEM_RESOURCE


def test_windows_registry_protection_uses_sfc_key_evidence():
    adapter = _WindowsFixture(
        windows_directory="D:\\OS\\Windows",
        system_directory="D:\\OS\\Windows\\System32",
        system_windows_directory="D:\\OS\\Windows",
        wrp_probe=lambda _path: False,
        wrp_key_probe=lambda root, subkey: (
            root == "HKEY_LOCAL_MACHINE" and subkey == r"SOFTWARE\PROTECTEDFIXTURE"
        ),
        storage_rows=(),
    )
    registry = SystemResourceRegistry(platform_family="windows", windows=adapter)
    evidence = registry.classify_registry_key(r"HKLM\SOFTWARE\ProtectedFixture")
    assert evidence.classification is Classification.PROTECTED_SYSTEM_RESOURCE
    assert evidence.reason_code == "windows_registry_protection"


def test_windows_registry_protection_unavailable_is_typed_and_fail_closed():
    adapter = _WindowsFixture(
        windows_directory="D:\\OS\\Windows",
        system_directory="D:\\OS\\Windows\\System32",
        system_windows_directory="D:\\OS\\Windows",
        wrp_probe=lambda _path: False,
        wrp_key_probe=lambda _root, _subkey: None,
        storage_rows=(),
    )
    registry = SystemResourceRegistry(platform_family="windows", windows=adapter)
    evidence = registry.classify_registry_key(r"HKCU\Software\Fixture")
    assert evidence.classification is Classification.INDETERMINATE_PROTECTED_TARGET
    assert evidence.reason_code == "wrp_registry_status_unavailable"


def test_parent_link_escape_resolves_into_protected_tree(tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    victim = protected / "resource.bin"
    victim.write_bytes(b"fixture")
    link = tmp_path / "alias"
    try:
        link.symlink_to(protected, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    adapter = _WindowsFixture(
        windows_directory=str(protected),
        system_directory=str(protected),
        system_windows_directory=str(protected),
        wrp_probe=lambda _path: True,
        storage_rows=(),
        volume_root=Path(tmp_path).anchor,
    )
    registry = SystemResourceRegistry(platform_family="windows", windows=adapter)
    assert registry.classify_path(link / "resource.bin").blocks_destruction
    # The link itself is a leaf and deleting it does not traverse its target.
    assert not registry.classify_path(link, follow_leaf=False).blocks_destruction


def test_official_windows_maintenance_requires_verified_native_invoker():
    registry = _registry()
    direct = registry.classify_official_maintenance(
        "usoclient.exe", ["StartInstall"], verified_native_invoker=False,
    )
    official = registry.classify_official_maintenance(
        "usoclient.exe", ["StartInstall"], verified_native_invoker=True,
    )
    assert direct.classification is Classification.NOT_SYSTEM_RESOURCE
    assert official.classification is Classification.OFFICIAL_MAINTENANCE_OPERATION


def test_evidence_cannot_be_fabricated():
    with pytest.raises(ValueError, match="untrusted"):
        SystemResourceEvidence(
            1,
            Classification.PROTECTED_SYSTEM_RESOURCE,
            "forged",
            "forged",
            "windows",
            "installation:forged",
            "",
            False,
            "private",
            object(),
        )


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows API validation")
def test_real_windows_api_discovers_active_directory_and_temp_is_ordinary(tmp_path):
    adapter = WindowsRuntimeAdapter(storage_rows=())
    registry = SystemResourceRegistry(platform_family="windows", windows=adapter)
    windows_dir = adapter.windows_directory
    assert os.path.isabs(windows_dir)
    assert registry.classify_path(windows_dir).blocks_destruction
    temp_file = tmp_path / "ordinary.tmp"
    temp_file.write_text("fixture", encoding="utf-8")
    assert registry.classify_path(temp_file).classification is Classification.NOT_SYSTEM_RESOURCE
