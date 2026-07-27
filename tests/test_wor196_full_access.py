"""Focused WOR-196 Full Access authority, constraints and privacy tests."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from osia.action_policy import (
    AUTOMATIC_PERMISSION_CATEGORIES,
    AuthorizationSource,
    DecisionType,
    ExecutionType,
    PermissionMode,
    PermissionPolicy,
    PermitError,
    UsePolicy,
    build_permission_request,
    automatic_permission_category,
    create_grant,
    make_execution_context,
    trusted_deletion_classification,
)
from osia.gui.permissions_center import FullAccessActivationService
from osia.local_reauth import (
    ReauthResult,
    ReauthStatus,
    WindowsHelloReauthProvider,
    issue_full_access_activation_proof,
)
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    SecureStorageBackend,
    UnavailableSecureStorageBackend,
)


class _SyntheticProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "synthetic_protected", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"W196" + bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"W196"):
            raise ValueError("invalid_protected_blob")
        return bytes(value ^ 0xA5 for value in protected[4:])


class _FixedReauth:
    def __init__(self, status: ReauthStatus):
        self.status = status

    def reauthenticate(self, prompt: str) -> ReauthResult:
        assert prompt
        method = (
            "unavailable" if self.status is ReauthStatus.UNAVAILABLE
            else "deterministic_test"
        )
        return ReauthResult(self.status, method, self.status.value)


def _policy(tmp_path: Path, name: str = "permissions.dat"):
    store = PermissionStore(tmp_path / name, backend=_SyntheticProtectedBackend())
    return PermissionPolicy(store)


def _enable(policy: PermissionPolicy):
    outcome = FullAccessActivationService(
        policy=policy,
        reauth_provider=_FixedReauth(ReauthStatus.SUCCESS),
    ).activate(acknowledged=True, second_confirmed=True)
    assert outcome.activated is True
    return policy.store.load_state()


def test_windows_hello_requires_desktop_window_handle(monkeypatch):
    version = type("Version", (), {"build": 26100})()
    monkeypatch.setattr("osia.local_reauth.sys.platform", "win32")
    monkeypatch.setattr(
        "osia.local_reauth.sys.getwindowsversion", lambda: version,
        raising=False,
    )
    result = WindowsHelloReauthProvider().reauthenticate("Confirm")
    assert result.status is ReauthStatus.ERROR
    assert result.reason_code == "window_handle_required"


def _request(
    policy: PermissionPolicy,
    *,
    execution_type: ExecutionType = ExecutionType.CHAT,
    operation: str = "operation:one",
    tool: str = "delete_file",
    arguments: dict | None = None,
    classification=None,
    force_sensitive: bool = True,
):
    context = make_execution_context(
        execution_type=execution_type,
        interactive=execution_type is ExecutionType.CHAT,
        owner_ref="owner:wor196",
        machine_ref="machine:wor196",
        run_id="run:wor196" if execution_type is ExecutionType.AUTONOMOUS else "",
        scheduled_task_id=(
            "" if execution_type is ExecutionType.SCHEDULED else ""
        ),
        task_revision="1" if execution_type is not ExecutionType.CHAT else "",
        plan_revision="1" if execution_type is not ExecutionType.CHAT else "",
        execution_id="execution:" + operation.replace(":", "-"),
        operation_id=operation,
        store=policy.store,
    )
    return build_permission_request(
        tool,
        arguments or {"path": str(policy.store.path.parent / "synthetic.txt")},
        execution_context=context,
        deletion_classification=(
            classification or trusted_deletion_classification()
        ),
        force_sensitive=force_sensitive,
        policy=policy,
    )


def test_unavailable_or_corrupt_store_is_ask_and_activation_unavailable(tmp_path):
    unavailable = PermissionStore(
        tmp_path / "missing.dat",
        backend=UnavailableSecureStorageBackend("synthetic_unavailable"),
    )
    snapshot = unavailable.permission_center_snapshot()
    assert snapshot["permission_mode"] == "ask"
    assert snapshot["activation_available"] is False
    assert not any(snapshot["automatic_categories"].values())

    policy = _policy(tmp_path, "corrupt.dat")
    _enable(policy)
    policy.store.path.write_bytes(b"corrupt")
    snapshot = policy.store.permission_center_snapshot()
    assert snapshot["permission_mode"] == "ask"
    assert snapshot["activation_available"] is False
    assert snapshot["store_corrupt"] is True
    assert not any(snapshot["automatic_categories"].values())


def test_acknowledgement_and_second_confirmation_are_both_required(tmp_path):
    policy = _policy(tmp_path)
    service = FullAccessActivationService(
        policy=policy, reauth_provider=_FixedReauth(ReauthStatus.SUCCESS),
    )
    assert service.activate(
        acknowledged=False, second_confirmed=True,
    ).reason_code == "acknowledgement_required"
    assert service.activate(
        acknowledged=True, second_confirmed=False,
    ).reason_code == "second_confirmation_required"
    assert policy.store.load_state().permission_mode is PermissionMode.ASK


def test_store_rejects_unsealed_or_replayed_activation(tmp_path):
    policy = _policy(tmp_path)
    with pytest.raises(ValueError, match="activation_proof_required"):
        policy.store.activate_full_access(activation_proof="windows_hello")
    proof = issue_full_access_activation_proof(
        ReauthResult(ReauthStatus.SUCCESS, "deterministic_test")
    )
    policy.store.activate_full_access(activation_proof=proof)
    policy.store.revoke_full_access()
    with pytest.raises(ValueError, match="activation_proof_used"):
        policy.store.activate_full_access(activation_proof=proof)


@pytest.mark.parametrize(
    ("status", "activated"),
    [
        (ReauthStatus.SUCCESS, True),
        (ReauthStatus.UNAVAILABLE, True),
        (ReauthStatus.DENIED, False),
        (ReauthStatus.CANCELLED, False),
        (ReauthStatus.ERROR, False),
    ],
)
def test_local_reauth_typed_outcomes(tmp_path, status, activated):
    policy = _policy(tmp_path, f"{status.value}.dat")
    outcome = FullAccessActivationService(
        policy=policy, reauth_provider=_FixedReauth(status),
    ).activate(acknowledged=True, second_confirmed=True)
    assert outcome.activated is activated
    assert (
        policy.store.load_state().permission_mode is PermissionMode.FULL_ACCESS
    ) is activated


def test_full_access_persists_and_activation_revocation_advance_epoch(tmp_path):
    path = tmp_path / "persistent.dat"
    first = PermissionStore(path, backend=_SyntheticProtectedBackend())
    before = first.load_state().policy_epoch
    enabled = _enable(PermissionPolicy(first))
    assert enabled.policy_epoch == before + 1
    assert all(first.permission_center_snapshot()["automatic_categories"].values())
    restarted = PermissionStore(path, backend=_SyntheticProtectedBackend())
    assert restarted.load_state().permission_mode is PermissionMode.FULL_ACCESS
    revoked = restarted.revoke_full_access()
    assert revoked.policy_epoch == enabled.policy_epoch + 1
    assert not any(
        restarted.permission_center_snapshot()["automatic_categories"].values()
    )
    assert restarted.revoke_full_access().changed is False


def test_each_switch_maps_to_trusted_category_and_controls_ordinary_action(tmp_path):
    policy = _policy(tmp_path)
    cases = (
        ("files_modify", ExecutionType.CHAT, "write_file", {
            "path": str(tmp_path / "write.txt"), "content": "synthetic",
        }),
        ("files_delete", ExecutionType.CHAT, "delete_file", {
            "path": str(tmp_path / "delete.txt"),
        }),
        ("commands", ExecutionType.CHAT, "run_command", {
            "command": "synthetic-command",
        }),
        ("browser_account", ExecutionType.CHAT, "fill_form_field", {
            "field_ref": "field:synthetic", "value": "synthetic",
        }),
        ("connectors", ExecutionType.CHAT, "gmail_mark_read", {
            "message_id": "message:synthetic",
        }),
        ("send_publish_submit", ExecutionType.CHAT, "gmail_send_draft", {
            "draft_id": "draft:synthetic",
        }),
        ("purchases_payments", ExecutionType.CHAT, "click", {
            "selector": "#checkout", "amount_minor": 25, "currency": "EUR",
        }),
        ("autonomous_actions", ExecutionType.AUTONOMOUS, "delete_file", {
            "path": str(tmp_path / "autonomous.txt"),
        }),
        ("scheduled_actions", ExecutionType.SCHEDULED, "delete_file", {
            "path": str(tmp_path / "scheduled.txt"),
        }),
    )
    assert {item[0] for item in cases} == set(AUTOMATIC_PERMISSION_CATEGORIES)
    for index, (category, execution_type, tool, arguments) in enumerate(cases):
        policy.store.update_automatic_permissions({category: True})
        allowed = _request(
            policy,
            execution_type=execution_type,
            operation=f"category:{index}:on",
            tool=tool,
            arguments=arguments,
        )
        assert automatic_permission_category(allowed) == category
        assert policy.evaluate(allowed).decision_type is DecisionType.ALLOW
        assert policy.store.load_state().permission_mode is PermissionMode.ASK
        assert policy.store.load_state().grants == ()

        policy.store.update_automatic_permissions({category: False})
        denied = _request(
            policy,
            execution_type=execution_type,
            operation=f"category:{index}:off",
            tool=tool,
            arguments=arguments,
        )
        assert policy.evaluate(denied).decision_type is not DecisionType.ALLOW


def test_category_change_advances_epoch_and_invalidates_loaded_permit(tmp_path):
    policy = _policy(tmp_path)
    policy.store.update_automatic_permissions({"files_delete": True})
    request = _request(policy, operation="category:permit")
    decision = policy.evaluate(request)
    permit = policy.mint_permit(request, decision)
    assert permit is not None
    epoch = policy.store.load_state().policy_epoch
    policy.store.update_automatic_permissions({"files_delete": False})
    assert policy.store.load_state().policy_epoch == epoch + 1
    with pytest.raises(PermitError):
        policy.consume_permit(permit, request)


def test_enabled_category_supersedes_a_narrower_matching_grant(tmp_path):
    policy = _policy(tmp_path)
    original = _request(policy, operation="category:grant:original")
    policy.store.add_grant(create_grant(
        original,
        use_policy=UsePolicy.RECURRING,
        persistent=True,
        constraints={"allowed": False},
    ))
    policy.store.update_automatic_permissions({"files_delete": True})
    current = _request(policy, operation="category:grant:current")
    decision = policy.evaluate(current)
    assert decision.decision_type is DecisionType.ALLOW
    assert decision.authorization_source is AuthorizationSource.FULL_ACCESS


def test_mixed_matrix_persists_only_in_protected_store_and_revoke_all_clears_it(
    tmp_path,
):
    path = tmp_path / "matrix.dat"
    first = PermissionStore(path, backend=_SyntheticProtectedBackend())
    first.update_automatic_permissions({"files_delete": True})
    assert b"files_delete" not in path.read_bytes()
    restarted = PermissionStore(path, backend=_SyntheticProtectedBackend())
    snapshot = restarted.permission_center_snapshot()
    assert snapshot["permission_mode"] == "ask"
    assert snapshot["automatic_categories"]["files_delete"] is True
    assert sum(snapshot["automatic_categories"].values()) == 1
    assert restarted.revoke_all_delegated_authority().changed is True
    assert not any(
        restarted.permission_center_snapshot()["automatic_categories"].values()
    )


def test_read_only_remains_allowed_with_every_switch_off(tmp_path):
    policy = _policy(tmp_path)
    request = _request(
        policy,
        operation="read:structural",
        tool="read_file",
        arguments={"path": str(tmp_path / "read.txt")},
        force_sensitive=False,
    )
    assert policy.evaluate(request).decision_type is DecisionType.ALLOW


def test_loaded_permit_is_invalid_after_immediate_revocation(tmp_path):
    policy = _policy(tmp_path)
    _enable(policy)
    request = _request(policy)
    decision = policy.evaluate(request)
    permit = policy.mint_permit(request, decision)
    assert permit is not None
    policy.store.revoke_full_access()
    with pytest.raises(PermitError):
        policy.consume_permit(permit, request)


def test_chat_autonomous_and_scheduled_ordinary_actions_allow_without_grants(tmp_path):
    policy = _policy(tmp_path)
    _enable(policy)
    for index, execution_type in enumerate(ExecutionType):
        decision = policy.evaluate(_request(
            policy,
            execution_type=execution_type,
            operation=f"ordinary:{index}",
        ))
        assert decision.decision_type is DecisionType.ALLOW
        assert decision.authorization_source is AuthorizationSource.FULL_ACCESS
    assert policy.store.load_state().grants == ()


def test_no_repeated_confirmation_but_universal_barriers_remain(tmp_path):
    policy = _policy(tmp_path)
    _enable(policy)
    for index in range(3):
        decision = policy.evaluate(_request(
            policy, operation=f"ordinary-repeat:{index}",
        ))
        assert decision.decision_type is DecisionType.ALLOW

    system = policy.evaluate(_request(
        policy,
        operation="system:barrier",
        classification=trusted_deletion_classification(system_destruction=True),
    ))
    assert system.decision_type is DecisionType.BLOCK_SYSTEM_DESTRUCTION
    colossal = policy.evaluate(_request(
        policy,
        operation="colossal:barrier",
        classification=trusted_deletion_classification(colossal_deletion=True),
    ))
    assert (
        colossal.decision_type
        is DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION
    )


def test_outcome_unknown_remains_non_replayable(tmp_path):
    policy = _policy(tmp_path)
    _enable(policy)
    request = _request(policy, operation="outcome:unknown")
    decision = policy.evaluate(request)
    permit = policy.mint_permit(request, decision)
    assert permit is not None
    policy.consume_permit(permit, request)
    policy.permit_ledger.mark_outcome_unknown(request.operation_id)
    replay = _request(policy, operation="outcome:unknown")
    assert policy.evaluate(replay).decision_type is DecisionType.DENY


def test_optional_payment_limit_enable_disable_and_no_conversion(tmp_path):
    policy = _policy(tmp_path)
    _enable(policy)
    policy.store.update_full_access_constraints(
        payment_enabled=True,
        max_amount_minor=1000,
        currency="EUR",
        destinations_enabled=False,
        origins=[],
        connectors=[],
    )
    within = _request(
        policy, operation="payment:within", tool="click",
        arguments={"amount_minor": 999, "currency": "EUR"},
    )
    over = _request(
        policy, operation="payment:over", tool="click",
        arguments={"amount_minor": 1001, "currency": "EUR"},
    )
    foreign = _request(
        policy, operation="payment:foreign", tool="click",
        arguments={"amount_minor": 1, "currency": "USD"},
    )
    assert policy.evaluate(within).decision_type is DecisionType.ALLOW
    assert policy.evaluate(over).decision_type is DecisionType.DENY
    assert policy.evaluate(foreign).decision_type is DecisionType.DENY

    policy.store.update_full_access_constraints(
        payment_enabled=False,
        max_amount_minor=1000,
        currency="EUR",
        destinations_enabled=False,
        origins=[],
        connectors=[],
    )
    restored = _request(
        policy, operation="payment:restored", tool="click",
        arguments={"amount_minor": 9000, "currency": "USD"},
    )
    assert policy.evaluate(restored).decision_type is DecisionType.ALLOW


def test_optional_exact_allowlist_enable_empty_and_disable(tmp_path):
    policy = _policy(tmp_path)
    _enable(policy)
    policy.store.update_full_access_constraints(
        payment_enabled=False,
        max_amount_minor=0,
        currency="EUR",
        destinations_enabled=True,
        origins=["https://allowed.example"],
        connectors=[],
    )
    snapshot = policy.store.permission_center_snapshot()
    assert "allowed.example" not in str(snapshot)
    assert snapshot["constraints"]["destinations"]["origin_count"] == 1
    policy.store.update_full_access_constraints(
        payment_enabled=False,
        max_amount_minor=25,
        currency="EUR",
        destinations_enabled=True,
        origins=None,
        connectors=None,
    )
    allowed = _request(
        policy, operation="origin:allowed", tool="click",
        arguments={"url": "https://allowed.example"},
    )
    denied = _request(
        policy, operation="origin:denied", tool="click",
        arguments={"url": "https://denied.example"},
    )
    assert policy.evaluate(allowed).decision_type is DecisionType.ALLOW
    assert policy.evaluate(denied).decision_type is DecisionType.DENY

    policy.store.update_full_access_constraints(
        payment_enabled=False,
        max_amount_minor=0,
        currency="EUR",
        destinations_enabled=True,
        origins=[],
        connectors=[],
    )
    empty = _request(
        policy, operation="origin:empty", tool="click",
        arguments={"url": "https://allowed.example"},
    )
    assert policy.evaluate(empty).decision_type is DecisionType.DENY

    policy.store.update_full_access_constraints(
        payment_enabled=False,
        max_amount_minor=0,
        currency="EUR",
        destinations_enabled=False,
        origins=[],
        connectors=[],
    )
    unrestricted = _request(
        policy, operation="origin:unrestricted", tool="click",
        arguments={"url": "https://denied.example"},
    )
    assert policy.evaluate(unrestricted).decision_type is DecisionType.ALLOW


def test_active_grants_projection_and_revocation_are_value_free(tmp_path):
    policy = _policy(tmp_path)
    sentinel = "WOR196_PRIVATE_PATH_SENTINEL"
    contexts = [
        (ExecutionType.CHAT, "grant:chat"),
        (ExecutionType.AUTONOMOUS, "grant:auto"),
        (ExecutionType.SCHEDULED, "grant:scheduled"),
    ]
    for execution_type, operation in contexts:
        request = _request(
            policy,
            execution_type=execution_type,
            operation=operation,
            arguments={"path": str(tmp_path / sentinel / execution_type.value)},
        )
        policy.store.add_grant(create_grant(
            request, use_policy=UsePolicy.RECURRING, persistent=True,
        ))
    snapshot = policy.store.permission_center_snapshot()
    assert {item["execution_type"] for item in snapshot["grants"]} == {
        "chat", "autonomous", "scheduled",
    }
    serialized = str(snapshot)
    for forbidden in (
        sentinel, "grant_id", "grant_ref", "scope_hash", "call_hash",
        "private_canonical",
    ):
        assert forbidden not in serialized

    first = snapshot["grants"][0]
    assert policy.store.revoke_center_grant(first["row_token"]).changed is True
    next_snapshot = policy.store.permission_center_snapshot()
    assert len(next_snapshot["grants"]) == 2
    grouped = next(
        item for item in next_snapshot["grants"] if item["group_token"]
    )
    assert policy.store.revoke_center_group(grouped["group_token"]).changed is True
    assert policy.store.revoke_all_delegated_authority().changed is True
    assert policy.store.permission_center_snapshot()["grants"] == []


def test_journal_is_bounded_clearable_and_sanitized_without_epoch_change(tmp_path):
    policy = _policy(tmp_path)
    sentinel = "C:/private/WOR196_SECRET?token=value"
    for index in range(210):
        policy.store.append_journal_event(
            "authorization_required",
            execution_type="chat",
            safe_category="local_path",
            authorization_source="none",
            result=sentinel if index == 209 else "confirmation_required",
            timestamp=float(index + 1),
        )
    snapshot = policy.store.permission_center_snapshot()
    assert len(snapshot["journal"]) == snapshot["journal_capacity"] == 200
    assert sentinel not in str(snapshot["journal"])
    epoch = snapshot["policy_epoch"]
    assert policy.store.clear_journal() is True
    cleared = policy.store.permission_center_snapshot()
    assert cleared["journal"] == []
    assert cleared["policy_epoch"] == epoch


def test_existing_ask_and_task_scoped_behavior_is_unchanged(tmp_path):
    policy = _policy(tmp_path)
    ask_request = _request(policy, operation="ask:ordinary")
    assert policy.evaluate(ask_request).decision_type is DecisionType.REQUIRE_CONFIRMATION
    grant = create_grant(
        ask_request, use_policy=UsePolicy.RECURRING, persistent=True,
    )
    policy.store.add_grant(grant)
    current = _request(policy, operation="ask:ordinary")
    # Exact operation identity remains bound and an altered replay cannot broaden.
    assert policy.store.load_state().permission_mode is PermissionMode.TASK_SCOPED
    assert policy.evaluate(current).authorization_source is AuthorizationSource.GRANT
