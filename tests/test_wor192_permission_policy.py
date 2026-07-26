from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from osia.action_policy import (
    AuthorizationSource,
    DecisionType,
    ExecutionType,
    PermissionMode,
    PermissionPolicy,
    TrustedDeletionClassification,
    UsePolicy,
    build_permission_request,
    create_grant,
    make_execution_context,
    trusted_deletion_classification,
)
from osia.permission_store import (
    PermissionStore,
    UnavailableSecureStorageBackend,
    WindowsDPAPISecureStorageBackend,
)
from osia.scope_normalization import canonical_hash


def _session_policy(tmp_path: Path, *, clock=None):
    store = PermissionStore(
        tmp_path / "session.dat", backend=UnavailableSecureStorageBackend(),
    )
    return store, PermissionPolicy(store, clock=clock)


def _secure_policy(tmp_path: Path):
    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("Windows DPAPI is required to simulate protected Full Access")
    store = PermissionStore(tmp_path / "protected.dat", backend=backend)
    return store, PermissionPolicy(store)


def _context(
    store,
    operation: str,
    *,
    execution_type=ExecutionType.CHAT,
    interactive=True,
    owner="owner:fixture",
    machine="machine:fixture",
    device="device:fixture",
    run="",
    scheduled="",
    task_revision="",
    plan_revision="",
):
    return make_execution_context(
        execution_type=execution_type,
        interactive=interactive,
        owner_ref=owner,
        machine_ref=machine,
        device_ref=device,
        run_id=run,
        scheduled_task_id=scheduled,
        task_revision=task_revision,
        plan_revision=plan_revision,
        execution_id=f"execution:{operation}",
        operation_id=f"operation:{operation}",
        store=store,
    )


def _request(policy, store, operation, tool, arguments, **context_kwargs):
    return build_permission_request(
        tool,
        arguments,
        execution_context=_context(store, operation, **context_kwargs),
        policy=policy,
    )


def _resign(grant, **changes):
    staged = replace(grant, integrity_metadata="0" * 64, **changes)
    return replace(
        staged,
        integrity_metadata=canonical_hash(staged._integrity_payload()),
    )


