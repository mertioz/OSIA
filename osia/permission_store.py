"""Protected permission-state persistence and revocation (WOR-192).

Windows uses user-bound DPAPI without a new dependency. Other platforms expose
an explicit unavailable backend in Phase 1; they remain ASK-only while allowing
non-persistent, process-local grants.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from osia.action_policy import (
    AUTOMATIC_PERMISSION_CATEGORIES,
    POLICY_VERSION,
    Grant,
    PermissionMode,
    PermissionRequest,
    PermissionState,
)
from osia.scope_normalization import (
    ScopeNormalizationError,
    WebScopeKind,
    canonical_hash,
    canonical_serialize,
    normalize_web_scope,
    sensitive_hash_equal,
)


_JOURNAL_SCHEMA_VERSION = 1
_JOURNAL_MAX_EVENTS = 200
_JOURNAL_EVENT_CODES = {
    "full_access_enabled", "full_access_disabled", "activation_failed",
    "grant_created", "grant_expired", "grant_revoked",
    "policy_epoch_changed", "optional_constraint_changed",
    "universal_barrier_blocked", "authorization_required",
    "delegated_authority_revoked",
    "automatic_permission_changed",
}
_LOCAL_AUTH_METHODS = {
    "windows_hello", "unavailable", "deterministic_test", "legacy_local_api",
}
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_EXECUTION_TYPES = {"", "chat", "autonomous", "scheduled"}
_SAFE_AUTHORIZATION_SOURCES = {
    "", "none", "confirmation", "grant", "full_access",
}
_SAFE_CATEGORIES = {
    "unknown", "local_path", "local_paths", "directory_tree",
    "local_command", "safe_local_setting", "tool_operation",
    "connector_operation", "web_origin", "web_url", "purchase",
}
_SAFE_TOOL_CATEGORIES = {
    "unknown", "gmail", "mcp", "shell", "filesystem", "browser", "native",
}
_SAFE_JOURNAL_RESULTS = {
    "unknown", "enabled", "disabled", "updated", "created",
    "mode_changed", "full_access_enabled", "full_access_disabled",
    "constraint_changed", "one_grant", "all_grants", "grant_revoked",
    "grants_revoked", "permits_invalidated", "autonomous_run",
    "scheduled_task", "all_authority", "authority_revoked",
    "system_destruction", "colossal_deletion_review",
    "optional_constraint_failed", "acknowledgement_required",
    "second_confirmation_required", "secure_storage_unavailable",
    "store_corrupt", "protected_store_write_failed", "reauth_denied",
    "reauth_cancelled", "reauth_error", "ask_sensitive_action",
    "scope_extension_required", "unattended_sensitive_action",
    "duplicate_outcome_unknown", "duplicate_success_verified",
    "authorization_revoked", "outcome_unknown_blocked",
    "authorization_pending", "authorization_required",
    "authorization_expired", "authorization_edited",
    "blocked_outcome_unknown", "expired_grant", "revoked_grant",
    "corrupt_grant", "exhausted_grant", "changed_arguments",
    "changed_scope", "wrong_effect_class", "wrong_capability",
    "wrong_tool_category", "wrong_plan_revision", "wrong_task_revision",
    "wrong_scheduled_task", "wrong_run", "wrong_device", "wrong_machine",
    "wrong_owner", "wrong_execution_type", "wrong_policy_version",
    "ambiguous_legacy_identity", "confirmation_required",
    "automatic_permissions_changed",
}


def _default_full_access_settings() -> dict[str, Any]:
    return {
        "activation": {
            "activated_at": None,
            "local_auth_method": "",
        },
        "constraints": {
            "payment": {
                "enabled": False,
                "max_amount_minor": 0,
                "currency": "EUR",
            },
            "destinations": {
                "enabled": False,
                "origins": [],
                "connectors": [],
            },
        },
    }


def _settings_copy(state: PermissionState) -> dict[str, Any]:
    settings = state.optional_settings
    return json.loads(canonical_serialize(settings))


def _full_access_settings_for_update(state: PermissionState) -> dict[str, Any]:
    settings = _settings_copy(state)
    current = settings.get("full_access")
    defaults = _default_full_access_settings()
    if current is None:
        settings["full_access"] = defaults
        current = settings["full_access"]
    elif not isinstance(current, dict):
        raise PermissionStoreCorrupt("corrupt_permission_settings")
    activation = current.setdefault("activation", defaults["activation"])
    constraints = current.setdefault("constraints", defaults["constraints"])
    if not isinstance(activation, dict) or not isinstance(constraints, dict):
        raise PermissionStoreCorrupt("corrupt_permission_settings")
    for name, value in defaults["activation"].items():
        activation.setdefault(name, value)
    for name, value in defaults["constraints"].items():
        constraints.setdefault(name, value)
    automatic = settings.get("automatic_categories")
    if automatic is None:
        enabled = state.permission_mode == PermissionMode.FULL_ACCESS
        settings["automatic_categories"] = {
            category: enabled for category in AUTOMATIC_PERMISSION_CATEGORIES
        }
    elif not isinstance(automatic, dict):
        raise PermissionStoreCorrupt("corrupt_permission_settings")
    return settings


def _validate_permission_settings(settings: Mapping[str, Any]) -> None:
    if not isinstance(settings, Mapping):
        raise ValueError("permission_settings_not_mapping")
    automatic = settings.get("automatic_categories")
    if automatic is not None and (
        not isinstance(automatic, Mapping)
        or set(automatic) != set(AUTOMATIC_PERMISSION_CATEGORIES)
        or any(not isinstance(value, bool) for value in automatic.values())
    ):
        raise ValueError("invalid_automatic_permission_categories")
    full_access = settings.get("full_access")
    if full_access is not None:
        if not isinstance(full_access, Mapping):
            raise ValueError("invalid_full_access_settings")
        activation = full_access.get("activation", {})
        constraints = full_access.get("constraints", {})
        if not isinstance(activation, Mapping) or not isinstance(constraints, Mapping):
            raise ValueError("invalid_full_access_settings")
        activated_at = activation.get("activated_at")
        if activated_at is not None and not isinstance(activated_at, (int, float)):
            raise ValueError("invalid_activation_time")
        method = activation.get("local_auth_method", "")
        if method and method not in _LOCAL_AUTH_METHODS:
            raise ValueError("invalid_local_auth_method")
        payment = constraints.get("payment", {})
        destinations = constraints.get("destinations", {})
        if not isinstance(payment, Mapping) or not isinstance(destinations, Mapping):
            raise ValueError("invalid_full_access_constraints")
        if not isinstance(payment.get("enabled", False), bool):
            raise ValueError("invalid_payment_constraint")
        maximum = payment.get("max_amount_minor", 0)
        currency = payment.get("currency", "EUR")
        if type(maximum) is not int or maximum < 0:
            raise ValueError("invalid_payment_constraint")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("invalid_payment_constraint")
        if not isinstance(destinations.get("enabled", False), bool):
            raise ValueError("invalid_destination_constraint")
        origins = destinations.get("origins", [])
        connectors = destinations.get("connectors", [])
        if not isinstance(origins, list) or not all(isinstance(x, str) for x in origins):
            raise ValueError("invalid_destination_constraint")
        if not isinstance(connectors, list) or not all(
            isinstance(x, str) and _SAFE_CODE_RE.fullmatch(x) for x in connectors
        ):
            raise ValueError("invalid_destination_constraint")
        for origin in origins:
            normalized = normalize_web_scope(origin, kind=WebScopeKind.EXACT_ORIGIN)
            canonical = json.loads(normalized.private_canonical)["identity"]
            if canonical != origin:
                raise ValueError("invalid_destination_constraint")
    journal = settings.get("journal")
    if journal is not None:
        if (
            not isinstance(journal, Mapping)
            or journal.get("schema_version") != _JOURNAL_SCHEMA_VERSION
            or not isinstance(journal.get("events"), list)
            or len(journal["events"]) > _JOURNAL_MAX_EVENTS
        ):
            raise ValueError("invalid_permission_journal")
        for event in journal["events"]:
            if not isinstance(event, Mapping) or set(event) != {
                "timestamp", "event_code", "execution_type", "safe_category",
                "authorization_source", "result",
            }:
                raise ValueError("invalid_permission_journal_event")
            if not isinstance(event["timestamp"], (int, float)):
                raise ValueError("invalid_permission_journal_event")
            if event["event_code"] not in _JOURNAL_EVENT_CODES:
                raise ValueError("invalid_permission_journal_event")
            for key in (
                "execution_type", "safe_category", "authorization_source", "result",
            ):
                value = event[key]
                if value and (
                    not isinstance(value, str) or not _SAFE_CODE_RE.fullmatch(value)
                ):
                    raise ValueError("invalid_permission_journal_event")


def _safe_code(value: Any, fallback: str = "") -> str:
    candidate = str(value or "")
    return candidate if _SAFE_CODE_RE.fullmatch(candidate) else fallback


def _safe_choice(value: Any, allowed: set[str], fallback: str = "") -> str:
    candidate = str(value or "")
    return candidate if candidate in allowed else fallback


def _append_event(
    settings: dict[str, Any],
    event_code: str,
    *,
    execution_type: str = "",
    safe_category: str = "",
    authorization_source: str = "",
    result: str = "",
    timestamp: float | None = None,
) -> None:
    if event_code not in _JOURNAL_EVENT_CODES:
        raise ValueError("invalid_journal_event_code")
    journal = settings.setdefault("journal", {
        "schema_version": _JOURNAL_SCHEMA_VERSION,
        "events": [],
    })
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != _JOURNAL_SCHEMA_VERSION
        or not isinstance(journal.get("events"), list)
    ):
        raise PermissionStoreCorrupt("corrupt_permission_journal")
    event = {
        "timestamp": float(time.time() if timestamp is None else timestamp),
        "event_code": event_code,
        "execution_type": _safe_choice(
            execution_type, _SAFE_EXECUTION_TYPES,
        ),
        "safe_category": _safe_choice(
            safe_category, _SAFE_CATEGORIES, "unknown",
        ),
        "authorization_source": _safe_choice(
            authorization_source, _SAFE_AUTHORIZATION_SOURCES,
        ),
        "result": _safe_choice(result, _SAFE_JOURNAL_RESULTS, "unknown"),
    }
    journal["events"] = (journal["events"] + [event])[-_JOURNAL_MAX_EVENTS:]


class PermissionStoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PersistentPermissionUnavailable(PermissionStoreError):
    pass


class PermissionStoreCorrupt(PermissionStoreError):
    pass


@dataclass(frozen=True)
class BackendAvailability:
    available: bool
    backend: str
    user_bound: bool
    reason_code: str = ""


class SecureStorageBackend:
    """Minimal future-backend interface; implementations protect opaque bytes."""

    @property
    def availability(self) -> BackendAvailability:
        raise NotImplementedError

    def protect(self, plaintext: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, protected: bytes) -> bytes:
        raise NotImplementedError


class UnavailableSecureStorageBackend(SecureStorageBackend):
    def __init__(self, reason_code: str = "secure_storage_unavailable"):
        self._availability = BackendAvailability(
            False, "unavailable", False, reason_code,
        )

    @property
    def availability(self) -> BackendAvailability:
        return self._availability

    def protect(self, plaintext: bytes) -> bytes:
        del plaintext
        raise PersistentPermissionUnavailable(self._availability.reason_code)

    def unprotect(self, protected: bytes) -> bytes:
        del protected
        raise PersistentPermissionUnavailable(self._availability.reason_code)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDPAPISecureStorageBackend(SecureStorageBackend):
    """Current-user DPAPI with authenticated ciphertext and no stored key."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1
    _ENTROPY = b"OSIA/WOR-192/permission-state/v1"

    def __init__(self) -> None:
        self._crypt32 = None
        self._kernel32 = None
        reason = "not_windows"
        if sys.platform == "win32":
            try:
                self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
                self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self._configure_functions()
                reason = ""
            except Exception:
                self._crypt32 = None
                self._kernel32 = None
                reason = "dpapi_unavailable"
        self._availability = BackendAvailability(
            self._crypt32 is not None,
            "windows_dpapi",
            self._crypt32 is not None,
            reason,
        )

    def _configure_functions(self) -> None:
        pointer = ctypes.POINTER(_DATA_BLOB)
        self._crypt32.CryptProtectData.argtypes = [
            pointer, ctypes.c_wchar_p, pointer, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ulong, pointer,
        ]
        self._crypt32.CryptProtectData.restype = ctypes.c_int
        self._crypt32.CryptUnprotectData.argtypes = [
            pointer, ctypes.POINTER(ctypes.c_wchar_p), pointer, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_ulong, pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = ctypes.c_int
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @property
    def availability(self) -> BackendAvailability:
        return self._availability

    @staticmethod
    def _input_blob(data: bytes) -> tuple[_DATA_BLOB, Any]:
        if not isinstance(data, bytes):
            raise PermissionStoreError("dpapi_bytes_required")
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _DATA_BLOB(
            len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    def _require_available(self) -> None:
        if not self.availability.available:
            raise PersistentPermissionUnavailable(
                self.availability.reason_code or "dpapi_unavailable"
            )

    def protect(self, plaintext: bytes) -> bytes:
        self._require_available()
        source, source_buffer = self._input_blob(plaintext)
        entropy, entropy_buffer = self._input_blob(self._ENTROPY)
        output = _DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "OSIA permission state",
            ctypes.byref(entropy),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        del source_buffer, entropy_buffer
        if not ok:
            raise PermissionStoreError("dpapi_protect_failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._kernel32.LocalFree(output.pbData)

    def unprotect(self, protected: bytes) -> bytes:
        self._require_available()
        source, source_buffer = self._input_blob(protected)
        entropy, entropy_buffer = self._input_blob(self._ENTROPY)
        output = _DATA_BLOB()
        description = ctypes.c_wchar_p()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        del source_buffer, entropy_buffer
        if not ok:
            raise PermissionStoreCorrupt("dpapi_unprotect_failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._kernel32.LocalFree(output.pbData)
            if description:
                self._kernel32.LocalFree(description)


def default_secure_storage_backend() -> SecureStorageBackend:
    if sys.platform == "win32":
        return WindowsDPAPISecureStorageBackend()
    return UnavailableSecureStorageBackend("platform_backend_not_implemented")


@dataclass(frozen=True)
class RevocationResult:
    changed: bool
    policy_epoch: int
    scope: str
    grant_ref: str = ""

    def public_metadata(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "policy_epoch": self.policy_epoch,
            "scope": self.scope,
            "grant_ref": self.grant_ref,
        }


def _new_integrity(grant: Grant) -> Grant:
    staged = replace(grant, integrity_metadata="0" * 64)
    return replace(
        staged,
        integrity_metadata=canonical_hash(staged._integrity_payload()),
    )


def _match_grant(
    grant: Grant,
    request: PermissionRequest,
    *,
    current_epoch: int,
    now: float,
) -> str:
    context = request.execution_context
    if not grant.integrity_is_valid():
        return "corrupt_grant"
    if grant.policy_version != POLICY_VERSION:
        return "wrong_policy_version"
    if request.ambiguous_legacy_identity or grant.identity_version != "opaque-v1":
        return "ambiguous_legacy_identity"
    if grant.execution_type != context.execution_type:
        return "wrong_execution_type"
    if grant.owner_ref != context.owner_ref:
        return "wrong_owner"
    if grant.machine_ref != context.machine_ref:
        return "wrong_machine"
    if grant.device_ref and grant.device_ref != context.device_ref:
        return "wrong_device"
    if grant.run_id != context.run_id:
        return "wrong_run"
    if grant.scheduled_task_id != context.scheduled_task_id:
        return "wrong_scheduled_task"
    if grant.execution_type.value == "autonomous":
        try:
            issued_revision = int(grant.task_revision)
            current_revision = int(context.task_revision)
        except (TypeError, ValueError, OverflowError):
            # Preserve legacy opaque revision matching.  WOR-194-generated
            # Autonomous revisions are numeric; pre-existing callers may still
            # use stable opaque labels and retain exact semantics.
            if grant.task_revision != context.task_revision:
                return "wrong_task_revision"
        # Autonomous authorization revisions are cumulative: a scope approved
        # in revision 1 remains part of revision 2 after an exact extension.
        else:
            if issued_revision < 1 or issued_revision > current_revision:
                return "wrong_task_revision"
    elif grant.task_revision != context.task_revision:
        return "wrong_task_revision"
    if grant.plan_revision != context.plan_revision:
        return "wrong_plan_revision"
    if grant.tool_category != request.tool_category:
        return "wrong_tool_category"
    if grant.capability != request.capability:
        return "wrong_capability"
    if grant.effect_class != request.effect_class:
        return "wrong_effect_class"
    if not sensitive_hash_equal(grant.scope_hash, request.scope_hash):
        return "changed_scope"
    if (
        not grant.constraints.get("scope_only", False)
        and not sensitive_hash_equal(grant.call_hash, request.normalized_call_hash)
    ):
        return "changed_arguments"
    if grant.expires_at is not None and grant.expires_at <= now:
        return "expired_grant"
    if grant.revocation_epoch != current_epoch:
        return "revoked_grant"
    if grant.exhausted:
        return "exhausted_grant"
    return "match"


class PermissionStore:
    """Thread-safe protected state plus in-memory session grants."""

    _FORMAT = "osia-permission-state-v1"

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        backend: SecureStorageBackend | None = None,
    ) -> None:
        if path is None:
            from osia.config import PRIVATE_DATA_DIR

            path = Path(PRIVATE_DATA_DIR) / "permissions.dat"
        self.path = Path(path)
        self.backend = backend or default_secure_storage_backend()
        self._lock = threading.RLock()
        self._session_grants: dict[str, Grant] = {}
        self._volatile_epoch = 0
        self._failure_epoch = 0
        self._last_good_state: PermissionState | None = None
        self._epoch_listeners: set[Callable[[int], Any]] = set()
        self._corrupt = False
        self._center_grant_tokens: dict[str, str] = {}
        self._center_group_tokens: dict[str, tuple[str, str]] = {}

    @contextmanager
    def _cross_process_mutation_lock(self):
        """Serialize protected read-modify-write cycles between PC processes."""

        if sys.platform != "win32" or not self.secure_available:
            yield
            return
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with open(lock_path, "a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    @property
    def availability(self) -> BackendAvailability:
        return self.backend.availability

    @property
    def secure_available(self) -> bool:
        return self.availability.available

    @property
    def corrupt(self) -> bool:
        with self._lock:
            return self._corrupt

    def add_epoch_listener(self, listener: Callable[[int], Any]) -> None:
        if callable(listener):
            with self._lock:
                self._epoch_listeners.add(listener)

    def remove_epoch_listener(self, listener: Callable[[int], Any]) -> None:
        with self._lock:
            self._epoch_listeners.discard(listener)

    def _notify_epoch(self, epoch: int) -> None:
        with self._lock:
            listeners = tuple(self._epoch_listeners)
        for listener in listeners:
            try:
                listener(epoch)
            except Exception:
                pass

    @staticmethod
    def _fail_closed_state(epoch: int) -> PermissionState:
        return PermissionState(
            permission_mode=PermissionMode.ASK,
            policy_epoch=max(0, int(epoch)),
            grants=(),
        )

    def _decode_state(self, protected: bytes) -> PermissionState:
        try:
            plaintext = self.backend.unprotect(protected)
            envelope = json.loads(plaintext.decode("utf-8", errors="strict"))
            if not isinstance(envelope, dict) or envelope.get("format") != self._FORMAT:
                raise ValueError("invalid_envelope")
            payload = envelope["payload"]
            expected = str(envelope["payload_hash"])
            if not sensitive_hash_equal(expected, canonical_hash(payload)):
                raise ValueError("payload_integrity_mismatch")
            state = PermissionState.from_protected_dict(payload)
            _validate_permission_settings(state.optional_settings)
        except Exception as exc:
            raise PermissionStoreCorrupt("corrupt_permission_state") from exc
        return state

    def load_state(self) -> PermissionState:
        with self._lock:
            if not self.secure_available:
                self._corrupt = False
                return self._fail_closed_state(self._volatile_epoch)
            try:
                protected = self.path.read_bytes()
            except FileNotFoundError:
                self._corrupt = False
                state = self._fail_closed_state(self._volatile_epoch)
                self._last_good_state = state
                return state
            except OSError:
                self._corrupt = True
                self._failure_epoch = max(
                    self._failure_epoch,
                    (self._last_good_state.policy_epoch + 1)
                    if self._last_good_state else 1,
                )
                return self._fail_closed_state(self._failure_epoch)
            try:
                state = self._decode_state(protected)
            except PermissionStoreCorrupt:
                self._corrupt = True
                self._failure_epoch = max(
                    self._failure_epoch,
                    (self._last_good_state.policy_epoch + 1)
                    if self._last_good_state else 1,
                )
                return self._fail_closed_state(self._failure_epoch)
            self._corrupt = False
            self._failure_epoch = 0
            self._volatile_epoch = max(self._volatile_epoch, state.policy_epoch)
            self._last_good_state = state
            return state

    def _encode_state(self, state: PermissionState) -> bytes:
        payload = state.to_protected_dict()
        plaintext = canonical_serialize({
            "format": self._FORMAT,
            "payload": payload,
            "payload_hash": canonical_hash(payload),
        }).encode("utf-8")
        return self.backend.protect(plaintext)

    def _write_state_locked(self, state: PermissionState) -> None:
        if not self.secure_available:
            raise PersistentPermissionUnavailable(
                self.availability.reason_code or "secure_storage_unavailable"
            )
        if self._corrupt:
            raise PermissionStoreCorrupt("corrupt_store_requires_explicit_reset")
        protected = self._encode_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            if sys.platform != "win32":
                os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temp_name, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        self._last_good_state = state
        self._volatile_epoch = max(self._volatile_epoch, state.policy_epoch)

    def migrate_safe_default(self) -> bool:
        """Create only an ASK state; never import authority from old settings."""

        with self._lock, self._cross_process_mutation_lock():
            if not self.secure_available or self.path.exists():
                return False
            self._write_state_locked(self._fail_closed_state(self._volatile_epoch))
            return True

    def set_permission_mode(self, mode: PermissionMode) -> PermissionState:
        """Compatibility mutator; the PC UI uses :meth:`activate_full_access`."""

        if not isinstance(mode, PermissionMode):
            raise ValueError("invalid_permission_mode")
        if mode != PermissionMode.ASK and not self.secure_available:
            raise PersistentPermissionUnavailable("secure_storage_required")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                raise PermissionStoreCorrupt("corrupt_store_requires_explicit_reset")
            if state.permission_mode == mode:
                return state
            epoch = state.policy_epoch + 1
            settings = _full_access_settings_for_update(state)
            activation = settings["full_access"]["activation"]
            if mode == PermissionMode.FULL_ACCESS:
                settings["automatic_categories"] = {
                    category: True
                    for category in AUTOMATIC_PERMISSION_CATEGORIES
                }
                activation.update({
                    "activated_at": float(time.time()),
                    "local_auth_method": "legacy_local_api",
                })
                _append_event(
                    settings, "full_access_enabled",
                    authorization_source="full_access", result="enabled",
                )
            elif state.permission_mode == PermissionMode.FULL_ACCESS:
                settings["automatic_categories"] = {
                    category: False
                    for category in AUTOMATIC_PERMISSION_CATEGORIES
                }
                _append_event(
                    settings, "full_access_disabled",
                    authorization_source="full_access", result="disabled",
                )
            _append_event(
                settings, "policy_epoch_changed", result="mode_changed",
            )
            rebased = tuple(
                _new_integrity(replace(grant, revocation_epoch=epoch))
                for grant in state.grants
            )
            new_state = replace(
                state,
                permission_mode=mode,
                policy_epoch=epoch,
                grants=rebased,
                optional_settings_canonical=canonical_serialize(settings),
            )
            self._session_grants = {
                grant_id: _new_integrity(replace(grant, revocation_epoch=epoch))
                for grant_id, grant in self._session_grants.items()
            }
            self._write_state_locked(new_state)
        self._notify_epoch(epoch)
        return new_state

    def add_grant(self, grant: Grant) -> None:
        if not isinstance(grant, Grant) or not grant.integrity_is_valid():
            raise ValueError("invalid_grant")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                raise PermissionStoreCorrupt("corrupt_store_requires_explicit_reset")
            if grant.revocation_epoch != state.policy_epoch:
                raise ValueError("grant_epoch_mismatch")
            if grant.policy_version != POLICY_VERSION:
                raise ValueError("wrong_policy_version")
            if grant.persistent:
                if not self.secure_available:
                    raise PersistentPermissionUnavailable("secure_storage_required")
                grants = tuple(
                    current for current in state.grants
                    if current.grant_id != grant.grant_id
                ) + (grant,)
                mode = (
                    PermissionMode.TASK_SCOPED
                    if state.permission_mode == PermissionMode.ASK
                    else state.permission_mode
                )
                settings = _settings_copy(state)
                _append_event(
                    settings, "grant_created",
                    execution_type=grant.execution_type.value,
                    safe_category=grant.normalized_target_scope.safe_category,
                    authorization_source="grant", result="created",
                )
                self._write_state_locked(replace(
                    state,
                    permission_mode=mode,
                    grants=grants,
                    optional_settings_canonical=canonical_serialize(settings),
                ))
            else:
                self._session_grants[grant.grant_id] = grant
                if self.secure_available:
                    settings = _settings_copy(state)
                    _append_event(
                        settings, "grant_created",
                        execution_type=grant.execution_type.value,
                        safe_category=grant.normalized_target_scope.safe_category,
                        authorization_source="grant", result="created",
                    )
                    self._write_state_locked(replace(
                        state,
                        optional_settings_canonical=canonical_serialize(settings),
                    ))

    def _all_grants_locked(self, state: PermissionState) -> tuple[Grant, ...]:
        persistent_ids = {grant.grant_id for grant in state.grants}
        session = tuple(
            grant for grant_id, grant in self._session_grants.items()
            if grant_id not in persistent_ids
        )
        return state.grants + session

    def autonomous_grant_refs_available(
        self,
        grant_refs,
        *,
        run_id: str,
        plan_revision: int,
        now: float,
    ) -> bool:
        """Validate checkpoint refs without exposing protected grant content."""

        refs = tuple(str(ref or "") for ref in (grant_refs or ()))
        if not refs:
            return True
        with self._lock:
            permission_state = self.load_state()
            by_id = {
                grant.grant_id: grant
                for grant in self._all_grants_locked(permission_state)
            }
            for grant_ref in refs:
                grant = by_id.get(grant_ref)
                if (
                    grant is None
                    or not grant.integrity_is_valid()
                    or grant.execution_type.value != "autonomous"
                    or grant.run_id != str(run_id or "")
                    or grant.plan_revision != str(int(plan_revision))
                    or grant.revocation_epoch != permission_state.policy_epoch
                    or (grant.expires_at is not None and grant.expires_at <= now)
                    or grant.exhausted
                ):
                    return False
            return True

    def scheduled_grant_refs_status(
        self,
        grant_refs,
        *,
        task_id: str,
        task_revision: int,
        authorization_binding: str,
        now: float,
    ) -> str:
        """Validate opaque Scheduled Task refs without exposing grant content."""

        refs = tuple(str(ref or "") for ref in (grant_refs or ()))
        if not refs:
            return "missing_grant"
        with self._lock:
            permission_state = self.load_state()
            by_id = {
                grant.grant_id: grant
                for grant in self._all_grants_locked(permission_state)
            }
            for grant_ref in refs:
                grant = by_id.get(grant_ref)
                if grant is None:
                    return "missing_grant"
                if not grant.integrity_is_valid():
                    return "corrupt_grant"
                if grant.policy_version != POLICY_VERSION:
                    return "wrong_policy_version"
                if grant.execution_type.value != "scheduled":
                    return "wrong_execution_type"
                if grant.run_id:
                    return "wrong_run"
                if grant.scheduled_task_id != str(task_id or ""):
                    return "wrong_scheduled_task"
                if grant.task_revision != str(int(task_revision)):
                    return "wrong_task_revision"
                if grant.plan_revision != str(authorization_binding or ""):
                    return "wrong_authorization_binding"
                if grant.revocation_epoch != permission_state.policy_epoch:
                    return "revoked_grant"
                if grant.expires_at is not None and grant.expires_at <= float(now):
                    return "expired_grant"
                if grant.exhausted:
                    return "exhausted_grant"
            return "valid"

    def find_matching_grant(
        self,
        request: PermissionRequest,
        *,
        now: float,
    ) -> tuple[Grant | None, str]:
        with self._lock:
            state = self.load_state()
            reasons: list[str] = []
            for grant in self._all_grants_locked(state):
                reason = _match_grant(
                    grant, request, current_epoch=state.policy_epoch, now=now,
                )
                if reason == "match":
                    return grant, "match"
                reasons.append(reason)
            if not reasons:
                return None, "no_grant"
            # Report the closest candidate deterministically. This remains a
            # value-free reason code and never guesses an authorization match.
            priority = {
                "corrupt_grant": 100,
                "wrong_policy_version": 95,
                "ambiguous_legacy_identity": 90,
                "exhausted_grant": 85,
                "expired_grant": 84,
                "revoked_grant": 83,
                "changed_arguments": 80,
                "changed_scope": 75,
                "wrong_effect_class": 70,
                "wrong_capability": 65,
                "wrong_tool_category": 60,
                "wrong_plan_revision": 55,
                "wrong_task_revision": 54,
                "wrong_scheduled_task": 53,
                "wrong_run": 52,
                "wrong_device": 51,
                "wrong_machine": 50,
                "wrong_owner": 49,
                "wrong_execution_type": 48,
            }
            return None, max(reasons, key=lambda reason: priority.get(reason, 0))

    def claim_grant_use(
        self,
        grant_id: str,
        request: PermissionRequest,
        *,
        now: float,
    ) -> bool:
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            persistent = next(
                (grant for grant in state.grants if grant.grant_id == grant_id),
                None,
            )
            grant = persistent or self._session_grants.get(grant_id)
            if grant is None or _match_grant(
                grant, request, current_epoch=state.policy_epoch, now=now,
            ) != "match":
                return False
            updated = _new_integrity(replace(
                grant,
                use_count=grant.use_count + 1,
                last_use_metadata_canonical=canonical_serialize({
                    "operation_id": request.operation_id,
                    "used_at": float(now),
                }),
            ))
            if persistent is not None:
                grants = tuple(
                    updated if item.grant_id == grant_id else item
                    for item in state.grants
                )
                self._write_state_locked(replace(state, grants=grants))
            else:
                self._session_grants[grant_id] = updated
            return True

    def _advance_epoch_locked(
        self,
        state: PermissionState,
        *,
        keep_persistent: tuple[Grant, ...],
        keep_session: Mapping[str, Grant],
        mode: PermissionMode | None = None,
        optional_settings: Mapping[str, Any] | None = None,
    ) -> tuple[PermissionState, int]:
        epoch = state.policy_epoch + 1
        persistent = tuple(
            _new_integrity(replace(grant, revocation_epoch=epoch))
            for grant in keep_persistent
        )
        self._session_grants = {
            grant_id: _new_integrity(replace(grant, revocation_epoch=epoch))
            for grant_id, grant in keep_session.items()
        }
        new_state = replace(
            state,
            permission_mode=mode or state.permission_mode,
            policy_epoch=epoch,
            grants=persistent,
            optional_settings_canonical=(
                state.optional_settings_canonical
                if optional_settings is None
                else canonical_serialize(optional_settings)
            ),
        )
        if self.secure_available:
            self._write_state_locked(new_state)
        else:
            self._volatile_epoch = epoch
            new_state = self._fail_closed_state(epoch)
        return new_state, epoch

    def activate_full_access(
        self,
        *,
        activation_proof,
        activated_at: float | None = None,
    ) -> PermissionState:
        """Persist permanent Full Access after the PC-only activation ceremony."""

        from osia.local_reauth import consume_full_access_activation_proof

        method = consume_full_access_activation_proof(activation_proof)
        if not self.secure_available:
            raise PersistentPermissionUnavailable("secure_storage_required")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                raise PermissionStoreCorrupt("corrupt_store_requires_explicit_reset")
            if state.permission_mode == PermissionMode.FULL_ACCESS:
                return state
            settings = _full_access_settings_for_update(state)
            settings["automatic_categories"] = {
                category: True
                for category in AUTOMATIC_PERMISSION_CATEGORIES
            }
            settings["full_access"]["activation"] = {
                "activated_at": float(
                    time.time() if activated_at is None else activated_at
                ),
                "local_auth_method": method,
            }
            _append_event(
                settings, "full_access_enabled",
                authorization_source="full_access", result="enabled",
            )
            _append_event(
                settings, "policy_epoch_changed", result="full_access_enabled",
            )
            new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=state.grants,
                keep_session=dict(self._session_grants),
                mode=PermissionMode.FULL_ACCESS,
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return new_state

    def update_automatic_permissions(
        self,
        updates: Mapping[str, bool],
        *,
        force_ask: bool = False,
    ) -> PermissionState:
        """Persist category switches and invalidate every loaded permit."""

        if (
            not isinstance(updates, Mapping)
            or not updates
            or set(updates) - set(AUTOMATIC_PERMISSION_CATEGORIES)
            or any(not isinstance(value, bool) for value in updates.values())
        ):
            raise ValueError("invalid_automatic_permission_update")
        if not self.secure_available:
            raise PersistentPermissionUnavailable("secure_storage_required")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                raise PermissionStoreCorrupt("corrupt_store_requires_explicit_reset")
            settings = _full_access_settings_for_update(state)
            current = dict(settings["automatic_categories"])
            updated = dict(current)
            updated.update(updates)
            next_mode = state.permission_mode
            if force_ask or (
                state.permission_mode == PermissionMode.FULL_ACCESS
                and not all(updated.values())
            ):
                next_mode = PermissionMode.ASK
            if updated == current and next_mode == state.permission_mode:
                return state
            settings["automatic_categories"] = updated
            if (
                state.permission_mode == PermissionMode.FULL_ACCESS
                and next_mode != PermissionMode.FULL_ACCESS
            ):
                _append_event(
                    settings, "full_access_disabled",
                    authorization_source="full_access", result="disabled",
                )
            _append_event(
                settings, "automatic_permission_changed",
                result="automatic_permissions_changed",
            )
            _append_event(
                settings, "policy_epoch_changed", result="mode_changed",
            )
            new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=state.grants,
                keep_session=dict(self._session_grants),
                mode=next_mode,
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return new_state

    def apply_default_access(self) -> PermissionState:
        return self.update_automatic_permissions(
            {
                category: False
                for category in AUTOMATIC_PERMISSION_CATEGORIES
            },
            force_ask=True,
        )

    def update_full_access_constraints(
        self,
        *,
        payment_enabled: bool,
        max_amount_minor: int,
        currency: str,
        destinations_enabled: bool,
        origins: list[str] | tuple[str, ...] | None,
        connectors: list[str] | tuple[str, ...] | None,
    ) -> PermissionState:
        """Replace exact optional limits and invalidate every loaded permit."""

        if not isinstance(payment_enabled, bool) or not isinstance(
            destinations_enabled, bool
        ):
            raise ValueError("invalid_constraint_toggle")
        if type(max_amount_minor) is not int or max_amount_minor < 0:
            raise ValueError("invalid_payment_maximum")
        currency_code = str(currency or "").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency_code):
            raise ValueError("invalid_payment_currency")
        normalized_origins: list[str] | None = None
        if origins is not None:
            normalized_origins = []
            for value in origins:
                try:
                    scope = normalize_web_scope(
                        str(value or "").strip(), kind=WebScopeKind.EXACT_ORIGIN,
                    )
                    normalized_origins.append(
                        json.loads(scope.private_canonical)["identity"]
                    )
                except (
                    KeyError, TypeError, ValueError, ScopeNormalizationError,
                ) as exc:
                    raise ValueError("invalid_destination_origin") from exc
        normalized_connectors: list[str] | None = None
        if connectors is not None:
            normalized_connectors = []
            for value in connectors:
                candidate = str(value or "").strip()
                if not _SAFE_CODE_RE.fullmatch(candidate):
                    raise ValueError("invalid_connector_allowlist")
                normalized_connectors.append(candidate)
        if not self.secure_available:
            raise PersistentPermissionUnavailable("secure_storage_required")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                raise PermissionStoreCorrupt("corrupt_store_requires_explicit_reset")
            settings = _full_access_settings_for_update(state)
            current_destinations = settings["full_access"]["constraints"][
                "destinations"
            ]
            next_constraints = {
                "payment": {
                    "enabled": payment_enabled,
                    "max_amount_minor": max_amount_minor,
                    "currency": currency_code,
                },
                "destinations": {
                    "enabled": destinations_enabled,
                    "origins": (
                        list(current_destinations.get("origins", []))
                        if normalized_origins is None
                        else sorted(set(normalized_origins))
                    ),
                    "connectors": (
                        list(current_destinations.get("connectors", []))
                        if normalized_connectors is None
                        else sorted(set(normalized_connectors))
                    ),
                },
            }
            if settings["full_access"].get("constraints") == next_constraints:
                return state
            settings["full_access"]["constraints"] = next_constraints
            _append_event(
                settings, "optional_constraint_changed", result="updated",
            )
            _append_event(
                settings, "policy_epoch_changed", result="constraint_changed",
            )
            new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=state.grants,
                keep_session=dict(self._session_grants),
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return new_state

    def append_journal_event(
        self,
        event_code: str,
        *,
        execution_type: str = "",
        safe_category: str = "",
        authorization_source: str = "",
        result: str = "",
        timestamp: float | None = None,
    ) -> bool:
        """Append one value-free event; never falls back to plaintext."""

        if event_code not in _JOURNAL_EVENT_CODES or not self.secure_available:
            return False
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                return False
            settings = _settings_copy(state)
            _append_event(
                settings,
                event_code,
                execution_type=execution_type,
                safe_category=safe_category,
                authorization_source=authorization_source,
                result=result,
                timestamp=timestamp,
            )
            self._write_state_locked(replace(
                state,
                optional_settings_canonical=canonical_serialize(settings),
            ))
        return True

    def clear_journal(self) -> bool:
        """Clear audit history without changing mode, grants or policy epoch."""

        if not self.secure_available:
            return False
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if self._corrupt:
                return False
            settings = _settings_copy(state)
            journal = settings.get("journal")
            if not isinstance(journal, Mapping) or not journal.get("events"):
                return False
            settings["journal"] = {
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "events": [],
            }
            self._write_state_locked(replace(
                state,
                optional_settings_canonical=canonical_serialize(settings),
            ))
        return True

    def permission_center_snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """Return a value-free desktop projection and ephemeral row handles."""

        current_time = float(time.time() if now is None else now)
        with self._lock:
            state = self.load_state()
            try:
                settings = _full_access_settings_for_update(state)
                _validate_permission_settings(settings)
            except Exception:
                settings = {
                    "full_access": _default_full_access_settings(),
                    "automatic_categories": {
                        category: False
                        for category in AUTOMATIC_PERMISSION_CATEGORIES
                    },
                }
            full = settings["full_access"]
            automatic = settings["automatic_categories"]
            constraints = full["constraints"]
            activation = full["activation"]
            grants = self._all_grants_locked(state)
            self._center_grant_tokens = {}
            self._center_group_tokens = {}
            summaries = []
            group_tokens: dict[tuple[str, str], str] = {}
            for grant in grants:
                row_token = "row:" + secrets.token_urlsafe(18)
                self._center_grant_tokens[row_token] = grant.grant_id
                group_token = ""
                group_key: tuple[str, str] | None = None
                if grant.execution_type.value == "autonomous" and grant.run_id:
                    group_key = ("autonomous", grant.run_id)
                elif (
                    grant.execution_type.value == "scheduled"
                    and grant.scheduled_task_id
                ):
                    group_key = ("scheduled", grant.scheduled_task_id)
                if group_key is not None:
                    group_token = group_tokens.get(group_key, "")
                    if not group_token:
                        group_token = "group:" + secrets.token_urlsafe(18)
                        group_tokens[group_key] = group_token
                        self._center_group_tokens[group_token] = group_key
                remaining = (
                    None if grant.max_uses is None
                    else max(0, grant.max_uses - grant.use_count)
                )
                summaries.append({
                    "row_token": row_token,
                    "group_token": group_token,
                    "execution_type": grant.execution_type.value,
                    "safe_category": _safe_choice(
                        grant.normalized_target_scope.safe_category,
                        _SAFE_CATEGORIES,
                        "unknown",
                    ),
                    "tool_category": _safe_choice(
                        grant.tool_category, _SAFE_TOOL_CATEGORIES, "unknown",
                    ),
                    "expires_at": grant.expires_at,
                    "remaining_seconds": (
                        None if grant.expires_at is None
                        else max(0.0, grant.expires_at - current_time)
                    ),
                    "remaining_uses": remaining,
                    "use_policy": grant.use_policy.value,
                    "persistent": grant.persistent,
                    "expired": bool(
                        grant.expires_at is not None
                        and grant.expires_at <= current_time
                    ),
                })
            journal = settings.get("journal", {})
            events = journal.get("events", []) if isinstance(journal, Mapping) else []
            payment_enabled = constraints["payment"]["enabled"]
            destinations_enabled = constraints["destinations"]["enabled"]
            mode = state.permission_mode.value
            return {
                "permission_mode": mode,
                "full_access_enabled": mode == PermissionMode.FULL_ACCESS.value,
                "full_access_with_limits": bool(
                    mode == PermissionMode.FULL_ACCESS.value
                    and (payment_enabled or destinations_enabled)
                ),
                "secure_storage_available": self.secure_available,
                "secure_storage_backend": self.availability.backend,
                "store_corrupt": self.corrupt,
                "activation_available": self.secure_available and not self.corrupt,
                "automatic_categories": dict(automatic),
                "policy_epoch": state.policy_epoch,
                "activated_at": activation.get("activated_at"),
                "local_auth_method": _safe_code(
                    activation.get("local_auth_method", ""), "unknown",
                ),
                "constraints": {
                    "payment": dict(constraints["payment"]),
                    "destinations": {
                        "enabled": destinations_enabled,
                        "origin_count": len(constraints["destinations"]["origins"]),
                        "connector_count": len(
                            constraints["destinations"]["connectors"]
                        ),
                    },
                },
                "grants": summaries,
                "journal": [dict(event) for event in events],
                "journal_schema_version": _JOURNAL_SCHEMA_VERSION,
                "journal_capacity": _JOURNAL_MAX_EVENTS,
            }

    def revoke_center_grant(self, row_token: str) -> RevocationResult:
        with self._lock:
            grant_ref = self._center_grant_tokens.get(str(row_token or ""), "")
        if not grant_ref:
            state = self.load_state()
            return RevocationResult(False, state.policy_epoch, "one_grant")
        return self.revoke_grant(grant_ref)

    def revoke_center_group(self, group_token: str) -> RevocationResult:
        with self._lock:
            group = self._center_group_tokens.get(str(group_token or ""))
        if not group:
            state = self.load_state()
            return RevocationResult(False, state.policy_epoch, "task_group")
        execution_type, authority_ref = group
        if execution_type == "autonomous":
            return self.revoke_run_grants(authority_ref)
        return self.revoke_scheduled_task_grants(authority_ref)

    def revoke_grant(self, grant_id: str) -> RevocationResult:
        grant_ref = str(grant_id or "")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            persistent = tuple(
                grant for grant in state.grants if grant.grant_id != grant_ref
            )
            session = {
                key: grant for key, grant in self._session_grants.items()
                if key != grant_ref
            }
            changed = (
                len(persistent) != len(state.grants)
                or len(session) != len(self._session_grants)
            )
            if not changed:
                return RevocationResult(
                    False, state.policy_epoch, "one_grant", grant_ref,
                )
            settings = _settings_copy(state) if self.secure_available else None
            if settings is not None:
                _append_event(settings, "grant_revoked", result="one_grant")
                _append_event(
                    settings, "policy_epoch_changed", result="grant_revoked",
                )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=persistent,
                keep_session=session,
                mode=(
                    PermissionMode.ASK
                    if (
                        state.permission_mode == PermissionMode.TASK_SCOPED
                        and not persistent
                        and not session
                    )
                    else state.permission_mode
                ),
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "one_grant", grant_ref)

    def revoke_all_grants(self) -> RevocationResult:
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            changed = bool(state.grants or self._session_grants)
            if not changed:
                return RevocationResult(False, state.policy_epoch, "all_grants")
            settings = _settings_copy(state) if self.secure_available else None
            if settings is not None:
                _append_event(settings, "grant_revoked", result="all_grants")
                _append_event(
                    settings, "policy_epoch_changed", result="grants_revoked",
                )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=(),
                keep_session={},
                mode=(
                    PermissionMode.ASK
                    if state.permission_mode == PermissionMode.TASK_SCOPED
                    else state.permission_mode
                ),
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "all_grants")

    def invalidate_active_permits(self) -> int:
        """Advance the protected epoch while preserving current grant scopes."""

        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            settings = _settings_copy(state) if self.secure_available else None
            if settings is not None:
                _append_event(
                    settings, "policy_epoch_changed", result="permits_invalidated",
                )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=state.grants,
                keep_session=self._session_grants,
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return epoch

    def revoke_run_grants(self, run_id: str) -> RevocationResult:
        """Revoke only grants bound to one exact Autonomous run."""

        run_ref = str(run_id or "")
        if not run_ref:
            raise ValueError("invalid_run_id")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            persistent = tuple(
                grant for grant in state.grants if grant.run_id != run_ref
            )
            session = {
                key: grant for key, grant in self._session_grants.items()
                if grant.run_id != run_ref
            }
            changed = (
                len(persistent) != len(state.grants)
                or len(session) != len(self._session_grants)
            )
            if not changed:
                return RevocationResult(
                    False, state.policy_epoch, "autonomous_run", run_ref,
                )
            settings = _settings_copy(state) if self.secure_available else None
            if settings is not None:
                _append_event(settings, "grant_revoked", result="autonomous_run")
                _append_event(
                    settings, "policy_epoch_changed", result="grants_revoked",
                )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=persistent,
                keep_session=session,
                mode=(
                    PermissionMode.ASK
                    if (
                        state.permission_mode == PermissionMode.TASK_SCOPED
                        and not persistent
                        and not session
                    )
                    else state.permission_mode
                ),
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "autonomous_run", run_ref)

    def revoke_scheduled_task_grants(self, task_id: str) -> RevocationResult:
        task_ref = str(task_id or "")
        if not task_ref:
            raise ValueError("invalid_scheduled_task_id")
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            persistent = tuple(
                grant for grant in state.grants
                if grant.scheduled_task_id != task_ref
            )
            session = {
                key: grant for key, grant in self._session_grants.items()
                if grant.scheduled_task_id != task_ref
            }
            changed = (
                len(persistent) != len(state.grants)
                or len(session) != len(self._session_grants)
            )
            if not changed:
                return RevocationResult(
                    False, state.policy_epoch, "scheduled_task", task_ref,
                )
            settings = _settings_copy(state) if self.secure_available else None
            if settings is not None:
                _append_event(settings, "grant_revoked", result="scheduled_task")
                _append_event(
                    settings, "policy_epoch_changed", result="grants_revoked",
                )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=persistent,
                keep_session=session,
                mode=(
                    PermissionMode.ASK
                    if (
                        state.permission_mode == PermissionMode.TASK_SCOPED
                        and not persistent
                        and not session
                    )
                    else state.permission_mode
                ),
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "scheduled_task", task_ref)

    def revoke_full_access(self) -> RevocationResult:
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if state.permission_mode != PermissionMode.FULL_ACCESS:
                return RevocationResult(False, state.policy_epoch, "full_access")
            settings = _full_access_settings_for_update(state)
            settings["automatic_categories"] = {
                category: False
                for category in AUTOMATIC_PERMISSION_CATEGORIES
            }
            _append_event(
                settings, "full_access_disabled",
                authorization_source="full_access", result="disabled",
            )
            _append_event(
                settings, "policy_epoch_changed", result="full_access_disabled",
            )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=state.grants,
                keep_session=dict(self._session_grants),
                mode=(
                    PermissionMode.TASK_SCOPED
                    if state.grants or self._session_grants
                    else PermissionMode.ASK
                ),
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "full_access")

    def revoke_all_delegated_authority(self) -> RevocationResult:
        """Atomically revoke Full Access, every grant and every loaded permit."""

        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            settings = _full_access_settings_for_update(state)
            changed = bool(
                state.permission_mode == PermissionMode.FULL_ACCESS
                or state.grants
                or self._session_grants
                or any(settings["automatic_categories"].values())
            )
            if not changed:
                return RevocationResult(
                    False, state.policy_epoch, "delegated_authority",
                )
            settings["automatic_categories"] = {
                category: False
                for category in AUTOMATIC_PERMISSION_CATEGORIES
            }
            if state.permission_mode == PermissionMode.FULL_ACCESS:
                _append_event(
                    settings, "full_access_disabled",
                    authorization_source="full_access", result="disabled",
                )
            if state.grants or self._session_grants:
                _append_event(settings, "grant_revoked", result="all_grants")
            _append_event(
                settings, "delegated_authority_revoked", result="all_authority",
            )
            _append_event(
                settings, "policy_epoch_changed", result="authority_revoked",
            )
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=(),
                keep_session={},
                mode=PermissionMode.ASK,
                optional_settings=settings,
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "delegated_authority")

    def reset_to_ask(self) -> RevocationResult:
        """Explicit recovery/reset; replaces even corrupt protected state."""

        with self._lock, self._cross_process_mutation_lock():
            previous = self.load_state()
            epoch = max(
                previous.policy_epoch,
                self._failure_epoch,
                self._volatile_epoch,
            ) + 1
            self._corrupt = False
            self._session_grants = {}
            state = self._fail_closed_state(epoch)
            if self.secure_available:
                self._write_state_locked(state)
            else:
                self._volatile_epoch = epoch
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "global_reset")

    def public_metadata(self) -> dict[str, Any]:
        state = self.load_state()
        metadata = state.public_metadata()
        metadata.update({
            "secure_storage_available": self.secure_available,
            "secure_storage_backend": self.availability.backend,
            "store_corrupt": self.corrupt,
            "session_grant_count": len(self._session_grants),
        })
        return metadata


__all__ = [
    "BackendAvailability", "PermissionStore", "PermissionStoreCorrupt",
    "PermissionStoreError", "PersistentPermissionUnavailable",
    "RevocationResult", "SecureStorageBackend",
    "UnavailableSecureStorageBackend", "WindowsDPAPISecureStorageBackend",
    "default_secure_storage_backend",
]
