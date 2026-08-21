from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from osia import action_policy
from osia.agent import OSIAAgent
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    AppServerError,
    CapabilityEvent,
    _TurnAttempt,
)
from osia.chatgpt_account.provider import (
    AccountProviderError,
    _AccountChatClient,
)
from osia.execution_strategy import (
    build_execution_strategy,
    execution_strategy_scope,
    record_tool_outcome,
    tool_preflight,
)
from osia.permission_outcome import PermissionOutcome
from osia.server import server
from osia.tool_attempts import ToolAttemptStore
from osia.turn_terminality import TurnTerminalityController


class _Channel:
    def __init__(self) -> None:
        self._closed = False
        self._permission_device_id = "device-wor256"
        self.frames: list[dict] = []
        self.sent = threading.Event()

    async def send_json(self, frame) -> None:
        self.frames.append(dict(frame))
        self.sent.set()


@pytest.fixture(autouse=True)
def _clear_confirmation_broker():
    with server._pending_confirms_lock:
        server._pending_confirms.clear()
        server._confirmation_channels_by_device.clear()
    yield
    with server._pending_confirms_lock:
        server._pending_confirms.clear()
        server._confirmation_channels_by_device.clear()


def _request_permission(loop, channel, correlation, results):
    def worker():
        target = server._turn_target.set((channel, loop))
        correlated = server._turn_confirmation_correlation.set(correlation)
        try:
            results.append(server._ws_confirm_provider(
                "osia", "run_command", "Run one safe local command",
            ))
        finally:
            server._turn_confirmation_correlation.reset(correlated)
            server._turn_target.reset(target)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def _native_item(app: str, item_id: str, client: AppServerClient) -> dict:
    command = f"Start-Process '{app}'"
    return {
        "id": item_id,
        "type": "commandExecution",
        "status": "inProgress",
        "command": command,
        "cwd": str(client.workspace_dir),
    }


