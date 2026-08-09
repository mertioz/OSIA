"""WOR-250 capability-freedom, broker-friction, and replay regressions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from osia import action_policy
from osia.action_policy import (
    PermissionMode,
    PermissionPolicy,
    build_permission_request,
    create_grant,
    execution_context_scope,
    make_execution_context,
)
from osia.capability_federation import federated_capabilities
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    _TurnAttempt,
    _canonical_native_tools,
    _exact_canonical_native_call,
)
from osia.chatgpt_account.provider import _CONTAINMENT_INSTRUCTIONS
from osia.execution_strategy import build_execution_strategy
from osia.mcp_client import registry as mcp_registry
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    SecureStorageBackend,
)
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
from osia.tool_contracts import (
    effective_tool_surface_scope,
    register_dynamic_tool,
    unregister_dynamic_tools,
)


class _ProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "wor250_synthetic", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"W250" + bytes(value ^ 0x5A for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"W250"):
            raise ValueError("invalid_protected_blob")
        return bytes(value ^ 0x5A for value in protected[4:])


@pytest.fixture(autouse=True)
def _reset_permission_runtime():
    action_policy.clear_pending_execution_permit()
    action_policy.set_default_permission_policy(None)
    previous_provider = mcp_registry.get_confirmation_provider()
    mcp_registry.set_confirmation_provider(None)
    try:
        yield
    finally:
        action_policy.clear_pending_execution_permit()
        action_policy.set_default_permission_policy(None)
        mcp_registry.set_confirmation_provider(previous_provider)


def _policy(tmp_path: Path, name: str, *, full_access: bool = False):
    store = PermissionStore(
        tmp_path / name,
        backend=_ProtectedBackend(),
    )
    if full_access:
        store.set_permission_mode(PermissionMode.FULL_ACCESS)
    return PermissionPolicy(store)


def _identities(provider: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.source, item.identity)
        for item in federated_capabilities(provider)
        if item.available
    )


def _client(tmp_path: Path, name: str = "native"):
    client = AppServerClient(
        base_dir=tmp_path / f"{name}-runtime",
        tool_attempt_store=ToolAttemptStore(tmp_path / f"{name}-attempts"),
    )
    sent = []
    client._send = sent.append
    return client, sent


def _native_attempt(
    *, conversation_id: str, turn_id: str, user_request: str,
) -> _TurnAttempt:
    return _TurnAttempt(
        thread_id=f"thread:{conversation_id}",
        conversation_id=conversation_id,
        osia_turn_id=turn_id,
        final_response_message_id=f"assistant:{turn_id}",
        turn_id=f"provider:{turn_id}",
        model_id="gpt-5.6",
        user_request=user_request,
    )


def _approve_command(
    client: AppServerClient,
    attempt: _TurnAttempt,
    *, item_id: str, command: str, request_id: int,
) -> None:
    attempt.native_items[item_id] = {
        "id": item_id,
        "type": "commandExecution",
        "status": "inProgress",
        "command": command,
        "cwd": str(client.workspace_dir),
        "commandActions": [{"type": "unknown", "command": command}],
    }
    client._resolve_native_approval(attempt, {
        "method": "item/commandExecution/requestApproval",
        "request_id": request_id,
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": item_id,
            "command": command,
            "cwd": str(client.workspace_dir),
            "commandActions": [{"type": "unknown", "command": command}],
        },
    })


def _complete_command(
    client: AppServerClient,
    attempt: _TurnAttempt,
    *, item_id: str, command: str, status: str = "completed",
    exit_code: int | None = 0,
) -> None:
    item = {
        "id": item_id,
        "type": "commandExecution",
        "status": status,
        "command": command,
        "cwd": str(client.workspace_dir),
        "commandActions": [{"type": "unknown", "command": command}],
    }
    if exit_code is not None:
        item["exitCode"] = exit_code
    client._complete_native_item(attempt, item)


def test_permission_matrix_changes_friction_not_planner_capabilities(tmp_path):
    ask = _policy(tmp_path, "ask.dat")
    full = _policy(tmp_path, "full.dat", full_access=True)

    action_policy.set_default_permission_policy(ask)
    chat_ask = _identities("chatgpt_account")
    api_ask = _identities("deepseek")
    with effective_tool_surface_scope("autonomous"):
        autonomous_ask = _identities("chatgpt_account")
    with effective_tool_surface_scope("scheduled"):
        scheduled_ask = _identities("chatgpt_account")

    action_policy.set_default_permission_policy(full)
    assert _identities("chatgpt_account") == chat_ask
    assert _identities("deepseek") == api_ask
    with effective_tool_surface_scope("autonomous"):
        assert _identities("chatgpt_account") == autonomous_ask
    with effective_tool_surface_scope("scheduled"):
        assert _identities("chatgpt_account") == scheduled_ask

    assert len(chat_ask) == len(set(chat_ask))
    assert len(chat_ask) > len(api_ask)
    assert {"webSearch", "commandExecution", "fileChange"} <= {
        identity for source, identity in chat_ask if source.startswith("provider")
    }


def test_native_osia_and_mcp_are_voluntary_direct_peers():
    registered = register_dynamic_tool(
        name="mcp__wor250__read",
        dispatcher=lambda **_kwargs: "synthetic",
        capability="Synthetic MCP read",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        connector_id="wor250",
        read_only=True,
        semantics_known=True,
    )
    try:
        view = federated_capabilities("chatgpt_account")
        web = [item for item in view if item.equivalence_class == "web_search"]
        assert {(item.source, item.identity) for item in web} >= {
            ("provider_hosted", "webSearch"),
            ("osia", "search_web"),
        }
        assert any(
            item.source == "mcp" and item.identity == registered.name
            for item in view
        )
        assert "optional additions" in _CONTAINMENT_INSTRUCTIONS
        assert "choose the best valid path" in _CONTAINMENT_INSTRUCTIONS
        assert "never invoke or request Codex-native" not in _CONTAINMENT_INSTRUCTIONS

        native = _exact_canonical_native_call({
            "callId": "call:voluntary",
            "tool": "write_file",
            "arguments": {"path": "synthetic.txt", "content": "one"},
        })
        assert native == {
            "call_id": "call:voluntary",
            "tool": "write_file",
            "arguments": {"path": "synthetic.txt", "content": "one"},
        }
        assert "write_file" in [tool["name"] for tool in _canonical_native_tools()]
    finally:
        unregister_dynamic_tools((registered.name,))


def test_observable_native_read_command_needs_no_osia_rewrite(tmp_path):
    client, _sent = _client(tmp_path, "native-read")
    attempt = _native_attempt(
        conversation_id="conversation:native-read",
        turn_id="turn:native-read",
        user_request="Identify the current local user.",
    )
    completed = {
        "id": "item:native-read",
        "type": "commandExecution",
        "status": "completed",
        "command": "whoami",
        "cwd": str(client.workspace_dir),
        "commandActions": [{"type": "unknown", "command": "whoami"}],
        "exitCode": 0,
    }
    client._complete_native_item(attempt, completed)
    client._complete_native_item(attempt, completed)
    assert attempt.outcome_unknown is False
    assert attempt.native_action is False
    assert [
        (event.identity, event.phase, event.outcome)
        for event in attempt.capability_events
    ] == [("commandExecution", "completed", "success_verified")]


def test_standard_native_command_pauses_then_continues_exact_action(tmp_path):
    action_policy.set_default_permission_policy(_policy(tmp_path, "ask.dat"))
    prompts = []
    mcp_registry.set_confirmation_provider(
        lambda *args: prompts.append(args) or True,
    )
    client, sent = _client(tmp_path)
    attempt = _native_attempt(
        conversation_id="conversation:standard",
        turn_id="turn:standard",
        user_request="Run the isolated synthetic command.",
    )

    _approve_command(
        client, attempt,
        item_id="item:standard", command="echo wor250", request_id=1,
    )
    assert sent == [{"id": 1, "result": {"decision": "accept"}}]
    assert len(prompts) == 1
    dispatch = attempt.native_dispatches["item:standard"]
    assert dispatch.arguments == {
        "command": "echo wor250",
        "cwd": str(client.workspace_dir.resolve(strict=False)),
    }
    assert dispatch.started is True
    assert client._attempt_store().get_attempt(dispatch.tool_attempt_id)[
        "status"
    ] == ToolAttemptStatus.RUNNING.value

    _complete_command(
        client, attempt,
        item_id="item:standard", command="echo wor250",
    )
    receipt = client._attempt_store().get_attempt(dispatch.tool_attempt_id)
    assert receipt["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert receipt["terminal_outcome"] == "success_verified"
    assert receipt["final_response_message_id"] == "assistant:turn:standard"
    assert receipt["continuation_state"] == "not_claimed"


def test_full_access_same_native_command_has_no_redundant_prompt(tmp_path):
    action_policy.set_default_permission_policy(
        _policy(tmp_path, "full.dat", full_access=True),
    )
    prompts = []
    mcp_registry.set_confirmation_provider(
        lambda *args: prompts.append(args) or True,
    )
    client, sent = _client(tmp_path)
    attempt = _native_attempt(
        conversation_id="conversation:full",
        turn_id="turn:full",
        user_request="Run the isolated synthetic command.",
    )

    _approve_command(
        client, attempt,
        item_id="item:full", command="echo wor250", request_id=2,
    )
    assert sent == [{"id": 2, "result": {"decision": "accept"}}]
    assert prompts == []
    _complete_command(
        client, attempt, item_id="item:full", command="echo wor250",
    )


def test_native_completion_must_match_the_approved_lifecycle(tmp_path):
    action_policy.set_default_permission_policy(
        _policy(tmp_path, "lifecycle.dat", full_access=True),
    )
    client, _sent = _client(tmp_path, "lifecycle")
    attempt = _native_attempt(
        conversation_id="conversation:lifecycle",
        turn_id="turn:lifecycle",
        user_request="Run the isolated synthetic command.",
    )
    _approve_command(
        client,
        attempt,
        item_id="item:lifecycle",
        command="echo approved",
        request_id=21,
    )
    _complete_command(
        client,
        attempt,
        item_id="item:lifecycle",
        command="echo different",
    )
    dispatch = attempt.native_dispatches["item:lifecycle"]
    receipt = client._attempt_store().get_attempt(dispatch.tool_attempt_id)
    assert receipt["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert receipt["terminal_outcome"] == "outcome_unknown"
    assert attempt.outcome_unknown is True


def test_denial_does_not_remove_native_capability_from_future_planning(tmp_path):
    action_policy.set_default_permission_policy(_policy(tmp_path, "ask.dat"))
    client, sent = _client(tmp_path)
    before = _identities("chatgpt_account")
    attempt = _native_attempt(
        conversation_id="conversation:deny",
        turn_id="turn:deny",
        user_request="Run the isolated synthetic command.",
    )
    mcp_registry.set_confirmation_provider(lambda *_args: False)
    _approve_command(
        client, attempt,
        item_id="item:deny", command="echo denied", request_id=3,
    )
    assert sent[-1] == {"id": 3, "result": {"decision": "decline"}}
    assert attempt.native_dispatches == {}
    assert _identities("chatgpt_account") == before

    mcp_registry.set_confirmation_provider(lambda *_args: True)
    retry = _native_attempt(
        conversation_id="conversation:retry",
        turn_id="turn:retry",
        user_request="Run the isolated synthetic command.",
    )
    _approve_command(
        client, retry,
        item_id="item:retry", command="echo denied", request_id=4,
    )
    assert sent[-1] == {"id": 4, "result": {"decision": "accept"}}
    _complete_command(
        client, retry, item_id="item:retry", command="echo denied",
    )


def test_grant_covered_call_skips_prompt_and_uncovered_call_requests_it(tmp_path):
    policy = _policy(tmp_path, "grant.dat")
    action_policy.set_default_permission_policy(policy)
    context = make_execution_context(
        owner_ref="owner:wor250",
        machine_ref="machine:wor250",
        execution_id="execution:wor250",
        operation_id="operation:wor250",
        store=policy.store,
    )
    arguments = {"level": 25}
    request = build_permission_request(
        "set_volume",
        arguments,
        execution_context=context,
        force_sensitive=True,
        policy=policy,
    )
    policy.store.add_grant(create_grant(request))
    prompts = []
    mcp_registry.set_confirmation_provider(
        lambda *args: prompts.append(args) or True,
    )

    with execution_context_scope(context):
        covered = mcp_registry.confirm_sensitive_action(
            mcp_registry.ConfirmationDomain.NATIVE,
            "osia", "set_volume", "Change one local setting",
            call_arguments=arguments,
        )
        assert covered is None
        assert prompts == []
        assert action_policy.consume_pending_execution_permit(
            "set_volume", arguments,
        ) is True

    uncovered_context = make_execution_context(
        owner_ref="owner:wor250",
        machine_ref="machine:wor250",
        execution_id="execution:wor250-uncovered",
        operation_id="operation:wor250-uncovered",
        store=policy.store,
    )
    with execution_context_scope(uncovered_context):
        uncovered_arguments = {"level": 30}
        uncovered = mcp_registry.confirm_sensitive_action(
            mcp_registry.ConfirmationDomain.NATIVE,
            "osia", "set_volume", "Change one local setting",
            call_arguments=uncovered_arguments,
        )
        assert uncovered is None
        assert len(prompts) == 1
        assert action_policy.consume_pending_execution_permit(
            "set_volume", uncovered_arguments,
        ) is True


def test_native_file_and_osia_equivalent_execute_exactly_once(tmp_path):
    client, sent = _client(tmp_path, "file")
    attempt = _native_attempt(
        conversation_id="conversation:file",
        turn_id="turn:file",
        user_request="Create the isolated fixture.",
    )
    target = tmp_path / "fixture.txt"
    change = {
        "path": str(target),
        "diff": "+native content",
        "kind": {"type": "add"},
    }
    attempt.native_items["item:file"] = {
        "id": "item:file", "type": "fileChange", "status": "inProgress",
        "changes": [change],
    }
    client._resolve_native_approval(attempt, {
        "method": "item/fileChange/requestApproval",
        "request_id": 5,
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "item:file",
        },
    })
    assert sent[-1] == {"id": 5, "result": {"decision": "accept"}}
    client._complete_native_item(attempt, {
        "id": "item:file", "type": "fileChange", "status": "completed",
        "changes": [change],
    })

    duplicate_change = {
        **change,
        "diff": "+different representation of the same mutation",
    }
    attempt.native_items["item:file-duplicate"] = {
        "id": "item:file-duplicate",
        "type": "fileChange",
        "status": "inProgress",
        "changes": [duplicate_change],
    }
    client._resolve_native_approval(attempt, {
        "method": "item/fileChange/requestApproval",
        "request_id": 51,
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "item:file-duplicate",
        },
    })
    assert sent[-1] == {"id": 51, "result": {"decision": "decline"}}
    assert "item:file-duplicate" not in attempt.native_dispatches

    from osia import agent as agent_module

    osia_dispatches = []
    with patch.dict(
        agent_module.TOOL_MAP,
        {"write_file": lambda **kwargs: osia_dispatches.append(kwargs) or "ok"},
    ):
        record = agent_module._execute_tool_with_policy(
            "write_file",
            {"path": str(target), "content": "native content"},
            attempt_context={
                "enabled": True,
                "store": client._attempt_store(),
                "conversation_id": attempt.conversation_id,
                "turn_id": attempt.osia_turn_id,
                "request_id": "request:osia",
                "tool_call_id": "toolcall:osia",
                "provider_id": "chatgpt_account",
                "model_id": attempt.model_id,
            },
        )
    assert record.dispatched is False
    assert "durable receipt" in str(record.result)
    assert osia_dispatches == []


def test_outcome_unknown_freezes_every_effectful_equivalent_path(tmp_path):
    action_policy.set_default_permission_policy(_policy(tmp_path, "ask.dat"))
    mcp_registry.set_confirmation_provider(lambda *_args: True)
    client, sent = _client(tmp_path, "unknown")
    attempt = _native_attempt(
        conversation_id="conversation:unknown",
        turn_id="turn:unknown",
        user_request="Run the isolated synthetic command.",
    )
    _approve_command(
        client, attempt,
        item_id="item:unknown", command="echo maybe", request_id=6,
    )
    _complete_command(
        client, attempt,
        item_id="item:unknown", command="echo maybe", status="failed",
        exit_code=None,
    )
    assert attempt.outcome_unknown is True

    _approve_command(
        client,
        attempt,
        item_id="item:after-unknown",
        command="echo must-not-run",
        request_id=61,
    )
    assert sent[-1] == {"id": 61, "result": {"decision": "decline"}}
    assert "item:after-unknown" not in attempt.native_dispatches

    from osia import agent as agent_module

    osia_dispatches = []
    with patch.dict(
        agent_module.TOOL_MAP,
        {"write_file": lambda **kwargs: osia_dispatches.append(kwargs) or "ok"},
    ):
        record = agent_module._execute_tool_with_policy(
            "write_file",
            {"path": str(tmp_path / "different.txt"), "content": "other"},
            attempt_context={
                "enabled": True,
                "store": client._attempt_store(),
                "conversation_id": attempt.conversation_id,
                "turn_id": attempt.osia_turn_id,
                "request_id": "request:unknown",
                "tool_call_id": "toolcall:unknown",
                "provider_id": "chatgpt_account",
                "model_id": attempt.model_id,
            },
        )
    assert record.dispatched is False
    assert osia_dispatches == []


def test_revoking_full_access_restores_prompt_without_removing_capabilities(tmp_path):
    policy = _policy(tmp_path, "revoke.dat", full_access=True)
    action_policy.set_default_permission_policy(policy)
    prompts = []
    mcp_registry.set_confirmation_provider(
        lambda *args: prompts.append(args) or True,
    )
    client, sent = _client(tmp_path, "revoke")
    before = _identities("chatgpt_account")

    full = _native_attempt(
        conversation_id="conversation:revoke-full",
        turn_id="turn:revoke-full",
        user_request="Run the isolated synthetic command.",
    )
    _approve_command(
        client, full,
        item_id="item:revoke-full", command="echo before", request_id=7,
    )
    _complete_command(
        client, full, item_id="item:revoke-full", command="echo before",
    )
    assert prompts == []
    assert sent[-1]["result"]["decision"] == "accept"

    policy.store.revoke_full_access()
    standard = _native_attempt(
        conversation_id="conversation:revoke-ask",
        turn_id="turn:revoke-ask",
        user_request="Run the isolated synthetic command.",
    )
    _approve_command(
        client, standard,
        item_id="item:revoke-ask", command="echo after", request_id=8,
    )
    assert len(prompts) == 1
    assert sent[-1]["result"]["decision"] == "accept"
    assert _identities("chatgpt_account") == before
    _complete_command(
        client, standard, item_id="item:revoke-ask", command="echo after",
    )


def test_wor248_strategy_and_wor249_prompt_compactness_remain_intact():
    instruction = build_execution_strategy(
        "Open Steam and go to my Library.",
        ("open_uri", "navigate_browser", "desktop_click"),
    ).model_instruction()
    expected_layers = (
        "0 exact direct provider/API/MCP/OSIA capability",
        "1 deep link or deterministic route",
        "2 DOM/Playwright",
        "3 native accessibility",
        "4 keyboard with observable focus",
        "5 local/zoomed vision",
        "6 global coordinates",
    )
    positions = [instruction.index(layer) for layer in expected_layers]
    assert positions == sorted(positions)
    assert len(_CONTAINMENT_INSTRUCTIONS) < 1_200
    assert _CONTAINMENT_INSTRUCTIONS.count("optional additions") == 1
