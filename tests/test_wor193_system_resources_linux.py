from __future__ import annotations

from osia.system_resources import (
    DpkgOwnership,
    LinuxRuntimeAdapter,
    PacmanOwnership,
    RpmOwnership,
    SystemResourceClassification as Classification,
    SystemResourceRegistry,
)


MOUNTINFO = """24 1 8:2 / / rw,relatime - ext4 /dev/root rw
25 24 8:1 / /boot rw,relatime - ext4 /dev/boot rw
26 25 8:3 / /boot/efi rw,relatime - vfat /dev/esp rw
27 24 8:4 / /home rw,relatime - ext4 /dev/home rw
"""


def _registry(mountinfo=MOUNTINFO):
    return SystemResourceRegistry(
        platform_family="linux",
        linux=LinuxRuntimeAdapter(
            mountinfo=mountinfo,
            os_release={"ID": "ubuntu"},
        ),
    )


def test_active_root_boot_efi_and_critical_prefixes_are_protected():
    registry = _registry()
    for target in ("/", "/boot", "/boot/efi", "/etc/passwd", "/usr/bin/tool", "/lib/libc.so"):
        assert registry.classify_path(target).blocks_destruction


def test_home_srv_mnt_opt_and_usr_local_are_not_prefix_protected():
    registry = _registry()
    for target in ("/home/user/file", "/srv/data", "/mnt/data", "/media/data", "/opt/app", "/usr/local/bin/app"):
        assert registry.classify_path(target).classification is Classification.NOT_SYSTEM_RESOURCE


def test_unknown_block_role_is_indeterminate_and_ubuntu_core_roles_are_protected():
    registry = _registry()
    assert registry.classify_path("/dev/sdz9").classification is Classification.INDETERMINATE_PROTECTED_TARGET
    assert registry.classify_path("/run/mnt/ubuntu-boot/grub.cfg").classification is Classification.PROTECTED_SYSTEM_VOLUME


def test_malformed_mountinfo_fails_closed_for_unknown_block_target():
    registry = _registry("malformed mount data")
    evidence = registry.classify_path("/dev/mapper/unknown")
    assert evidence.classification is Classification.INDETERMINATE_PROTECTED_TARGET


def test_distribution_ownership_parsers_are_deterministic():
    assert PacmanOwnership.parse("/usr/bin/bash is owned by bash 5.2") is True
    assert PacmanOwnership.parse("error: No package owns /fixture") is False
    assert PacmanOwnership.parse("ambiguous") is None
    assert RpmOwnership.parse("bash-5.2-1.fc", 0) is True
    assert RpmOwnership.parse("file is not owned by any package", 1) is False
    assert DpkgOwnership.parse("bash: /bin/bash", 0) is True
    assert DpkgOwnership.parse("no path found matching pattern /fixture", 1) is False


def test_package_manager_transaction_is_distinct_from_direct_deletion():
    registry = _registry()
    official = registry.classify_official_maintenance(
        "apt", ["install", "fixture"], verified_native_invoker=True,
    )
    direct = registry.classify_path("/usr/bin/fixture")
    assert official.classification is Classification.OFFICIAL_MAINTENANCE_OPERATION
    assert direct.classification is Classification.PROTECTED_SYSTEM_RESOURCE


def test_lsblk_parent_lvm_xbootldr_and_root_verity_identities_are_protected():
    lsblk = {
        "blockdevices": [{
            "name": "sda",
            "path": "/dev/sda",
            "children": [
                {
                    "name": "sda1",
                    "path": "/dev/sda1",
                    "parttype": "bc13c2ff-59e6-4262-a352-b275fd6f7172",
                },
                {
                    "name": "sda2",
                    "path": "/dev/sda2",
                    "children": [{
                        "name": "vg-root",
                        "path": "/dev/mapper/vg-root",
                        "mountpoints": ["/"],
                        "fstype": "btrfs",
                    }],
                },
                {
                    "name": "sda3",
                    "path": "/dev/sda3",
                    "parttype": "2c7357ed-ebd2-46d9-aec1-23d437ec2bf5",
                },
                {
                    "name": "sda4",
                    "path": "/dev/sda4",
                    "parttype": "df3300ce-d69f-4c92-978c-9bfb0f38d820",
                },
                {
                    "name": "sda5",
                    "path": "/dev/sda5",
                    "parttype": "6db69de6-29f4-4758-a7a5-962190f00ce3",
                },
            ],
        }],
    }
    registry = SystemResourceRegistry(
        platform_family="linux",
        linux=LinuxRuntimeAdapter(
            mountinfo="24 1 253:0 /@ / rw - btrfs /dev/mapper/vg-root rw\n",
            os_release={"ID": "fedora"},
            lsblk=lsblk,
        ),
    )
    for device in (
        "/dev/sda", "/dev/sda1", "/dev/sda2", "/dev/sda3",
        "/dev/sda4", "/dev/sda5",
        "/dev/mapper/vg-root",
    ):
        assert registry.classify_path(device).classification is Classification.PROTECTED_SYSTEM_VOLUME


def test_separate_usr_package_database_and_package_owned_opt_resource_are_protected():
    registry = SystemResourceRegistry(
        platform_family="linux",
        linux=LinuxRuntimeAdapter(
            mountinfo=(
                MOUNTINFO
                + "28 24 8:5 / /usr rw,relatime - ext4 /dev/usr rw\n"
            ),
            os_release={"ID": "linuxmint", "ID_LIKE": "ubuntu debian"},
            ownership_probe=lambda path: path == "/opt/vendor/bin/owned",
        ),
    )
    for target in ("/usr", "/dev/usr", "/var/lib/dpkg/status", "/opt/vendor/bin/owned"):
        assert registry.classify_path(target).blocks_destruction
    assert registry.classify_path("/opt/vendor/bin/unowned").classification is Classification.NOT_SYSTEM_RESOURCE


def test_fixture_path_alias_escaping_into_etc_is_protected():
    registry = SystemResourceRegistry(
        platform_family="linux",
        linux=LinuxRuntimeAdapter(mountinfo=MOUNTINFO, os_release={"ID": "arch"}),
        path_resolver=lambda _path, _follow_leaf: "/etc/passwd",
    )
    assert registry.classify_path("/fixture/alias/passwd").blocks_destruction