def test_three_sequential_permissions_share_turn_and_execute_each_node_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(action_policy, "guard_tool_call", lambda *_args: None)
    monkeypatch.setattr(
        action_policy, "pending_execution_permit_matches", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit", lambda *_args: True,
    )
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(
        base_dir=tmp_path / "native-runtime",
        tool_attempt_store=store,
    )
    sent_to_provider = []
    client._send = sent_to_provider.append
    request_id = "client-request-wor256"
    turn_id = "turn-wor256-three"
    conversation_id = "conversation-wor256"
    attempt = _TurnAttempt(
        thread_id="thread-wor256",
        conversation_id=conversation_id,
        osia_turn_id=turn_id,
        client_request_id=request_id,
        turn_id="provider-turn-wor256",
        model_id="wor256-model",
        user_request="Open Calculator, Paint, and Character Map",
    )
    strategy = build_execution_strategy(attempt.user_request, {"run_command"})
    controller = TurnTerminalityController(
        turn_id=turn_id,
        objective=attempt.user_request,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    controller.set_strategy_snapshot(strategy.snapshot())
    attempt.execution_strategy = strategy

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    channel = _Channel()
    permissions = []
    max_pending = 0
    try:
        for index, app in enumerate(("Calculator", "Paint", "Character Map"), 1):
            channel.sent.clear()
            results = []
            worker = _request_permission(
                loop,
                channel,
                {
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "turn_id": turn_id,
                },
                results,
            )
            assert channel.sent.wait(2)
            frame = channel.frames[-1]
            permissions.append(frame["permission_request_id"])
            with server._pending_confirms_lock:
                max_pending = max(max_pending, len(server._pending_confirms))
            assert server._resolve_confirm(frame["id"], True, channel=channel)
            worker.join(timeout=2)
            assert [item.outcome for item in results] == [PermissionOutcome.ALLOWED]

            item_id = f"native-item-{index}"
            item = _native_item(app, item_id, client)
            attempt.native_items[item_id] = dict(item)
            client._resolve_native_approval(attempt, {
                "method": "item/commandExecution/requestApproval",
                "request_id": index,
                "params": {
                    "itemId": item_id,
                    "command": item["command"],
                    "cwd": item["cwd"],
                    "availableDecisions": ["accept", "decline"],
                },
            })
            client._complete_native_item(attempt, {
                **item,
                "status": "completed",
                "exitCode": 0,
            })
            event = attempt.capability_events[-1]
            assert event.outcome == "success_verified"
            node = strategy.node_by_id(event.semantic_node_id)
            assert node is not None
            assert node.observable_success_postcondition not in (
                strategy.proven_postconditions
            )
            assert strategy.verify_postcondition(
                f"exact {app} window observation",
                postcondition=node.observable_success_postcondition,
            )
            controller.record_typed_evidence(
                outcome=event.outcome,
                evidence={"app": app},
                postcondition=node.observable_success_postcondition,
                attempt_id=event.tool_attempt_id,
                useful_progress=True,
            )
            controller.prove_postcondition(
                node.observable_success_postcondition,
                {"app": app, "attempt": event.tool_attempt_id},
            )
            controller.set_strategy_snapshot(strategy.snapshot())
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in store.attempts_dir.glob("attempt_*.json")
    ]
    tool_attempt_ids = [item["tool_attempt_id"] for item in records]
    assert len(set(permissions)) == 3
    assert len(set(tool_attempt_ids)) == 3
    assert {item["request_id"] for item in records} == {request_id}
    assert {item["turn_id"] for item in records} == {turn_id}
    assert [message["id"] for message in sent_to_provider] == [1, 2, 3]
    assert {item["tool_call_id"] for item in records} == {
        "native-item-1", "native-item-2", "native-item-3",
    }
    assert max_pending == 1
    assert strategy.succeeded is True
    assert len(controller.snapshot()["proven_postconditions"]) == 3
    decision = controller.evaluate(provider_route="chatgpt_account:wor256")
    assert decision.terminal_state == "success_verified"
    assert controller.reserve_final(
        decision.terminal_state,
        reason_code=decision.reason_code,
        final_text="All three applications were opened.",
    )
    assert not controller.reserve_final(
        decision.terminal_state,
        reason_code=decision.reason_code,
        final_text="duplicate",
    )
    assert server._resolve_confirm(permissions[0], True, channel=channel) is False


def test_multi_application_preflight_advances_nodes_but_rejects_unrequested_d():
    with execution_strategy_scope(
        "Open Steam, then Discord, then Notepad", {"open_application"},
    ) as strategy:
        steam = tool_preflight("open_application", {"name": "Steam"})
        assert steam is not None and steam.allowed
        premature_discord = tool_preflight(
            "open_application", {"name": "Discord"},
        )
        assert premature_discord is not None
        assert premature_discord.reason_code == "earlier_action_node_pending"
        record_tool_outcome(
            "open_application", steam,
            outcome="success_verified", is_error=False, reason_code="",
            result="Steam running",
        )
        discord = tool_preflight("open_application", {"name": "Discord"})
        assert discord is not None and discord.allowed
        record_tool_outcome(
            "open_application", discord,
            outcome="success_verified", is_error=False, reason_code="",
            result="Discord running",
        )
        unrelated = tool_preflight("open_application", {"name": "WordPad"})
        assert unrelated is not None
        assert unrelated.reason_code == "required_application_changed"
        notepad = tool_preflight("open_application", {"name": "Notepad"})
        assert notepad is not None and notepad.allowed
        record_tool_outcome(
            "open_application", notepad,
            outcome="success_verified", is_error=False, reason_code="",
            result="Notepad running",
        )

    assert strategy.succeeded
    assert len(strategy.proven_postconditions) == 3


def test_distinct_open_type_and_visit_clauses_keep_their_own_applications():
    strategy = build_execution_strategy(
        "Open Calculator, type hello in Notepad, then visit example.com in Chrome",
        {"open_application", "type_text", "navigate_browser"},
    )
    nodes = strategy.mission.action_nodes
    assert len(nodes) == 3
    assert [node.requested_action for node in nodes] == [
        "open", "type", "navigate",
    ]
    assert [node.required_application for node in nodes] == [
        "calculator", "notepad", "chrome",
    ]
    assert "example.com" in nodes[2].target_object
    assert len(set(strategy.required_postconditions)) == 3


