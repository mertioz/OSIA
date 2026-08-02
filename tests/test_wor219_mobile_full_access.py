"""WOR-219 confirmed Mobile Full Access security contract."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from osia import action_policy
from osia.local_reauth import issue_mobile_full_access_activation_proof
from osia.permission_contract import mobile_permission_contract
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
        return BackendAvailability(True, "wor219_test", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"WOR219" + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"WOR219"):
            raise ValueError("corrupt")
        return protected[6:][::-1]


class _Channel:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _store(tmp_path) -> PermissionStore:
    return PermissionStore(
        tmp_path / "permissions.dat",
        backend=_ProtectedBackend(),
    )


def _change(
    session,
    snapshot,
    *,
    request_id="permission-change:wor219",
    auth_nonce="auth:wor219",
    expires_at=None,
):
    return {
        "schema_version": 1,
        "contract_schema_version": 2,
        "request_id": request_id,
        "session_id": session.session_id,
        "expected_policy_epoch": snapshot["policy_epoch"],
        "expected_snapshot_sequence": snapshot["sequence"],
        "operation": "request_full_access",
        "category_id": "",
        "enabled": False,
        "auth_method": "osia_confirmation",
        "auth_nonce": auth_nonce,
        "expires_at": time.time() + 20 if expires_at is None else expires_at,
    }


def test_contract_declares_osia_confirmation_without_system_authentication():
    contract = mobile_permission_contract()
    assert contract["schema_version"] == 2
    assert contract["full_access_activation"] == "mobile_osia_confirmation"
    assert len(contract["categories"]) == 9
    rendered = json.dumps(contract, sort_keys=True)
    assert "credential" not in rendered
    assert "biometric_data" not in rendered
    assert "pin" not in rendered
    assert "android_system_authentication" not in rendered


def test_full_access_activation_is_epoch_bound_atomically(tmp_path):
    store = _store(tmp_path)
    stale_epoch = store.load_state().policy_epoch
    store.update_automatic_permissions({"commands": True})

    with pytest.raises(PermissionStoreError, match="stale_policy_epoch"):
        store.activate_full_access(
            activation_proof=issue_mobile_full_access_activation_proof(),
            expected_policy_epoch=stale_epoch,
        )

    assert store.load_state().permission_mode is action_policy.PermissionMode.ASK


async def _exercise_authenticated_activation(tmp_path):
    store = _store(tmp_path)
    channel = _Channel()
    session = server._PermissionMobileSession(
        channel,
        asyncio.get_running_loop(),
        store,
    )
    session.activate()
    with server._PERMISSION_REQUESTS_LOCK:
        server._PERMISSION_REQUESTS.clear()
    try:
        # A token-authenticated socket still cannot mutate before it is bound to
        # the paired device identity.
        await session.handle_change_request(_change(
            session,
            {
                "policy_epoch": store.load_state().policy_epoch,
                "sequence": 0,
            },
            request_id="permission-change:unpaired",
        ))
        assert channel.frames == []
        assert store.load_state().permission_mode is action_policy.PermissionMode.ASK

        assert session.bind_device("permission-device:wor219")
        snapshot = await session.send_snapshot()

        for name, mutate in (
            (
                "wrong-session",
                lambda value: value.update(session_id="permission-session:wrong"),
            ),
            (
                "wrong-version",
                lambda value: value.update(contract_schema_version=99),
            ),
            (
                "expired",
                lambda value: value.update(expires_at=time.time() - 1),
            ),
            (
                "missing-auth",
                lambda value: value.update(auth_method="", auth_nonce=""),
            ),
        ):
            request = _change(
                session,
                snapshot,
                request_id=f"permission-change:{name}",
                auth_nonce=f"auth:{name}",
            )
            mutate(request)
            await session.handle_change_request(request)
            snapshot = channel.frames[-1]
            assert snapshot["full_access_active"] is False
            assert store.load_state().permission_mode is action_policy.PermissionMode.ASK

        request = _change(session, snapshot)
        frames_before = len(channel.frames)
        await session.handle_change_request(request)
        activated = channel.frames[-1]
        assert len(channel.frames) == frames_before + 1
        assert activated["full_access_active"] is True
        assert activated["preset"] == "full_access"
        assert all(activated["categories"].values())
        assert activated["policy_epoch"] == snapshot["policy_epoch"] + 1
        assert "auth:wor219" not in json.dumps(activated, sort_keys=True)

        # A duplicate request and a fresh request reusing the auth nonce are
        # both ignored and cannot advance the canonical epoch.
        epoch = activated["policy_epoch"]
        await session.handle_change_request(request)
        duplicate_snapshot = channel.frames[-1]
        assert duplicate_snapshot["policy_epoch"] == epoch
        replay = _change(
            session,
            duplicate_snapshot,
            request_id="permission-change:replayed-auth",
            auth_nonce="auth:wor219",
        )
        await session.handle_change_request(replay)
        replay_snapshot = channel.frames[-1]
        assert replay_snapshot["policy_epoch"] == epoch

        revoke = {
            "type": "permission_revocation_request",
            "schema_version": 1,
            "request_id": "permission-revocation:wor219",
            "device_id": session.device_id,
            "session_id": session.session_id,
            "expected_policy_epoch": replay_snapshot["policy_epoch"],
            "expected_snapshot_sequence": replay_snapshot["sequence"],
            "operation": "disable_full_access",
            "target_id": "",
            "expires_at": time.time() + 20,
        }
        await session.handle_revocation(revoke)
        revoked = channel.frames[-1]
        assert revoked["full_access_active"] is False
        assert revoked["policy_epoch"] == epoch + 1
    finally:
        session.close()


def test_paired_session_accepts_once_and_replay_fails_closed(tmp_path):
    asyncio.run(_exercise_authenticated_activation(tmp_path))
