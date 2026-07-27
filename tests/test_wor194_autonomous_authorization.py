from __future__ import annotations

import json
import time

import pytest

from osia.action_policy import (
    DecisionType,
    ExecutionType,
    PermissionMode,
    PermissionPolicy,
    build_permission_request,
    execution_context_scope,
    make_execution_context,
    set_default_permission_policy,
    trusted_deletion_classification,
)
from osia.autonomous import authorization, state
from osia.permission_store import (
    PermissionStore,
    UnavailableSecureStorageBackend,
    WindowsDPAPISecureStorageBackend,
)
from osia.tool_contracts import ExecutionOutcome, ToolExecutionRecord


class _PlannerAgent:
    provider = "fixture"
    model = "fixture-model"

    def __init__(self, path):
        self.path = path
        self.calls = 0

    def set_turn_context(self, _context):
        return None

    def reset(self):
        return None

    def run(self, _message, **_kwargs):
        self.calls += 1
        return json.dumps({
            "phases": [{
                "name": "Prepare",
                "tasks": [{
                    "objective": f"Write {self.path}",
                    "success_criterion": "The temporary file exists",
                }],
            }],
        })


@pytest.fixture
def policy(tmp_path):
    store = PermissionStore(
        tmp_path / "permissions.dat",
        backend=UnavailableSecureStorageBackend(),
    )
    value = PermissionPolicy(store)
    set_default_permission_policy(value)
    try:
        yield value
    finally:
        set_default_permission_policy(None)


def _planned(tmp_path, policy):
    target = tmp_path / "one.txt"
    st = state.new_task_state(
        f"Write {target}",
        "fixture",
        "fixture-model",
        [{"name": "P", "tasks": [{
            "objective": f"Write {target}",
            "success_criterion": "file exists",
        }]}],
        run_id="run:wor194",
    )
    authorization.prepare_planned_state(st, origin_ref="desktop", policy=policy)
    return st, target


def _response(st, action="approve", origin="desktop"):
    return authorization.response_for_current_request(
        st, action=action, origin_ref=origin,
    )


def _runtime_context(st, policy):
    return make_execution_context(
        execution_type=ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id=st["run_id"],
        plan_revision=str(st["plan_revision"]),
        task_revision=str(st["authorization_revision"]),
        store=policy.store,
    )


def test_plan_is_persistable_before_authority_and_does_not_start(tmp_path, policy):
    from osia.autonomous import planner

    agent = _PlannerAgent(tmp_path / "planned.txt")
    st = planner.create_plan_state(
        "Create the disposable file",
        "fixture",
        "fixture-model",
        agent,
        run_id="run:plan-first",
        origin_ref="desktop",
        permission_policy=policy,
    )
    assert st is not None
    assert st["run_state"] == "authorization_pending"
    assert st["status"] == "in_progress"
    assert st["capability_summary"]
    assert policy.store.load_state().grants == ()
    assert agent.calls == 1


def test_authorize_creates_exact_refs_without_plain_private_scope(tmp_path, policy):
    st, target = _planned(tmp_path, policy)
    assert str(target) not in json.dumps(state._checkpoint_projection(st))

    refs = authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    assert st["run_state"] == "ready"
    assert st["authorization_revision"] == 1
    assert refs == st["grant_refs"]
    assert len(refs) == 1
    checkpoint = json.dumps(state._checkpoint_projection(st))
    assert str(target) not in checkpoint
    assert "arguments" not in checkpoint
    assert policy.store.autonomous_grant_refs_available(
        refs,
        run_id=st["run_id"],
        plan_revision=st["plan_revision"],
        now=time.time(),
    )


