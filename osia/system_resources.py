"""Trusted cross-platform evidence for the catastrophic host-destruction guard.

The public result is deliberately categorical.  Raw paths and storage labels stay
inside the registry instance and never appear in ``repr`` or public metadata.
Only this module can construct :class:`SystemResourceEvidence`.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import posixpath
import plistlib
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from osia.runtime import critical_install_paths


SYSTEM_RESOURCE_SCHEMA_VERSION = 1

_ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
_MSR_GUID = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
_WINRE_GUID = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
_LINUX_ROOT_GPT_TYPES = frozenset({
    "6523f8ae-3eb1-4e2a-a05a-18b695ae656f",
    "d27f46ed-2919-4cb8-bd25-9531f3c16534",
    "69dad710-2ce4-4e3c-b16c-21a1d49abed3",
    "b921b045-1df0-41c3-af44-4c6f280d3fae",
    "993d8d3d-f80e-4225-855a-9daf8ed7ea97",
    "77055800-792c-4f94-b39a-98c91b762bb6",
    "e9434544-6e2c-47cc-bae2-12d6deafb44c",
    "d113af76-80ef-41b4-bdb6-0cff4d3d4a25",
    "37c58c8a-d913-4156-a25f-48b1b64e07f0",
    "700bda43-7a34-4507-b179-eeb93d7a7ca3",
    "1aacdb3b-5444-4138-bd9e-e5c2239b2346",
    "1de3f1ef-fa98-47b5-8dcd-4a860a654d78",
    "912ade1d-a839-4913-8964-a10eee08fbd2",
    "c31c45e6-3f39-412e-80fb-4809c4980599",
    "60d5a7fe-8e7d-435c-b714-3dd8162144e1",
    "72ec70a6-cf74-40e6-bd49-4bda08e8f224",
    "08a7acea-624c-4a20-91e8-6e0fa67d23f9",
    "5eead9a9-fe09-4a1e-a1d7-520d00531306",
    "c50cdd70-3862-4cc3-90e1-809a8c93ee2c",
    "44479540-f297-41b2-9af7-d131d5f0458a",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",
})
_LINUX_ROOT_VERITY_GPT_TYPES = frozenset({
    # UAPI Discoverable Partitions: root Verity data, all defined architectures.
    "fc56d9e9-e6e5-4c06-be32-e74407ce09a5",
    "24b2d975-0f97-4521-afa1-cd531e421b8d",
    "7386cdf2-203c-47a9-a498-f2ecce45a2d6",
    "df3300ce-d69f-4c92-978c-9bfb0f38d820",
    "86ed10d5-b607-45bb-8957-d350f23d0571",
    "f3393b22-e9af-4613-a948-9d3bfbd0c535",
    "7a430799-f711-4c7e-8e5b-1d685bd48607",
    "579536f8-6a33-4055-a95a-df2d5e2c42a8",
    "d7d150d2-2a04-4a33-8f12-16651205ff7b",
    "16b417f8-3e06-4f57-8dd2-9b5232f41aa6",
    "d212a430-fbc5-49f9-a983-a7feef2b8d0e",
    "906bd944-4589-4aae-a4e4-dd983917446a",
    "9225a9a3-3c19-4d89-b4f6-eeff88f17631",
    "98cfe649-1588-46dc-b2f0-add147424925",
    "ae0253be-1167-4007-ac68-43926c14c5de",
    "b6ed5582-440b-4209-b8da-5ff7c419ea3d",
    "7ac63b47-b25c-463b-8df8-b4a94e6c90e1",
    "b325bfbe-c7be-4ab8-8357-139e652d2f6b",
    "966061ec-28e4-4b2e-b4a5-1f0a825a1d84",
    "2c7357ed-ebd2-46d9-aec1-23d437ec2bf5",
    "d13c5d3b-b5d1-422a-b29f-9454fdc89d76",
    # Matching root Verity signature partitions.
    "d46495b7-a053-414f-80f7-700c99921ef8",
    "143a70ba-cbd3-4f06-919f-6c05683a78bc",
    "42b0455f-eb11-491d-98d3-56145ba9d037",
    "6db69de6-29f4-4758-a7a5-962190f00ce3",
    "e98b36ee-32ba-4882-9b12-0ce14655f46a",
    "5afb67eb-ecc8-4f85-ae8e-ac1e7c50e7d0",
    "bba210a2-9c5d-45ee-9e87-ff2ccbd002d0",
    "43ce94d4-0f3d-4999-8250-b9deafd98e6e",
    "c919cc1f-4456-4eff-918c-f75e94525ca5",
    "904e58ef-5c65-4a31-9c57-6af5fc7c5de7",
    "15de6170-65d3-431c-916e-b0dcd8393f25",
    "d4a236e7-e873-4c07-bf1d-bf6cf7f1c3c6",
    "f5e2c20c-45b2-4ffa-bce9-2a60737e1aaf",
    "1b31b5aa-add9-463a-b2ed-bd467fc857e7",
    "3a112a75-8729-4380-b4cf-764d79934448",
    "efe0f087-ea8d-4469-821a-4c2a96a8386a",
    "3482388e-4254-435a-a241-766a065f9960",
    "c80187a5-73a3-491a-901a-017c3fa953e9",
    "b3671439-97b0-4a53-90f7-2d5a8f3ad47b",
    "41092b05-9fc8-4523-994f-2def0408b176",
    "5996fc05-109c-48de-808b-23fa0830b676",
})


class SystemResourceClassification(str, Enum):
    NOT_SYSTEM_RESOURCE = "not_system_resource"
    PROTECTED_SYSTEM_RESOURCE = "protected_system_resource"
    PROTECTED_SYSTEM_VOLUME = "protected_system_volume"
    OFFICIAL_MAINTENANCE_OPERATION = "official_maintenance_operation"
    INDETERMINATE_PROTECTED_TARGET = "indeterminate_protected_target"


class DestructiveOperation(str, Enum):
    DELETE = "delete"
    RECURSIVE_DELETE = "recursive_delete"
    TRUNCATE = "truncate"
    REPLACE_UNUSABLE = "replace_unusable"
    MOVE_OUT = "move_out"
    RAW_ERASE = "raw_erase"
    FORMAT = "format"
    PARTITION_DELETE = "partition_delete"
    VOLUME_DELETE = "volume_delete"


_HOST_TREE_DESTRUCTION_OPERATIONS = frozenset({
    DestructiveOperation.DELETE,
    DestructiveOperation.RECURSIVE_DELETE,
})
_HOST_VOLUME_DESTRUCTION_OPERATIONS = frozenset({
    DestructiveOperation.DELETE,
    DestructiveOperation.RECURSIVE_DELETE,
    DestructiveOperation.RAW_ERASE,
    DestructiveOperation.FORMAT,
    DestructiveOperation.PARTITION_DELETE,
    DestructiveOperation.VOLUME_DELETE,
})


_EVIDENCE_SEAL = object()


@dataclass(frozen=True, repr=False)
class SystemResourceEvidence:
    schema_version: int
    classification: SystemResourceClassification
    reason_code: str
    evidence_category: str
    platform_family: str
    installation_ref: str
    volume_ref: str
    official_maintenance: bool
    _private_target: str = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _EVIDENCE_SEAL:
            raise ValueError("untrusted_system_resource_evidence")
        if self.schema_version != SYSTEM_RESOURCE_SCHEMA_VERSION:
            raise ValueError("unsupported_system_resource_schema")
        if not isinstance(self.classification, SystemResourceClassification):
            raise ValueError("invalid_system_resource_classification")
        if not re.fullmatch(r"[a-z0-9_:-]{1,96}", self.reason_code):
            raise ValueError("invalid_system_resource_reason")
        if not re.fullmatch(r"[a-z0-9_:-]{1,96}", self.evidence_category):
            raise ValueError("invalid_system_resource_category")

    @property
    def blocks_destruction(self) -> bool:
        if self.classification is SystemResourceClassification.PROTECTED_SYSTEM_VOLUME:
            return True
        return (
            self.classification is SystemResourceClassification.PROTECTED_SYSTEM_RESOURCE
            and self.evidence_category in {"catastrophic_os_root", "active_osia_runtime"}
        )

    def public_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "classification": self.classification.value,
            "reason_code": self.reason_code,
            "evidence_category": self.evidence_category,
            "platform_family": self.platform_family,
            "installation_ref": self.installation_ref,
            "volume_ref": self.volume_ref,
            "official_maintenance": self.official_maintenance,
        }


def _safe_ref(label: str, value: str) -> str:
    digest = hashlib.sha256(str(value or "unknown").encode("utf-8")).hexdigest()
    return f"{label}:{digest[:32]}"


def _platform_family(value: str | None = None) -> str:
    raw = str(value or sys.platform).lower()
    if raw.startswith("win"):
        return "windows"
    if raw == "darwin" or raw.startswith("mac"):
        return "macos"
    return "linux"


def _evidence(
    classification: SystemResourceClassification,
    reason_code: str,
    category: str,
    *,
    platform_family: str,
    installation_ref: str,
    volume_ref: str = "",
    private_target: str = "",
    official_maintenance: bool = False,
) -> SystemResourceEvidence:
    return SystemResourceEvidence(
        schema_version=SYSTEM_RESOURCE_SCHEMA_VERSION,
        classification=classification,
        reason_code=reason_code,
        evidence_category=category,
        platform_family=platform_family,
        installation_ref=installation_ref,
        volume_ref=volume_ref,
        official_maintenance=official_maintenance,
        _private_target=private_target,
        _seal=_EVIDENCE_SEAL,
    )


def _path_within(path: str, root: str, *, casefold: bool = False) -> bool:
    candidate = os.path.normpath(path)
    boundary = os.path.normpath(root)
    if casefold:
        candidate, boundary = candidate.casefold(), boundary.casefold()
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except (OSError, ValueError):
        return False


def _leaf_is_link_or_reparse(path: str) -> bool:
    try:
        stat = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    attributes = int(getattr(stat, "st_file_attributes", 0) or 0)
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _resolved_for_classification(path: str, *, follow_leaf: bool) -> str:
    lexical = os.path.abspath(os.path.normpath(path))
    if not follow_leaf and _leaf_is_link_or_reparse(lexical):
        return os.path.join(os.path.realpath(os.path.dirname(lexical)), os.path.basename(lexical))
    return os.path.realpath(lexical)


class WindowsRuntimeAdapter:
    """Read-only Windows API adapter; injectable for deterministic tests."""

    def __init__(
        self,
        *,
        windows_directory: str | None = None,
        system_directory: str | None = None,
        system_windows_directory: str | None = None,
        wrp_probe=None,
        storage_rows: Sequence[Mapping[str, Any]] | None = None,
    ):
        self._windows_directory = windows_directory
        self._system_directory = system_directory
        self._system_windows_directory = system_windows_directory
        self._wrp_probe = wrp_probe
        self._storage_rows = tuple(storage_rows) if storage_rows is not None else None
        self._storage_rows_injected = storage_rows is not None

    @staticmethod
    def _directory_api(name: str) -> str:
        if sys.platform != "win32":
            raise OSError("windows_api_unavailable")
        fn = getattr(ctypes.windll.kernel32, name)
        fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        fn.restype = ctypes.c_uint
        size = 32768
        buffer = ctypes.create_unicode_buffer(size)
        length = int(fn(buffer, size))
        if length <= 0 or length >= size:
            raise OSError("windows_directory_api_failed")
        return buffer.value

    @property
    def windows_directory(self) -> str:
        return self._windows_directory or self._directory_api("GetWindowsDirectoryW")

    @property
    def system_directory(self) -> str:
        return self._system_directory or self._directory_api("GetSystemDirectoryW")

    @property
    def system_windows_directory(self) -> str:
        return self._system_windows_directory or self._directory_api(
            "GetSystemWindowsDirectoryW"
        )

    def wrp_status(self, path: str) -> bool | None:
        if self._wrp_probe is not None:
            value = self._wrp_probe(path)
            return value if isinstance(value, bool) else None
        if sys.platform != "win32":
            return None
        try:
            sfc = ctypes.WinDLL("sfc.dll", use_last_error=True)
            fn = sfc.SfcIsFileProtected
            fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            fn.restype = ctypes.c_int
            ctypes.set_last_error(0)
            result = int(fn(None, path))
            error = ctypes.get_last_error()
            if result:
                return True
            return False if error == 0 else None
        except (AttributeError, OSError):
            return None

    def volume_root(self, path: str) -> str:
        if sys.platform != "win32":
            drive, _ = os.path.splitdrive(path)
            return drive + "\\" if drive else ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            fn = ctypes.windll.kernel32.GetVolumePathNameW
            fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            if not fn(path, buffer, len(buffer)):
                return ""
            return buffer.value
        except (AttributeError, OSError):
            return ""

    def storage_rows(self, *, refresh: bool = False) -> tuple[Mapping[str, Any], ...]:
        if self._storage_rows is not None and (not refresh or self._storage_rows_injected):
            return self._storage_rows
        if sys.platform != "win32":
            self._storage_rows = ()
            return self._storage_rows
        command = (
            "Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,"
            "Type,GptType,IsBoot,IsSystem,AccessPaths | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                self._storage_rows = ()
                return self._storage_rows
            payload = json.loads(result.stdout)
            rows = payload if isinstance(payload, list) else [payload]
            self._storage_rows = tuple(row for row in rows if isinstance(row, Mapping))
            return self._storage_rows
        except (OSError, ValueError, subprocess.SubprocessError):
            self._storage_rows = ()
            return self._storage_rows


class MacOSRuntimeAdapter:
    """Deterministic diskutil-plist parser plus current macOS path policy."""

    def __init__(self, plist_payload: bytes | Mapping[str, Any] | None = None):
        if plist_payload is None and sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["/usr/sbin/diskutil", "apfs", "list", "-plist"],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    plist_payload = result.stdout
            except (OSError, subprocess.SubprocessError):
                plist_payload = None
        if isinstance(plist_payload, bytes):
            try:
                parsed = plistlib.loads(plist_payload)
            except (ValueError, TypeError, plistlib.InvalidFileException):
                parsed = {}
        elif isinstance(plist_payload, Mapping):
            parsed = dict(plist_payload)
        else:
            parsed = {}
        self.payload = parsed
        self.volumes = tuple(self._walk_volumes(parsed))

    @classmethod
    def _walk_volumes(cls, value: Any) -> Iterable[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            if any(
                key in value
                for key in (
                    "DeviceIdentifier", "APFSVolumeUUID", "VolumeUUID",
                    "MountPoint", "Roles", "Role",
                )
            ):
                yield value
            for child in value.values():
                yield from cls._walk_volumes(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_volumes(child)

    def active_relationships(self) -> dict[str, set[str]]:
        grouped: dict[str, set[str]] = {}
        active_groups: set[str] = set()
        for volume in self.volumes:
            mount = str(volume.get("MountPoint") or "")
            roles_raw = volume.get("Roles", volume.get("Role", ()))
            roles = {str(item).lower() for item in (
                roles_raw if isinstance(roles_raw, list) else [roles_raw]
            ) if item}
            group = str(
                volume.get("APFSVolumeGroupUUID")
                or volume.get("VolumeGroupUUID")
                or volume.get("AssociatedVolumeGroupUUID")
                or ""
            )
            identifier = str(
                volume.get("DeviceIdentifier")
                or volume.get("APFSVolumeUUID")
                or volume.get("VolumeUUID")
                or ""
            )
            if mount == "/" and ("system" in roles or not roles):
                if group:
                    active_groups.add(group)
                if identifier:
                    grouped.setdefault("active", set()).add(identifier)
        for volume in self.volumes:
            group = str(
                volume.get("APFSVolumeGroupUUID")
                or volume.get("VolumeGroupUUID")
                or volume.get("AssociatedVolumeGroupUUID")
                or ""
            )
            roles_raw = volume.get("Roles", volume.get("Role", ()))
            roles = {str(item).lower() for item in (
                roles_raw if isinstance(roles_raw, list) else [roles_raw]
            ) if item}
            identifier = str(
                volume.get("DeviceIdentifier")
                or volume.get("APFSVolumeUUID")
                or volume.get("VolumeUUID")
                or ""
            )
            if identifier and group in active_groups:
                grouped.setdefault("associated", set()).add(identifier)

        def add_active_containers(value: Any) -> None:
            if isinstance(value, Mapping):
                volumes = value.get("APFSVolumes")
                if isinstance(volumes, list):
                    groups = {
                        str(
                            item.get("APFSVolumeGroupUUID")
                            or item.get("VolumeGroupUUID")
                            or item.get("AssociatedVolumeGroupUUID")
                            or ""
                        )
                        for item in volumes
                        if isinstance(item, Mapping)
                    }
                    container = str(
                        value.get("ContainerReference")
                        or value.get("DeviceIdentifier")
                        or value.get("APFSContainerUUID")
                        or ""
                    )
                    if container and groups & active_groups:
                        grouped.setdefault("associated", set()).add(container)
                        for item in volumes:
                            if not isinstance(item, Mapping):
                                continue
                            roles_raw = item.get("Roles", item.get("Role", ()))
                            roles = {
                                str(role).lower()
                                for role in (
                                    roles_raw
                                    if isinstance(roles_raw, list)
                                    else [roles_raw]
                                )
                                if role
                            }
                            if roles & {"preboot", "recovery", "vm"}:
                                identifier = str(
                                    item.get("DeviceIdentifier")
                                    or item.get("APFSVolumeUUID")
                                    or item.get("VolumeUUID")
                                    or ""
                                )
                                if identifier:
                                    grouped.setdefault("associated", set()).add(
                                        identifier
                                    )
                for child in value.values():
                    add_active_containers(child)
            elif isinstance(value, list):
                for child in value:
                    add_active_containers(child)

        add_active_containers(self.payload)
        grouped["groups"] = active_groups
        return grouped


@dataclass(frozen=True)
class LinuxMount:
    mount_point: str
    source: str
    fs_type: str
    major_minor: str
    options: tuple[str, ...]


class LinuxRuntimeAdapter:
    """Parser for mountinfo/findmnt-style runtime identities."""

    def __init__(
        self,
        *,
        mountinfo: str | None = None,
        os_release: Mapping[str, str] | None = None,
        lsblk: Mapping[str, Any] | None = None,
        ownership_probe=None,
    ):
        if mountinfo is None and sys.platform.startswith("linux"):
            try:
                mountinfo = Path("/proc/self/mountinfo").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                mountinfo = ""
        self.mounts = self.parse_mountinfo(mountinfo or "")
        if os_release is None and sys.platform.startswith("linux"):
            try:
                parsed_release = {}
                for line in Path("/etc/os-release").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    key, marker, value = line.partition("=")
                    if marker:
                        parsed_release[key] = value.strip().strip("'\"")
                os_release = parsed_release
            except OSError:
                os_release = {}
        if lsblk is None and sys.platform.startswith("linux"):
            try:
                result = subprocess.run(
                    [
                        "lsblk", "--json", "--paths",
                        "--output", "NAME,KNAME,PATH,TYPE,FSTYPE,MOUNTPOINTS,PARTTYPE,PKNAME",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parsed_lsblk = json.loads(result.stdout)
                    if isinstance(parsed_lsblk, Mapping):
                        lsblk = parsed_lsblk
            except (OSError, ValueError, subprocess.SubprocessError):
                lsblk = None
        self.os_release = dict(os_release or {})
        self.lsblk = dict(lsblk or {})
        self._ownership_probe = ownership_probe

    @staticmethod
    def _unescape(value: str) -> str:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    @classmethod
    def parse_mountinfo(cls, text: str) -> tuple[LinuxMount, ...]:
        mounts: list[LinuxMount] = []
        for raw in str(text or "").splitlines():
            left, marker, right = raw.partition(" - ")
            left_fields, right_fields = left.split(), right.split()
            if not marker or len(left_fields) < 6 or len(right_fields) < 2:
                continue
            mount_point = cls._unescape(left_fields[4])
            if not mount_point.startswith("/"):
                continue
            mounts.append(
                LinuxMount(
                    mount_point=posixpath.normpath(mount_point),
                    source=cls._unescape(right_fields[1]),
                    fs_type=right_fields[0],
                    major_minor=left_fields[2],
                    options=tuple(left_fields[5].split(",")),
                )
            )
        return tuple(sorted(mounts, key=lambda item: len(item.mount_point), reverse=True))

    def mount_for(self, path: str) -> LinuxMount | None:
        normalized = posixpath.normpath(path.replace("\\", "/"))
        for mount in self.mounts:
            try:
                inside = posixpath.commonpath((normalized, mount.mount_point)) == mount.mount_point
            except ValueError:
                inside = False
            if inside:
                return mount
        return None

    def active_block_identities(self) -> set[str]:
        active: set[str] = set()
        protected_types = {
            _ESP_GUID,
            "bc13c2ff-59e6-4262-a352-b275fd6f7172",  # XBOOTLDR
        } | set(_LINUX_ROOT_GPT_TYPES) | set(_LINUX_ROOT_VERITY_GPT_TYPES)

        def identity(node: Mapping[str, Any]) -> str:
            value = str(node.get("path") or "")
            if value:
                return value.replace("\\", "/")
            name = str(node.get("kname") or node.get("name") or "")
            return f"/dev/{name}" if name else ""

        def walk(node: Mapping[str, Any], ancestors: tuple[str, ...]) -> None:
            current = identity(node)
            mount_values = node.get("mountpoints", node.get("mountpoint", ()))
            if isinstance(mount_values, str) or mount_values is None:
                mount_values = (mount_values,) if mount_values else ()
            mounts = {str(item) for item in mount_values if item}
            fstype = str(node.get("fstype") or "").lower()
            parttype = str(node.get("parttype") or "").strip("{} ").lower()
            system_mounts = {
                "/", "/usr", "/boot", "/boot/efi", "/efi", "[SWAP]",
                "/run/mnt/ubuntu-seed", "/run/mnt/ubuntu-boot",
                "/run/mnt/ubuntu-save", "/run/mnt/ubuntu-data",
            }
            required = bool(
                mounts & system_mounts
                or fstype in {"swap", "crypto_luks"} and "[SWAP]" in mounts
                or parttype in protected_types
            )
            chain = ancestors + ((current,) if current else ())
            if required:
                active.update(item for item in chain if item)
            for child in node.get("children", ()) or ():
                if isinstance(child, Mapping):
                    walk(child, chain)

        for node in self.lsblk.get("blockdevices", ()) or ():
            if isinstance(node, Mapping):
                walk(node, ())
        for mount in self.mounts:
            if mount.mount_point in {
                "/", "/usr", "/boot", "/boot/efi", "/efi",
                "/run/mnt/ubuntu-seed", "/run/mnt/ubuntu-boot",
                "/run/mnt/ubuntu-save", "/run/mnt/ubuntu-data",
            }:
                active.add(mount.source.replace("\\", "/"))
        return active

    def package_ownership(self, path: str) -> bool | None:
        if self._ownership_probe is not None:
            value = self._ownership_probe(path)
            return value if isinstance(value, bool) else None
        distro = " ".join((
            str(self.os_release.get("ID") or ""),
            str(self.os_release.get("ID_LIKE") or ""),
        )).lower()
        if "arch" in distro:
            command = ["pacman", "-Qo", "--", path]
            parser = lambda result: PacmanOwnership.parse(result.stdout + result.stderr)
        elif any(name in distro for name in ("fedora", "rhel", "centos")):
            command = ["rpm", "-qf", "--", path]
            parser = lambda result: RpmOwnership.parse(
                result.stdout + result.stderr, result.returncode
            )
        elif any(name in distro for name in ("ubuntu", "debian", "linuxmint", "mint")):
            command = ["dpkg-query", "-S", path]
            parser = lambda result: DpkgOwnership.parse(
                result.stdout + result.stderr, result.returncode
            )
        else:
            return None
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return parser(result)


class PacmanOwnership:
    @staticmethod
    def parse(output: str) -> bool | None:
        text = str(output or "").strip()
        if re.search(r"\bis owned by\b", text):
            return True
        if re.search(r"\bNo package owns\b", text, re.IGNORECASE):
            return False
        return None


class RpmOwnership:
    @staticmethod
    def parse(output: str, returncode: int = 0) -> bool | None:
        text = str(output or "").strip()
        if returncode == 0 and text and "not owned" not in text.lower():
            return True
        if "not owned by any package" in text.lower():
            return False
        return None


class DpkgOwnership:
    @staticmethod
    def parse(output: str, returncode: int = 0) -> bool | None:
        text = str(output or "").strip()
        if returncode == 0 and re.match(r"^[^:\r\n]+:\s+.+", text):
            return True
        if "no path found matching pattern" in text.lower():
            return False
        return None


class SystemResourceRegistry:
    """Versioned facade selecting a platform adapter and returning sealed evidence."""

    def __init__(
        self,
        *,
        platform_family: str | None = None,
        windows: WindowsRuntimeAdapter | None = None,
        macos: MacOSRuntimeAdapter | None = None,
        linux: LinuxRuntimeAdapter | None = None,
        path_resolver=None,
    ):
        self.platform_family = _platform_family(platform_family)
        self.windows = windows or WindowsRuntimeAdapter()
        self.macos = macos or MacOSRuntimeAdapter()
        self.linux = linux or LinuxRuntimeAdapter()
        self._path_resolver = path_resolver
        self._runtime_paths = (
            critical_install_paths() if self.platform_family == _platform_family() else ()
        )
        self._installation_ref = self._resolve_installation_ref()

    @property
    def current_os_installation_identity(self) -> str:
        return self._installation_ref

    def _resolve_installation_ref(self) -> str:
        if self.platform_family == "windows":
            try:
                identity = self.windows.system_windows_directory
            except OSError:
                identity = "windows-unavailable"
        elif self.platform_family == "macos":
            relationships = self.macos.active_relationships()
            identity = "|".join(sorted(relationships.get("active", set()))) or "macos-unavailable"
        else:
            root = next((m for m in self.linux.mounts if m.mount_point == "/"), None)
            identity = f"{root.source}|{root.major_minor}" if root else "linux-unavailable"
        return _safe_ref("installation", identity)

    def classify_official_maintenance(
        self,
        executable: str,
        argv: Sequence[str],
        *,
        verified_native_invoker: bool,
    ) -> SystemResourceEvidence:
        name = os.path.basename(str(executable or "")).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        allowed = {
            "windows": {"usoclient", "wuauclt", "tiworker", "trustedinstaller", "msiexec"},
            "macos": {"softwareupdate", "installer"},
            "linux": {"pacman", "rpm", "dnf", "dpkg", "apt", "apt-get"},
        }[self.platform_family]
        official = bool(verified_native_invoker and name in allowed and argv)
        return _evidence(
            SystemResourceClassification.OFFICIAL_MAINTENANCE_OPERATION
            if official else SystemResourceClassification.NOT_SYSTEM_RESOURCE,
            "verified_native_maintenance" if official else "maintenance_not_verified",
            "official_maintenance" if official else "ordinary_operation",
            platform_family=self.platform_family,
            installation_ref=self._installation_ref,
            official_maintenance=official,
        )

    def classify_path(
        self,
        path: str | os.PathLike[str],
        *,
        operation: DestructiveOperation = DestructiveOperation.DELETE,
        follow_leaf: bool = False,
    ) -> SystemResourceEvidence:
        if not isinstance(operation, DestructiveOperation):
            raise ValueError("invalid_destructive_operation")
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            return self._indeterminate("invalid_target", "target_identity", raw)
        try:
            if self.platform_family in {"linux", "macos"} and raw.startswith("/"):
                if self._path_resolver is not None:
                    resolved = str(self._path_resolver(raw, follow_leaf))
                elif (
                    self.platform_family == "linux" and sys.platform.startswith("linux")
                ) or (
                    self.platform_family == "macos" and sys.platform == "darwin"
                ):
                    resolved = _resolved_for_classification(
                        raw,
                        follow_leaf=follow_leaf,
                    )
                else:
                    resolved = posixpath.normpath(raw.replace("\\", "/"))
            else:
                resolved = _resolved_for_classification(raw, follow_leaf=follow_leaf)
        except (OSError, ValueError):
            return self._indeterminate("target_resolution_failed", "target_identity", raw)
        if operation in _HOST_TREE_DESTRUCTION_OPERATIONS:
            for critical in self._runtime_paths:
                same_path = os.path.normcase(os.path.normpath(resolved)) == os.path.normcase(str(critical))
                contains_critical = (
                    operation is DestructiveOperation.RECURSIVE_DELETE
                    and _path_within(str(critical), resolved, casefold=self.platform_family == "windows")
                )
                if same_path or contains_critical:
                    return self._protected(resolved, "active_osia_runtime_destruction", "active_osia_runtime")
        if self.platform_family == "windows":
            return self._classify_windows(resolved, operation)
        if self.platform_family == "macos":
            return self._classify_macos(resolved, operation)
        return self._classify_linux(resolved, operation)

    def classify_windows_partition(self, disk_number: int, partition_number: int) -> SystemResourceEvidence:
        """Use current partition identity; disk numbers can change after hotplug."""
        target = f"disk:{disk_number}:partition:{partition_number}"
        rows = self.windows.storage_rows(refresh=True)
        disk = [row for row in rows if str(row.get("DiskNumber")) == str(disk_number)]
        matches = [row for row in disk if str(row.get("PartitionNumber")) == str(partition_number)]
        if len(matches) != 1:
            return self._indeterminate("unknown_windows_partition", "storage_role", target)
        row = matches[0]
        active = row.get("IsBoot") is True or row.get("IsSystem") is True
        guid = str(row.get("GptType") or "").strip("{} ").lower()
        active_disk = any(item.get("IsBoot") is True or item.get("IsSystem") is True for item in disk)
        if active or (active_disk and guid in {_ESP_GUID, _MSR_GUID, _WINRE_GUID}):
            return self._protected(target, "protected_windows_partition", "boot_recovery_partition", volume=True)
        return self._not_system(target)

    def indeterminate_destructive_target(
        self,
        reason_code: str = "destructive_target_not_normalizable",
    ) -> SystemResourceEvidence:
        return self._indeterminate(reason_code, "destructive_semantics", "")

    def _not_system(self, target: str, reason: str = "ordinary_data") -> SystemResourceEvidence:
        return _evidence(
            SystemResourceClassification.NOT_SYSTEM_RESOURCE,
            reason,
            "ordinary_data",
            platform_family=self.platform_family,
            installation_ref=self._installation_ref,
            private_target=target,
        )

    def _indeterminate(self, reason: str, category: str, target: str) -> SystemResourceEvidence:
        return _evidence(
            SystemResourceClassification.INDETERMINATE_PROTECTED_TARGET,
            reason,
            category,
            platform_family=self.platform_family,
            installation_ref=self._installation_ref,
            private_target=target,
        )

    def _protected(self, target: str, reason: str, category: str, *, volume: bool = False) -> SystemResourceEvidence:
        return _evidence(
            SystemResourceClassification.PROTECTED_SYSTEM_VOLUME
            if volume else SystemResourceClassification.PROTECTED_SYSTEM_RESOURCE,
            reason,
            category,
            platform_family=self.platform_family,
            installation_ref=self._installation_ref,
            volume_ref=_safe_ref("volume", target) if volume else "",
            private_target=target,
        )

    def _classify_windows(self, target: str, operation: DestructiveOperation) -> SystemResourceEvidence:
        normalized = os.path.normpath(target)
        lowered = normalized.casefold()
        if re.match(r"^(?:\\\\[.?]\\|\\\\physicaldrive|\\\\harddisk)", lowered):
            return self._indeterminate("unsupported_windows_device_target", "storage_role", target)
        try:
            windows_dir = os.path.normpath(self.windows.windows_directory)
            system_dir = os.path.normpath(self.windows.system_directory)
            system_windows = os.path.normpath(self.windows.system_windows_directory)
        except OSError:
            windows_dir = system_dir = system_windows = ""
        roots = tuple(root for root in (windows_dir, system_dir, system_windows) if root)
        if not roots:
            return self._indeterminate(
                "windows_directory_api_unavailable",
                "protected_windows_root",
                target,
            )
        volume_root = self.windows.volume_root(normalized)
        target_is_volume_root = bool(
            volume_root and os.path.normcase(os.path.normpath(normalized))
            == os.path.normcase(os.path.normpath(volume_root))
        )
        if target_is_volume_root and operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS and any(
            _path_within(root, volume_root, casefold=True) for root in roots
        ):
            return self._protected(target, "active_windows_volume", "active_os_volume", volume=True)
        for row in self.windows.storage_rows():
            guid = str(row.get("GptType") or row.get("Type") or "").strip("{} ").lower()
            access_paths = row.get("AccessPaths") or ()
            if isinstance(access_paths, str):
                access_paths = (access_paths,)
            drive = str(row.get("DriveLetter") or "").strip()
            matches = bool(
                target_is_volume_root
                and (
                    (drive and volume_root[:1].casefold() == drive[:1].casefold())
                    or any(
                        os.path.normcase(os.path.normpath(str(item)))
                        == os.path.normcase(os.path.normpath(volume_root))
                        for item in access_paths if item
                    )
                )
            )
            if matches and operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS and (
                row.get("IsBoot") is True
                or row.get("IsSystem") is True
                or guid in {_ESP_GUID, _MSR_GUID, _WINRE_GUID}
            ):
                return self._protected(target, "protected_windows_partition", "boot_recovery_partition", volume=True)
        normalized_key = os.path.normcase(os.path.normpath(normalized))
        if (
            operation in _HOST_TREE_DESTRUCTION_OPERATIONS
            and any(
                normalized_key == os.path.normcase(os.path.normpath(root))
                for root in roots
            )
        ):
            return self._protected(
                target,
                "catastrophic_windows_root_destruction",
                "catastrophic_os_root",
            )
        if any(_path_within(normalized, root, casefold=True) for root in roots):
            wrp = self.windows.wrp_status(normalized)
            if wrp is True:
                return self._protected(target, "windows_resource_protection", "wrp_resource")
            if wrp is None:
                return self._indeterminate("wrp_status_unavailable", "protected_windows_root", target)
            return self._protected(target, "active_windows_tree", "active_os_resource")
        boot_names = ("\\boot\\bcd", "\\efi\\microsoft\\boot\\bcd")
        if any(lowered.endswith(name) for name in boot_names):
            return self._protected(target, "windows_boot_configuration", "boot_resource")
        return self._not_system(target)

    def _classify_macos(self, target: str, operation: DestructiveOperation) -> SystemResourceEvidence:
        normalized = os.path.normpath(target).replace("\\", "/")
        if normalized == "/" and operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS:
            return self._protected(
                target,
                "active_macos_root",
                "active_system_volume",
                volume=True,
            )
        if normalized == "/usr/local" or _path_within(normalized, "/usr/local"):
            return self._not_system(target, "usr_local_third_party")
        for root in ("/System", "/bin", "/sbin", "/usr"):
            if normalized == root and operation in _HOST_TREE_DESTRUCTION_OPERATIONS:
                return self._protected(
                    target,
                    "catastrophic_macos_root_destruction",
                    "catastrophic_os_root",
                )
            if _path_within(normalized, root):
                return self._protected(target, "sip_ssv_critical_root", "protected_os_resource")
        relationships = self.macos.active_relationships()
        identifier = normalized.removeprefix("/dev/")
        if (
            operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS
            and identifier and identifier in relationships.get("active", set())
        ):
            return self._protected(target, "active_apfs_system_volume", "active_system_volume", volume=True)
        if (
            operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS
            and identifier and identifier in relationships.get("associated", set())
        ):
            return self._protected(target, "associated_apfs_system_volume", "system_volume_group", volume=True)
        if normalized.startswith("/dev/"):
            return self._indeterminate("unknown_macos_storage_role", "storage_role", target)
        return self._not_system(target)

    def _classify_linux(self, target: str, operation: DestructiveOperation) -> SystemResourceEvidence:
        normalized = os.path.normpath(target).replace("\\", "/")
        if normalized == "/usr/local" or _path_within(normalized, "/usr/local"):
            return self._not_system(target, "usr_local_third_party")
        critical = ("/boot", "/efi", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
        for root in critical:
            if normalized == root and operation in _HOST_TREE_DESTRUCTION_OPERATIONS:
                return self._protected(
                    target,
                    "catastrophic_linux_root_destruction",
                    "catastrophic_os_root",
                )
            if _path_within(normalized, root):
                return self._protected(target, "linux_critical_prefix", "active_os_resource")
        for database in ("/var/lib/pacman", "/var/lib/rpm", "/usr/lib/sysimage/rpm", "/var/lib/dpkg"):
            if _path_within(normalized, database):
                return self._protected(target, "package_database_required", "package_management_resource")
        mount = self.linux.mount_for(normalized)
        protected_mounts = {"/", "/usr", "/boot", "/boot/efi", "/efi"}
        if (
            operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS
            and mount is not None and mount.mount_point in protected_mounts
        ):
            if normalized == mount.mount_point:
                return self._protected(target, "active_linux_filesystem", "active_system_volume", volume=True)
        if normalized.startswith("/dev/"):
            if (
                operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS
                and normalized in self.linux.active_block_identities()
            ):
                return self._protected(target, "active_linux_block_role", "active_system_volume", volume=True)
            device_mount = next(
                (item for item in self.linux.mounts if item.source == normalized),
                None,
            )
            if device_mount is None:
                return self._indeterminate("unknown_linux_block_role", "storage_role", target)
            if (
                operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS
                and (device_mount.mount_point in protected_mounts or "swap" in device_mount.fs_type.lower())
            ):
                return self._protected(target, "active_linux_block_role", "active_system_volume", volume=True)
        ubuntu_core_roles = {"/run/mnt/ubuntu-seed", "/run/mnt/ubuntu-boot", "/run/mnt/ubuntu-save", "/run/mnt/ubuntu-data"}
        if (
            operation in _HOST_VOLUME_DESTRUCTION_OPERATIONS
            and any(normalized == root for root in ubuntu_core_roles)
        ):
            return self._protected(target, "ubuntu_core_active_role", "active_system_volume", volume=True)
        if self.linux.package_ownership(normalized) is True:
            return self._protected(target, "package_owned_system_resource", "package_owned_resource")
        return self._not_system(target)


_DEFAULT_REGISTRY_LOCK = threading.Lock()
_DEFAULT_REGISTRY: SystemResourceRegistry | None = None


def get_default_system_resource_registry() -> SystemResourceRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = SystemResourceRegistry()
        return _DEFAULT_REGISTRY


def _powershell_what_if(args: Sequence[str]) -> bool:
    return any(
        re.fullmatch(r"-(?:w|wh|wha|what|whati|whatif)(?::.*)?", item, re.IGNORECASE)
        and not re.fullmatch(r"-whatif:\s*\$?false", item, re.IGNORECASE)
        for item in args
    )


def _partition_argument(args: Sequence[str], option: str) -> int | None:
    values = [
        item.split(":", 1)[1] if ":" in item else (args[i + 1] if i + 1 < len(args) else "")
        for i, item in enumerate(args)
        if item.casefold().split(":", 1)[0] == option
    ]
    if len(values) != 1:
        return None
    value = values[0].strip("'\"")
    return int(value) if re.fullmatch(r"[0-9]{1,10}", value) else None


def classify_non_deletion_system_mutation(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    registry: SystemResourceRegistry | None = None,
) -> SystemResourceEvidence | None:
    """Recognize direct, standalone operations on a proven critical volume."""

    if str(tool_name or "") not in {"bash", "run_command"}:
        return None
    values = dict(arguments) if isinstance(arguments, Mapping) else {}
    command = str(values.get("command") or "").strip()
    if not command or any(char in command for char in "\r\n;&|<>`"):
        return None

    active = registry or get_default_system_resource_registry()
    try:
        parts = shlex.split(command, posix=active.platform_family != "windows")
    except (TypeError, ValueError):
        return None
    if not parts:
        return None

    executable = os.path.basename(parts[0].strip("'\"")).casefold()
    args = parts[1:]
    if any(
        item.casefold() in {"/?", "-?", "/help", "/h", "--help", "-h", "--version"}
        for item in args
    ):
        return None
    target = None
    if active.platform_family == "windows":
        if executable in {"format", "format.com"}:
            target = next(
                (item for item in args if re.fullmatch(r"[A-Za-z]:\\?", item)),
                None,
            )
        elif executable == "remove-partition" and not _powershell_what_if(args):
            disk = _partition_argument(args, "-disknumber")
            partition = _partition_argument(args, "-partitionnumber")
            if disk is not None and partition is not None and partition > 0:
                return active.classify_windows_partition(disk, partition)
        elif executable == "format-volume":
            drive_letter_args = [
                index for index, item in enumerate(args[:-1])
                if item.casefold() == "-driveletter"
            ]
            if _powershell_what_if(args) or len(drive_letter_args) != 1:
                return None
            index = drive_letter_args[0]
            if re.fullmatch(r"[A-Za-z]", args[index + 1]):
                target = args[index + 1] + ":\\"
    else:
        if executable == "wipefs":
            recognized_flags = {"-a", "--all", "-f", "--force", "-q", "--quiet"}
            if all(item.casefold() in recognized_flags for item in args if item.startswith("-")):
                operands = [item for item in args if not item.startswith("-")]
                if len(operands) == 1:
                    target = operands[0]
        elif executable in {"mkfs"} or re.fullmatch(r"mkfs\.[a-z0-9]+", executable):
            recognized_flags = {"-f", "-q", "-v"}
            if all(item.casefold() in recognized_flags for item in args if item.startswith("-")):
                operands = [item for item in args if not item.startswith("-")]
                if len(operands) == 1:
                    target = operands[0]
        elif executable == "dd":
            outputs = [item[3:] for item in args if item.casefold().startswith("of=")]
            zero_count = any(re.fullmatch(r"count=0+", item, re.IGNORECASE) for item in args)
            if len(outputs) == 1 and not zero_count:
                target = outputs[0]

    if not target:
        return None
    if active.platform_family == "windows" and re.fullmatch(
        r"[A-Za-z]:", target,
    ):
        target += "\\"
    return active.classify_path(
        target,
        operation=DestructiveOperation.VOLUME_DELETE,
    )


__all__ = [
    "SYSTEM_RESOURCE_SCHEMA_VERSION",
    "DestructiveOperation",
    "DpkgOwnership",
    "LinuxMount",
    "LinuxRuntimeAdapter",
    "MacOSRuntimeAdapter",
    "PacmanOwnership",
    "RpmOwnership",
    "SystemResourceClassification",
    "SystemResourceEvidence",
    "SystemResourceRegistry",
    "WindowsRuntimeAdapter",
    "classify_non_deletion_system_mutation",
    "get_default_system_resource_registry",
]
