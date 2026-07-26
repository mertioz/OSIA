from __future__ import annotations

from pathlib import Path
import time

import pytest

from osia.action_policy import (
    AuthorizationSource,
    DecisionType,
    PermissionMode,
    PermissionPolicy,
    UsePolicy,
    build_permission_request,
    create_grant,
    make_execution_context,
)
from osia.permission_store import (
    PermissionStore,
    PermissionStoreCorrupt,
    PersistentPermissionUnavailable,
    UnavailableSecureStorageBackend,
    WindowsDPAPISecureStorageBackend,
)


def _secure_store(tmp_path: Path) -> PermissionStore:
    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("Windows DPAPI is the Phase 1 secure backend")
    return PermissionStore(tmp_path / "permissions.dat", backend=backend)


def _sensitive_request(store: PermissionStore, policy: PermissionPolicy, path: Path):
    context = make_execution_context(
        owner_ref="owner:fixture",
        machine_ref="machine:fixture",
        execution_id="execution:fixture",
        operation_id="operation:fixture",
        store=store,
    )
    return build_permission_request(
        "delete_file", {"path": str(path)},
        execution_context=context, policy=policy,
    )


def test_windows_secure_store_round_trip_is_user_bound_and_not_plaintext(tmp_path):
    store = _secure_store(tmp_path)
    sentinel = "WOR192_PROTECTED_SENTINEL_UNIQUE"
    policy = PermissionPolicy(store)
    request = _sensitive_request(store, policy, tmp_path / sentinel)
    grant = create_grant(
        request,
        use_policy=UsePolicy.BOUNDED,
        max_uses=2,
        expires_at=time.time() + 60.0,
        persistent=True,
    )
    store.add_grant(grant)

    disk = store.path.read_bytes()
    assert sentinel.encode("utf-8") not in disk
    assert b"scope_hash" not in disk
    assert store.availability.user_bound is True

    reloaded = PermissionStore(
        store.path, backend=WindowsDPAPISecureStorageBackend(),
    ).load_state()
    assert reloaded.permission_mode == PermissionMode.TASK_SCOPED
    assert len(reloaded.grants) == 1
    assert reloaded.grants[0].grant_id == grant.grant_id


def test_secure_store_uses_atomic_replacement(tmp_path):
    store = _secure_store(tmp_path)
    assert store.migrate_safe_default() is True
    first = store.path.read_bytes()
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    assert store.path.read_bytes() != first
    assert list(tmp_path.glob(".permissions.dat.*.tmp")) == []