def test_ambiguous_clause_is_preserved_as_an_independent_requirement():
    strategy = build_execution_strategy(
        "Check the status, do the ambiguous remaining thing, then open Calculator",
        {"open_application"},
    )
    nodes = strategy.mission.action_nodes
    assert len(nodes) == 3
    assert nodes[0].source_clause == "Check the status"
    assert nodes[1].source_clause == "do the ambiguous remaining thing"
    assert nodes[0].requires_execution is False
    assert nodes[1].requires_execution is False
    assert nodes[2].required_application == "calculator"


def test_native_batch_rejects_completed_node_replay_before_permission(
    tmp_path, monkeypatch,
):
    guard_calls = []
    monkeypatch.setattr(
        action_policy,
        "guard_tool_call",
        lambda *args: guard_calls.append(args) or None,
    )
    monkeypatch.setattr(
        action_policy, "pending_execution_permit_matches", lambda *_args: True,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit", lambda *_args: True,
    )
    client = AppServerClient(
        base_dir=tmp_path / "native-runtime",
        tool_attempt_store=ToolAttemptStore(tmp_path / "attempts"),
    )
    sent = []
    client._send = sent.append
    strategy = build_execution_strategy(
        "Open Calculator, then Paint", {"run_command"},
    )
    attempt = _TurnAttempt(
        thread_id="thread-wor256-no-replay",
        turn_id="provider-turn-wor256-no-replay",
        user_request=strategy.mission.objective,
        execution_strategy=strategy,
    )

    def request(app, item_id, request_id):
        item = _native_item(app, item_id, client)
        attempt.native_items[item_id] = item
        client._resolve_native_approval(attempt, {
            "method": "item/commandExecution/requestApproval",
            "request_id": request_id,
            "params": {
                "itemId": item_id,
                "command": item["command"],
                "cwd": item["cwd"],
                "availableDecisions": ["accept", "decline"],
            },
        })
        return item

    calculator = request("Calculator", "native-calculator", 1)
    client._complete_native_item(
        attempt, {**calculator, "status": "completed", "exitCode": 0},
    )
    verification = {
        "id": "native-calculator-verification",
        "type": "commandExecution",
        "status": "inProgress",
        "command": "Get-Process CalculatorApp",
        "cwd": str(client.workspace_dir),
    }
    attempt.native_items[verification["id"]] = verification
    client._resolve_native_approval(attempt, {
        "method": "item/commandExecution/requestApproval",
        "request_id": 15,
        "params": {
            "itemId": verification["id"],
            "command": verification["command"],
            "cwd": verification["cwd"],
            "availableDecisions": ["accept", "decline"],
        },
    })
    client._complete_native_item(
        attempt, {**verification, "status": "completed", "exitCode": 0},
    )
    calculator_node = strategy.mission.action_nodes[0]
    assert strategy.verify_postcondition(
        "exact Calculator window observation",
        postcondition=calculator_node.observable_success_postcondition,
    )
    request("Calculator", "native-calculator-replay", 2)
    paint = request("Paint", "native-paint", 3)
    client._complete_native_item(
        attempt, {**paint, "status": "completed", "exitCode": 0},
    )
    paint_node = strategy.mission.action_nodes[1]
    assert strategy.verify_postcondition(
        "exact Paint window observation",
        postcondition=paint_node.observable_success_postcondition,
    )

    assert sent == [
        {"id": 1, "result": {"decision": "accept"}},
        {"id": 15, "result": {"decision": "accept"}},
        {"id": 2, "result": {"decision": "decline"}},
        {"id": 3, "result": {"decision": "accept"}},
    ]
    assert len(guard_calls) == 3
    assert "native-calculator-replay" not in attempt.native_dispatches
    assert strategy.succeeded is True