def test_mobile_cannot_authorize_hidden_local_exact_scope(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    authorization.prepare_planned_state(
        st, origin_ref="mobile_one", policy=policy,
    )
    capability_id = next(
        item["capability_id"] for item in st["capability_summary"]
        if item["delegable"]
    )
    response = _response(st, origin="mobile_one")
    with pytest.raises(authorization.AuthorizationError, match="local_pc_review_required"):
        authorization.authorize_and_start(
            st,
            response,
            selected_capability_ids=[capability_id],
            origin_ref="mobile_one",
            policy=policy,
        )
    assert policy.store.load_state().grants == ()
    assert st["authorization_request"]["responded"] is False


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("run_id", "run:wrong", "wrong_run_id"),
        ("plan_revision", 99, "wrong_plan_revision"),
        ("authorization_revision", 99, "wrong_authorization_revision"),
        ("expected_policy_epoch", 99, "wrong_policy_epoch"),
        ("origin_ref", "mobile:wrong", "wrong_origin"),
    ],
)
def test_authorization_response_binding_rejects_stale_fields(
    tmp_path, policy, field, value, code,
):
    st, _target = _planned(tmp_path, policy)
    response = _response(st)
    response[field] = value
    with pytest.raises(authorization.AuthorizationError, match=code):
        authorization.authorize_and_start(
            st, response, origin_ref="desktop", policy=policy,
        )
    assert policy.store.load_state().grants == ()


