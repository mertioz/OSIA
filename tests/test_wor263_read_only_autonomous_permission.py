"""Focused WOR-263 regression tests for Autonomous read-only permission truth."""

from __future__ import annotations

from dataclasses import replace

import pytest

from osia import action_policy
from osia.action_policy import (
    AuthorizationSource,
    DecisionType,
    ExecutionType,
    PermissionMode,
    PermissionPolicy,
    build_permission_request,
    execution_context_scope,
    make_execution_context,
    pending_execution_permit_matches,
    set_default_permission_policy,
)
from osia.autonomous import authorization, state
from osia.mcp_client import registry as mcp_registry
from osia.permission_outcome import PermissionOutcome, PermissionResult
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    SecureStorageBackend,
    UnavailableSecureStorageBackend,
)
from osia.tool_contracts import (
    ConfirmationPolicy,
    SideEffectClass,
    effective_contract,
    get_tool_spec,
    register_dynamic_tool,
    unregister_dynamic_tools,
)


class _SyntheticProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "wor263_synthetic", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"W263" + bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"W263"):
            raise ValueError("invalid_protected_blob")
        return bytes(value ^ 0xA5 for value in protected[4:])


@pytest.fixture
def permission_policy(tmp_path):
    policy = PermissionPolicy(PermissionStore(
        tmp_path / "permissions.dat",
        backend=UnavailableSecureStorageBackend("wor263_unavailable"),
    ))
    set_default_permission_policy(policy)
    action_policy.clear_pending_execution_permit()
    try:
        yield policy
    finally:
        action_policy.clear_pending_execution_permit()
        mcp_registry.set_confirmation_provider(None)
        set_default_permission_policy(None)


def _context(
    policy: PermissionPolicy,
    execution_type: ExecutionType,
    operation_id: str,
):
    autonomous = execution_type is ExecutionType.AUTONOMOUS
    return make_execution_context(
        execution_type=execution_type,
        interactive=not autonomous,
        owner_ref="owner:wor263",
        machine_ref="machine:wor263",
        run_id="run:wor263" if autonomous else "",
        plan_revision="1" if autonomous else "",
        task_revision="0" if autonomous else "",
        execution_id="execution:" + operation_id.replace(":", "-"),
        operation_id=operation_id,
        store=policy.store,
    )


def _request(
    policy: PermissionPolicy,
    tool_name: str,
    arguments: dict,
    *,
    execution_type: ExecutionType,
    operation_id: str,
):
    return build_permission_request(
        tool_name,
        arguments,
        execution_context=_context(policy, execution_type, operation_id),
        policy=policy,
    )


def _read_only_plan(policy: PermissionPolicy) -> dict:
    st = state.new_task_state(
        "Inspect the current desktop and report visible applications.",
        "fixture",
        "fixture-model",
        [{"name": "Observe", "tasks": [{
            "id": "observe-desktop",
            "objective": "Inspect the current desktop and report visible applications.",
            "success_criterion": "Visible applications are reported without mutation.",
        }]}],
        run_id="run:wor263",
    )
    return authorization.prepare_planned_state(
        st, origin_ref="desktop", policy=policy,
    )


def _runtime_context(st: dict, policy: PermissionPolicy):
    return make_execution_context(
        execution_type=ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id=st["run_id"],
        plan_revision=str(st["plan_revision"]),
        task_revision=str(st["authorization_revision"]),
        store=policy.store,
    )


