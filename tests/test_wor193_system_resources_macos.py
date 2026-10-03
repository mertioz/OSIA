from __future__ import annotations

import plistlib

from osia.system_resources import (
    MacOSRuntimeAdapter,
    SystemResourceClassification as Classification,
    SystemResourceRegistry,
)


def _registry():
    payload = {
        "Containers": [{
            "ContainerReference": "disk3",
            "APFSVolumes": [
                {
                    "DeviceIdentifier": "disk3s1",
                    "MountPoint": "/",
                    "Roles": ["System"],
                    "APFSVolumeGroupUUID": "GROUP-A",
                },
                {
                    "DeviceIdentifier": "disk3s2",
                    "MountPoint": "/System/Volumes/Data",
                    "Roles": ["Data"],
                    "APFSVolumeGroupUUID": "GROUP-A",
                },
                {
                    "DeviceIdentifier": "disk3s3",
                    "Roles": ["Preboot"],
                    "APFSVolumeGroupUUID": "GROUP-A",
                },
                {
                    "DeviceIdentifier": "disk3s4",
                    "Roles": ["Recovery"],
                    "APFSVolumeGroupUUID": "GROUP-A",
                },
                {
                    "DeviceIdentifier": "disk3s5",
                    "Roles": ["VM"],
                    "APFSVolumeGroupUUID": "GROUP-A",
                },
                {
                    "DeviceIdentifier": "disk4s1",
                    "MountPoint": "/Volumes/External",
                    "Roles": ["Data"],
                    "APFSVolumeGroupUUID": "GROUP-B",
                },
            ]
        }]
    }
    return SystemResourceRegistry(
        platform_family="macos",
        macos=MacOSRuntimeAdapter(payload),
    )


def test_sip_ssv_roots_are_protected_but_usr_local_is_not():
    registry = _registry()
    for target in ("/", "/System"):
        assert registry.classify_path(target).blocks_destruction
    for target in ("/System/Library", "/bin/sh", "/sbin/mount", "/usr/bin/python3"):
        assert not registry.classify_path(target).blocks_destruction
    assert registry.classify_path("/usr/local/bin/tool").classification is Classification.NOT_SYSTEM_RESOURCE


def test_active_system_data_and_preboot_group_relationships_are_protected():
    registry = _registry()
    for device in (
        "/dev/disk3", "/dev/disk3s1", "/dev/disk3s2", "/dev/disk3s3",
        "/dev/disk3s4", "/dev/disk3s5",
    ):
        assert registry.classify_path(device).classification is Classification.PROTECTED_SYSTEM_VOLUME
    assert registry.classify_path("/dev/disk4s1").classification is Classification.INDETERMINATE_PROTECTED_TARGET


def test_user_and_third_party_data_paths_are_ordinary():
    registry = _registry()
    assert registry.classify_path("/Users/fixture/document.txt").classification is Classification.NOT_SYSTEM_RESOURCE
    assert registry.classify_path("/Applications/ThirdParty.app").classification is Classification.NOT_SYSTEM_RESOURCE


def test_apple_installer_is_official_only_with_verified_invoker():
    registry = _registry()
    assert registry.classify_official_maintenance(
        "softwareupdate", ["--install", "--all"], verified_native_invoker=True,
    ).classification is Classification.OFFICIAL_MAINTENANCE_OPERATION
    assert registry.classify_official_maintenance(
        "softwareupdate", ["--install", "--all"], verified_native_invoker=False,
    ).classification is Classification.NOT_SYSTEM_RESOURCE


def test_diskutil_plist_bytes_and_parent_alias_resolution_are_fixture_validated():
    payload = _registry().macos.payload
    adapter = MacOSRuntimeAdapter(plistlib.dumps(payload))
    registry = SystemResourceRegistry(
        platform_family="macos",
        macos=adapter,
        path_resolver=lambda _path, _follow_leaf: "/System/Library/CoreServices",
    )
    assert not registry.classify_path("/fixture/alias/CoreServices").blocks_destruction
    assert registry.classify_official_maintenance(
        "installer", ["-pkg", "fixture.pkg"], verified_native_invoker=True,
    ).classification is Classification.OFFICIAL_MAINTENANCE_OPERATION
