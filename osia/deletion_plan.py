"""Deterministic deletion inventory, exact permits, revalidation and execution."""

from __future__ import annotations

import contextvars
import ctypes
import os
import re
import secrets
import shlex
import stat as stat_module
import threading
import time
import sys
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from osia.scope_normalization import canonical_hash, canonical_serialize, sensitive_hash_equal
from osia.system_resources import (
    DestructiveOperation,
    SystemResourceClassification,
    SystemResourceEvidence,
    SystemResourceRegistry,
    get_default_system_resource_registry,
)


DELETION_PLAN_SCHEMA_VERSION = 1
DELETION_POLICY_VERSION = "wor193-phase2-v1"
DELETION_PERMIT_SCHEMA_VERSION = 1
DEFAULT_PLAN_TTL_SECONDS = 30.0


class DeletionPlanError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


class InventoryReliability(str, Enum):
    COMPLETE = "complete"
    UNRELIABLE = "unreliable"


class RevalidationState(str, Enum):
    PENDING = "pending"
    VALIDATED = "validated"


class InventoryEntryType(str, Enum):
    FILE = "file"
    FOLDER = "folder"
    SYMLINK = "symlink"
    REPARSE_POINT = "reparse_point"
    MOUNT_POINT = "mount_point"
    OTHER = "other"


class DeletionExecutionOutcome(str, Enum):
    SUCCESS_VERIFIED = "success_verified"
    CANCELLED_PROVEN = "cancelled_proven"
    PARTIAL_EFFECT_VERIFIED = "partial_effect_verified"
    OUTCOME_UNKNOWN = "outcome_unknown"


class DeletionPreflightKind(str, Enum):
    NOT_DELETION = "not_deletion"
    PLANNED = "planned"


@dataclass(frozen=True)
class ColossalDeletionPolicy:
    version: str = DELETION_POLICY_VERSION
    object_hard_limit: int = 1000
    byte_hard_limit: int = 1024 * 1024 * 1024
    root_hard_limit: int = 3
    object_risk_limit: int = 100
    byte_risk_limit: int = 100 * 1024 * 1024
    risk_score_limit: int = 3

    def __post_init__(self) -> None:
        for name in (
            "object_hard_limit", "byte_hard_limit", "root_hard_limit",
            "object_risk_limit", "byte_risk_limit", "risk_score_limit",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("invalid_colossal_policy")


DEFAULT_COLOSSAL_POLICY = ColossalDeletionPolicy()
_PLAN_SEAL = object()
_ENTRY_SEAL = object()
_PERMIT_SEAL = object()
_DISPATCH_SEAL = object()


@dataclass(frozen=True, repr=False)
class DeletionInventoryEntry:
    root_index: int
    relative_path: str = field(repr=False)
    entry_type: InventoryEntryType
    size: int
    device_id: int
    object_id: int
    volume_ref: str
    link_tag: str
    mtime_ns: int
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ENTRY_SEAL:
            raise ValueError("untrusted_deletion_inventory_entry")
        if type(self.root_index) is not int or self.root_index < 0:
            raise ValueError("invalid_inventory_root_index")
        if not isinstance(self.entry_type, InventoryEntryType):
            raise ValueError("invalid_inventory_entry_type")
        if self.relative_path.startswith(("/", "\\")) or "\x00" in self.relative_path:
            raise ValueError("invalid_inventory_relative_path")
        if self.relative_path not in {"", "."} and any(
            part == ".." for part in re.split(r"[/\\]+", self.relative_path)
        ):
            raise ValueError("inventory_path_escape")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "root": self.root_index,
            "relative_path": self.relative_path.replace("\\", "/"),
            "entry_type": self.entry_type.value,
            "size": self.size,
            "device_id": self.device_id,
            "object_id": self.object_id,
            "volume_ref": self.volume_ref,
            "link_tag": self.link_tag,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True, repr=False)
class DeletionPlan:
    schema_version: int
    policy_version: str
    plan_id: str
    operation_id: str
    platform_family: str
    created_at: float
    expires_at: float
    inventory_reliability: InventoryReliability
    revalidation_state: RevalidationState
    files_count: int
    folders_count: int
    link_reparse_count: int
    total_bytes: int
    root_count: int
    recursive: bool
    colossal_deletion: bool
    risk_score: int
    reason_codes: tuple[str, ...]
    inventory_hash: str = field(repr=False)
    root_identities: tuple[str, ...] = field(repr=False)
    entries: tuple[DeletionInventoryEntry, ...] = field(repr=False)
    system_evidence: tuple[SystemResourceEvidence, ...] = field(repr=False)
    _private_roots: tuple[str, ...] = field(repr=False, compare=False)
    _policy: ColossalDeletionPolicy = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _PLAN_SEAL:
            raise ValueError("untrusted_deletion_plan")
        if self.schema_version != DELETION_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported_deletion_plan_schema")
        if self.inventory_reliability is not InventoryReliability.COMPLETE:
            raise ValueError("unreliable_deletion_inventory")
        if not isinstance(self.revalidation_state, RevalidationState):
            raise ValueError("invalid_revalidation_state")
        if self.root_count != len(self._private_roots) or self.root_count != len(self.root_identities):
            raise ValueError("deletion_root_count_mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", self.inventory_hash):
            raise ValueError("invalid_inventory_hash")
        if self.expires_at <= self.created_at:
            raise ValueError("invalid_deletion_plan_expiry")
        if not isinstance(self.colossal_deletion, bool):
            raise ValueError("invalid_colossal_classification")

    @property
    def system_destruction(self) -> bool:
        return any(item.blocks_destruction for item in self.system_evidence)

    def safe_public_summary(self) -> dict[str, Any]:
        categories = sorted({
            item.evidence_category
            for item in self.system_evidence
            if item.classification is not SystemResourceClassification.NOT_SYSTEM_RESOURCE
        })
        return {
            "schema_version": self.schema_version,
            "revalidation_state": self.revalidation_state.value,
            "request_id": self.plan_id,
            "files_count": self.files_count,
            "folders_count": self.folders_count,
            "link_reparse_count": self.link_reparse_count,
            "total_bytes": self.total_bytes,
            "root_count": self.root_count,
            "recursive": self.recursive,
            "colossal_deletion": self.colossal_deletion,
            "risk_score": self.risk_score,
            "reason_codes": list(self.reason_codes),
            "safe_reason_categories": categories,
            "expires_at": self.expires_at,
        }

    # Local GUI-only accessors.  Callers must not serialize or log these values.
    def _local_roots(self) -> tuple[str, ...]:
        return self._private_roots

    def _local_entries(self) -> tuple[DeletionInventoryEntry, ...]:
        return self.entries


