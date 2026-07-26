from __future__ import annotations

from pathlib import Path

import pytest

from osia.action_policy import (
    DecisionType,
    PermissionMode,
    PermissionPolicy,
    UsePolicy,
    build_permission_request,
    create_grant,
    make_execution_context,
    trusted_deletion_classification,
)
from osia.deletion_plan import (
    ColossalDeletionPolicy,
    DeletionPermitLedger,
    DeletionPlanError,
    build_deletion_plan,
)
from osia.permission_store import (
    PermissionStore,
    UnavailableSecureStorageBackend,
    WindowsDPAPISecureStorageBackend,
)
from osia.system_resources import SystemResourceRegistry, WindowsRuntimeAdapter


def _safe_registry(tmp_path: Path):
    unused = tmp_path / "never-system"
    return SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(unused),
            system_directory=str(unused),
            system_windows_directory=str(unused),
            wrp_probe=lambda _path: False,
            storage_rows=(),
        ),
    )


def _request(store, policy, target, *, classification, operation):
    context = make_execution_context(
        owner_ref="owner:fixture",
        machine_ref="machine:fixture",
        execution_id=f"execution:{operation}",
        operation_id=f"operation:{operation}",
        store=store,
    )
    return build_permission_request(
        "delete_file",
        {"path": str(target)},
        execution_context=context,
        deletion_classification=classification,
        policy=policy,
    )


def _session_policy(tmp_path):
    store = PermissionStore(
        tmp_path / "session.dat",
        backend=UnavailableSecureStorageBackend(),
    )
    return store, PermissionPolicy(store)


def test_system_destruction_blocks_ask_and_exact_grant(tmp_path):
    target = tmp_path / "target"
    store, policy = _session_policy(tmp_path)
    clear = _request(
        store,
        policy,
        target,
        classification=trusted_deletion_classification(),
        operation="grant-base",
    )
    store.add_grant(create_grant(clear, use_policy=UsePolicy.SINGLE))
    blocked = _request(
        store,
        policy,
        target,
        classification=trusted_deletion_classification(system_destruction=True),
        operation="system",
    )
    assert policy.evaluate(blocked).decision_type is DecisionType.BLOCK_SYSTEM_DESTRUCTION


def test_system_destruction_blocks_full_access(tmp_path):
    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("Windows DPAPI required")
    store = PermissionStore(tmp_path / "full.dat", backend=backend)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    policy = PermissionPolicy(store)
    request = _request(
        store,
        policy,
        tmp_path / "target",
        classification=trusted_deletion_classification(system_destruction=True),
        operation="full-system",
    )
    assert policy.evaluate(request).decision_type is DecisionType.BLOCK_SYSTEM_DESTRUCTION


def test_colossal_always_requires_reinforced_but_ordinary_full_access_allows(tmp_path):
    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("Windows DPAPI required")
    store = PermissionStore(tmp_path / "full2.dat", backend=backend)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    policy = PermissionPolicy(store)
    colossal = _request(
        store,
        policy,
        tmp_path / "target",
        classification=trusted_deletion_classification(colossal_deletion=True),
        operation="full-colossal",
    )
    ordinary = _request(
        store,
        policy,
        tmp_path / "ordinary",
        classification=trusted_deletion_classification(),
        operation="full-ordinary",
    )
    assert policy.evaluate(colossal).decision_type is DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION
    assert policy.evaluate(ordinary).decision_type is DecisionType.ALLOW


def test_model_dictionary_cannot_fabricate_universal_evidence(tmp_path):
    store, policy = _session_policy(tmp_path)
    request = build_permission_request(
        "delete_file",
        {
            "path": str(tmp_path / "ordinary"),
            "system_destruction": True,
            "colossal_deletion": True,
            "inventory_hash": "f" * 64,
        },
        execution_context=make_execution_context(
            owner_ref="owner:fixture",
            machine_ref="machine:fixture",
            store=store,
        ),
        policy=policy,
    )
    assert request.system_destruction is False
    assert request.colossal_deletion is False


def test_reinforced_permit_binds_hash_epoch_plan_and_is_single_use(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    policy = ColossalDeletionPolicy(
        object_hard_limit=1,
        byte_hard_limit=1000,
        root_hard_limit=10,
        object_risk_limit=10,
        byte_risk_limit=1000,
        risk_score_limit=10,
    )
    registry = _safe_registry(tmp_path)
    one = build_deletion_plan([first], recursive=False, registry=registry, policy=policy)
    two = build_deletion_plan([second], recursive=False, registry=registry, policy=policy)
    ledger = DeletionPermitLedger()
    permit = ledger.issue(one, policy_epoch=7, confirmation_strength="reinforced")
    with pytest.raises(DeletionPlanError, match="plan_mismatch"):
        ledger.consume(permit, two, policy_epoch=7)
    replay = ledger.issue(one, policy_epoch=7, confirmation_strength="reinforced")
    consumed = ledger.consume(replay, one, policy_epoch=7)
    assert consumed.used is True
    with pytest.raises(DeletionPlanError, match="missing_or_used"):
        ledger.consume(replay, one, policy_epoch=7)
    changed_epoch = ledger.issue(one, policy_epoch=7, confirmation_strength="reinforced")
    with pytest.raises(DeletionPlanError, match="epoch_changed"):
        ledger.consume(changed_epoch, one, policy_epoch=8)


def test_ordinary_permit_cannot_authorize_colossal_plan(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    plan = build_deletion_plan(
        [target],
        recursive=False,
        registry=_safe_registry(tmp_path),
        policy=ColossalDeletionPolicy(
            object_hard_limit=1,
            byte_hard_limit=1000,
            root_hard_limit=10,
            object_risk_limit=10,
            byte_risk_limit=1000,
            risk_score_limit=10,
        ),
    )
    with pytest.raises(DeletionPlanError, match="reinforced_confirmation_required"):
        DeletionPermitLedger().issue(
            plan,
            policy_epoch=0,
            confirmation_strength="ordinary",
        )