def test_mixed_informational_and_executable_nodes_remain_independent():
    strategy = build_execution_strategy(
        "Tell me the time, then open Calculator", {"open_application"},
    )
    assert len(strategy.mission.action_nodes) == 2
    assert strategy.mission.action_nodes[0].requires_execution is False
    assert strategy.mission.action_nodes[1].requires_execution is True
    with execution_strategy_scope(
        "Tell me the time, then open Calculator",
        {"open_application"},
        strategy=strategy,
    ):
        decision = tool_preflight("open_application", {"name": "Calculator"})
        assert decision is not None and decision.allowed
        record_tool_outcome(
            "open_application", decision,
            outcome="success_verified", is_error=False, reason_code="",
            result="Calculator running",
        )
    assert len(strategy.proven_postconditions) == 1
    informational = strategy.prove_informational_nodes(
        "provider_answer:time-visible",
    )
    assert len(informational) == 1
    assert strategy.succeeded is True


def test_mixed_permissionless_and_permissioned_actions_complete_in_order():
    request = (
        "Open Calculator, then Paint, then Character Map, then Notepad"
    )
    strategy = build_execution_strategy(
        request, {"open_application", "run_command"},
    )
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    channel = _Channel()
    permission_ids = []
    try:
        with execution_strategy_scope(
            request,
            {"open_application", "run_command"},
            strategy=strategy,
        ):
            calls = (
                ("open_application", {"name": "Calculator"}, False),
                ("run_command", {"command": "Start-Process Paint"}, True),
                ("open_application", {"name": "Character Map"}, False),
                ("run_command", {"command": "Start-Process Notepad"}, True),
            )
            for index, (tool_name, arguments, permissioned) in enumerate(calls):
                if permissioned:
                    channel.sent.clear()
                    results = []
                    worker = _request_permission(
                        loop,
                        channel,
                        {
                            "conversation_id": "conversation-wor256-mixed",
                            "request_id": "request-wor256-mixed",
                            "turn_id": "turn-wor256-mixed",
                        },
                        results,
                    )
                    assert channel.sent.wait(2)
                    frame = channel.frames[-1]
                    permission_ids.append(frame["permission_request_id"])
                    assert server._resolve_confirm(
                        frame["id"], True, channel=channel,
                    )
                    worker.join(timeout=2)
                    assert [item.outcome for item in results] == [
                        PermissionOutcome.ALLOWED,
                    ]
                decision = tool_preflight(tool_name, arguments)
                assert decision is not None and decision.allowed, index
                record_tool_outcome(
                    tool_name,
                    decision,
                    outcome="success_verified",
                    is_error=False,
                    reason_code="",
                    result={"step": index, "verified": True},
                )
                if tool_name == "run_command":
                    node = strategy.node_by_id(decision.node_id)
                    assert node is not None
                    assert strategy.verify_postcondition(
                        f"exact window observation for step {index}",
                        postcondition=node.observable_success_postcondition,
                    )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert len(set(permission_ids)) == 2
    assert strategy.succeeded is True
    assert [node.status for node in strategy.mission.action_nodes] == [
        "succeeded", "succeeded", "succeeded", "succeeded",
    ]


@pytest.mark.parametrize(
    "reason_code",
    ("permission_presentation_failed", "permission_disconnected"),
)
def test_non_denial_permission_failure_preserves_recoverable_remaining_nodes(
    reason_code,
):
    strategy = build_execution_strategy(
        "Open Calculator, then Paint, then Character Map",
        {"open_application"},
    )
    with execution_strategy_scope(
        strategy.mission.objective,
        {"open_application"},
        strategy=strategy,
    ):
        first = tool_preflight("open_application", {"name": "Calculator"})
        assert first is not None and first.allowed
        record_tool_outcome(
            "open_application", first,
            outcome="success_verified", is_error=False, reason_code="",
            result="Calculator running",
        )
        second = tool_preflight("open_application", {"name": "Paint"})
        assert second is not None and second.allowed
        record_tool_outcome(
            "open_application", second,
            outcome="failed_before_effect", is_error=True,
            reason_code=reason_code,
            result=f"ERROR: {reason_code}",
        )

    assert strategy.terminal_blocker == ""
    assert strategy.mission.action_nodes[0].status == "succeeded"
    assert strategy.mission.action_nodes[1].status == "pending"
    assert strategy.mission.action_nodes[2].status == "pending"
    assert len(strategy.proven_postconditions) == 1