@dataclass(frozen=True)
class DeletionPreflight:
    kind: DeletionPreflightKind
    plan: DeletionPlan | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ShellDeletionSpec:
    targets: tuple[str, ...]
    recursive: bool
    directory_operation: bool


@dataclass(frozen=True, repr=False)
class DeletionPermit:
    schema_version: int
    permit_id: str
    plan_id: str
    operation_id: str
    inventory_hash: str = field(repr=False)
    policy_version: str
    policy_epoch: int
    confirmation_strength: str
    issued_at: float
    expires_at: float
    used: bool
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _PERMIT_SEAL:
            raise ValueError("untrusted_deletion_permit")
        if self.schema_version != DELETION_PERMIT_SCHEMA_VERSION:
            raise ValueError("unsupported_deletion_permit_schema")
        if self.confirmation_strength not in {"ordinary", "reinforced"}:
            raise ValueError("invalid_deletion_confirmation_strength")
        if not re.fullmatch(r"[0-9a-f]{64}", self.inventory_hash):
            raise ValueError("invalid_deletion_permit_hash")


class DeletionPermitLedger:
    def __init__(self, *, clock=None, maximum: int = 1024):
        self._clock = clock or time.time
        self._maximum = max(1, min(int(maximum), 4096))
        self._lock = threading.Lock()
        self._permits: dict[str, DeletionPermit] = {}

    def issue(
        self,
        plan: DeletionPlan,
        *,
        policy_epoch: int,
        confirmation_strength: str,
    ) -> DeletionPermit:
        if not isinstance(plan, DeletionPlan):
            raise DeletionPlanError("untrusted_deletion_plan")
        if plan.system_destruction:
            raise DeletionPlanError("system_destruction_absolute")
        if plan.colossal_deletion and confirmation_strength != "reinforced":
            raise DeletionPlanError("reinforced_confirmation_required")
        now = float(self._clock())
        expires_at = min(plan.expires_at, now + DEFAULT_PLAN_TTL_SECONDS)
        if expires_at <= now:
            raise DeletionPlanError("deletion_plan_expired")
        permit = DeletionPermit(
            schema_version=DELETION_PERMIT_SCHEMA_VERSION,
            permit_id=secrets.token_hex(24),
            plan_id=plan.plan_id,
            operation_id=plan.operation_id,
            inventory_hash=plan.inventory_hash,
            policy_version=plan.policy_version,
            policy_epoch=int(policy_epoch),
            confirmation_strength=confirmation_strength,
            issued_at=now,
            expires_at=expires_at,
            used=False,
            _seal=_PERMIT_SEAL,
        )
        with self._lock:
            self._purge(now)
            self._permits[permit.permit_id] = permit
            while len(self._permits) > self._maximum:
                self._permits.pop(next(iter(self._permits)), None)
        return permit

    def consume(
        self,
        permit: DeletionPermit,
        plan: DeletionPlan,
        *,
        policy_epoch: int,
    ) -> DeletionPermit:
        now = float(self._clock())
        with self._lock:
            stored = self._permits.pop(permit.permit_id, None)
        if stored is None:
            raise DeletionPlanError("deletion_permit_missing_or_used")
        if stored.expires_at <= now or plan.expires_at <= now:
            raise DeletionPlanError("deletion_permit_expired")
        if stored.used:
            raise DeletionPlanError("deletion_permit_replayed")
        if stored.policy_epoch != int(policy_epoch):
            raise DeletionPlanError("deletion_policy_epoch_changed")
        if stored.policy_version != plan.policy_version:
            raise DeletionPlanError("deletion_policy_version_changed")
        if stored.plan_id != plan.plan_id or stored.operation_id != plan.operation_id:
            raise DeletionPlanError("deletion_permit_plan_mismatch")
        if not sensitive_hash_equal(stored.inventory_hash, plan.inventory_hash):
            raise DeletionPlanError("deletion_permit_inventory_mismatch")
        if plan.colossal_deletion and stored.confirmation_strength != "reinforced":
            raise DeletionPlanError("reinforced_confirmation_required")
        return DeletionPermit(
            schema_version=stored.schema_version,
            permit_id=stored.permit_id,
            plan_id=stored.plan_id,
            operation_id=stored.operation_id,
            inventory_hash=stored.inventory_hash,
            policy_version=stored.policy_version,
            policy_epoch=stored.policy_epoch,
            confirmation_strength=stored.confirmation_strength,
            issued_at=stored.issued_at,
            expires_at=stored.expires_at,
            used=True,
            _seal=_PERMIT_SEAL,
        )

    def cancel(self, permit: DeletionPermit) -> None:
        with self._lock:
            self._permits.pop(permit.permit_id, None)

    def _purge(self, now: float) -> None:
        for permit_id, permit in tuple(self._permits.items()):
            if permit.expires_at <= now:
                self._permits.pop(permit_id, None)


