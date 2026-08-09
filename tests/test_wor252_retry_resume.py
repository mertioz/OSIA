"""Focused deterministic WOR-252 retry/resume and permission outcomes."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from osia import action_policy, tool_attempts
from osia.action_policy import PermissionPolicy
from osia.agent import OSIAAgent, TOOL_MAP
from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
from osia.execution_strategy import execution_strategy_scope
from osia.mcp_client import registry as mcp_registry
from osia.permission_outcome import PermissionOutcome, PermissionResult
from osia.permission_store import (
    BackendAvailability,
    PermissionStore,
    SecureStorageBackend,
)
from osia.server import server
from osia.tool_attempts import ToolAttemptCoordinator, ToolAttemptStore


class _ProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "wor252_synthetic", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"W252" + bytes(value ^ 0x5A for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"W252"):
            raise ValueError("invalid_protected_blob")
        return bytes(value ^ 0x5A for value in protected[4:])


class _SequenceCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_by_call = []

    def create(self, **kwargs):
        self.messages_by_call.append(copy.deepcopy(kwargs.get("messages") or []))
        if self.calls >= len(self.responses):
            raise AssertionError("unexpected provider round")
        content = self.responses[self.calls]
        self.calls += 1
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _agent_with(*responses):
    agent = OSIAAgent(model="wor252-model", lang="fr")
    agent.provider = "ollama"
    completions = _SequenceCompletions(responses)
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    return agent, completions


class _Channel:
    def __init__(self):
        self._closed = False
        self._permission_device_id = "device-wor252"
        self.frames = []
        self.sent = threading.Event()

    async def send_json(self, frame):
        self.frames.append(dict(frame))
        self.sent.set()


class _FailingChannel(_Channel):
    async def send_json(self, frame):
        self.sent.set()
        raise RuntimeError("synthetic presentation failure")


def _configure(agent, suffix: str) -> None:
    agent.configure_tool_attempts(
        conversation_id="wor252-conversation",
        request_id=f"wor252-request-{suffix}",
        turn_id=f"wor252-turn-{suffix}",
        final_response_message_id=f"wor252-final-{suffix}",
    )


def _records(store: ToolAttemptStore) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(store.attempts_dir.glob("attempt_*.json"))
    ]


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    policy = PermissionPolicy(PermissionStore(
        tmp_path / "permissions.dat",
        backend=_ProtectedBackend(),
    ))
    action_policy.set_default_permission_policy(policy)
    action_policy.clear_pending_execution_permit()
    previous_provider = mcp_registry.get_confirmation_provider()
    mcp_registry.set_confirmation_provider(None)
    yield store
    action_policy.clear_pending_execution_permit()
    action_policy.set_default_permission_policy(None)
    mcp_registry.set_confirmation_provider(previous_provider)


def test_partial_success_retry_executes_only_unfinished_new_attempt(
    monkeypatch, _isolated_runtime,
):
    dispatches = []
    gate_count = {"command": 0}

    monkeypatch.setitem(
        TOOL_MAP, "get_system_info",
        lambda **_kwargs: dispatches.append("read") or "system-ok",
    )
    monkeypatch.setitem(
        TOOL_MAP, "run_command",
        lambda **_kwargs: dispatches.append("command") or "command-ok",
    )

    def gate(_request, tool_name, _arguments):
        if tool_name != "run_command":
            return None
        gate_count["command"] += 1
        if gate_count["command"] == 1:
            return (
                "ERROR: action/osia/run_command: "
                "permission_outcome=presentation_failed; "
                "sensitive action blocked (confirmation required, "
                "no approver available)"
            )
        return None

    monkeypatch.setattr(action_policy, "guard_tool_call", gate)
    monkeypatch.setattr(
        action_policy, "pending_execution_permit_matches",
        lambda tool, _arguments: tool == "run_command",
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit",
        lambda tool, _arguments: tool == "run_command",
    )
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call",
        lambda *_args: False,
    )

    agent, completions = _agent_with(
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        '<tool_call>{"tool":"run_command","command":"echo wor252"}</tool_call>',
        '<tool_call>{"tool":"resolve_unfinished_execution",'
        '"decision":"resume"}</tool_call>',
        "Les étapes demandées sont terminées.",
    )
    _configure(agent, "original")
    first = agent.run("Effectue la lecture puis la commande demandée.")
    assert "n’a pas pu être présentée" in first
    assert dispatches == ["read"]

    _configure(agent, "retry")
    final = agent.run("réessaye")
    assert final == "Les étapes demandées sont terminées."
    assert dispatches == ["read", "command"]
    records = _records(_isolated_runtime)
    assert [item["status"] for item in records].count("succeeded") == 2
    assert [item["status"] for item in records].count(
        "failed_before_effect"
    ) == 1
    command_records = [item for item in records if item["tool_name"] == "run_command"]
    assert {item["request_id"] for item in command_records} == {
        "wor252-request-original", "wor252-request-retry",
    }
    assert completions.calls == 4


def test_completed_mutation_is_not_replayed_or_reclassified(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        TOOL_MAP, "write_file",
        lambda **_kwargs: dispatches.append("write") or "file-created",
    )
    monkeypatch.setattr(action_policy, "guard_tool_call", lambda *_args: None)
    monkeypatch.setattr(
        action_policy, "pending_execution_permit_matches", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call", lambda *_args: False,
    )
    call = (
        '<tool_call>{"tool":"write_file","path":"C:/synthetic/wor252.md",'
        '"content":"done"}</tool_call>'
    )
    agent, _completions = _agent_with(
        call,
        call,
        "The file was created once.",
    )
    _configure(agent, "same-turn-completed")

    final = agent.run("Create the file now.")

    assert final == "The file was created once."
    assert dispatches == ["write"]
    assert agent._unfinished_execution is None


def test_missing_presentation_and_provider_error_are_never_user_denied(
    monkeypatch,
):
    mcp_registry.set_confirmation_provider(
        lambda *_args: PermissionResult(PermissionOutcome.PRESENTATION_FAILED),
    )
    missing = mcp_registry.confirm_sensitive_action(
        mcp_registry.ConfirmationDomain.NATIVE,
        "osia",
        "run_command",
        "Run one command",
        call_arguments={"command": "echo wor252"},
    )
    assert "permission_outcome=presentation_failed" in missing
    assert "user declined" not in missing

    def broken(*_args):
        raise RuntimeError("synthetic disconnected presenter")

    mcp_registry.set_confirmation_provider(broken)
    failed = mcp_registry.confirm_sensitive_action(
        mcp_registry.ConfirmationDomain.NATIVE,
        "osia",
        "run_command",
        "Run one command",
        call_arguments={"command": "echo wor252"},
    )
    assert "permission_outcome=internal_failure" in failed
    assert "user declined" not in failed


def test_explicit_correlated_deny_is_user_denied_and_zero_dispatch(monkeypatch):
    dispatches = []
    mcp_registry.set_confirmation_provider(
        lambda *_args: PermissionResult(
            PermissionOutcome.USER_DENIED,
            "permission-request-wor252",
        ),
    )
    blocked = action_policy.guard_tool_call(
        "Exécute la commande synthétique.",
        "run_command",
        {"command": "echo wor252"},
    )
    if blocked is None:
        dispatches.append("unexpected")
    assert "permission_outcome=user_denied" in blocked
    assert dispatches == []


def test_standard_one_allow_produces_one_correlated_dispatch():
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    channel = _Channel()
    dispatches = []
    results = []
    mcp_registry.set_confirmation_provider(server._ws_confirm_provider)

    def worker():
        target_token = server._turn_target.set((channel, loop))
        correlation_token = server._turn_confirmation_correlation.set({
            "conversation_id": "wor252-conversation-allow",
            "request_id": "wor252-request-allow",
            "turn_id": "wor252-turn-allow",
        })
        try:
            blocked = action_policy.guard_tool_call(
                "Exécute la commande synthétique.",
                "run_command",
                {"command": "echo wor252"},
            )
            if blocked is None and action_policy.consume_pending_execution_permit(
                "run_command", {"command": "echo wor252"},
            ):
                dispatches.append("command")
            results.append(blocked)
        finally:
            server._turn_confirmation_correlation.reset(correlation_token)
            server._turn_target.reset(target_token)

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    try:
        assert channel.sent.wait(2)
        assert len(channel.frames) == 1
        frame = channel.frames[0]
        assert frame["conversation_id"] == "wor252-conversation-allow"
        assert frame["request_id"] == "wor252-request-allow"
        assert frame["turn_id"] == "wor252-turn-allow"
        assert server._resolve_confirm(
            frame["id"], True, channel=channel,
        ) is True
        assert server._resolve_confirm(
            frame["id"], True, channel=channel,
        ) is False
        worker_thread.join(timeout=2)
        assert results == [None]
        assert dispatches == ["command"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()


def test_permission_projection_failure_stays_typed():
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    channel = _FailingChannel()
    channel._permission_device_id = ""
    results = []

    def worker():
        token = server._turn_target.set((channel, loop))
        try:
            results.append(server._ws_confirm_provider(
                "osia", "run_command", "Run one command",
            ))
        finally:
            server._turn_target.reset(token)

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    try:
        assert channel.sent.wait(2)
        worker_thread.join(timeout=2)
        assert [item.outcome for item in results] == [
            PermissionOutcome.PRESENTATION_FAILED,
        ]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()


def test_active_attempt_retry_surfaces_existing_state_without_ghost(monkeypatch):
    agent, completions = _agent_with("provider must not be called")
    agent._active_tool_attempt_snapshot = lambda: {
        "tool_attempt_id": "attempt-active-wor252",
        "status": "pending",
    }
    _configure(agent, "active-retry")

    result = agent.run("continue")

    assert "already active" in result
    assert "No second attempt" in result
    assert completions.calls == 0


@pytest.mark.parametrize("resume_response", [
    '<tool_call>{"tool":"resolve_unfinished_execution",'
    '"decision":"resume"}</tool_call>',
    '<tool_call>{"tool":"run_command","command":"echo wor252"}</tool_call>',
])
def test_outcome_unknown_resume_reconciles_before_replay(
    monkeypatch, resume_response,
):
    order = []
    monkeypatch.setitem(
        TOOL_MAP, "read_file",
        lambda **_kwargs: order.append("reconcile") or "operation absent",
    )
    monkeypatch.setitem(
        TOOL_MAP, "run_command",
        lambda **_kwargs: order.append("replay") or "command-ok",
    )
    monkeypatch.setattr(action_policy, "guard_tool_call", lambda *_args: None)
    monkeypatch.setattr(
        action_policy, "pending_execution_permit_matches", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call", lambda *_args: False,
    )
    agent, _completions = _agent_with(
        resume_response,
        '<tool_call>{"tool":"read_file","path":"C:/synthetic/state.txt"}</tool_call>',
        '<tool_call>{"tool":"run_command","command":"echo wor252"}</tool_call>',
        "Reconciliation and execution completed.",
    )
    agent._unfinished_execution = {
        "call": {"tool": "run_command", "command": "echo wor252"},
        "outcome": "outcome_unknown",
        "reason_code": "tool_timeout",
        "attempt_id": "attempt-unknown-wor252",
        "original_request": "commande synthétique",
    }
    _configure(agent, "unknown-retry")

    final = agent.run("vas-y")

    assert final == "Reconciliation and execution completed."
    assert order == ["reconcile", "replay"]


def test_repeated_future_narration_cannot_terminalize_resume(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        TOOL_MAP, "run_command",
        lambda **_kwargs: dispatches.append("command") or "unexpected",
    )
    agent, completions = _agent_with(
        "Je réessaie : je vais exécuter la commande maintenant.",
        "Je réessaie : je vais exécuter la commande maintenant.",
    )
    agent._unfinished_execution = {
        "call": {"tool": "run_command", "command": "echo wor252"},
        "outcome": "failed_before_effect",
        "reason_code": "permission_presentation_failed",
        "attempt_id": "attempt-failed-wor252",
        "original_request": "commande synthétique",
    }
    _configure(agent, "promise")

    final = agent.run("reprends")

    assert "no new attempt" in final
    assert "Je réessaie" not in final
    assert dispatches == []
    assert completions.calls == 2


def test_provider_native_accepted_transition_can_finalize_execution():
    event = SimpleNamespace(
        source="provider_native",
        phase="accepted",
        outcome="dispatch_started",
    )
    agent = OSIAAgent(model="wor252-model", lang="en")
    agent.provider = "chatgpt_account"
    completions = _SequenceCompletions([
        "Steam was opened at the Subnautica search results.",
    ])
    events = [
        event,
        SimpleNamespace(
            source="provider_native",
            phase="completed",
            outcome="success_verified",
        ),
    ]
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        consume_capability_events=lambda: tuple(events),
        set_conversation_id=lambda _value: None,
        set_tool_attempt_turn_id=lambda _value: None,
        set_tool_attempt_final_response_message_id=lambda _value: None,
    )
    _configure(agent, "provider-native")

    request = "Open Steam and search for Subnautica now."
    with execution_strategy_scope(request, {"run_command"}) as strategy:
        assert strategy.goal.requires_execution is True
        final = agent.run(request)

    assert final == "Steam was opened at the Subnautica search results."
    assert completions.calls == 1


def test_provider_native_presentation_failure_stays_typed(
    tmp_path, _isolated_runtime,
):
    mcp_registry.set_confirmation_provider(
        lambda *_args: PermissionResult(PermissionOutcome.PRESENTATION_FAILED),
    )
    client = AppServerClient(
        base_dir=tmp_path / "native-runtime",
        tool_attempt_store=_isolated_runtime,
    )
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(
        thread_id="thread-wor252-native",
        conversation_id="conversation-wor252-native",
        osia_turn_id="turn-wor252-native",
        turn_id="provider-turn-wor252-native",
        model_id="wor252-model",
        user_request="Run the synthetic command.",
    )
    item_id = "item-wor252-native"
    command = "echo wor252"
    attempt.native_items[item_id] = {
        "id": item_id,
        "type": "commandExecution",
        "status": "inProgress",
        "command": command,
        "cwd": str(client.workspace_dir),
    }

    client._resolve_native_approval(attempt, {
        "method": "item/commandExecution/requestApproval",
        "request_id": 252,
        "params": {
            "itemId": item_id,
            "command": command,
            "cwd": str(client.workspace_dir),
            "availableDecisions": ["accept", "decline"],
        },
    })

    assert sent == [{"id": 252, "result": {"decision": "decline"}}]
    assert attempt.native_dispatches == {}
    denied = attempt.capability_events[-1]
    assert denied.phase == "denied"
    assert denied.outcome == "permission_presentation_failed"
    assert denied.tool_name == "run_command"
    assert denied.arguments["command"] == command


def test_provider_native_typed_permission_failure_overrides_refusal_narration():
    event = SimpleNamespace(
        source="provider_native",
        phase="denied",
        outcome="permission_presentation_failed",
        tool_name="run_command",
        arguments={"command": "echo wor252"},
    )
    agent = OSIAAgent(model="wor252-model", lang="en")
    agent.provider = "chatgpt_account"
    completions = _SequenceCompletions(["The user refused permission."])
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        consume_capability_events=lambda: (event,),
        set_conversation_id=lambda _value: None,
        set_tool_attempt_turn_id=lambda _value: None,
        set_tool_attempt_final_response_message_id=lambda _value: None,
    )
    _configure(agent, "provider-native-presentation")

    final = agent.run("Run the command now.")

    assert "could not be presented" in final
    assert "refused" not in final
    assert agent._unfinished_execution["call"] == {
        "tool": "run_command",
        "command": "echo wor252",
    }