def _blocked_unknown_mutation(
    st: dict,
    policy: PermissionPolicy,
    *,
    tool_name: str = "open_application",
    arguments: dict | None = None,
) -> dict:
    st["run_state"] = "running"
    call_arguments = (
        {"name": "Calculator"} if arguments is None else dict(arguments)
    )
    with authorization.run_authorization_scope(
        st, save=lambda _value: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, policy)):
        request = build_permission_request(
            tool_name, call_arguments, policy=policy,
        )
    record = {
        "operation_id": request.operation_id,
        "task_id": "task:observe-desktop",
        "phase_id": "phase:0",
        "requirement_id": "",
        "plan_revision": st["plan_revision"],
        "tool_identity": request.tool_identity,
        "normalized_call_hash": request.normalized_call_hash,
        "autonomous_equivalence_hash": request.autonomous_equivalence_hash,
        "private_scope_hash": request.scope_hash,
        "grant_ref": "",
        "authorization_source": "none",
        "effect_class": request.effect_class,
        "policy_epoch": request.execution_context.policy_epoch,
        "status": "outcome_unknown",
        "dispatch_attempt_count": 1,
        "started_at": "synthetic",
        "completed_at": "synthetic",
        "execution_outcome": "outcome_unknown",
        "safe_reason_code": "outcome_unknown",
    }
    state.upsert_operation_record(st, record)
    state.add_ambiguity_barrier(st, record)
    st["run_state"] = "blocked_outcome_unknown"
    st["authorization_status"] = "outcome_unknown"
    return request


def test_normal_static_and_effective_read_only_is_structurally_allowed(
    permission_policy,
):
    spec = get_tool_spec("inspect_desktop_surfaces")
    effective = effective_contract(spec, {"target": ""})
    request = _request(
        permission_policy,
        "inspect_desktop_surfaces",
        {"target": ""},
        execution_type=ExecutionType.CHAT,
        operation_id="operation:normal-read",
    )

    decision = permission_policy.evaluate(request)

    assert spec.side_effect is SideEffectClass.READ_ONLY
    assert effective.side_effect is SideEffectClass.READ_ONLY
    assert decision.decision_type is DecisionType.ALLOW
    assert decision.authorization_source is AuthorizationSource.NONE
    assert permission_policy.mint_permit(request, decision) is None


def test_read_only_autonomous_plan_starts_without_allow_request(
    permission_policy, tmp_path,
):
    st = _read_only_plan(permission_policy)

    assert st["run_state"] == "ready"
    assert st["authorization_status"] == "authorized"
    assert st["authorization_source"] == "none"
    assert st["authorization_request"] is None
    assert st["grant_refs"] == []

    st["run_state"] = "running"
    authorization.revalidate_loaded_authorization(
        st, origin_ref="desktop", policy=permission_policy,
    )
    assert st["run_state"] == "ready"
    assert st["authorization_status"] == "authorized"
    assert st["authorization_request"] is None
    assert state.execution_state_is_valid(st) is True
    assert state.resume_state_is_valid(st) is True

    # Plan review must not use mutation-looking words from observational prose
    # or historical completed phases as execution authority signals.
    for index, objective in enumerate((
        "Report which applications are open.",
        "Inspect the download status and report it without changing anything.",
        "List the open browser tabs and report them without navigating.",
        "Search Gmail and list matching messages without sending anything.",
    )):
        observation = state.new_task_state(
            objective,
            "fixture",
            "fixture-model",
            [{"name": "Previously open Calculator", "tasks": [{
                "id": f"observe-{index}",
                "objective": objective,
                "success_criterion": "Observation is reported.",
            }]}],
            run_id=f"run:wor263:observation:{index}",
        )
        authorization.prepare_planned_state(
            observation, origin_ref="desktop", policy=permission_policy,
        )
        assert observation["run_state"] == "ready"
        assert observation["authorization_request"] is None

    # A concrete mutation remains a structured capability proposal even when
    # its path happens to contain an observation heuristic substring.
    mutation_path = tmp_path / "test_expired_and_duplicate_payload.txt"
    mutation = state.new_task_state(
        f"Write file {mutation_path}",
        "fixture",
        "fixture-model",
        [{"name": "Write", "tasks": [{
            "objective": f"Write file {mutation_path}",
            "success_criterion": "The file exists.",
        }]}],
        run_id="run:wor263:mutation-plan",
    )
    authorization.prepare_planned_state(
        mutation, origin_ref="desktop", policy=permission_policy,
    )
    assert mutation["run_state"] == "authorization_pending"
    assert mutation["authorization_request"] is not None