def test_rejecting_second_node_preserves_first_and_blocks_third_continuation():
    strategy = build_execution_strategy(
        "Open Calculator, then Paint, then Character Map",
        {"open_application"},
    )
    with execution_strategy_scope(
        "Open Calculator, then Paint, then Character Map",
        {"open_application"},
        strategy=strategy,
    ):
        first = tool_preflight("open_application", {"name": "Calculator"})
        assert first is not None and first.allowed
        record_tool_outcome(
            "open_application", first,
            outcome="success_verified", is_error=False, reason_code="",
            result="Calculator running",
        )
        second = tool_preflight("open_application", {"name": "Paint"})
        assert second is not None and second.allowed
        # A correlated denial happens before dispatch, so no outcome is
        # recorded for B and no permission/permit can authorize C.
        strategy.record_external_event(
            "open_application", {"name": "Paint"},
            outcome="permission_user_denied",
            evidence="permission-b-denied",
        )
        third = tool_preflight("open_application", {"name": "Character Map"})
        assert third is not None
        assert third.reason_code == "earlier_action_node_pending"

    assert len(strategy.proven_postconditions) == 1
    assert strategy.mission.action_nodes[0].status == "succeeded"
    assert strategy.mission.action_nodes[1].status == "blocked"
    assert strategy.mission.action_nodes[2].status == "pending"


def test_native_user_denial_freezes_turn_and_silently_blocks_later_approval(
    tmp_path, monkeypatch,
):
    guard_calls = []

    def deny_once(*args):
        guard_calls.append(args)
        return "ERROR: permission_outcome=user_denied; user declined"

    monkeypatch.setattr(action_policy, "guard_tool_call", deny_once)
    client = AppServerClient(base_dir=tmp_path / "native-runtime")
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(
        thread_id="provider-thread-wor256-denial",
        turn_id="provider-turn-wor256-denial",
        user_request="Open Calculator, then Paint, then Character Map",
    )

    for index, app in enumerate(("Paint", "Character Map"), 2):
        item_id = f"native-denial-item-{index}"
        item = _native_item(app, item_id, client)
        attempt.native_items[item_id] = item
        client._resolve_native_approval(attempt, {
            "method": "item/commandExecution/requestApproval",
            "request_id": index,
            "params": {
                "itemId": item_id,
                "command": item["command"],
                "cwd": item["cwd"],
                "availableDecisions": ["accept", "decline"],
            },
        })

    assert sent == [
        {"id": 2, "result": {"decision": "decline"}},
        {"id": 3, "result": {"decision": "decline"}},
    ]
    assert len(guard_calls) == 1
    assert attempt.terminal_permission_outcome == "permission_user_denied"
    assert [event.outcome for event in attempt.capability_events] == [
        "permission_user_denied",
    ]
    assert attempt.native_dispatches == {}