@dataclass(frozen=True, repr=False)
class _PendingDeletion:
    tool_name: str
    call_hash: str = field(repr=False)
    plan: DeletionPlan = field(repr=False)
    permit: DeletionPermit = field(repr=False)
    ledger: DeletionPermitLedger = field(repr=False)


@dataclass(frozen=True, repr=False)
class DeletionDispatch:
    plan: DeletionPlan = field(repr=False)
    permit: DeletionPermit = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _DISPATCH_SEAL or not self.permit.used:
            raise ValueError("invalid_deletion_dispatch")


@dataclass(frozen=True, repr=False)
class DeletionExecutionResult:
    outcome: DeletionExecutionOutcome
    planned_entries: int
    affected_entries: int
    reason_code: str
    _affected_private: tuple[tuple[int, str], ...] = field(repr=False, compare=False)

    def model_text(self, *, directory: bool, recursive: bool) -> str:
        if self.outcome is DeletionExecutionOutcome.SUCCESS_VERIFIED:
            prefix = (
                "Recursively deleted directory: " if directory and recursive
                else "Deleted (empty) directory: " if directory
                else "Deleted: "
            )
            return (
                f"{prefix}SUCCESS_VERIFIED "
                f"({self.affected_entries} planned entries removed and verified)"
            )
        return f"ERROR: {self.outcome.value.upper()} ({self.reason_code})"


_DEFAULT_DELETION_LEDGER = DeletionPermitLedger()
_PENDING_DELETION: contextvars.ContextVar[_PendingDeletion | None] = contextvars.ContextVar(
    "osia_pending_deletion", default=None,
)


def _entry_type(path: str, info: os.stat_result) -> tuple[InventoryEntryType, str]:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    if attributes & 0x400:
        directory_tag = "_dir" if attributes & 0x10 else "_file"
        return InventoryEntryType.REPARSE_POINT, f"reparse{directory_tag}"
    if stat_module.S_ISLNK(info.st_mode):
        return InventoryEntryType.SYMLINK, "symlink"
    if os.path.ismount(path) and os.path.dirname(path) != path:
        return InventoryEntryType.MOUNT_POINT, "mount"
    if stat_module.S_ISDIR(info.st_mode):
        return InventoryEntryType.FOLDER, ""
    if stat_module.S_ISREG(info.st_mode):
        return InventoryEntryType.FILE, ""
    return InventoryEntryType.OTHER, "other"


def _canonical_root(path: str) -> str:
    lexical = os.path.abspath(os.path.normpath(path))
    try:
        info = os.lstat(lexical)
    except FileNotFoundError as exc:
        raise DeletionPlanError("deletion_root_missing") from exc
    except PermissionError as exc:
        raise DeletionPlanError("inventory_access_denied") from exc
    except OSError as exc:
        raise DeletionPlanError("inventory_root_unreadable") from exc
    entry_type, _ = _entry_type(lexical, info)
    if entry_type in {InventoryEntryType.SYMLINK, InventoryEntryType.REPARSE_POINT}:
        return os.path.join(os.path.realpath(os.path.dirname(lexical)), os.path.basename(lexical))
    return os.path.realpath(lexical)


def _windows_file_identity(path: str) -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", ctypes.c_uint32),
            ("creation_time", _FileTime),
            ("access_time", _FileTime),
            ("write_time", _FileTime),
            ("volume_serial", ctypes.c_uint32),
            ("size_high", ctypes.c_uint32),
            ("size_low", ctypes.c_uint32),
            ("links", ctypes.c_uint32),
            ("file_index_high", ctypes.c_uint32),
            ("file_index_low", ctypes.c_uint32),
        ]

    kernel32 = ctypes.windll.kernel32
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        path,
        0,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return None
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    try:
        info = _ByHandleFileInformation()
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)]
        get_info.restype = ctypes.c_int
        if not get_info(handle, ctypes.byref(info)):
            return None
        file_id = (int(info.file_index_high) << 32) | int(info.file_index_low)
        if file_id == 0:
            return None
        return int(info.volume_serial), file_id
    finally:
        close_handle(handle)


