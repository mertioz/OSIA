"""WOR-250 capability-freedom, broker-friction, and replay regressions."""

from __future__ import annotations

from pathlib import Path
import pytest

from osia import action_policy
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
from osia.tool_attempts import ToolAttemptStore
from osia.tool_contracts import (
    effective_tool_surface_scope,
    register_dynamic_tool,
    unregister_dynamic_tools,
)


@pytest.fixture(autouse=True)
def _reset_confirmation_runtime():
    action_policy.clear_pending_execution_permit()
    previous_provider = mcp_registry.get_confirmation_provider()
    mcp_registry.set_confirmation_provider(None)
    try:
        yield
    finally:
        action_policy.clear_pending_execution_permit()
        mcp_registry.set_confirmation_provider(previous_provider)


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
    client._attempts[attempt.thread_id] = attempt
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
    client._attempts[attempt.thread_id] = attempt
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


def test_capability_identities_are_config_free_and_surface_scoped():
    chat_first = _identities("chatgpt_account")
    api_first = _identities("deepseek")
    with effective_tool_surface_scope("chat"):
        autonomous_first = _identities("chatgpt_account")
    with effective_tool_surface_scope("scheduled"):
        scheduled_first = _identities("chatgpt_account")

    # WOR-301: no permission configuration exists that could add or remove a
    # capability identity; repeated composition is stable and scope-driven.
    assert _identities("chatgpt_account") == chat_first
    assert _identities("deepseek") == api_first
    with effective_tool_surface_scope("chat"):
        assert _identities("chatgpt_account") == autonomous_first
    with effective_tool_surface_scope("scheduled"):
        assert _identities("chatgpt_account") == scheduled_first

    assert len(chat_first) == len(set(chat_first))
    assert len(chat_first) > len(api_first)
    assert {"webSearch", "commandExecution", "fileChange"} <= {
        identity for source, identity in chat_first if source.startswith("provider")
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
        containment = " ".join(_CONTAINMENT_INSTRUCTIONS.split())
        assert "listed OSIA capabilities are both available" in containment
        assert "same permission, ActionTruth, and evidence rules" in containment
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


def test_observable_native_read_command_is_still_rejected(tmp_path):
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
    assert attempt.outcome_unknown is True
    assert [
        (event.identity, event.phase, event.outcome)
        for event in attempt.capability_events
    ] == [("commandExecution", "completed", "outcome_unknown")]
    assert not any(event.tool_attempt_id for event in attempt.capability_events)
    assert list(client._attempt_store().attempts_dir.glob("attempt_*.json")) == []


def test_raw_command_approval_never_starts_any_executor(tmp_path):
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
    assert sent == [{"id": 1, "result": {"decision": "decline"}}]
    assert prompts == []
    assert attempt.capability_events[-1].reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
    assert not attempt.capability_events[-1].tool_attempt_id
    assert list(client._attempt_store().attempts_dir.glob("attempt_*.json")) == []


def test_unapproved_native_completion_stays_unknown_without_receipt(tmp_path):
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
    assert not any(event.tool_attempt_id for event in attempt.capability_events)
    assert attempt.outcome_unknown is True
    assert list(client._attempt_store().attempts_dir.glob("attempt_*.json")) == []


def test_native_file_change_is_declined_without_fake_dispatch(tmp_path):
    client, sent = _client(tmp_path, "file")
    attempt = _native_attempt(
        conversation_id="conversation:file",
        turn_id="turn:file",
        user_request="Create the isolated fixture.",
    )
    change = {
        "path": str(tmp_path / "fixture.txt"),
        "diff": "+native content",
        "kind": {"type": "add"},
    }
    client._attempts[attempt.thread_id] = attempt
    client._resolve_native_approval(attempt, {
        "method": "item/fileChange/requestApproval",
        "request_id": 5,
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "itemId": "item:file",
        },
    })
    assert sent[-1] == {"id": 5, "result": {"decision": "decline"}}
    client._complete_native_item(attempt, {
        "id": "item:file", "type": "fileChange", "status": "completed",
        "changes": [change],
    })
    assert attempt.outcome_unknown is True
    assert not any(event.tool_attempt_id for event in attempt.capability_events)
    assert list(client._attempt_store().attempts_dir.glob("attempt_*.json")) == []


def test_unexpected_provider_effect_never_becomes_canonical_attempt(tmp_path):
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
    assert all(not event.tool_attempt_id for event in attempt.capability_events)
    assert list(client._attempt_store().attempts_dir.glob("attempt_*.json")) == []


def test_wor248_strategy_and_wor249_prompt_compactness_remain_intact():
    instruction = build_execution_strategy(
        "Open Steam and go to my Library.",
        ("open_uri", "navigate_browser", "desktop_click"),
    ).model_instruction()
    assert '"unverified_outcomes"' in instruction
    assert '"applicable_capabilities"' not in instruction
    assert '"semantic_mission"' not in instruction
    assert '"goal_contract"' not in instruction
    assert "0 exact direct provider/API/MCP/OSIA capability" not in instruction
    assert len(_CONTAINMENT_INSTRUCTIONS) < 1_200
    assert " ".join(_CONTAINMENT_INSTRUCTIONS.split()).count(
        "listed OSIA capabilities"
    ) == 1