def test_corruption_fails_closed_to_ask(tmp_path):
    store = _secure_store(tmp_path)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    blob = bytearray(store.path.read_bytes())
    blob[len(blob) // 2] ^= 0x5A
    store.path.write_bytes(blob)

    failed = store.load_state()
    assert failed.permission_mode == PermissionMode.ASK
    assert failed.grants == ()
    assert store.corrupt is True
    assert failed.policy_epoch > 0
    policy = PermissionPolicy(store)
    request = _sensitive_request(store, policy, tmp_path / "after-corruption.txt")
    with pytest.raises(PermissionStoreCorrupt):
        store.add_grant(create_grant(request))


def test_missing_backend_is_ask_only_and_rejects_persistent_authority(tmp_path):
    store = PermissionStore(
        tmp_path / "unavailable.dat",
        backend=UnavailableSecureStorageBackend("synthetic_unavailable"),
    )
    assert store.load_state().permission_mode == PermissionMode.ASK
    assert store.secure_available is False
    with pytest.raises(PersistentPermissionUnavailable):
        store.set_permission_mode(PermissionMode.FULL_ACCESS)

    policy = PermissionPolicy(store)
    request = _sensitive_request(store, policy, tmp_path / "fixture.txt")
    persistent = create_grant(request, persistent=True)
    with pytest.raises(PersistentPermissionUnavailable):
        store.add_grant(persistent)


def test_session_only_grant_works_without_secure_storage_and_counts_use(tmp_path):
    store = PermissionStore(
        tmp_path / "unavailable.dat",
        backend=UnavailableSecureStorageBackend(),
    )
    policy = PermissionPolicy(store)
    request = _sensitive_request(store, policy, tmp_path / "fixture.txt")
    grant = create_grant(
        request, use_policy=UsePolicy.BOUNDED, max_uses=2, persistent=False,
    )
    store.add_grant(grant)

    decision = policy.evaluate(request)
    assert decision.decision_type == DecisionType.ALLOW
    assert decision.authorization_source == AuthorizationSource.GRANT
    assert policy.mint_permit(request, decision) is not None
    matched, _reason = store.find_matching_grant(request, now=grant.issued_at)
    assert matched is not None and matched.use_count == 1
    assert store.path.exists() is False


def test_safe_default_migration_is_idempotent_and_never_broadens(tmp_path):
    store = _secure_store(tmp_path)
    assert store.migrate_safe_default() is True
    first = store.path.read_bytes()
    assert store.migrate_safe_default() is False
    assert store.path.read_bytes() == first
    assert store.load_state().permission_mode == PermissionMode.ASK
    assert store.load_state().grants == ()


def test_public_metadata_never_contains_private_scope(tmp_path):
    store = PermissionStore(
        tmp_path / "unavailable.dat",
        backend=UnavailableSecureStorageBackend(),
    )
    policy = PermissionPolicy(store)
    sentinel = "WOR192_PRIVATE_SCOPE_SENTINEL"
    request = _sensitive_request(store, policy, tmp_path / sentinel)
    grant = create_grant(request)
    store.add_grant(grant)
    assert sentinel not in str(store.public_metadata())
    assert sentinel not in str(grant.public_metadata())
    with pytest.raises(ValueError, match="unknown_permission_constraint"):
        create_grant(request, constraints={"recipient": sentinel})


def test_one_grant_revocation_is_idempotent_and_rebases_other_grants(tmp_path):
    store = PermissionStore(
        tmp_path / "session.dat",
        backend=UnavailableSecureStorageBackend(),
    )
    policy = PermissionPolicy(store)
    first = _sensitive_request(store, policy, tmp_path / "first.txt")
    second_context = make_execution_context(
        owner_ref="owner:fixture",
        machine_ref="machine:fixture",
        execution_id="execution:second",
        operation_id="operation:second",
        store=store,
    )
    second = build_permission_request(
        "delete_file", {"path": str(tmp_path / "second.txt")},
        execution_context=second_context, policy=policy,
    )
    first_grant = create_grant(first, use_policy=UsePolicy.RECURRING)
    second_grant = create_grant(second, use_policy=UsePolicy.RECURRING)
    store.add_grant(first_grant)
    store.add_grant(second_grant)

    revoked = store.revoke_grant(first_grant.grant_id)
    assert revoked.changed is True
    assert revoked.policy_epoch == 1
    repeated = store.revoke_grant(first_grant.grant_id)
    assert repeated.changed is False
    assert repeated.policy_epoch == revoked.policy_epoch

    current_context = make_execution_context(
        owner_ref="owner:fixture",
        machine_ref="machine:fixture",
        execution_id="execution:current",
        operation_id="operation:current",
        store=store,
    )
    current_second = build_permission_request(
        "delete_file", {"path": str(tmp_path / "second.txt")},
        execution_context=current_context, policy=policy,
    )
    assert policy.evaluate(current_second).authorization_source == AuthorizationSource.GRANT


def test_revoke_full_access_and_global_reset_return_to_ask(tmp_path):
    store = _secure_store(tmp_path)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    revoked = store.revoke_full_access()
    assert revoked.changed is True
    assert store.load_state().permission_mode == PermissionMode.ASK
    repeated = store.revoke_full_access()
    assert repeated.changed is False
    reset = store.reset_to_ask()
    assert reset.policy_epoch > revoked.policy_epoch
    assert store.load_state().permission_mode == PermissionMode.ASK
    assert store.load_state().grants == ()
