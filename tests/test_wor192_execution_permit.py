from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from osia import action_policy
from osia.action_policy import (
    DecisionType,
    ExecutionPermitLedger,
    PermitError,
    PermissionPolicy,
    UsePolicy,
    build_permission_request,
    create_grant,
    execution_context_scope,
    make_execution_context,
    trusted_deletion_classification,
)
from osia.mcp_client import registry
from osia.permission_store import (
    PermissionStore,
    UnavailableSecureStorageBackend,
    WindowsDPAPISecureStorageBackend,
)


def _policy(tmp_path: Path, *, clock=None):
    store = PermissionStore(
        tmp_path / "session.dat", backend=UnavailableSecureStorageBackend(),
    )
    ledger = ExecutionPermitLedger(clock=clock) if clock else ExecutionPermitLedger()
    return store, PermissionPolicy(store, permit_ledger=ledger, clock=clock)


def _request(policy, store, operation="one", path=None):
    context = make_execution_context(
        owner_ref="owner:fixture",
        machine_ref="machine:fixture",
        execution_id=f"execution:{operation}",
        operation_id=f"operation:{operation}",
        store=store,
    )
    return build_permission_request(
        "delete_file", {"path": str(path or "fixture.txt")},
        execution_context=context, policy=policy,
    )


def _confirmed_permit(policy, request, **kwargs):
    decision = policy.evaluate(request)
    assert decision.decision_type == DecisionType.REQUIRE_CONFIRMATION
    permit = policy.mint_permit(request, decision, confirmed=True, **kwargs)
    assert permit is not None
    return permit


def test_exact_permit_is_accepted_atomically_once(tmp_path):
    store, policy = _policy(tmp_path)
    request = _request(policy, store, path=tmp_path / "fixture.txt")
    permit = _confirmed_permit(policy, request)
    successes = []

    def consume():
        try:
            policy.consume_permit(permit, request)
            successes.append(True)
        except PermitError:
            successes.append(False)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(successes) == [False, True]