def test_expired_and_duplicate_responses_are_rejected(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    st["authorization_request"]["expires_at"] = time.time() - 1
    with pytest.raises(authorization.AuthorizationError, match="expired_request"):
        authorization.authorize_and_start(
            st, _response(st), origin_ref="desktop", policy=policy,
        )

    st, _target = _planned(tmp_path, policy)
    request_id = st["authorization_request"]["request_id"]
    st["handled_authorization_requests"].append(request_id)
    with pytest.raises(authorization.AuthorizationError, match="duplicate_response"):
        authorization.authorize_and_start(
            st, _response(st), origin_ref="desktop", policy=policy,
        )


def test_changed_scope_suspends_before_dispatch_and_extension_is_exact(
    tmp_path, policy,
):
    st, _target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    other = tmp_path / "other.txt"
    saves = []
    events = []
    with authorization.run_authorization_scope(
        st,
        save=lambda value: saves.append(state._checkpoint_projection(value)),
        emit=events.append,
        origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request(
            "write_file", {"path": str(other), "content": "synthetic"},
            policy=policy,
        )
        decision = policy.evaluate(request)

    assert decision.decision_type == DecisionType.SUSPEND_SCOPE_EXTENSION
    assert st["run_state"] == "suspended_scope_extension"
    assert st["pending_scope_extension"]["local_pc_review_required"] is True
    assert state.operation_record(st, request.operation_id)["dispatch_attempt_count"] == 0
    assert saves and events[-1]["event"] == "autonomous_scope_extension_request"
    pending = st["pending_scope_extension"]
    assert str(other) not in json.dumps(pending)
    pc_detail = authorization.pc_projection(st)["pending_scope_extension"][
        "private_scope_detail"
    ]
    assert pc_detail.lower() == str(other).lower()
    assert "other.txt" not in json.dumps(authorization.mobile_projection(st)).lower()

    response = authorization.response_for_current_request(
        st, action="approve_exact", origin_ref="desktop",
    )
    grant_ref = authorization.approve_scope_extension(
        st, response, origin_ref="desktop", policy=policy,
    )
    assert grant_ref in st["grant_refs"]
    assert st["authorization_revision"] == 2
    assert st["run_state"] == "ready"


def test_denied_extension_dispatches_nothing_and_keeps_suspended(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request(
            "write_file",
            {"path": str(tmp_path / "denied.txt"), "content": "x"},
            policy=policy,
        )
        assert policy.evaluate(request).decision_type == (
            DecisionType.SUSPEND_SCOPE_EXTENSION
        )
    response = authorization.response_for_current_request(
        st, action="deny", origin_ref="desktop",
    )
    assert authorization.approve_scope_extension(
        st, response, origin_ref="desktop", policy=policy,
    ) is None
    assert st["run_state"] == "suspended_scope_extension"
    assert state.operation_record(st, request.operation_id)["dispatch_attempt_count"] == 0


def test_outcome_unknown_is_durable_and_blocks_equivalent_operation(tmp_path, policy):
    st, target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    arguments = {"path": str(target), "content": "synthetic"}
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request("write_file", arguments, policy=policy)
        decision = policy.evaluate(request)
        assert decision.decision_type == DecisionType.ALLOW
        record = ToolExecutionRecord(
            "ERROR: synthetic timeout",
            True,
            ExecutionOutcome.OUTCOME_UNKNOWN,
            1,
            True,
            True,
            request.effective_contract,
        )
        authorization.record_tool_execution("write_file", arguments, record)
        duplicate = build_permission_request("write_file", arguments, policy=policy)
        blocked = policy.evaluate(duplicate)

    assert st["run_state"] == "blocked_outcome_unknown"
    assert st["ambiguity_barriers"]
    assert blocked.decision_type == DecisionType.DENY
    assert blocked.reason_code in {"duplicate_outcome_unknown", "outcome_unknown_blocked"}
    assert "synthetic" not in json.dumps(state._checkpoint_projection(st))


def test_revocation_invalidates_run_refs_and_is_idempotent(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    first = authorization.revoke_run_authorization(
        st, policy=policy, save=lambda _st: None,
    )
    second = authorization.revoke_run_authorization(
        st, policy=policy, save=lambda _st: None,
    )
    assert first["changed"] is True
    assert second["changed"] is False
    assert st["run_state"] == "suspended_authorization_revoked"
    assert not policy.store.autonomous_grant_refs_available(
        st["grant_refs"],
        run_id=st["run_id"],
        plan_revision=st["plan_revision"],
        now=time.time(),
    )
    authorization.revalidate_loaded_authorization(
        st, origin_ref="desktop", policy=policy,
    )
    assert st["run_state"] == "suspended_authorization_revoked"
    assert st["authorization_request"] is None


def test_revoke_one_grant_preserves_other_protected_grant_but_stops_run(
    tmp_path, policy,
):
    st, _target = _planned(tmp_path, policy)
    first = authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )[0]
    st["run_state"] = "running"
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request(
            "write_file",
            {"path": str(tmp_path / "second.txt"), "content": "x"},
            policy=policy,
        )
        assert policy.evaluate(request).decision_type == (
            DecisionType.SUSPEND_SCOPE_EXTENSION
        )
    second = authorization.approve_scope_extension(
        st,
        authorization.response_for_current_request(
            st, action="approve_exact", origin_ref="desktop",
        ),
        origin_ref="desktop",
        policy=policy,
    )
    result = authorization.revoke_run_authorization(
        st, grant_ref=first, policy=policy, save=lambda _st: None,
    )
    assert result["changed"] is True
    assert st["run_state"] == "suspended_authorization_revoked"
    assert not policy.store.autonomous_grant_refs_available(
        [first], run_id=st["run_id"], plan_revision=1, now=time.time(),
    )
    assert policy.store.autonomous_grant_refs_available(
        [second], run_id=st["run_id"], plan_revision=1, now=time.time(),
    )


def test_safe_non_private_exact_extension_can_be_approved_from_mobile(
    tmp_path, policy,
):
    st, _target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    origin = "mobile:safe-extension"
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref=origin,
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request(
            "set_volume", {"level": 37}, policy=policy,
        )
        assert policy.evaluate(request).decision_type == (
            DecisionType.SUSPEND_SCOPE_EXTENSION
        )

    pending = st["pending_scope_extension"]
    assert pending["safe_category"] == "safe_local_setting"
    assert pending["local_pc_review_required"] is False
    assert pending["can_approve_mobile"] is True
    ref = authorization.approve_scope_extension(
        st,
        authorization.response_for_current_request(
            st, action="approve_exact", origin_ref=origin,
        ),
        origin_ref=origin,
        policy=policy,
    )
    assert ref in st["grant_refs"]
    assert st["authorization_revision"] == 2


def test_derived_colossal_deletion_keeps_universal_pc_barrier(tmp_path, policy):
    target = tmp_path / "colossal"
    target.mkdir()
    for index in range(1001):
        (target / f"item-{index}.tmp").write_text("x", encoding="utf-8")
    st = state.new_task_state(
        f"Delete directory {target} recursively",
        "fixture",
        "fixture-model",
        [{"name": "Delete", "tasks": [{
            "objective": f"Delete directory {target} recursively",
            "success_criterion": "The disposable directory is absent",
        }]}],
        run_id="run:colossal-proposal",
    )

    authorization.prepare_planned_state(
        st, origin_ref="mobile:colossal", policy=policy,
    )

    capability = st["capability_summary"][0]
    assert capability["universal_barrier"] == "colossal_deletion_pc_only"
    assert capability["delegable"] is False
    assert capability["can_approve_mobile"] is False


def test_session_grants_require_reauthorization_after_process_restart(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    assert st["grant_refs"]
    restarted_store = PermissionStore(
        tmp_path / "permissions.dat",
        backend=UnavailableSecureStorageBackend(),
    )
    restarted_policy = PermissionPolicy(restarted_store)
    authorization.revalidate_loaded_authorization(
        st, origin_ref="desktop", policy=restarted_policy,
    )
    assert st["run_state"] == "authorization_pending"
    assert st["grant_refs"] == []


def test_pending_review_restart_restores_exact_descriptor_and_request(
    tmp_path, policy,
):
    st, _target = _planned(tmp_path, policy)
    request_id = st["authorization_request"]["request_id"]
    with authorization._PRIVATE_LOCK:
        authorization._PRIVATE_DESCRIPTORS.clear()
    st.pop("_runtime_capability_descriptors", None)
    st.pop("_runtime_pc_capability_details", None)

    authorization.revalidate_loaded_authorization(
        st, origin_ref="mobile:reconnected", policy=policy,
    )

    assert st["authorization_request"]["request_id"] == request_id
    assert st["plan_revision"] == 1
    assert st["authorization_revision"] == 0
    st["authorization_request"]["origin_ref"] = "desktop"
    refs = authorization.authorize_and_start(
        st,
        _response(st),
        origin_ref="desktop",
        policy=policy,
    )
    assert len(refs) == 1


def test_stale_full_access_claim_cannot_survive_protected_ask_mode(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    st["run_state"] = "ready"
    st["authorization_status"] = "not_required_full_access"
    st["authorization_source"] = "full_access"
    authorization.revalidate_loaded_authorization(
        st, origin_ref="desktop", policy=policy,
    )
    assert st["run_state"] == "authorization_pending"
    assert st["authorization_source"] == "none"


def test_replan_revokes_old_revision_grants_and_preserves_operations(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    old_refs = authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    st["operation_records"].append({
        "operation_id": "op_preserved",
        "task_id": "task:t0",
        "phase_id": "phase:0",
        "plan_revision": 1,
        "tool_identity": "write_file",
        "normalized_call_hash": "a" * 64,
        "private_scope_hash": "b" * 64,
        "grant_ref": old_refs[0],
        "authorization_source": "grant",
        "policy_epoch": st["policy_epoch"],
        "status": "success_verified",
        "dispatch_attempt_count": 1,
        "started_at": "now",
        "completed_at": "now",
        "execution_outcome": "success_verified",
        "safe_reason_code": "success_verified",
    })
    authorization.prepare_replan(st, origin_ref="desktop", policy=policy)
    assert st["plan_revision"] == 2
    assert st["run_state"] == "authorization_pending"
    assert st["grant_refs"] == []
    assert state.operation_record(st, "op_preserved")["status"] == "success_verified"
    assert not policy.store.autonomous_grant_refs_available(
        old_refs,
        run_id=st["run_id"],
        plan_revision=1,
        now=time.time(),
    )


def test_full_access_skips_task_scope_but_not_universal_barriers(tmp_path):
    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("DPAPI unavailable")
    store = PermissionStore(tmp_path / "protected.dat", backend=backend)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    policy = PermissionPolicy(store)
    st = state.new_task_state(
        "Read system information", "fixture", "fixture-model",
        [{"name": "P", "tasks": ["Read system information"]}],
        run_id="run:full",
    )
    authorization.prepare_planned_state(st, origin_ref="desktop", policy=policy)
    assert st["run_state"] == "ready"
    assert st["authorization_source"] == "full_access"
    assert st["grant_refs"] == []

    context = make_execution_context(
        execution_type=ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id=st["run_id"],
        plan_revision="1",
        task_revision="0",
        store=store,
    )
    request = build_permission_request(
        "delete_directory",
        {"path": str(tmp_path), "recursive": True},
        execution_context=context,
        deletion_classification=trusted_deletion_classification(
            system_destruction=True,
        ),
        policy=policy,
    )
    assert policy.evaluate(request).decision_type == DecisionType.BLOCK_SYSTEM_DESTRUCTION

    authorization.revoke_run_authorization(st, policy=policy, save=lambda _st: None)
    authorization.revalidate_loaded_authorization(
        st, origin_ref="desktop", policy=policy,
    )
    assert st["run_state"] == "suspended_authorization_revoked"
    assert st["authorization_status"] == "revoked"


def test_lost_private_scope_request_returns_to_fresh_review(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    authorization.authorize_and_start(
        st, _response(st), origin_ref="desktop", policy=policy,
    )
    st["run_state"] = "running"
    with authorization.run_authorization_scope(
        st, save=lambda _st: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request(
            "write_file",
            {"path": str(tmp_path / "lost.txt"), "content": "x"},
            policy=policy,
        )
        assert policy.evaluate(request).decision_type == (
            DecisionType.SUSPEND_SCOPE_EXTENSION
        )
    with authorization._PRIVATE_LOCK:
        authorization._PRIVATE_REQUESTS.clear()
    authorization.revalidate_loaded_authorization(
        st, origin_ref="desktop", policy=policy,
    )
    assert st["run_state"] == "authorization_pending"
    assert st["pending_scope_extension"] is None


def test_partial_effect_is_durable_non_replayable_barrier(tmp_path, policy):
    st, _target = _planned(tmp_path, policy)
    record = {
        "operation_id": "op_partial",
        "task_id": "task:t0",
        "phase_id": "phase:0",
        "plan_revision": 1,
        "tool_identity": "write_file",
        "normalized_call_hash": "c" * 64,
        "private_scope_hash": "d" * 64,
        "grant_ref": "grant_ref",
        "authorization_source": "grant",
        "policy_epoch": 0,
        "status": "dispatched",
        "dispatch_attempt_count": 1,
        "started_at": "now",
        "completed_at": None,
        "execution_outcome": "dispatched",
        "safe_reason_code": "permit_consumed",
    }
    state.upsert_operation_record(st, record)
    authorization.mark_partial_effect(st, "op_partial", save=lambda _st: None)
    assert state.operation_record(st, "op_partial")["status"] == (
        "partial_effect_verified"
    )
    assert st["run_state"] == "blocked_outcome_unknown"
    assert st["ambiguity_barriers"][0]["operation_id"] == "op_partial"


def test_mobile_projection_contains_no_private_targets(tmp_path, policy):
    st, target = _planned(tmp_path, policy)
    st["objective"] = f"Write {target} then email private@example.com at https://private.test/a"
    projection = authorization.mobile_projection(st)
    wire = json.dumps(projection)
    assert str(target) not in wire
    assert "private@example.com" not in wire
    assert "https://private.test" not in wire
    safe = authorization.mobile_safe_text(st["objective"])
    assert str(target) not in safe
    assert "private@example.com" not in safe
