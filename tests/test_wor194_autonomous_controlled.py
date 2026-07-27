"""Controlled WOR-194 validation using only disposable local files."""

from __future__ import annotations

import time

import pytest

from osia.action_policy import (
    DecisionType,
    ExecutionType,
    PermissionPolicy,
    build_permission_request,
    execution_context_scope,
    guard_tool_call,
    make_execution_context,
    set_default_permission_policy,
)
from osia.agent import _execute_tool_with_policy
from osia.autonomous import authorization, state
from osia.permission_store import PermissionStore, UnavailableSecureStorageBackend
from osia.tool_contracts import ExecutionOutcome, ToolExecutionRecord


@pytest.fixture
def policy(tmp_path):
    value = PermissionPolicy(PermissionStore(
        tmp_path / "controlled-permissions.dat",
        backend=UnavailableSecureStorageBackend(),
    ))
    set_default_permission_policy(value)
    try:
        yield value
    finally:
        set_default_permission_policy(None)


def _state_for(target, policy):
    st = state.new_task_state(
        f"Write {target}",
        "fixture-provider",
        "fixture-model",
        [{"name": "Controlled", "tasks": [{
            "objective": f"Write {target}",
            "success_criterion": "The disposable file contains the marker",
        }]}],
        run_id="run:controlled-wor194",
    )
    authorization.prepare_planned_state(st, origin_ref="desktop", policy=policy)
    return st


def _response(st, action):
    return authorization.response_for_current_request(
        st, action=action, origin_ref="desktop",
    )


def _context(st, policy):
    return make_execution_context(
        execution_type=ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id=st["run_id"],
        plan_revision=str(st["plan_revision"]),
        task_revision=str(st["authorization_revision"]),
        store=policy.store,
    )


def _dispatch_write(st, policy, target, content):
    arguments = {"path": str(target), "content": content}
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_context(st, policy)):
        blocked = guard_tool_call(
            "Write the disposable test file", "write_file", arguments,
        )
        if blocked is not None:
            return blocked, None
        record = _execute_tool_with_policy(
            "write_file",
            arguments,
            user_request="Write the disposable test file",
            lang="en",
            enforce_execution_permit=True,
        )
        authorization.record_tool_execution("write_file", arguments, record)
        return None, record


def test_plan_authorize_extend_deny_revoke_and_no_replay(tmp_path, policy):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    denied = tmp_path / "denied.txt"
    revoked = tmp_path / "revoked.txt"
    st = _state_for(first, policy)

    # Planning is persistable and produces no mutation or authority.
    assert st["run_state"] == "authorization_pending"
    assert not first.exists()
    assert policy.store.load_state().grants == ()

    authorization.authorize_and_start(
        st, _response(st, "approve"), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    blocked, record = _dispatch_write(st, policy, first, "first")
    assert blocked is None
    assert record.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    assert first.read_text("utf-8") == "first"
    first_operation = next(
        item for item in st["operation_records"]
        if item["status"] == "success_verified"
    )
    assert first_operation["dispatch_attempt_count"] == 1

    # A changed exact scope suspends before dispatch and does not touch retries.
    attempts_before = st["phases"][0]["tasks"][0]["attempts"]
    blocked, record = _dispatch_write(st, policy, second, "second")
    assert blocked.startswith("ERROR: SUSPEND_SCOPE_EXTENSION")
    assert record is None
    assert not second.exists()
    assert st["phases"][0]["tasks"][0]["attempts"] == attempts_before
    assert st["run_state"] == "suspended_scope_extension"

    authorization.approve_scope_extension(
        st, _response(st, "approve_exact"), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    blocked, record = _dispatch_write(st, policy, second, "second")
    assert blocked is None
    assert record.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    assert second.read_text("utf-8") == "second"

    # The durable verified record blocks an equivalent replay, even though the
    # first exact grant is already exhausted.
    blocked, record = _dispatch_write(st, policy, first, "first")
    assert "duplicate_success_verified" in blocked
    assert record is None
    assert first.read_text("utf-8") == "first"
    assert state.operation_record(st, first_operation["operation_id"])["status"] == (
        "success_verified"
    )

    # Denial remains zero-dispatch and leaves the run safely suspended.
    blocked, _record = _dispatch_write(st, policy, denied, "denied")
    assert blocked.startswith("ERROR: SUSPEND_SCOPE_EXTENSION")
    authorization.approve_scope_extension(
        st, _response(st, "deny"), origin_ref="desktop", policy=policy,
    )
    assert not denied.exists()
    assert st["run_state"] == "suspended_scope_extension"

    # Run-specific revocation blocks the next sensitive dispatch immediately.
    authorization.revoke_run_authorization(st, policy=policy, save=lambda _st: None)
    blocked, record = _dispatch_write(st, policy, revoked, "revoked")
    assert "authorization_revoked" in blocked
    assert record is None
    assert not revoked.exists()
    assert first.exists() and second.exists()


def test_fault_injected_unknown_survives_new_policy_ledger(tmp_path, policy):
    target = tmp_path / "ambiguous.txt"
    st = _state_for(target, policy)
    authorization.authorize_and_start(
        st, _response(st, "approve"), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    arguments = {"path": str(target), "content": "ambiguous"}
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_context(st, policy)):
        request = build_permission_request("write_file", arguments, policy=policy)
        decision = policy.evaluate(request)
        assert decision.decision_type is DecisionType.ALLOW
        permit = policy.mint_permit(request, decision)
        assert permit is not None
        policy.consume_permit(permit, request)
        authorization.on_permit_consumed(request, permit)
        unknown = ToolExecutionRecord(
            "ERROR: injected post-dispatch transport loss",
            True,
            ExecutionOutcome.OUTCOME_UNKNOWN,
            1,
            True,
            True,
            request.effective_contract,
        )
        authorization.record_tool_execution("write_file", arguments, unknown)

    assert st["run_state"] == "blocked_outcome_unknown"
    assert st["ambiguity_barriers"]
    assert not target.exists()

    # A fresh policy/permit ledger models restart/fallback. Durable run state,
    # not an in-memory duplicate cache, rejects the same operation.
    restarted_policy = PermissionPolicy(policy.store)
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_context(st, restarted_policy)):
        duplicate = build_permission_request(
            "write_file", arguments, policy=restarted_policy,
        )
        blocked = restarted_policy.evaluate(duplicate)
    assert blocked.decision_type is DecisionType.DENY
    assert blocked.reason_code in {
        "duplicate_outcome_unknown", "outcome_unknown_blocked",
    }
    assert state.operation_record(st, request.operation_id)["status"] == (
        "outcome_unknown"
    )
    assert policy.store.autonomous_grant_refs_available(
        st["grant_refs"],
        run_id=st["run_id"],
        plan_revision=st["plan_revision"],
        now=time.time(),
    ) is False
