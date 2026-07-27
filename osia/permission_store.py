"""Protected permission-state persistence and revocation (WOR-192).

Windows uses user-bound DPAPI without a new dependency. Other platforms expose
an explicit unavailable backend in Phase 1; they remain ASK-only while allowing
non-persistent, process-local grants.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from osia.action_policy import (
    POLICY_VERSION,
    Grant,
    PermissionMode,
    PermissionRequest,
    PermissionState,
)
from osia.scope_normalization import (
    canonical_hash,
    canonical_serialize,
    sensitive_hash_equal,
)


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
            rebased = tuple(
                _new_integrity(replace(grant, revocation_epoch=epoch))
                for grant in state.grants
            )
            new_state = replace(
                state, permission_mode=mode, policy_epoch=epoch, grants=rebased,
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
                self._write_state_locked(replace(
                    state, permission_mode=mode, grants=grants,
                ))
            else:
                self._session_grants[grant.grant_id] = grant

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
        )
        if self.secure_available:
            self._write_state_locked(new_state)
        else:
            self._volatile_epoch = epoch
            new_state = self._fail_closed_state(epoch)
        return new_state, epoch

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
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "one_grant", grant_ref)

    def revoke_all_grants(self) -> RevocationResult:
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            changed = bool(state.grants or self._session_grants)
            if not changed:
                return RevocationResult(False, state.policy_epoch, "all_grants")
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=(),
                keep_session={},
                mode=(
                    PermissionMode.ASK
                    if state.permission_mode == PermissionMode.TASK_SCOPED
                    else state.permission_mode
                ),
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "all_grants")

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
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "autonomous_run", run_ref)

    def revoke_full_access(self) -> RevocationResult:
        with self._lock, self._cross_process_mutation_lock():
            state = self.load_state()
            if state.permission_mode != PermissionMode.FULL_ACCESS:
                return RevocationResult(False, state.policy_epoch, "full_access")
            _new_state, epoch = self._advance_epoch_locked(
                state,
                keep_persistent=state.grants,
                keep_session=dict(self._session_grants),
                mode=(
                    PermissionMode.TASK_SCOPED
                    if state.grants or self._session_grants
                    else PermissionMode.ASK
                ),
            )
        self._notify_epoch(epoch)
        return RevocationResult(True, epoch, "full_access")

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