def _entry(root_index: int, root: str, path: str, info: os.stat_result) -> DeletionInventoryEntry:
    entry_type, link_tag = _entry_type(path, info)
    relative = os.path.relpath(path, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise DeletionPlanError("inventory_path_escape")
    size = int(info.st_size) if entry_type is InventoryEntryType.FILE else 0
    device = int(getattr(info, "st_dev", 0) or 0)
    inode = int(getattr(info, "st_ino", 0) or 0)
    windows_identity = _windows_file_identity(path)
    if windows_identity is not None:
        device, inode = windows_identity
    elif inode == 0:
        if sys.platform == "win32":
            raise DeletionPlanError("inconsistent_filesystem_identity")
        # Deterministic fixture fallback for non-Windows adapters.
        inode = int(canonical_hash({"canonical_path": os.path.normcase(path)})[:16], 16)
    return DeletionInventoryEntry(
        root_index=root_index,
        relative_path=relative,
        entry_type=entry_type,
        size=max(0, size),
        device_id=device,
        object_id=inode,
        volume_ref=canonical_hash({"device": device})[:32],
        link_tag=link_tag,
        mtime_ns=int(getattr(info, "st_mtime_ns", 0) or 0),
        _seal=_ENTRY_SEAL,
    )


def _enumerate_root(root_index: int, root: str, recursive: bool) -> tuple[DeletionInventoryEntry, ...]:
    try:
        root_info = os.lstat(root)
    except FileNotFoundError as exc:
        raise DeletionPlanError("deletion_root_disappeared") from exc
    except PermissionError as exc:
        raise DeletionPlanError("inventory_access_denied") from exc
    root_entry = _entry(root_index, root, root, root_info)
    if root_entry.entry_type is not InventoryEntryType.FOLDER:
        return (root_entry,)
    if not recursive:
        try:
            with os.scandir(root) as iterator:
                if next(iterator, None) is not None:
                    raise DeletionPlanError("non_recursive_directory_not_empty")
        except PermissionError as exc:
            raise DeletionPlanError("inventory_access_denied") from exc
        except OSError as exc:
            raise DeletionPlanError("inventory_directory_unreadable") from exc
        return (root_entry,)
    entries = [root_entry]
    visited = {(root_entry.device_id, root_entry.object_id)}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(tuple(iterator), key=lambda item: os.path.normcase(item.name))
        except PermissionError as exc:
            raise DeletionPlanError("inventory_access_denied") from exc
        except OSError as exc:
            raise DeletionPlanError("inventory_directory_unreadable") from exc
        descend: list[str] = []
        for child in children:
            child_path = child.path
            try:
                info = child.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise DeletionPlanError("inventory_entry_disappeared") from exc
            except PermissionError as exc:
                raise DeletionPlanError("inventory_access_denied") from exc
            except OSError as exc:
                raise DeletionPlanError("inventory_entry_unreadable") from exc
            item = _entry(root_index, root, child_path, info)
            entries.append(item)
            if item.entry_type is InventoryEntryType.FOLDER:
                identity = (item.device_id, item.object_id)
                if identity in visited:
                    raise DeletionPlanError("inventory_traversal_loop")
                visited.add(identity)
                descend.append(child_path)
        stack.extend(reversed(descend))
    return tuple(entries)


def _root_is_sensitive(path: str) -> bool:
    normalized = os.path.abspath(os.path.normpath(path))
    if os.path.normcase(normalized) == os.path.normcase(str(Path.home())):
        return True
    sensitive_names = {
        ".config", "config", "desktop", "documents", "icloud drive",
        "onedrive", "pictures", "dropbox", "music", "videos",
    }
    if any(part.casefold() in sensitive_names for part in Path(normalized).parts):
        return True
    candidate = Path(normalized)
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").is_dir():
            return True
    return False


def _root_is_high_breadth(path: str) -> bool:
    anchor = Path(path).anchor
    return bool(
        os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(anchor))
        or os.path.normcase(path) == os.path.normcase(str(Path.home()))
    )


def _inventory_payload(
    *,
    policy: ColossalDeletionPolicy,
    platform_family: str,
    root_identities: Sequence[str],
    entries: Sequence[DeletionInventoryEntry],
    files_count: int,
    folders_count: int,
    link_count: int,
    total_bytes: int,
    recursive: bool,
) -> dict[str, Any]:
    return {
        "deletion_policy_version": policy.version,
        "platform": platform_family,
        "root_identities": list(root_identities),
        "entries": [item.canonical_payload() for item in entries],
        "counts": {
            "files": files_count,
            "folders": folders_count,
            "links": link_count,
        },
        "total_bytes": total_bytes,
        "recursive": recursive,
    }