def test_autonomous_read_only_gate_allows_without_grant_or_permit(permission_policy):
    st = _read_only_plan(permission_policy)
    st["run_state"] = "running"
    arguments = {"target": ""}

    with authorization.run_authorization_scope(
        st, save=lambda _value: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, permission_policy)):
        blocked = action_policy.autonomous_permission_gate(
            "inspect_desktop_surfaces", arguments,
        )
        pending = pending_execution_permit_matches(
            "inspect_desktop_surfaces", arguments,
        )

    assert blocked is None
    assert pending is False
    observer = next(
        record for record in st["operation_records"]
        if record["tool_identity"] == "inspect_desktop_surfaces"
    )
    assert observer["status"] == "authorized"
    assert observer["authorization_source"] == "none"
    assert observer["safe_reason_code"] == "structurally_safe"

    for run_state, authorization_status in (
        ("authorization_pending", "pending"),
        ("suspended_authorization_revoked", "revoked"),
    ):
        gmail_state = _read_only_plan(permission_policy)
        gmail_state["run_state"] = run_state
        gmail_state["authorization_status"] = authorization_status
        if authorization_status == "revoked":
            gmail_state["revocation"] = {
                "revoked": True,
                "reason_code": "run_authorization_revoked",
            }
        with authorization.run_authorization_scope(
            gmail_state, save=lambda _value: None, origin_ref="desktop",
        ), execution_context_scope(_runtime_context(gmail_state, permission_policy)):
            gmail_blocked = action_policy.autonomous_permission_gate(
                "gmail_search", {"query": "fixture", "max_results": 1},
            )
        assert gmail_blocked is not None
        assert (
            "authorization_pending" in gmail_blocked
            if authorization_status == "pending"
            else "authorization_revoked" in gmail_blocked
        )


def test_outcome_unknown_allows_observers_own_read_only_contract(permission_policy):
    st = _read_only_plan(permission_policy)
    _blocked_unknown_mutation(st, permission_policy)
    arguments = {"target": "calculator"}

    with authorization.run_authorization_scope(
        st, save=lambda _value: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, permission_policy)):
        blocked = action_policy.autonomous_permission_gate(
            "inspect_desktop_surfaces", arguments,
        )
        pending = pending_execution_permit_matches(
            "inspect_desktop_surfaces", arguments,
        )

    assert blocked is None
    assert pending is False
    assert st["run_state"] == "blocked_outcome_unknown"
    assert st["authorization_status"] == "outcome_unknown"


def test_original_outcome_unknown_mutation_remains_replay_blocked(permission_policy):
    st = _read_only_plan(permission_policy)
    original = _blocked_unknown_mutation(st, permission_policy)

    with authorization.run_authorization_scope(
        st, save=lambda _value: None, origin_ref="desktop",
    ), execution_context_scope(_runtime_context(st, permission_policy)):
        blocked = action_policy.autonomous_permission_gate(
            "open_application", {"name": "Calculator"},
        )
        pending = pending_execution_permit_matches(
            "open_application", {"name": "Calculator"},
        )

    assert "duplicate_outcome_unknown" in blocked
    assert pending is False
    assert state.operation_record(st, original.operation_id)["status"] == (
        "outcome_unknown"
    )

    dynamic_name = "reclassified_probe_wor263"
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    register_dynamic_tool(
        name=dynamic_name,
        dispatcher=lambda: "mutated",
        capability="Synthetic changing capability",
        input_schema=schema,
        connector_id="wor263-reclassified",
        read_only=False,
        semantics_known=True,
    )
    changed = _read_only_plan(permission_policy)
    dynamic_original = _blocked_unknown_mutation(
        changed,
        permission_policy,
        tool_name=dynamic_name,
        arguments={},
    )
    unregister_dynamic_tools([dynamic_name])
    register_dynamic_tool(
        name=dynamic_name,
        dispatcher=lambda: "observed",
        capability="Synthetic changing capability",
        input_schema=schema,
        connector_id="wor263-reclassified",
        read_only=True,
        semantics_known=True,
    )
    try:
        with authorization.run_authorization_scope(
            changed, save=lambda _value: None, origin_ref="desktop",
        ), execution_context_scope(_runtime_context(changed, permission_policy)):
            dynamic_blocked = action_policy.autonomous_permission_gate(
                dynamic_name, {},
            )
    finally:
        unregister_dynamic_tools([dynamic_name])

    assert "duplicate_outcome_unknown" in dynamic_blocked
    assert state.operation_record(
        changed, dynamic_original.operation_id,
    )["effect_class"] == SideEffectClass.EXTERNAL_MUTATION.value

    class _Manager:
        def __init__(self):
            self.calls = []

        def call_tool(self, server_id, tool_name, arguments, *, timeout):
            self.calls.append((server_id, tool_name, arguments, timeout))
            return "MUTATION_REPLAY_DISPATCHED"

    manager = _Manager()
    server_id = "wor263_reclassified"
    descriptor = {
        "name": "changing_probe",
        "description": "Synthetic changing connector capability",
        "inputSchema": schema,
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    }
    try:
        mcp_name = mcp_registry.register_server_tools(
            manager, server_id, [descriptor],
        )[0]
        mcp_state = _read_only_plan(permission_policy)
        _blocked_unknown_mutation(
            mcp_state,
            permission_policy,
            tool_name=mcp_name,
            arguments={},
        )
        mcp_registry.unregister_server_tools(server_id)
        descriptor["annotations"] = {
            "readOnlyHint": True,
            "destructiveHint": False,
        }
        mcp_registry.register_server_tools(manager, server_id, [descriptor])
        from osia.agent import TOOL_MAP

        with authorization.run_authorization_scope(
            mcp_state, save=lambda _value: None, origin_ref="desktop",
        ), execution_context_scope(_runtime_context(mcp_state, permission_policy)):
            connector_result = TOOL_MAP[mcp_name]()
    finally:
        mcp_registry.unregister_server_tools(server_id)

    assert "duplicate_outcome_unknown" in connector_result
    assert manager.calls == []


