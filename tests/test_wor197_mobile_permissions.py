"""Focused WOR-197 canonical mobile permission and revocation regressions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from osia import action_policy
from osia.action_policy import (
    ExecutionType,
    PermissionPolicy,
    UsePolicy,
    build_permission_request,
    create_grant,
    make_execution_context,
)
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    PermissionStoreError,
    SecureStorageBackend,
)
from osia.server import server


class _ProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "wor197_test", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"WOR197" + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"WOR197"):
            raise ValueError("corrupt")
        return protected[6:][::-1]


class _Channel:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _store(tmp_path: Path, name: str = "permissions.dat") -> PermissionStore:
    return PermissionStore(tmp_path / name, backend=_ProtectedBackend())


def _grant(
    store: PermissionStore,
    tmp_path: Path,
    *,
    marker: str,
    execution_type: ExecutionType = ExecutionType.CHAT,
    run_id: str = "",
    scheduled_task_id: str = "",
    max_uses: int | None = 3,
):
    context = make_execution_context(
        execution_type=execution_type,
        interactive=True,
        owner_ref="owner:wor197",
        machine_ref="machine:wor197",
        run_id=run_id,
        scheduled_task_id=scheduled_task_id,
        task_revision="1" if execution_type != ExecutionType.CHAT else "",
        plan_revision="1" if execution_type == ExecutionType.AUTONOMOUS else "",
        execution_id=f"execution:{marker}",
        operation_id=f"operation:{marker}",
        store=store,
    )
    request = build_permission_request(
        "write_file",
        {"path": str(tmp_path / marker / "private-name.txt"), "content": marker},
        execution_context=context,
        force_sensitive=True,
        policy=PermissionPolicy(store),
    )
    grant = create_grant(
        request,
        use_policy=UsePolicy.BOUNDED,
        max_uses=max_uses,
        expires_at=time.time() + 3600,
        persistent=True,
    )
    store.add_grant(grant)
    return grant, request


def _snapshot(store: PermissionStore, sequence: int = 1, **extra):
    return store.mobile_permission_snapshot(
        server_id="a" * 64,
        session_id="permission-session:test",
        sequence=sequence,
        **extra,
    )


def _mobile_item(snapshot: dict, context: str) -> str:
    return next(
        item["mobile_item_id"]
        for item in snapshot["temporary_permissions"]
        if item["context"] == context
    )


def test_snapshot_is_strict_allowlisted_and_contains_no_private_values(tmp_path):
    store = _store(tmp_path)
    sentinel = "WOR197_PRIVATE_PATH_FILENAME_RECIPIENT_URL"
    _grant(store, tmp_path, marker=sentinel)
    store.append_journal_event(
        "authorization_required",
        execution_type="chat",
        safe_category="local_path",
        result="colossal_deletion_review",
    )
    snapshot = _snapshot(
        store,
        pending_requests=({"request_id": "permission:request", "status": "pending"},),
    )

    assert set(snapshot) == {
        "type", "schema_version", "snapshot_id", "server_id", "session_id",
        "policy_epoch", "sequence", "generated_at", "preset", "categories",
        "full_access_active", "protected_store", "temporary_permissions",
        "history", "pending_requests",
    }
    assert set(snapshot["categories"]) == set(
        action_policy.AUTOMATIC_PERMISSION_CATEGORIES
    )
    assert len(snapshot["categories"]) == 9
    assert set(snapshot["temporary_permissions"][0]) == {
        "action_label", "context", "expires_at", "remaining_uses",
        "mobile_item_id", "revocable",
    }
    assert set(snapshot["history"][0]) == {"timestamp", "event", "context"}
    assert set(snapshot["pending_requests"][0]) == {"request_id", "status"}
    rendered = json.dumps(snapshot, sort_keys=True)
    for forbidden in (
        sentinel, "private-name.txt", "grant_ref", "scope_hash", "call_hash",
        "private_canonical", "tool_arguments", "colossal_deletion",
        "secure_storage_backend", "store_corrupt",
    ):
        assert forbidden not in rendered


def test_snapshot_sequence_is_caller_monotonic_and_item_ids_are_stable(tmp_path):
    store = _store(tmp_path)
    _grant(store, tmp_path, marker="stable")
    first = _snapshot(store, 1)
    second = _snapshot(store, 2)
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["snapshot_id"] != second["snapshot_id"]
    assert (
        first["temporary_permissions"][0]["mobile_item_id"]
        == second["temporary_permissions"][0]["mobile_item_id"]
    )


def test_mobile_revocations_cover_one_run_task_all_and_full_access(tmp_path):
    store = _store(tmp_path)
    chat, _ = _grant(store, tmp_path, marker="chat")
    run_one, _ = _grant(
        store,
        tmp_path,
        marker="run-one",
        execution_type=ExecutionType.AUTONOMOUS,
        run_id="run:synthetic",
    )
    _grant(
        store,
        tmp_path,
        marker="run-two",
        execution_type=ExecutionType.AUTONOMOUS,
        run_id="run:synthetic",
    )
    task, _ = _grant(
        store,
        tmp_path,
        marker="task",
        execution_type=ExecutionType.SCHEDULED,
        scheduled_task_id="task:synthetic",
    )
    initial = _snapshot(store)
    epoch = initial["policy_epoch"]

    one = store.revoke_mobile_authority(
        operation="revoke_temporary_permission",
        target_id=next(
            item["mobile_item_id"]
            for item in initial["temporary_permissions"]
            if item["context"] == "chat"
        ),
        expected_policy_epoch=epoch,
    )
    assert one.changed and one.policy_epoch == epoch + 1
    assert all(grant.grant_id != chat.grant_id for grant in store.load_state().grants)

    after_one = _snapshot(store, 2)
    run_result = store.revoke_mobile_authority(
        operation="revoke_autonomous_run",
        target_id=_mobile_item(after_one, "autonomous"),
        expected_policy_epoch=after_one["policy_epoch"],
    )
    assert run_result.changed
    assert run_result.authority_ref == "run:synthetic"
    assert all(grant.run_id != "run:synthetic" for grant in store.load_state().grants)

    after_run = _snapshot(store, 3)
    task_result = store.revoke_mobile_authority(
        operation="revoke_scheduled_task",
        target_id=_mobile_item(after_run, "scheduled"),
        expected_policy_epoch=after_run["policy_epoch"],
    )
    assert task_result.changed
    assert task_result.authority_ref == "task:synthetic"
    assert all(
        grant.scheduled_task_id != "task:synthetic"
        for grant in store.load_state().grants
    )

    store.set_permission_mode(action_policy.PermissionMode.FULL_ACCESS)
    full = _snapshot(store, 4)
    assert full["full_access_active"] is True
    disabled = store.revoke_mobile_authority(
        operation="disable_full_access",
        expected_policy_epoch=full["policy_epoch"],
    )
    assert disabled.changed
    assert _snapshot(store, 5)["full_access_active"] is False

    _grant(store, tmp_path, marker="all")
    before_all = _snapshot(store, 6)
    all_result = store.revoke_mobile_authority(
        operation="revoke_all_delegated_authority",
        expected_policy_epoch=before_all["policy_epoch"],
    )
    final = _snapshot(store, 7)
    assert all_result.changed
    assert final["preset"] == "default_access"
    assert final["temporary_permissions"] == []
    assert not any(final["categories"].values())
    assert run_one.grant_id != task.grant_id


def test_stale_epoch_unknown_target_and_idempotent_revocation(tmp_path):
    store = _store(tmp_path)
    _grant(store, tmp_path, marker="idempotent")
    first = _snapshot(store)
    target = _mobile_item(first, "chat")
    with pytest.raises(PermissionStoreError, match="stale_policy_epoch"):
        store.revoke_mobile_authority(
            operation="revoke_temporary_permission",
            target_id=target,
            expected_policy_epoch=first["policy_epoch"] + 1,
        )
    with pytest.raises(PermissionStoreError, match="unknown_mobile_target"):
        store.revoke_mobile_authority(
            operation="revoke_temporary_permission",
            target_id="permission:unknown",
            expected_policy_epoch=first["policy_epoch"],
        )
    applied = store.revoke_mobile_authority(
        operation="revoke_temporary_permission",
        target_id=target,
        expected_policy_epoch=first["policy_epoch"],
    )
    repeated = store.revoke_mobile_authority(
        operation="revoke_temporary_permission",
        target_id=target,
        expected_policy_epoch=applied.policy_epoch,
    )
    assert repeated.changed is False
    assert repeated.policy_epoch == applied.policy_epoch


def test_epoch_change_invalidates_pending_permit_but_preserves_dispatched_outcome(
    tmp_path,
):
    store = _store(tmp_path)
    policy = PermissionPolicy(store)
    _grant_obj, request = _grant(store, tmp_path, marker="pending-permit")
    decision = policy.evaluate(request)
    pending = policy.mint_permit(request, decision)
    assert pending is not None
    snapshot = _snapshot(store)
    result = store.revoke_mobile_authority(
        operation="revoke_temporary_permission",
        target_id=_mobile_item(snapshot, "chat"),
        expected_policy_epoch=snapshot["policy_epoch"],
    )
    assert result.policy_epoch == snapshot["policy_epoch"] + 1
    with pytest.raises(action_policy.PermitError, match="permit_missing_or_used"):
        policy.consume_permit(pending, request)

    dispatched_grant, dispatched_request = _grant(
        store, tmp_path, marker="dispatched"
    )
    dispatched_decision = policy.evaluate(dispatched_request)
    dispatched_permit = policy.mint_permit(dispatched_request, dispatched_decision)
    assert policy.consume_permit(dispatched_permit, dispatched_request).used
    dispatched_snapshot = _snapshot(store, 2)
    dispatched_target = next(
        item["mobile_item_id"]
        for item in dispatched_snapshot["temporary_permissions"]
        if item["context"] == "chat"
    )
    store.revoke_mobile_authority(
        operation="revoke_temporary_permission",
        target_id=dispatched_target,
        expected_policy_epoch=dispatched_snapshot["policy_epoch"],
    )
    assert policy.permit_ledger.operation_is_duplicate(
        dispatched_request.operation_id
    )
    policy.permit_ledger.mark_outcome_unknown(dispatched_request.operation_id)
    assert policy.permit_ledger.operation_is_duplicate(
        dispatched_request.operation_id
    )
    assert dispatched_grant.grant_id


async def _authenticated_session_rejects_stale_wrong_duplicate_and_schema(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    _grant(store, tmp_path, marker="session")
    channel = _Channel()
    session = server._PermissionMobileSession(
        channel, asyncio.get_running_loop(), store,
    )
    session.activate()
    assert session.bind_device("permission-device:test")
    monkeypatch.setattr(
        server,
        "_refresh_revoked_permission_contexts",
        lambda _result: asyncio.sleep(0),
    )
    with server._PERMISSION_REQUESTS_LOCK:
        server._PERMISSION_REQUESTS.clear()
    try:
        snapshot = await session.send_snapshot()
        assert snapshot is not None
        target = _mobile_item(snapshot, "chat")

        def envelope(**updates):
            value = {
                "type": "permission_revocation_request",
                "schema_version": 1,
                "request_id": "permission:request-one",
                "device_id": "permission-device:test",
                "session_id": session.session_id,
                "expected_policy_epoch": snapshot["policy_epoch"],
                "expected_snapshot_sequence": snapshot["sequence"],
                "operation": "revoke_temporary_permission",
                "target_id": target,
                "expires_at": time.time() + 30,
            }
            value.update(updates)
            return value

        assert (await session.handle_revocation(
            envelope(expected_snapshot_sequence=0)
        ))["reason"] == "stale_snapshot"
        assert (await session.handle_revocation(
            envelope(device_id="permission-device:wrong")
        ))["reason"] == "wrong_device"
        assert (await session.handle_revocation(
            envelope(session_id="permission-session:wrong")
        ))["reason"] == "wrong_session"
        assert (await session.handle_revocation(
            envelope(schema_version=99)
        ))["reason"] == "unsupported_schema"
        assert (await session.handle_revocation(
            envelope(expires_at=time.time() - 1)
        ))["reason"] == "expired_request"

        applied = await session.handle_revocation(envelope())
        assert applied["status"] == "applied"
        latest = channel.frames[-1]
        assert latest["type"] == "permission_snapshot"
        duplicate = await session.handle_revocation(envelope(
            expected_policy_epoch=latest["policy_epoch"],
            expected_snapshot_sequence=latest["sequence"],
        ))
        assert duplicate["status"] == "idempotent"
    finally:
        session.close()


def test_authenticated_session_rejects_stale_wrong_duplicate_and_schema(
    tmp_path, monkeypatch,
):
    asyncio.run(_authenticated_session_rejects_stale_wrong_duplicate_and_schema(
        tmp_path, monkeypatch,
    ))


async def _reconnect_gets_fresh_session_snapshot(tmp_path):
    store = _store(tmp_path)
    _grant(store, tmp_path, marker="reconnect")
    first_channel = _Channel()
    first = server._PermissionMobileSession(
        first_channel, asyncio.get_running_loop(), store,
    )
    first.activate()
    first.bind_device("permission-device:reconnect")
    first_snapshot = await first.send_snapshot()
    first.close()

    second_channel = _Channel()
    second = server._PermissionMobileSession(
        second_channel, asyncio.get_running_loop(), store,
    )
    second.activate()
    second.bind_device("permission-device:reconnect")
    try:
        second_snapshot = await second.send_snapshot()
        assert second_snapshot["server_id"] == first_snapshot["server_id"]
        assert second_snapshot["session_id"] != first_snapshot["session_id"]
        assert second_snapshot["sequence"] == 1
        assert second_snapshot["policy_epoch"] >= first_snapshot["policy_epoch"]
    finally:
        second.close()


def test_reconnect_gets_fresh_session_snapshot(tmp_path):
    asyncio.run(_reconnect_gets_fresh_session_snapshot(tmp_path))