def build_deletion_plan(
    targets: Sequence[str | os.PathLike[str]],
    *,
    recursive: bool,
    operation_id: str | None = None,
    registry: SystemResourceRegistry | None = None,
    policy: ColossalDeletionPolicy = DEFAULT_COLOSSAL_POLICY,
    clock=None,
    ttl_seconds: float = DEFAULT_PLAN_TTL_SECONDS,
) -> DeletionPlan:
    if not isinstance(policy, ColossalDeletionPolicy):
        raise DeletionPlanError("invalid_colossal_policy")
    raw_targets = tuple(os.fspath(item) for item in targets)
    if not raw_targets or any(not isinstance(item, str) or not item.strip() for item in raw_targets):
        raise DeletionPlanError("invalid_deletion_targets")
    if any(re.match(r"^(?:\\\\[.?]\\|/dev/(?:sd|nvme|mapper/))", item, re.IGNORECASE) for item in raw_targets):
        raise DeletionPlanError("unsupported_device_path")
    roots = tuple(_canonical_root(item) for item in raw_targets)
    key = (lambda value: os.path.normcase(os.path.normpath(value)))
    if len({key(root) for root in roots}) != len(roots):
        raise DeletionPlanError("duplicate_deletion_root")
    ordered_roots = tuple(sorted(roots, key=key))
    for index, left in enumerate(ordered_roots):
        for right in ordered_roots[index + 1:]:
            try:
                if os.path.commonpath((left, right)) == left:
                    raise DeletionPlanError("overlapping_deletion_roots")
            except ValueError as exc:
                raise DeletionPlanError("incompatible_deletion_roots") from exc
    all_entries: list[DeletionInventoryEntry] = []
    for index, root in enumerate(ordered_roots):
        all_entries.extend(_enumerate_root(index, root, bool(recursive)))
    entries = tuple(sorted(
        all_entries,
        key=lambda item: (item.root_index, item.relative_path.replace("\\", "/")),
    ))
    files_count = sum(item.entry_type is InventoryEntryType.FILE for item in entries)
    folders_count = sum(item.entry_type is InventoryEntryType.FOLDER for item in entries)
    link_count = sum(item.entry_type in {
        InventoryEntryType.SYMLINK,
        InventoryEntryType.REPARSE_POINT,
        InventoryEntryType.MOUNT_POINT,
    } for item in entries)
    total_bytes = sum(item.size for item in entries)
    active_registry = registry or get_default_system_resource_registry()
    operation = DestructiveOperation.RECURSIVE_DELETE if recursive else DestructiveOperation.DELETE
    evidence: list[SystemResourceEvidence] = [
        active_registry.classify_path(root, operation=operation, follow_leaf=False)
        for root in ordered_roots
    ]
    for item in entries:
        if item.entry_type is InventoryEntryType.MOUNT_POINT:
            absolute = os.path.join(
                ordered_roots[item.root_index],
                "" if item.relative_path == "." else item.relative_path,
            )
            evidence.append(active_registry.classify_path(absolute, operation=operation, follow_leaf=False))
    root_identities = tuple(canonical_hash({
        "platform": active_registry.platform_family,
        "canonical_root": os.path.normcase(root),
        "installation": active_registry.current_os_installation_identity,
    }) for root in ordered_roots)
    file_folder_count = files_count + folders_count
    object_count = len(entries)
    reason_codes: list[str] = []
    risk_score = 0
    if recursive and len(entries) > len(ordered_roots):
        risk_score += 1
        reason_codes.append("recursive_non_empty")
    if object_count >= policy.object_risk_limit:
        risk_score += 1
        reason_codes.append("object_breadth")
    if total_bytes >= policy.byte_risk_limit:
        risk_score += 1
        reason_codes.append("byte_breadth")
    if len(ordered_roots) >= 2:
        risk_score += 1
        reason_codes.append("multiple_roots")
    if any(_root_is_sensitive(root) for root in ordered_roots):
        risk_score += 2
        reason_codes.append("sensitive_scope")
    hard = False
    if file_folder_count >= policy.object_hard_limit:
        hard = True
        reason_codes.append("hard_object_threshold")
    if total_bytes >= policy.byte_hard_limit:
        hard = True
        reason_codes.append("hard_byte_threshold")
    if len(ordered_roots) >= policy.root_hard_limit:
        hard = True
        reason_codes.append("hard_root_threshold")
    if any(_root_is_high_breadth(root) for root in ordered_roots):
        hard = True
        reason_codes.append("high_breadth_root")
    colossal = hard or risk_score >= policy.risk_score_limit
    payload = _inventory_payload(
        policy=policy,
        platform_family=active_registry.platform_family,
        root_identities=root_identities,
        entries=entries,
        files_count=files_count,
        folders_count=folders_count,
        link_count=link_count,
        total_bytes=total_bytes,
        recursive=bool(recursive),
    )
    # Validate canonical serializability before hashing; locale never participates.
    canonical_serialize(payload)
    now = float((clock or time.time)())
    ttl = max(1.0, min(float(ttl_seconds), 120.0))
    return DeletionPlan(
        schema_version=DELETION_PLAN_SCHEMA_VERSION,
        policy_version=policy.version,
        plan_id=secrets.token_hex(16),
        operation_id=operation_id or secrets.token_hex(16),
        platform_family=active_registry.platform_family,
        created_at=now,
        expires_at=now + ttl,
        inventory_reliability=InventoryReliability.COMPLETE,
        revalidation_state=RevalidationState.PENDING,
        files_count=files_count,
        folders_count=folders_count,
        link_reparse_count=link_count,
        total_bytes=total_bytes,
        root_count=len(ordered_roots),
        recursive=bool(recursive),
        colossal_deletion=colossal,
        risk_score=risk_score,
        reason_codes=tuple(sorted(set(reason_codes))),
        inventory_hash=canonical_hash(payload),
        root_identities=root_identities,
        entries=entries,
        system_evidence=tuple(evidence),
        _private_roots=ordered_roots,
        _policy=policy,
        _seal=_PLAN_SEAL,
    )


def revalidate_deletion_plan(
    plan: DeletionPlan,
    *,
    registry: SystemResourceRegistry | None = None,
    clock=None,
) -> DeletionPlan:
    if not isinstance(plan, DeletionPlan):
        raise DeletionPlanError("untrusted_deletion_plan")
    now = float((clock or time.time)())
    if plan.expires_at <= now:
        raise DeletionPlanError("deletion_plan_expired")
    rebuilt = build_deletion_plan(
        plan._local_roots(),
        recursive=plan.recursive,
        operation_id=plan.operation_id,
        registry=registry,
        policy=plan._policy,
        clock=lambda: now,
        ttl_seconds=max(1.0, plan.expires_at - now),
    )
    exact = (
        rebuilt.policy_version == plan.policy_version
        and rebuilt.operation_id == plan.operation_id
        and rebuilt.root_identities == plan.root_identities
        and rebuilt.files_count == plan.files_count
        and rebuilt.folders_count == plan.folders_count
        and rebuilt.link_reparse_count == plan.link_reparse_count
        and rebuilt.total_bytes == plan.total_bytes
        and sensitive_hash_equal(rebuilt.inventory_hash, plan.inventory_hash)
        and tuple(item.public_metadata() for item in rebuilt.system_evidence)
        == tuple(item.public_metadata() for item in plan.system_evidence)
    )
    if not exact:
        raise DeletionPlanError("deletion_plan_changed")
    return replace(plan, revalidation_state=RevalidationState.VALIDATED)


def issue_pending_deletion(
    tool_name: str,
    arguments: Mapping[str, Any],
    plan: DeletionPlan,
    *,
    policy_epoch: int,
    confirmation_strength: str,
    ledger: DeletionPermitLedger | None = None,
) -> DeletionPermit:
    from osia.tool_contracts import normalized_call_hash

    clear_pending_deletion()
    active_ledger = ledger or _DEFAULT_DELETION_LEDGER
    permit = active_ledger.issue(
        plan,
        policy_epoch=policy_epoch,
        confirmation_strength=confirmation_strength,
    )
    _PENDING_DELETION.set(_PendingDeletion(
        tool_name=str(tool_name),
        call_hash=normalized_call_hash(tool_name, arguments),
        plan=plan,
        permit=permit,
        ledger=active_ledger,
    ))
    return permit