@pytest.mark.parametrize(
    "runtime_effect",
    [
        pytest.param(SideEffectClass.REVERSIBLE_LOCAL, id="effective-mutating"),
        pytest.param(SideEffectClass.UNKNOWN_HIGH_RISK, id="effective-unknown"),
    ],
)
def test_static_read_only_with_unsafe_effective_contract_fails_closed(
    permission_policy, monkeypatch, runtime_effect,
):
    from osia import tool_contracts

    spec = get_tool_spec("inspect_desktop_surfaces")
    original = effective_contract(spec, {"target": ""})
    monkeypatch.setattr(
        tool_contracts,
        "effective_contract",
        lambda *_args, **_kwargs: replace(
            original,
            side_effect=runtime_effect,
            confirmation=ConfirmationPolicy.CONFIRMATION_REQUIRED,
        ),
    )
    request = _request(
        permission_policy,
        "inspect_desktop_surfaces",
        {"target": ""},
        execution_type=ExecutionType.AUTONOMOUS,
        operation_id=f"operation:{runtime_effect.value}",
    )

    decision = permission_policy.evaluate(request)

    assert request.tool_spec.side_effect is SideEffectClass.READ_ONLY
    assert request.effective_contract.side_effect is runtime_effect
    assert decision.decision_type is (
        DecisionType.DENY
        if runtime_effect is SideEffectClass.UNKNOWN_HIGH_RISK
        else DecisionType.SUSPEND_SCOPE_EXTENSION
    )
    assert decision.authorization_source is AuthorizationSource.NONE


def test_standard_sensitive_mutation_still_requires_canonical_confirmation(
    permission_policy, tmp_path,
):
    request = _request(
        permission_policy,
        "delete_file",
        {"path": str(tmp_path / "synthetic.txt")},
        execution_type=ExecutionType.CHAT,
        operation_id="operation:standard-delete",
    )

    decision = permission_policy.evaluate(request)

    assert decision.decision_type is DecisionType.REQUIRE_CONFIRMATION
    assert decision.reason_code == "ask_sensitive_action"
    assert permission_policy.mint_permit(request, decision) is None


