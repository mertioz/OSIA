from __future__ import annotations

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
from osia.tool_attempts import ToolAttemptStore
from osia.turn_terminality import TurnTerminalityController


def _native_item(app: str, item_id: str, client: AppServerClient) -> dict:
    command = f"Start-Process '{app}'"
    return {
        "id": item_id,
        "type": "commandExecution",
        "status": "inProgress",
        "command": command,
        "cwd": str(client.workspace_dir),
    }


def test_provider_native_commands_never_advance_execution_strategy(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(
        base_dir=tmp_path / "native-runtime",
        tool_attempt_store=store,
    )
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(
        thread_id="thread-wor256",
        turn_id="provider-turn-wor256",
        model_id="wor256-model",
        user_request="Open Calculator, Paint, and Character Map",
    )
    strategy = build_execution_strategy(attempt.user_request, {"run_command"})
    controller = TurnTerminalityController(
        turn_id="turn-wor256-three",
        objective=attempt.user_request,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path / "terminality",
    )
    attempt.execution_strategy = strategy

    for index, app in enumerate(("Calculator", "Paint", "Character Map"), 1):
        item = _native_item(app, f"native-item-{index}", client)
        client._resolve_native_approval(attempt, {
            "method": "item/commandExecution/requestApproval",
            "request_id": index,
            "params": {
                "itemId": item["id"],
                "command": item["command"],
                "cwd": item["cwd"],
            },
        })
        event = attempt.capability_events[-1]
        assert event.outcome == "rejected_before_execution"
        assert event.reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
        assert not event.tool_attempt_id

    assert [message["result"]["decision"] for message in sent] == [
        "decline", "decline", "decline",
    ]
    assert not strategy.succeeded
    assert not controller.snapshot()["proven_postconditions"]
    assert list(store.attempts_dir.glob("attempt_*.json")) == []


def test_multi_application_preflight_advances_nodes_and_allows_unrequested_d():
    with execution_strategy_scope(
        "Open Steam, then Discord, then Notepad", {"open_application"},
    ) as strategy:
        steam = tool_preflight("open_application", {"name": "Steam"})
        assert steam is not None and steam.allowed
        # WOR-301: plan order no longer refuses an available tool call.
        premature_discord = tool_preflight(
            "open_application", {"name": "Discord"},
        )
        assert premature_discord is None or premature_discord.allowed is True
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
        # WOR-301: an application outside the requested scope is dispatchable.
        unrelated = tool_preflight("open_application", {"name": "WordPad"})
        assert unrelated is None or unrelated.allowed is True
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
    # WOR-299: an unclassified clause of an execution objective is required
    # work, never prose-provable completion; each stays its own requirement.
    assert nodes[0].requires_execution is True
    assert nodes[1].requires_execution is True
    assert nodes[2].required_application == "calculator"


def test_raw_commands_decline_before_policy_or_strategy(tmp_path, monkeypatch):
    guard_calls = []
    monkeypatch.setattr(
        action_policy,
        "guard_tool_call",
        lambda *args: guard_calls.append(args) or None,
    )
    client = AppServerClient(
        base_dir=tmp_path / "native-runtime",
        tool_attempt_store=ToolAttemptStore(tmp_path / "attempts"),
    )
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(
        thread_id="thread-wor256-no-replay",
        turn_id="provider-turn-wor256-no-replay",
        user_request="Open Calculator, then Paint",
    )

    for request_id, app in enumerate(("Calculator", "Paint"), 1):
        item = _native_item(app, f"native-{request_id}", client)
        client._resolve_native_approval(attempt, {
            "method": "item/commandExecution/requestApproval",
            "request_id": request_id,
            "params": {
                "itemId": item["id"],
                "command": item["command"],
                "cwd": item["cwd"],
            },
        })

    assert [message["result"]["decision"] for message in sent] == [
        "decline", "decline",
    ]
    assert guard_calls == []
    assert all(
        event.reason_code == "CANONICAL_OSIA_TOOL_REQUIRED"
        for event in attempt.capability_events
    )
    assert list(client._attempt_store().attempts_dir.glob("attempt_*.json")) == []


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


def test_mixed_actions_complete_in_order():
    request = (
        "Open Calculator, then Paint, then Character Map, then Notepad"
    )
    strategy = build_execution_strategy(
        request, {"open_application", "run_command"},
    )
    with execution_strategy_scope(
        request,
        {"open_application", "run_command"},
        strategy=strategy,
    ):
        calls = (
            ("open_application", {"name": "Calculator"}),
            ("run_command", {"command": "Start-Process Paint"}),
            ("open_application", {"name": "Character Map"}),
            ("run_command", {"command": "Start-Process Notepad"}),
        )
        for index, (tool_name, arguments) in enumerate(calls):
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


def test_rejecting_second_node_preserves_first_and_allows_third_dispatch():
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
        # recorded for B.  WOR-301: the denial does not refuse a C dispatch.
        strategy.record_external_event(
            "open_application", {"name": "Paint"},
            outcome="permission_user_denied",
            evidence="permission-b-denied",
        )
        third = tool_preflight("open_application", {"name": "Character Map"})
        assert third is None or third.allowed is True

    assert len(strategy.proven_postconditions) == 1
    assert strategy.mission.action_nodes[0].status == "succeeded"
    assert strategy.mission.action_nodes[1].status == "blocked"
    assert strategy.mission.action_nodes[2].status == "pending"


def test_provider_native_requests_decline_before_osia_permission_policy(
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
        item = _native_item(app, f"native-denial-item-{index}", client)
        client._resolve_native_approval(attempt, {
            "method": "item/commandExecution/requestApproval",
            "request_id": index,
            "params": {
                "itemId": item["id"],
                "command": item["command"],
                "cwd": item["cwd"],
            },
        })

    assert sent == [
        {"id": 2, "result": {"decision": "decline"}},
        {"id": 3, "result": {"decision": "decline"}},
    ]
    assert guard_calls == []
    assert attempt.terminal_permission_outcome == ""
    assert [event.outcome for event in attempt.capability_events] == [
        "rejected_before_execution", "rejected_before_execution",
    ]


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
    failures = []

    def fail_before_effect(_text, **_kwargs):
        controller_holder.append(agent._terminality_controller)
        failures.append(1)
        # WOR-303: no provider-failure count may terminalize the objective.
        # The harness ends the run with the exact factual Stop instead.
        if len(failures) >= 10:
            agent.request_turn_cancellation(
                "turn-before-effect", effect_possible=False,
            )
        raise RuntimeError("synthetic provider failure before effect")

    agent.run = fail_before_effect
    final = agent.run_terminal_turn(
        "Open Calculator",
        turn_id="turn-before-effect",
        required_postconditions=("Calculator is open",),
        execution_required=True,
    )
    assert isinstance(final, str)
    assert len(failures) >= 10
    snapshot = controller_holder[0].snapshot()
    assert snapshot["terminal_state"] == "user_cancelled"
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


def test_complete_final_rejects_candidate_that_denies_verified_success():
    agent = OSIAAgent(model="wor256", lang="fr")
    final = agent._terminal_final_text(
        "success_verified",
        "Je n’ai pas pu vérifier de manière fiable le résultat.",
        snapshot={},
    )
    assert final == "Tout ce qui était demandé est terminé."