def clear_pending_deletion() -> None:
    pending = _PENDING_DELETION.get()
    _PENDING_DELETION.set(None)
    if pending is not None:
        pending.ledger.cancel(pending.permit)


def pending_deletion_matches(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    from osia.tool_contracts import normalized_call_hash

    pending = _PENDING_DELETION.get()
    return bool(
        pending is not None
        and pending.tool_name == str(tool_name)
        and sensitive_hash_equal(
            pending.call_hash,
            normalized_call_hash(tool_name, arguments),
        )
    )


def consume_pending_deletion_for_dispatch(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    policy_epoch: int,
    registry: SystemResourceRegistry | None = None,
) -> DeletionDispatch:
    from osia.tool_contracts import normalized_call_hash

    pending = _PENDING_DELETION.get()
    _PENDING_DELETION.set(None)
    if pending is None:
        raise DeletionPlanError("deletion_authorization_missing")
    try:
        if pending.tool_name != str(tool_name) or not sensitive_hash_equal(
            pending.call_hash, normalized_call_hash(tool_name, arguments)
        ):
            raise DeletionPlanError("deletion_call_mismatch")
        validated_plan = revalidate_deletion_plan(pending.plan, registry=registry)
        consumed = pending.ledger.consume(
            pending.permit,
            validated_plan,
            policy_epoch=policy_epoch,
        )
        return DeletionDispatch(plan=validated_plan, permit=consumed, _seal=_DISPATCH_SEAL)
    except Exception:
        pending.ledger.cancel(pending.permit)
        raise


def _absolute_entry(plan: DeletionPlan, item: DeletionInventoryEntry) -> str:
    root = plan._local_roots()[item.root_index]
    if item.relative_path in {"", "."}:
        return root
    candidate = os.path.normpath(os.path.join(root, item.relative_path))
    try:
        if os.path.commonpath((root, candidate)) != root:
            raise DeletionPlanError("inventory_path_escape")
    except ValueError as exc:
        raise DeletionPlanError("inventory_path_escape") from exc
    return candidate


def _verify_affected(plan: DeletionPlan) -> tuple[bool, tuple[tuple[int, str], ...]]:
    affected: list[tuple[int, str]] = []
    for item in plan.entries:
        try:
            os.lstat(_absolute_entry(plan, item))
        except FileNotFoundError:
            affected.append((item.root_index, item.relative_path))
        except OSError:
            return False, tuple(affected)
    return True, tuple(affected)


def _entry_still_matches(plan: DeletionPlan, item: DeletionInventoryEntry, target: str) -> bool:
    info = os.lstat(target)
    current = _entry(item.root_index, plan._local_roots()[item.root_index], target, info)
    identity_fields = (
        "entry_type", "device_id", "object_id", "volume_ref", "link_tag",
    )
    if any(getattr(current, name) != getattr(item, name) for name in identity_fields):
        return False
    if item.entry_type is InventoryEntryType.FOLDER:
        # Child deletion changes parent directory timestamps; stable identity is
        # the invariant immediately before its final rmdir.
        return True
    return current.canonical_payload() == item.canonical_payload()


def execute_deletion_dispatch(dispatch: DeletionDispatch) -> DeletionExecutionResult:
    if not isinstance(dispatch, DeletionDispatch):
        raise DeletionPlanError("invalid_deletion_dispatch")
    plan = dispatch.plan
    ordered = sorted(
        plan.entries,
        key=lambda item: (
            item.entry_type is not InventoryEntryType.FOLDER,
            item.relative_path.count(os.sep) + item.relative_path.count("/"),
            item.relative_path,
        ),
        reverse=True,
    )
    deleted = 0
    try:
        for item in ordered:
            target = _absolute_entry(plan, item)
            if not _entry_still_matches(plan, item, target):
                raise DeletionPlanError("entry_identity_changed_before_io")
            if (
                item.entry_type in {InventoryEntryType.FOLDER, InventoryEntryType.MOUNT_POINT}
                or item.link_tag == "reparse_dir"
            ):
                os.rmdir(target)
            else:
                os.unlink(target)
            deleted += 1
    except Exception:
        known, affected = _verify_affected(plan)
        if not known:
            return DeletionExecutionResult(
                DeletionExecutionOutcome.OUTCOME_UNKNOWN,
                len(plan.entries),
                len(affected),
                "postcondition_unverifiable",
                affected,
            )
        if not affected:
            return DeletionExecutionResult(
                DeletionExecutionOutcome.CANCELLED_PROVEN,
                len(plan.entries),
                0,
                "dispatch_failed_before_effect",
                (),
            )
        return DeletionExecutionResult(
            DeletionExecutionOutcome.PARTIAL_EFFECT_VERIFIED,
            len(plan.entries),
            len(affected),
            "partial_effect_known",
            affected,
        )
    known, affected = _verify_affected(plan)
    if not known:
        return DeletionExecutionResult(
            DeletionExecutionOutcome.OUTCOME_UNKNOWN,
            len(plan.entries),
            deleted,
            "postcondition_unverifiable",
            (),
        )
    if len(affected) != len(plan.entries):
        return DeletionExecutionResult(
            DeletionExecutionOutcome.PARTIAL_EFFECT_VERIFIED,
            len(plan.entries),
            len(affected),
            "postcondition_partial",
            affected,
        )
    return DeletionExecutionResult(
        DeletionExecutionOutcome.SUCCESS_VERIFIED,
        len(plan.entries),
        len(affected),
        "all_entries_removed",
        affected,
    )


def verify_external_deletion_dispatch(
    dispatch: DeletionDispatch,
) -> DeletionExecutionResult:
    """Verify an MCP deletion from local postconditions, never its response text."""

    if not isinstance(dispatch, DeletionDispatch):
        raise DeletionPlanError("invalid_deletion_dispatch")
    plan = dispatch.plan
    known, affected = _verify_affected(plan)
    if not known:
        return DeletionExecutionResult(
            DeletionExecutionOutcome.OUTCOME_UNKNOWN,
            len(plan.entries),
            len(affected),
            "external_postcondition_unverifiable",
            affected,
        )
    if len(affected) == len(plan.entries):
        return DeletionExecutionResult(
            DeletionExecutionOutcome.SUCCESS_VERIFIED,
            len(plan.entries),
            len(affected),
            "all_entries_removed",
            affected,
        )
    if not affected:
        return DeletionExecutionResult(
            DeletionExecutionOutcome.CANCELLED_PROVEN,
            len(plan.entries),
            0,
            "external_dispatch_had_no_effect",
            (),
        )
    return DeletionExecutionResult(
        DeletionExecutionOutcome.PARTIAL_EFFECT_VERIFIED,
        len(plan.entries),
        len(affected),
        "external_partial_effect_known",
        affected,
    )


_FORBIDDEN_MODEL_FIELDS = frozenset({
    "system_destruction", "colossal_deletion", "inventory_hash",
    "reinforced_approval", "reinforced_approved", "platform_resource_evidence",
})
_COMPLEX_SHELL_RE = re.compile(
    r"(?:[\r\n;&|<>`]|\$\(|\b(?:for|foreach|while|do)\b|\bfind\b[^\r\n]*\s-delete\b|\bxargs\b)",
    re.IGNORECASE,
)
_RAW_DESTRUCTION_RE = re.compile(
    r"\b(?:format|format-volume|remove-partition|remove-volume|clear-disk|diskpart|"
    r"mkfs(?:\.[a-z0-9]+)?|wipefs|sgdisk|fdisk|parted)\b|\bdd\b[^\r\n]*\bof=",
    re.IGNORECASE,
)


def _literal_token(value: str) -> str:
    token = str(value).strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        token = token[1:-1]
    if not token or any(char in token for char in ("*", "?", "[", "]", "$", "%", "`")):
        raise DeletionPlanError("dynamic_shell_target_rejected")
    if token.startswith("~"):
        raise DeletionPlanError("dynamic_shell_target_rejected")
    return token


def _split_shell(text: str, *, posix: bool) -> list[str]:
    try:
        return shlex.split(text, posix=posix)
    except (TypeError, ValueError) as exc:
        raise DeletionPlanError("malformed_shell_quoting") from exc


def parse_shell_deletion(
    command: str,
    *,
    platform_family: str | None = None,
) -> ShellDeletionSpec | None:
    text = str(command or "").strip()
    if not text:
        return None
    if re.match(r"(?i)^\s*reg(?:\.exe)?\s+delete\b", text) or re.match(
        r"(?is)^\s*remove-item\b.*?-literalpath\s+"
        r"(?:\"|')?(?:registry::)?(?:hkey_[a-z_]+|hk[a-z]{1,2}):?\\",
        text,
    ):
        # Registry deletion is classified by the system-mutation barrier.  It
        # is not a filesystem inventory and must never be coerced into one.
        return None
    first_match = re.match(r"^\s*(\"[^\"]+\"|'[^']+'|\S+)", text)
    first = (
        os.path.basename(first_match.group(1).strip("'\"").replace("\\", "/"))
        .lower()
        if first_match
        else ""
    )
    direct_delete = first in {
        "remove-item", "remove-item.exe", "del", "del.exe", "erase",
        "erase.exe", "rd", "rd.exe", "rmdir", "rmdir.exe", "rm",
        "unlink", "cmd", "cmd.exe",
    }
    scripted_delete = first in {
        "find", "xargs", "bash", "sh", "zsh", "fish", "powershell",
        "powershell.exe", "pwsh", "pwsh.exe", "sudo", "env", "for",
        "foreach", "while", "do",
    } and bool(re.search(
        r"(?i)\b(?:remove-item|del|erase|rmdir|rm|unlink|find|xargs)\b|\s-delete\b",
        text,
    ))
    raw_delete = bool(re.match(
        r"(?is)^\s*(?:remove-partition|remove-volume|clear-disk|format-volume|"
        r"format\b|mkfs|wipefs|sgdisk|fdisk|parted|dd\b.*\bof=)",
        text,
    ))
    destructive_word = direct_delete or scripted_delete or raw_delete
    if not destructive_word:
        return None
    if _COMPLEX_SHELL_RE.search(text):
        raise DeletionPlanError("complex_deletion_shell_rejected")
    if _RAW_DESTRUCTION_RE.search(text):
        raise DeletionPlanError("raw_system_destruction_shell_rejected")
    family = str(platform_family or ("windows" if os.name == "nt" else "posix")).lower()
    posix = family not in {"windows", "win32", "powershell", "cmd"}
    parts = _split_shell(text, posix=posix)
    if not parts:
        return None
    executable = os.path.basename(parts[0].strip("'\"")).lower()
    if executable in {"cmd", "cmd.exe"}:
        if len(parts) < 3 or parts[1].lower() not in {"/c", "/k"}:
            raise DeletionPlanError("unsupported_cmd_deletion")
        return parse_shell_deletion(" ".join(parts[2:]), platform_family="cmd")
    args = parts[1:]
    recursive = False
    directory = False
    targets: list[str] = []
    if executable in {"remove-item", "remove-item.exe"}:
        lowered = [item.lower() for item in args]
        if "-path" in lowered or "-literalpath" not in lowered:
            raise DeletionPlanError("powershell_literal_path_required")
        recursive = "-recurse" in lowered
        directory = recursive
        index = lowered.index("-literalpath") + 1
        raw_targets = [item for item in args[index:] if not item.startswith("-")]
        for raw in raw_targets:
            for item in raw.split(","):
                if item.strip():
                    targets.append(_literal_token(item))
    elif posix and executable in {"rm", "unlink", "rmdir"}:
        for item in args:
            if item == "--":
                continue
            if item.startswith("-"):
                allowed = set(item[1:])
                if not allowed <= {"r", "R", "f", "d"}:
                    raise DeletionPlanError("unsupported_deletion_option")
                recursive = recursive or bool(allowed & {"r", "R"})
                continue
            targets.append(_literal_token(item))
        directory = executable == "rmdir" or recursive
        if executable == "unlink" and len(targets) != 1:
            raise DeletionPlanError("unlink_requires_one_target")
    elif executable in {"rm", "del", "erase", "rd", "rmdir"}:
        for item in args:
            if executable == "rm" and item.startswith("-"):
                allowed = set(item[1:])
                if not allowed <= {"r", "R", "f", "d"}:
                    raise DeletionPlanError("unsupported_deletion_option")
                recursive = recursive or bool(allowed & {"r", "R"})
                continue
            if item.startswith("/") and re.fullmatch(r"/[sq]+", item, re.IGNORECASE):
                recursive = recursive or "s" in item.lower()
                continue
            targets.append(_literal_token(item))
        directory = executable in {"rd", "rmdir"} or recursive
    else:
        raise DeletionPlanError("unsupported_deletion_shell")
    if not targets:
        raise DeletionPlanError("deletion_shell_target_missing")
    return ShellDeletionSpec(tuple(targets), recursive=recursive, directory_operation=directory)


def prepare_deletion_call(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    mcp_destructive: bool = False,
    registry: SystemResourceRegistry | None = None,
    policy: ColossalDeletionPolicy = DEFAULT_COLOSSAL_POLICY,
) -> DeletionPreflight:
    values = dict(arguments) if isinstance(arguments, Mapping) else {}
    if _FORBIDDEN_MODEL_FIELDS.intersection(values):
        raise DeletionPlanError("untrusted_deletion_classification_field")
    tool = str(tool_name or "")
    targets: tuple[str, ...]
    recursive = False
    if tool == "delete_file":
        targets = (str(values.get("path") or ""),)
    elif tool == "delete_directory":
        targets = (str(values.get("path") or ""),)
        recursive = bool(values.get("recursive", False))
    elif tool in {"bash", "run_command"}:
        spec = parse_shell_deletion(str(values.get("command") or ""))
        if spec is None:
            return DeletionPreflight(DeletionPreflightKind.NOT_DELETION)
        targets, recursive = spec.targets, spec.recursive
    elif mcp_destructive:
        raw = values.get("paths", values.get("targets"))
        if raw is None:
            raw = values.get("path", values.get("target"))
        if isinstance(raw, str):
            targets = (raw,)
        elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            targets = tuple(item for item in raw if isinstance(item, str))
            if len(targets) != len(raw):
                raise DeletionPlanError("mcp_deletion_targets_not_literal")
        else:
            raise DeletionPlanError("mcp_deletion_inventory_unavailable")
        recursive = bool(values.get("recursive", False))
    else:
        return DeletionPreflight(DeletionPreflightKind.NOT_DELETION)
    active_registry = registry or get_default_system_resource_registry()
    try:
        plan = build_deletion_plan(
            targets,
            recursive=recursive,
            registry=active_registry,
            policy=policy,
        )
    except DeletionPlanError as exc:
        # Inventory errors never weaken the universal barrier.  Even a missing
        # or unreadable leaf beneath a proven system root is still a prohibited
        # destructive target.
        operation = (
            DestructiveOperation.RECURSIVE_DELETE
            if recursive
            else DestructiveOperation.DELETE
        )
        for target in targets:
            evidence = active_registry.classify_path(
                target,
                operation=operation,
                follow_leaf=False,
            )
            if evidence.blocks_destruction:
                raise DeletionPlanError("system_destruction") from exc
        raise
    return DeletionPreflight(DeletionPreflightKind.PLANNED, plan)


__all__ = [
    "DEFAULT_COLOSSAL_POLICY",
    "DEFAULT_PLAN_TTL_SECONDS",
    "DELETION_PLAN_SCHEMA_VERSION",
    "DELETION_POLICY_VERSION",
    "ColossalDeletionPolicy",
    "DeletionDispatch",
    "DeletionExecutionOutcome",
    "DeletionExecutionResult",
    "DeletionInventoryEntry",
    "DeletionPermit",
    "DeletionPermitLedger",
    "DeletionPlan",
    "DeletionPlanError",
    "DeletionPreflight",
    "DeletionPreflightKind",
    "InventoryEntryType",
    "InventoryReliability",
    "RevalidationState",
    "ShellDeletionSpec",
    "build_deletion_plan",
    "clear_pending_deletion",
    "consume_pending_deletion_for_dispatch",
    "execute_deletion_dispatch",
    "issue_pending_deletion",
    "parse_shell_deletion",
    "pending_deletion_matches",
    "prepare_deletion_call",
    "revalidate_deletion_plan",
    "verify_external_deletion_dispatch",
]