def test_single_grant_use_is_claimed_atomically_across_operations(tmp_path):
    store, policy = _policy(tmp_path)
    path = tmp_path / "fixture.txt"
    grant_request = _request(policy, store, operation="grant", path=path)
    store.add_grant(
        create_grant(grant_request, use_policy=UsePolicy.SINGLE)
    )
    requests = [
        _request(policy, store, operation=f"claim-{index}", path=path)
        for index in range(2)
    ]
    decisions = [policy.evaluate(request) for request in requests]
    assert all(decision.decision_type == DecisionType.ALLOW for decision in decisions)
    permits = []

    def mint(index):
        permits.append(policy.mint_permit(requests[index], decisions[index]))

    threads = [threading.Thread(target=mint, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(permit is not None for permit in permits) == 1


def test_expired_permit_is_rejected_and_burned(tmp_path):
    now = [100.0]
    store, policy = _policy(tmp_path, clock=lambda: now[0])
    request = _request(policy, store, path=tmp_path / "fixture.txt")
    permit = _confirmed_permit(policy, request, ttl_seconds=1.0)
    now[0] = 102.0
    with pytest.raises(PermitError, match="permit_expired"):
        policy.consume_permit(permit, request)
    with pytest.raises(PermitError, match="permit_missing_or_used"):
        policy.consume_permit(permit, request)


@pytest.mark.parametrize("change", ["tool", "arguments", "scope"])
def test_permit_cannot_move_to_another_tool_call_or_scope(tmp_path, change):
    store, policy = _policy(tmp_path)
    request = _request(policy, store, path=tmp_path / "one.txt")
    permit = _confirmed_permit(policy, request)
    context = request.execution_context
    if change == "tool":
        altered = build_permission_request(
            "delete_directory",
            {"path": str(tmp_path / "one.txt"), "recursive": False},
            execution_context=context,
            policy=policy,
        )
    elif change == "scope":
        altered = build_permission_request(
            "delete_file", {"path": str(tmp_path / "two.txt")},
            execution_context=context, policy=policy,
        )
    else:
        altered = replace(request, normalized_call_hash="f" * 64)
    with pytest.raises(PermitError, match="permit_scope_mismatch"):
        policy.consume_permit(permit, altered)
    with pytest.raises(PermitError, match="permit_missing_or_used"):
        policy.consume_permit(permit, request)


def test_epoch_revocation_invalidates_minted_permit_before_dispatch(tmp_path):
    store, policy = _policy(tmp_path)
    request = _request(policy, store, path=tmp_path / "fixture.txt")
    permit = _confirmed_permit(policy, request)
    result = store.reset_to_ask()
    assert result.policy_epoch > request.execution_context.policy_epoch
    with pytest.raises(PermitError, match="permit_missing_or_used"):
        policy.consume_permit(permit, request)


def test_denied_system_destruction_never_mints_permit(tmp_path):
    store, policy = _policy(tmp_path)
    ordinary = _request(policy, store, path=tmp_path / "fixture.txt")
    denied = build_permission_request(
        ordinary.tool_identity,
        {"path": str(tmp_path / "fixture.txt")},
        execution_context=ordinary.execution_context,
        deletion_classification=trusted_deletion_classification(
            system_destruction=True,
        ),
        policy=policy,
    )
    decision = policy.evaluate(denied)
    assert decision.decision_type == DecisionType.BLOCK_SYSTEM_DESTRUCTION
    assert policy.mint_permit(denied, decision, confirmed=True, reinforced=True) is None


def test_outcome_unknown_operation_cannot_receive_second_permit(tmp_path):
    store, policy = _policy(tmp_path)
    request = _request(policy, store, path=tmp_path / "fixture.txt")
    permit = _confirmed_permit(policy, request)
    assert policy.consume_permit(permit, request).used is True
    policy.permit_ledger.mark_outcome_unknown(request.operation_id)
    second_decision = policy.evaluate(request)
    assert second_decision.decision_type == DecisionType.DENY
    assert second_decision.reason_code == "duplicate_or_ambiguous_operation"


def test_confirmation_approval_dispatches_once_and_replay_is_rejected(
    monkeypatch, tmp_path,
):
    from osia import agent as agent_module

    store, policy = _policy(tmp_path)
    action_policy.set_default_permission_policy(policy)
    registry.set_confirmation_provider(lambda *_args: True)
    target = tmp_path / "controlled.txt"
    target.write_text("synthetic", encoding="utf-8")
    try:
        assert action_policy.guard_tool_call(
            "Delete the isolated fixture.",
            "delete_file",
            {"path": str(target)},
        ) is None
        record = agent_module._execute_tool_with_policy(
            "delete_file",
            {"path": str(target)},
            enforce_execution_permit=True,
            allow_retry=False,
            lang="en",
        )
        assert record.dispatched is True
        assert target.exists() is False

        replay = agent_module._execute_tool_with_policy(
            "delete_file",
            {"path": str(target)},
            enforce_execution_permit=True,
            allow_retry=False,
            lang="en",
        )
        assert replay.dispatched is False
        assert replay.outcome.value == "rejected_before_execution"
    finally:
        registry.set_confirmation_provider(None)
        action_policy.set_default_permission_policy(None)


@pytest.mark.parametrize("provider_result", [False, None])
def test_denial_timeout_or_missing_approver_has_no_permit_or_dispatch(
    provider_result, tmp_path,
):
    store, policy = _policy(tmp_path)
    action_policy.set_default_permission_policy(policy)
    registry.set_confirmation_provider(
        None if provider_result is None else lambda *_args: provider_result
    )
    try:
        blocked = action_policy.guard_tool_call(
            "Delete the isolated fixture.",
            "delete_file",
            {"path": str(tmp_path / "fixture.txt")},
        )
        assert blocked.startswith("ERROR:")
        assert action_policy.consume_pending_execution_permit(
            "delete_file", {"path": str(tmp_path / "fixture.txt")},
        ) is False
    finally:
        registry.set_confirmation_provider(None)
        action_policy.set_default_permission_policy(None)


def test_dispatch_exception_does_not_restore_consumed_permit(monkeypatch, tmp_path):
    from osia import agent as agent_module

    store, policy = _policy(tmp_path)
    action_policy.set_default_permission_policy(policy)
    registry.set_confirmation_provider(lambda *_args: True)
    calls = []

    def fail(path):
        calls.append(path)
        raise RuntimeError("WOR192_PRIVATE_EXCEPTION_SENTINEL")

    monkeypatch.setitem(agent_module.TOOL_MAP, "delete_file", fail)
    arguments = {"path": str(tmp_path / "fixture.txt")}
    try:
        assert action_policy.guard_tool_call(
            "Delete the isolated fixture.", "delete_file", arguments,
        ) is None
        failed = agent_module._execute_tool_with_policy(
            "delete_file", arguments,
            enforce_execution_permit=True, allow_retry=False, lang="en",
        )
        assert failed.dispatched is True
        assert failed.outcome.value == "outcome_unknown"
        assert "WOR192_PRIVATE_EXCEPTION_SENTINEL" not in str(failed.result)

        replay = agent_module._execute_tool_with_policy(
            "delete_file", arguments,
            enforce_execution_permit=True, allow_retry=False, lang="en",
        )
        assert replay.dispatched is False
        assert len(calls) == 1
    finally:
        registry.set_confirmation_provider(None)
        action_policy.set_default_permission_policy(None)


def test_exact_session_grant_dispatches_without_prompt_and_wrong_scope_does_not(
    tmp_path,
):
    from osia import agent as agent_module

    store, policy = _policy(tmp_path)
    target = tmp_path / "granted.txt"
    target.write_text("synthetic", encoding="utf-8")
    context = make_execution_context(
        owner_ref="owner:fixture",
        machine_ref="machine:fixture",
        execution_id="execution:grant",
        operation_id="operation:grant",
        store=store,
    )
    request = build_permission_request(
        "delete_file", {"path": str(target)},
        execution_context=context, policy=policy,
    )
    store.add_grant(create_grant(request))
    action_policy.set_default_permission_policy(policy)
    registry.set_confirmation_provider(None)
    try:
        with execution_context_scope(context):
            assert action_policy.guard_tool_call(
                "Delete the granted isolated fixture.",
                "delete_file",
                {"path": str(target)},
            ) is None
            record = agent_module._execute_tool_with_policy(
                "delete_file", {"path": str(target)},
                enforce_execution_permit=True, allow_retry=False, lang="en",
            )
        assert record.dispatched is True
        assert target.exists() is False

        wrong = tmp_path / "not-granted.txt"
        wrong.write_text("synthetic", encoding="utf-8")
        wrong_context = replace(
            context,
            execution_id="execution:wrong",
            operation_id="operation:wrong",
        )
        with execution_context_scope(wrong_context):
            blocked = action_policy.guard_tool_call(
                "Delete another isolated fixture.",
                "delete_file",
                {"path": str(wrong)},
            )
        assert blocked.startswith("ERROR:")
        assert wrong.exists() is True
    finally:
        registry.set_confirmation_provider(None)
        action_policy.set_default_permission_policy(None)


def test_full_access_shell_and_mcp_doubles_dispatch_without_prompt(
    monkeypatch, tmp_path,
):
    from osia import agent as agent_module
    from osia.action_policy import PermissionMode

    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("Windows DPAPI is required for protected Full Access")
    store = PermissionStore(tmp_path / "full.dat", backend=backend)
    store.set_permission_mode(PermissionMode.FULL_ACCESS)
    policy = PermissionPolicy(store)
    action_policy.set_default_permission_policy(policy)
    registry.set_confirmation_provider(None)
    shell_calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "run_command",
        lambda command, timeout=None: shell_calls.append(command) or "synthetic ok",
    )

    class Manager:
        calls = []

        def call_tool(self, server_id, tool_name, arguments, *, timeout):
            self.calls.append((server_id, tool_name, arguments, timeout))
            return "synthetic ok"

    manager = Manager()
    wrapper = registry._make_wrapper(
        manager,
        "fixture-server",
        "write_item",
        is_write=True,
        input_schema={
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "required": ["item"],
            "additionalProperties": False,
        },
    )
    try:
        assert action_policy.guard_tool_call(
            "Run the isolated synthetic command.",
            "run_command",
            {"command": "echo synthetic"},
        ) is None
        shell = agent_module._execute_tool_with_policy(
            "run_command", {"command": "echo synthetic"},
            enforce_execution_permit=True, allow_retry=False, lang="en",
        )
        assert shell.dispatched is True
        assert shell_calls == ["echo synthetic"]

        local_target = tmp_path / "full-access-local.txt"
        assert action_policy.guard_tool_call(
            "Create neutral content for the synthetic fixture.",
            "write_file",
            {"path": str(local_target), "content": "synthetic"},
        ) is None
        local = agent_module._execute_tool_with_policy(
            "write_file",
            {"path": str(local_target), "content": "synthetic"},
            # Ordinary reversible local writes do not require a Chat permit.
            enforce_execution_permit=False,
            allow_retry=False,
            lang="en",
        )
        assert local.dispatched is True
        assert local_target.read_text(encoding="utf-8") == "synthetic"

        assert wrapper(item="fixture") == "synthetic ok"
        assert len(manager.calls) == 1
    finally:
        registry.set_confirmation_provider(None)
        action_policy.set_default_permission_policy(None)