def test_provider_exception_events_survive_adapter_and_project_unknown_effect(
    tmp_path,
):
    event = CapabilityEvent(
        source="provider_native",
        identity="commandExecution",
        phase="denied",
        outcome="outcome_unknown",
        item_id="provider-item-unknown",
        tool_attempt_id="attempt-unknown-wor256",
        tool_name="run_command",
        arguments={"command": "Start-Process Calculator"},
    )

    class _Provider:
        def complete(self, **_kwargs):
            raise AccountProviderError(AppServerError(
                "ERROR: provider failed after dispatch.",
                native_action=True,
                allow_fallback=False,
                category="native_outcome_unknown",
                capability_events=(event,),
                native_turn_id="provider-turn-unknown",
            ))

    client = _AccountChatClient(_Provider())
    with pytest.raises(AccountProviderError):
        client.chat.completions.create(
            model="wor256", messages=[{"role": "user", "content": "open"}],
        )
    assert client.consume_capability_events() == (event,)
    metadata = client.consume_invocation_metadata()
    assert metadata["native_turn_id"] == "provider-turn-unknown"

    strategy = build_execution_strategy("Open Calculator", {"run_command"})
    controller = TurnTerminalityController(
        turn_id="turn-provider-unknown",
        objective="Open Calculator",
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    agent = OSIAAgent(model="wor256", lang="en")
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: (event,),
    )
    agent._terminality_controller = controller
    agent._terminality_strategy = strategy
    summary = agent._consume_provider_capability_events()
    snapshot = controller.snapshot()
    assert summary["barrier"] is True
    assert "attempt-unknown-wor256" in snapshot["unknown_effects"]
    assert not any(
        item["reason_code"] == "provider_invocation_failed_before_effect"
        for item in snapshot["typed_evidence"]
    )
    assert strategy.goal.status == "unknown"


def test_provider_exception_before_effect_preserves_true_failure_classification(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path))
    controller_holder = []
    agent = OSIAAgent(model="wor256", lang="en")

    def fail_before_effect(_text, **_kwargs):
        controller_holder.append(agent._terminality_controller)
        raise RuntimeError("synthetic provider failure before effect")

    agent.run = fail_before_effect
    final = agent.run_terminal_turn(
        "Open Calculator",
        turn_id="turn-before-effect",
        required_postconditions=("Calculator is open",),
        execution_required=True,
    )
    assert final
    snapshot = controller_holder[0].snapshot()
    assert snapshot["unknown_effects"] == {}
    assert any(
        item["reason_code"] == "provider_invocation_failed_before_effect"
        for item in snapshot["typed_evidence"]
    )


@pytest.mark.parametrize(
    ("permission_state", "unknown", "reason", "expected"),
    [
        ("user_denied", False, "safe_semantic_frontier_exhausted", "declined"),
        ("presentation_failed", False, "safe_semantic_frontier_exhausted", "displayed"),
        ("none", True, "safe_reconciliation_frontier_exhausted", "may have happened"),
        ("none", False, "safe_semantic_frontier_exhausted", "No valid way"),
    ],
)
def test_blocked_finals_are_partial_truth_without_internal_jargon(
    permission_state, unknown, reason, expected,
):
    strategy = build_execution_strategy(
        "Open Calculator, then Paint", {"open_application"},
    )
    first = strategy.match_action_node("open_application", {"name": "Calculator"})
    strategy.verify_postcondition(
        "calculator-running", postcondition=first.observable_success_postcondition,
    )
    snapshot = {
        "strategy_snapshot": strategy.snapshot(),
        "proven_postconditions": dict(strategy.proven_postconditions),
        "permission_state": permission_state,
        "unknown_effects": (
            {"attempt-unknown": {"outcome": "outcome_unknown"}}
            if unknown else {}
        ),
        "typed_evidence": [{"reason_code": f"permission_{permission_state}"}],
    }
    text = OSIAAgent(model="wor256", lang="en")._terminal_final_text(
        "blocked_verified",
        "internal candidate",
        reason_code=reason,
        snapshot=snapshot,
    )
    assert "Completed: open calculator" in text
    assert "Still incomplete: open paint" in text
    assert expected in text
    for jargon in (
        "postcondition", "terminality", "strategy frontier",
        "semantic equivalence", "evidence revision", "invocation lifecycle",
    ):
        assert jargon not in text.lower()


def test_complete_final_rejects_internal_jargon_candidate():
    agent = OSIAAgent(model="wor256", lang="en")
    final = agent._terminal_final_text(
        "success_verified",
        "All postconditions are proven by terminality.",
        snapshot={},
    )
    assert final == "Everything requested is complete."