def test_ask_allows_structural_read_and_requires_sensitive_confirmation(tmp_path):
    store, policy = _session_policy(tmp_path)
    read = _request(policy, store, "read", "get_system_info", {})
    delete = _request(
        policy, store, "delete", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    assert policy.evaluate(read).decision_type == DecisionType.ALLOW
    decision = policy.evaluate(delete)
    assert decision.decision_type == DecisionType.REQUIRE_CONFIRMATION
    assert decision.authorization_source == AuthorizationSource.NONE


def test_untrusted_model_style_classification_cannot_enter_a_request(tmp_path):
    store, policy = _session_policy(tmp_path)
    request = _request(
        policy, store, "trusted-boundary", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    with pytest.raises(ValueError, match="untrusted_deletion_classification"):
        TrustedDeletionClassification(False, False, object())
    with pytest.raises(ValueError, match="untrusted_deletion_classification"):
        replace(request, deletion_classification={"system_destruction": True})


def test_exact_session_grant_allows_and_scope_or_arguments_cannot_drift(tmp_path):
    store, policy = _session_policy(tmp_path)
    original = _request(
        policy, store, "original", "delete_file",
        {"path": str(tmp_path / "one.txt")},
    )
    grant = create_grant(
        original, use_policy=UsePolicy.BOUNDED, max_uses=3,
    )
    store.add_grant(grant)
    exact = policy.evaluate(original)
    assert exact.decision_type == DecisionType.ALLOW
    assert exact.authorization_source == AuthorizationSource.GRANT

    changed_scope = _request(
        policy, store, "other-scope", "delete_file",
        {"path": str(tmp_path / "two.txt")},
    )
    assert policy.evaluate(changed_scope).authorization_source == AuthorizationSource.NONE
    assert policy.evaluate(changed_scope).reason_code == "changed_scope"

    write = _request(
        policy, store, "write", "write_file",
        {"path": str(tmp_path / "stable.txt"), "content": "one"},
    )
    write_grant = create_grant(write, use_policy=UsePolicy.RECURRING)
    store.add_grant(write_grant)
    changed_arguments = _request(
        policy, store, "write-drift", "write_file",
        {"path": str(tmp_path / "stable.txt"), "content": "two"},
    )
    changed = policy.evaluate(changed_arguments)
    assert changed.authorization_source == AuthorizationSource.NONE
    assert changed.reason_code == "structurally_safe"


@pytest.mark.parametrize(
    ("field", "changed", "reason"),
    [
        ("execution_type", ExecutionType.SCHEDULED, "wrong_execution_type"),
        ("owner", "owner:other", "wrong_owner"),
        ("machine", "machine:other", "wrong_machine"),
        ("device", "device:other", "wrong_device"),
        ("run", "run:other", "wrong_run"),
        ("scheduled", "scheduled:other", "wrong_scheduled_task"),
        ("task_revision", "task:other", "wrong_task_revision"),
        ("plan_revision", "plan:other", "wrong_plan_revision"),
    ],
)
def test_grant_identity_dimensions_match_exactly(tmp_path, field, changed, reason):
    store, policy = _session_policy(tmp_path)
    base = {
        "execution_type": ExecutionType.AUTONOMOUS,
        "interactive": False,
        "owner": "owner:fixture",
        "machine": "machine:fixture",
        "device": "device:fixture",
        "run": "run:fixture",
        "scheduled": "scheduled:fixture",
        "task_revision": "task:fixture",
        "plan_revision": "plan:fixture",
    }
    original = _request(
        policy, store, "grant-base", "delete_file",
        {"path": str(tmp_path / "fixture.txt")}, **base,
    )
    store.add_grant(create_grant(original, use_policy=UsePolicy.RECURRING))
    altered = dict(base)
    altered[field] = changed
    request = _request(
        policy, store, f"changed-{field}", "delete_file",
        {"path": str(tmp_path / "fixture.txt")}, **altered,
    )
    decision = policy.evaluate(request)
    assert decision.authorization_source == AuthorizationSource.NONE
    assert decision.reason_code == reason


def test_expired_exhausted_corrupt_and_ambiguous_grants_fail_closed(tmp_path):
    now = [100.0]
    store, policy = _session_policy(tmp_path, clock=lambda: now[0])
    request = _request(
        policy, store, "expiry", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    expired = create_grant(
        request, issued_at=90.0, expires_at=99.0,
    )
    store.add_grant(expired)
    assert policy.evaluate(request).reason_code == "expired_grant"

    store.revoke_all_grants()
    request = _request(
        policy, store, "single", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    single = create_grant(request)
    store.add_grant(single)
    decision = policy.evaluate(request)
    assert policy.mint_permit(request, decision) is not None
    second = _request(
        policy, store, "single-again", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    assert policy.evaluate(second).reason_code == "exhausted_grant"

    corrupt = replace(single, use_count=1)
    assert corrupt.integrity_is_valid() is False
    with pytest.raises(ValueError, match="invalid_grant"):
        store.add_grant(corrupt)

    ambiguous = _request(
        policy, store, "gmail", "gmail_send_draft", {"draft_id": "draft-1"},
    )
    ambiguous_grant = create_grant(
        ambiguous, use_policy=UsePolicy.RECURRING,
    )
    store.add_grant(ambiguous_grant)
    ambiguous_decision = policy.evaluate(ambiguous)
    assert ambiguous_decision.authorization_source == AuthorizationSource.NONE
    assert ambiguous_decision.reason_code == "ambiguous_legacy_identity"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"tool_category": "other"}, "wrong_tool_category"),
        ({"capability": "Other capability"}, "wrong_capability"),
        ({"effect_class": "read_only"}, "wrong_effect_class"),
        ({"policy_version": "legacy-policy"}, "wrong_policy_version"),
    ],
)
def test_structural_grant_contract_mismatches_are_rejected(
    tmp_path, changes, reason,
):
    store, policy = _session_policy(tmp_path)
    request = _request(
        policy, store, "structural", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    grant = _resign(create_grant(request), **changes)
    # Inject a synthetically well-formed stored grant so matching itself, not
    # the public add API's earlier validation, owns this assertion.
    store._session_grants[grant.grant_id] = grant
    decision = policy.evaluate(request)
    assert decision.authorization_source == AuthorizationSource.NONE
    assert decision.reason_code == reason


def test_stale_epoch_and_corrupt_grants_are_rejected_by_matching(tmp_path):
    store, policy = _session_policy(tmp_path)
    request = _request(
        policy, store, "epoch", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    grant = create_grant(request)
    store._volatile_epoch = 1
    current = _request(
        policy, store, "epoch-current", "delete_file",
        {"path": str(tmp_path / "fixture.txt")},
    )
    store._session_grants[grant.grant_id] = grant
    assert policy.evaluate(current).reason_code == "revoked_grant"

    store._session_grants.clear()
    valid_current = _resign(create_grant(current))
    corrupt = replace(valid_current, issued_at=valid_current.issued_at + 1.0)
    assert corrupt.integrity_is_valid() is False
    store._session_grants[corrupt.grant_id] = corrupt
    assert policy.evaluate(current).reason_code == "corrupt_grant"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", {"path": "fixture.txt", "content": "synthetic"}),
        ("run_command", {"command": "echo synthetic"}),
        ("gmail_send_draft", {"draft_id": "draft-1"}),
        ("find_and_click", {"text": "Buy"}),
        ("find_and_click", {"text": "Publish"}),
    ],
)
def test_full_access_allows_ordinary_local_shell_and_external_actions(
    tmp_path, tool, arguments,
):
    store, policy = _secure_policy(tmp_path)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    request = _request(policy, store, f"full-{tool}", tool, arguments)
    decision = policy.evaluate(request)
    assert decision.decision_type == DecisionType.ALLOW
    assert decision.authorization_source == AuthorizationSource.FULL_ACCESS


@pytest.mark.parametrize("mode", [PermissionMode.ASK, PermissionMode.FULL_ACCESS])
def test_universal_system_destruction_barrier_cannot_be_bypassed(tmp_path, mode):
    store, policy = _secure_policy(tmp_path)
    if mode == PermissionMode.FULL_ACCESS:
        store.set_permission_mode(mode)
    request = build_permission_request(
        "delete_directory",
        {"path": str(tmp_path / "synthetic-system"), "recursive": True},
        execution_context=_context(store, f"system-{mode.value}"),
        deletion_classification=trusted_deletion_classification(
            system_destruction=True,
        ),
        policy=policy,
    )
    assert (
        policy.evaluate(request).decision_type
        == DecisionType.BLOCK_SYSTEM_DESTRUCTION
    )


def test_grant_cannot_bypass_system_destruction_or_colossal_deletion(tmp_path):
    store, policy = _session_policy(tmp_path)
    ordinary = _request(
        policy, store, "ordinary", "delete_directory",
        {"path": str(tmp_path / "tree"), "recursive": True},
    )
    store.add_grant(create_grant(ordinary, use_policy=UsePolicy.RECURRING))
    for classification, expected in (
        (
            trusted_deletion_classification(system_destruction=True),
            DecisionType.BLOCK_SYSTEM_DESTRUCTION,
        ),
        (
            trusted_deletion_classification(colossal_deletion=True),
            DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION,
        ),
    ):
        request = build_permission_request(
            ordinary.tool_identity,
            {"path": str(tmp_path / "tree"), "recursive": True},
            execution_context=ordinary.execution_context,
            deletion_classification=classification,
            policy=policy,
        )
        assert policy.evaluate(request).decision_type == expected


def test_full_access_still_requires_reinforced_colossal_approval(tmp_path):
    store, policy = _secure_policy(tmp_path)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    request = build_permission_request(
        "delete_directory",
        {"path": str(tmp_path / "large-tree"), "recursive": True},
        execution_context=_context(store, "colossal"),
        deletion_classification=trusted_deletion_classification(
            colossal_deletion=True,
        ),
        policy=policy,
    )
    decision = policy.evaluate(request)
    assert decision.decision_type == DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION
    assert policy.mint_permit(request, decision, confirmed=True) is None
    assert policy.mint_permit(request, decision, reinforced=True) is not None


def test_ask_still_requires_reinforced_colossal_approval(tmp_path):
    store, policy = _session_policy(tmp_path)
    request = build_permission_request(
        "delete_directory",
        {"path": str(tmp_path / "large-tree"), "recursive": True},
        execution_context=_context(store, "colossal-ask"),
        deletion_classification=trusted_deletion_classification(
            colossal_deletion=True,
        ),
        policy=policy,
    )
    decision = policy.evaluate(request)
    assert decision.decision_type == DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION
    assert policy.mint_permit(request, decision, confirmed=True) is None


def test_optional_constraint_failure_and_unattended_scope_extension(tmp_path):
    store, policy = _session_policy(tmp_path)
    constrained = build_permission_request(
        "delete_file", {"path": str(tmp_path / "fixture.txt")},
        execution_context=_context(store, "constraint"),
        optional_constraints={"allowed": False},
        policy=policy,
    )
    store.add_grant(create_grant(constrained, use_policy=UsePolicy.RECURRING))
    assert policy.evaluate(constrained).reason_code == "optional_constraint_failed"

    store.revoke_all_grants()
    over_limit = build_permission_request(
        "delete_file", {"path": str(tmp_path / "limited.txt")},
        execution_context=_context(store, "over-limit"),
        optional_constraints={
            "amount_minor": 500,
            "currency": "EUR",
            "max_amount_minor": 1000,
        },
        policy=policy,
    )
    limited_grant = create_grant(
        over_limit,
        use_policy=UsePolicy.RECURRING,
        constraints={"max_amount_minor": 100, "currency": "EUR"},
    )
    store.add_grant(limited_grant)
    assert policy.evaluate(over_limit).reason_code == "optional_constraint_failed"

    within_limit = build_permission_request(
        "delete_file", {"path": str(tmp_path / "limited.txt")},
        execution_context=_context(store, "within-limit"),
        optional_constraints={"amount_minor": 50, "currency": "EUR"},
        policy=policy,
    )
    assert policy.evaluate(within_limit).authorization_source == AuthorizationSource.GRANT

    unattended = _request(
        policy, store, "headless", "delete_file",
        {"path": str(tmp_path / "other.txt")},
        execution_type=ExecutionType.AUTONOMOUS,
        interactive=False,
        run="run:headless",
    )
    assert (
        policy.evaluate(unattended).decision_type
        == DecisionType.SUSPEND_SCOPE_EXTENSION
    )