def test_explicit_user_denial_remains_zero_dispatch(permission_policy):
    dispatches = []
    mcp_registry.set_confirmation_provider(
        lambda *_args: PermissionResult(
            PermissionOutcome.USER_DENIED,
            "permission-request-wor263",
        ),
    )

    blocked = action_policy.guard_tool_call(
        "Run the synthetic command.",
        "run_command",
        {"command": "Write-Output WOR263"},
    )
    if blocked is None:
        dispatches.append("unexpected")

    assert "permission_outcome=user_denied" in blocked
    assert dispatches == []


def test_unavailable_permission_presentation_is_not_user_denial(permission_policy):
    mcp_registry.set_confirmation_provider(
        lambda *_args: PermissionResult(PermissionOutcome.PRESENTATION_FAILED),
    )

    blocked = action_policy.guard_tool_call(
        "Run the synthetic command.",
        "run_command",
        {"command": "Write-Output WOR263"},
    )

    assert "permission_outcome=presentation_failed" in blocked
    assert "user_denied" not in blocked
    assert "user declined" not in blocked


def test_full_access_keeps_mutation_authority_but_read_only_needs_no_permit(
    tmp_path, monkeypatch,
):
    store = PermissionStore(
        tmp_path / "full-access.dat",
        backend=_SyntheticProtectedBackend(),
    )
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    policy = PermissionPolicy(store)
    read = _request(
        policy,
        "get_system_info",
        {},
        execution_type=ExecutionType.AUTONOMOUS,
        operation_id="operation:full-read",
    )
    mutation = _request(
        policy,
        "write_file",
        {"path": str(tmp_path / "full.txt"), "content": "synthetic"},
        execution_type=ExecutionType.AUTONOMOUS,
        operation_id="operation:full-mutation",
    )

    read_decision = policy.evaluate(read)
    mutation_decision = policy.evaluate(mutation)

    assert read_decision.decision_type is DecisionType.ALLOW
    assert read_decision.authorization_source is AuthorizationSource.NONE
    assert policy.mint_permit(read, read_decision) is None
    assert mutation_decision.decision_type is DecisionType.ALLOW
    assert mutation_decision.authorization_source is AuthorizationSource.FULL_ACCESS
    assert policy.mint_permit(mutation, mutation_decision) is not None

    from osia import tool_contracts

    spec = get_tool_spec("inspect_desktop_surfaces")
    original = effective_contract(spec, {"target": ""})
    monkeypatch.setattr(
        tool_contracts,
        "effective_contract",
        lambda *_args, **_kwargs: replace(
            original,
            side_effect=SideEffectClass.UNKNOWN_HIGH_RISK,
            confirmation=ConfirmationPolicy.CONFIRMATION_REQUIRED,
        ),
    )
    unknown = _request(
        policy,
        "inspect_desktop_surfaces",
        {"target": ""},
        execution_type=ExecutionType.AUTONOMOUS,
        operation_id="operation:full-unknown",
    )
    unknown_decision = policy.evaluate(unknown)
    assert unknown_decision.decision_type is DecisionType.DENY
    assert unknown_decision.reason_code == "unknown_effect_classification"
    assert policy.mint_permit(unknown, unknown_decision) is None


def test_generic_dynamic_non_desktop_read_only_tool_uses_same_rule(
    permission_policy,
):
    tool_name = "generic_read_probe_wor263"
    register_dynamic_tool(
        name=tool_name,
        dispatcher=lambda: "observed",
        capability="Synthetic generic read",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        connector_id="wor263",
        read_only=True,
        semantics_known=True,
    )
    st = _read_only_plan(permission_policy)
    st["run_state"] = "running"
    try:
        with authorization.run_authorization_scope(
            st, save=lambda _value: None, origin_ref="desktop",
        ), execution_context_scope(_runtime_context(st, permission_policy)):
            blocked = action_policy.autonomous_permission_gate(tool_name, {})
            pending = pending_execution_permit_matches(tool_name, {})
    finally:
        unregister_dynamic_tools([tool_name])

    assert blocked is None
    assert pending is False
    record = next(
        item for item in st["operation_records"]
        if item["tool_identity"] == tool_name
    )
    assert record["authorization_source"] == "none"
    assert record["safe_reason_code"] == "structurally_safe"
