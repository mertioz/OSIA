"""WOR-218 PC-canonical Mobile permission request regressions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from osia import action_policy
from osia.action_policy import (
    PermissionPolicy,
    UsePolicy,
    build_permission_request,
    create_grant,
    make_execution_context,
)
from osia.permission_contract import mobile_permission_contract
from osia.gui.permissions_center import PermissionsPage
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    PermissionStoreError,
    SecureStorageBackend,
)
from osia.server import server


_QT_APP = QApplication.instance() or QApplication([])


class _ProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "wor218_test", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"WOR218" + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"WOR218"):
            raise ValueError("corrupt")
        return protected[6:][::-1]


class _Channel:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _store(tmp_path: Path) -> PermissionStore:
    return PermissionStore(
        tmp_path / "permissions.dat", backend=_ProtectedBackend(),
    )


def _grant(store: PermissionStore, tmp_path: Path) -> None:
    context = make_execution_context(
        owner_ref="owner:wor218",
        machine_ref="machine:wor218",
        execution_id="execution:wor218",
        operation_id="operation:wor218",
        store=store,
    )
    request = build_permission_request(
        "write_file",
        {"path": str(tmp_path / "private-name.txt"), "content": "private"},
        execution_context=context,
        force_sensitive=True,
        policy=PermissionPolicy(store),
    )
    store.add_grant(create_grant(
        request,
        use_policy=UsePolicy.BOUNDED,
        max_uses=2,
        expires_at=time.time() + 3600,
        persistent=True,
    ))


def test_contract_is_canonical_complete_and_value_free():
    contract = mobile_permission_contract()
    categories = contract["categories"]

    assert contract["schema_version"] == 1
    assert contract["custom_access"] == "derived"
    assert contract["full_access_activation"] == "local_pc_ceremony"
    assert [item["id"] for item in categories] == list(
        action_policy.AUTOMATIC_PERMISSION_CATEGORIES
    )
    assert len(categories) == 9
    assert all(item["mobile_request_allowed"] is True for item in categories)
    rendered = json.dumps(contract, sort_keys=True)
    for forbidden in (
        "path", "argument", "secret", "token", "grant_ref", "scope_hash",
        "permit", "recipient",
    ):
        assert forbidden not in rendered


def test_epoch_bound_category_default_and_temporary_revocation(tmp_path):
    store = _store(tmp_path)
    initial = store.load_state()
    custom = store.update_automatic_permissions(
        {"files_modify": True},
        expected_policy_epoch=initial.policy_epoch,
    )
    assert custom.policy_epoch == initial.policy_epoch + 1
    snapshot = store.mobile_permission_snapshot(
        server_id="a" * 64,
        session_id="permission-session:test",
        sequence=1,
    )
    assert snapshot["preset"] == "mixed"
    assert snapshot["categories"]["files_modify"] is True

    with pytest.raises(PermissionStoreError, match="stale_policy_epoch"):
        store.apply_default_access(expected_policy_epoch=initial.policy_epoch)

    _grant(store, tmp_path)
    before_revoke = store.load_state()
    revoked = store.revoke_all_grants(
        expected_policy_epoch=before_revoke.policy_epoch,
    )
    assert revoked.changed is True
    after_revoke = store.mobile_permission_snapshot(
        server_id="a" * 64,
        session_id="permission-session:test",
        sequence=2,
    )
    assert after_revoke["temporary_permissions"] == []
    assert after_revoke["categories"]["files_modify"] is True

    default = store.apply_default_access(
        expected_policy_epoch=after_revoke["policy_epoch"],
    )
    assert default.policy_epoch == after_revoke["policy_epoch"] + 1
    assert not any(
        store.mobile_permission_snapshot(
            server_id="a" * 64,
            session_id="permission-session:test",
            sequence=3,
        )["categories"].values()
    )


async def _exercise_session_change_contract(tmp_path):
    store = _store(tmp_path)
    channel = _Channel()
    session = server._PermissionMobileSession(
        channel, asyncio.get_running_loop(), store,
    )
    session.activate()
    assert session.bind_device("permission-device:wor218")
    with server._PERMISSION_REQUESTS_LOCK:
        server._PERMISSION_REQUESTS.clear()

    def envelope(snapshot, *, request_id, operation, category="", enabled=False):
        return {
            "schema_version": 1,
            "contract_schema_version": 1,
            "request_id": request_id,
            "session_id": session.session_id,
            "expected_policy_epoch": snapshot["policy_epoch"],
            "expected_snapshot_sequence": snapshot["sequence"],
            "operation": operation,
            "category_id": category,
            "enabled": enabled,
            "expires_at": time.time() + 30,
        }

    try:
        first = await session.send_snapshot()
        await session.handle_change_request(envelope(
            first,
            request_id="permission-change:category",
            operation="set_category",
            category="commands",
            enabled=True,
        ))
        custom = channel.frames[-1]
        assert custom["preset"] == "mixed"
        assert custom["categories"]["commands"] is True
        assert custom["policy_epoch"] == first["policy_epoch"] + 1

        stale = envelope(
            custom,
            request_id="permission-change:stale",
            operation="set_category",
            category="files_delete",
            enabled=True,
        )
        stale["expected_policy_epoch"] -= 1
        await session.handle_change_request(stale)
        rejected = channel.frames[-1]
        assert rejected["categories"]["files_delete"] is False

        await session.handle_change_request(envelope(
            rejected,
            request_id="permission-change:full",
            operation="request_full_access",
        ))
        pending = channel.frames[-1]
        assert pending["full_access_active"] is False
        assert pending["policy_epoch"] == rejected["policy_epoch"]
        assert store.load_state().permission_mode is action_policy.PermissionMode.ASK

        session._suppress_epoch_snapshot = True
        try:
            _grant(store, tmp_path)
        finally:
            session._suppress_epoch_snapshot = False
        with_grant = await session.send_snapshot()
        assert with_grant["temporary_permissions"]
        await session.handle_change_request(envelope(
            with_grant,
            request_id="permission-change:revoke-temporary",
            operation="revoke_all_temporary_permissions",
        ))
        temporary_cleared = channel.frames[-1]
        assert temporary_cleared["temporary_permissions"] == []
        assert temporary_cleared["categories"]["commands"] is True

        malformed = envelope(
            temporary_cleared,
            request_id="permission-change:malformed",
            operation="set_category",
            category="unknown_category",
            enabled=True,
        )
        await session.handle_change_request(malformed)
        assert channel.frames[-1]["categories"] == temporary_cleared["categories"]

        incompatible = envelope(
            channel.frames[-1],
            request_id="permission-change:incompatible",
            operation="apply_default_access",
        )
        incompatible["contract_schema_version"] = 99
        await session.handle_change_request(incompatible)
        assert channel.frames[-1]["categories"]["commands"] is True
    finally:
        session.close()


def test_session_accepts_only_canonical_snapshot_bound_changes(tmp_path):
    asyncio.run(_exercise_session_change_contract(tmp_path))


def test_mobile_store_change_is_reloaded_by_the_pc_page(tmp_path):
    pc_store = _store(tmp_path)
    mobile_process_store = _store(tmp_path)
    page = PermissionsPage(policy=PermissionPolicy(pc_store))
    assert page._switches["commands"].isChecked() is False

    mobile_process_store.update_automatic_permissions({"commands": True})
    page._refresh_external_epoch()
    _QT_APP.processEvents()

    assert page._switches["commands"].isChecked() is True
    assert page._snapshot["policy_epoch"] == pc_store.load_state().policy_epoch
    page.deleteLater()


async def _exercise_pc_to_mobile_broadcast(tmp_path):
    store = _store(tmp_path)
    channel = _Channel()
    session = server._PermissionMobileSession(
        channel, asyncio.get_running_loop(), store,
    )
    session.activate()
    session.bind_device("permission-device:pc-update")
    try:
        await session.send_snapshot()
        session._suppress_epoch_snapshot = True
        try:
            store.update_automatic_permissions({"files_delete": True})
            sent = await server._broadcast_permission_snapshots()
        finally:
            session._suppress_epoch_snapshot = False
        assert sent >= 1
        assert channel.frames[-1]["categories"]["files_delete"] is True
    finally:
        session.close()


def test_pc_change_broadcasts_a_fresh_mobile_snapshot(tmp_path):
    asyncio.run(_exercise_pc_to_mobile_broadcast(tmp_path))
