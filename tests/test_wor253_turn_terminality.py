import asyncio
import json
import os
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from osia.agent import OSIAAgent
from osia.execution_strategy import (
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    derive_semantic_goal,
    mechanism_for_tool,
    record_resumed_tool_outcome,
)
from osia.permission_outcome import execution_state_text
from osia.server import server
from osia.tool_attempts import WAITING_TOOL_RESULT
from osia.tool_contracts import (
    ExecutionOutcome,
    ToolExecutionRecord,
    effective_contract,
    get_tool_spec,
)
from osia.turn_terminality import (
    TerminalState,
    TerminalityDecision,
    TurnTerminalityController,
    WaitType,
)


def _controller(tmp, *, turn="turn-1", execution=True, postconditions=("done",)):
    return TurnTerminalityController(
        turn_id=turn,
        objective="original objective",
        required_postconditions=postconditions,
        execution_required=execution,
        storage_dir=Path(tmp),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_first_turn_binding_survives_a_brief_windows_reader(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    real_replace = os.replace
    failures = []
    target = next(tmp_path.glob("*.json"))

    def replace(source, destination):
        if destination == target and not failures:
            with target.open(encoding="utf-8"):
                try:
                    real_replace(source, destination)
                except PermissionError as exc:
                    failures.append(exc.winerror)
                    raise
        else:
            real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    binding = {
        "conversation_id": "new-chat", "request_id": "first-request",
        "turn_id": "turn-1", "run_id": "first-run",
        "provider_id": "deepseek", "model_id": "deepseek-flash",
        "effective_reasoning": "auto",
    }
    controller.set_execution_binding(binding)
    assert len(failures) == 1 and failures[0] in (5, 32, 33)
    assert json.loads(target.read_text(encoding="utf-8"))["execution_binding"] == binding
    assert not list(tmp_path.glob("*.tmp"))


def _completion_fixture(agent, answer, directory=None):
    """Return the current provider protocol, using a real read for external facts."""
    controller = agent._terminality_controller
    requirements = controller.snapshot()["outcome_contract"]["requirements"]
    if directory is None:
        assert not requirements, "external requirements need an observed fixture"
        outcomes = [{"id": "answer", "result": "requested answer", "kind": "assistant_content", "receipt_ids": []}]
    else:
        from osia.agent import _execute_tool_with_policy

        path = Path(directory) / "verified-effect.txt"
        declarations = [
            {"id": requirement["id"], "result": requirement["result"],
             "kind": "artifact", "target": {"path": str(path)},
             "expected": {"exists": True}}
            for requirement in requirements
        ] or [{"id": "verified-fixture", "result": "fixture created and observed",
               "kind": "artifact", "target": {"path": str(path)}, "expected": {"exists": True}}]
        assert all(item["kind"] in {"unbound", "artifact"} for item in requirements)
        controller.declare_outcomes(declarations)
        path.write_text("verified fixture result", encoding="utf-8")
        record = _execute_tool_with_policy("read_file", {"path": str(path)}, allow_retry=False, lang="en")
        assert record.outcome is ExecutionOutcome.SUCCESS_VERIFIED
        assert "verified fixture result" in str(record.result)
        controller.record_execution_record("read_file", record, arguments={"path": str(path)})
        receipt_id = controller.receipt_reference(record.result, record.tool_attempt_id)
        assert receipt_id
        outcomes = [
            {**requirement, "receipt_ids": [receipt_id]}
            for requirement in declarations
        ]
    return answer + '<osia_completion>' + json.dumps({"outcomes": outcomes}) + '</osia_completion>'


def _fixture_summary(directory):
    from osia.outcome_contract import normalize_fact_path
    return "- Path " + normalize_fact_path(str(Path(directory) / "verified-effect.txt")) + " is present."


def test_selected_model_completion_exposes_one_final_without_forced_synthesis():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []
        streamed = []

        def fake_run(self, text, **kwargs):
            calls.append(text)
            candidate = "premature narration" if len(calls) == 1 else _completion_fixture(self, "verified final", tmp)
            self.messages.append({"role": "user", "content": text})
            self.messages.append({"role": "assistant", "content": candidate})
            if kwargs.get("on_sentence"):
                kwargs["on_sentence"](candidate)
            return candidate

        agent.run = types.MethodType(fake_run, agent)
        final = agent.run_terminal_turn(
            "Do the external effect",
            turn_id="same-turn",
            required_postconditions=("effect verified",),
            execution_required=True,
            on_sentence=streamed.append,
        )
        assert final == "premature narration"
        assert len(calls) == 1
        assert streamed == ["premature narration"]
        assert agent.terminality_result("same-turn")["terminal_state"] == "success_verified"
        assert agent.terminality_result("same-turn")["reason_code"] == "selected_model_completed"
        assert [
            item["content"] for item in agent.exported_history()
            if item.get("role") == "assistant"
        ] == ["premature narration"]


def test_completed_pending_receipt_gets_automatic_final_synthesis_segment():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            if len(calls) == 1:
                # The tool receipt exists before the synthesis invocation;
                # only that invocation's declaration can complete the turn.
                _completion_fixture(self, "receipt ready", tmp)
                return WAITING_TOOL_RESULT
            candidate = _completion_fixture(self, "synthesized final", tmp)
            self.messages.append({
                "role": "assistant", "content": candidate,
            })
            return candidate

        agent.run = types.MethodType(fake_run, agent)
        final = agent.run_terminal_turn(
            "Do the external effect",
            turn_id="pending-finished-before-evaluate",
            required_postconditions=("effect verified",),
            execution_required=True,
        )
        assert final == _fixture_summary(tmp)
        assert len(calls) == 2
        assert "same original objective" in calls[1]


def test_simple_informational_answer_completes_in_one_invocation_with_streaming():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []
        streamed = []

        def fake_run(self, text, **kwargs):
            calls.append(text)
            candidate = _completion_fixture(self, "Paris.")
            self.messages.append({"role": "assistant", "content": candidate})
            assert kwargs["on_sentence"] is None
            return candidate

        agent.run = types.MethodType(fake_run, agent)
        assert agent.run_terminal_turn(
            "What is the capital of France?",
            turn_id="info-turn",
            on_sentence=streamed.append,
        ) == "Paris."
        assert calls == ["What is the capital of France?"]
        assert streamed == ["Paris."]
        assert agent.terminality_result("info-turn")["terminal_state"] == "success_verified"


def test_ordinary_content_creation_remains_single_invocation_information():
    goal = derive_semantic_goal("Create a short poem about rain")
    assert goal.requires_execution is False


def test_partial_multistep_continues_only_unproven_postcondition():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(
            tmp, postconditions=("step one", "step two"),
        )
        controller.set_strategy_frontier(("safe-next",))
        controller.prove_postcondition("step one", {"receipt": 1})
        decision = controller.evaluate(provider_route="provider:model")
        assert decision.action == "continue"
        assert decision.postcondition == "step two"
        assert controller.snapshot()["remaining_work"] == ["step two"]


def test_pending_and_active_attempts_do_not_launch_concurrent_invocation():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_strategy_frontier(("safe-next",))
        controller.set_attempt_state("attempt-1", status="running")
        assert controller.evaluate().action == "wait"
        assert controller.snapshot()["invocations"] == []


def test_active_provider_invocation_rejects_a_concurrent_segment():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_strategy_frontier(("first", "second"))
        first = controller.begin_invocation(
            provider_route="p:m",
            strategy_id="first",
            postcondition="done",
        )
        assert first
        assert not controller.begin_invocation(
            provider_route="p:m",
            strategy_id="second",
            postcondition="done",
        )
        assert controller.evaluate(provider_route="p:m").action == "wait"


def test_terminal_event_without_exact_durable_attempt_stays_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_attempt_state(
            "attempt-failed", status="pending", permission_state="pending",
        )
        controller.record_typed_evidence(
            outcome="confirmed_failure_before_effect",
            evidence={"failure": "before dispatch"},
            attempt_id="attempt-failed",
        )
        snapshot = controller.snapshot()
        assert "attempt-failed" in snapshot["active_attempts"]
        assert snapshot["permission_state"] == "pending"
        assert any(
            item["attempt_id"] == "attempt-failed"
            and item["outcome"] == "confirmed_failure_before_effect"
            for item in snapshot["typed_evidence"]
        )
        assert controller.evaluate().wait_type == WaitType.WAIT_PENDING_TOOL.value


def test_unverified_event_without_exact_durable_attempt_remains_pending():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_attempt_state("attempt-effect", status="pending")
        controller.record_typed_evidence(
            outcome="success_unverified",
            evidence={"dispatch": "accepted"},
            attempt_id="attempt-effect",
        )
        decision = controller.evaluate(provider_route="p:m")
        snapshot = controller.snapshot()
        assert "attempt-effect" in snapshot["active_attempts"]
        assert snapshot["unknown_effects"]["attempt-effect"]["outcome"] == (
            "success_unverified"
        )
        assert decision.wait_type == WaitType.WAIT_PENDING_TOOL.value


def test_provider_invocation_is_not_persisted_as_a_strategy_attempt():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp, turn="durable-initial")
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id="provider_initial",
            postcondition="done",
        )
        assert invocation
        controller.record_provider_invocation_completed(invocation)

        restored = _controller(tmp, turn="durable-initial")
        assert restored.begin_invocation(
            provider_route="p:m",
            strategy_id="provider_initial",
            postcondition="done",
        )
        assert restored.snapshot()["strategy_attempt_evidence"] == []


def test_cross_process_interrupted_provider_enters_read_only_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp, turn="interrupted-provider")
        assert controller.begin_invocation(
            provider_route="p:m",
            strategy_id="provider_initial",
            postcondition="done",
        )
        state = json.loads(controller._path.read_text(encoding="utf-8"))
        state["invocations"][-1]["owner_pid"] = os.getpid() + 100_000
        controller._path.write_text(json.dumps(state), encoding="utf-8")

        restored = _controller(tmp, turn="interrupted-provider")
        decision = restored.evaluate(provider_route="p:m")
        assert decision.next_strategy == "reconcile_unknown_read_only"
        assert restored.snapshot()["invocations"][-1]["status"] == (
            "interrupted_outcome_unknown"
        )


def test_multistep_semantic_goal_targets_final_requested_effect():
    goal = derive_semantic_goal("Open the Steam Store and select Subnautica")
    assert goal.requested_action == "activate"
    assert goal.target_object == "subnautica"
    assert "activate" in goal.observable_success_postcondition


def test_semantic_target_stops_before_later_control_sentences():
    goal = derive_semantic_goal(
        "Open Calculator. Then verify it with has_window. Report WOR253-OPEN-PASS.",
    )
    assert goal.target_object == "calculator"


def test_quoted_search_target_survives_later_control_instructions():
    goal = derive_semantic_goal(
        'Search files for "README.md". Use search_files only.',
    )
    assert goal.target_object == "README.md"


def test_unquoted_search_target_stops_at_sentence_boundary():
    goal = derive_semantic_goal(
        "Search for requirements.txt. Use search_files only.",
    )
    assert goal.target_object == "requirements.txt"


def test_unproven_pending_receipt_never_exposes_tool_wait_sentinel():
    # WOR-303: a structural wait without a proven correlated attempt is a
    # stalled cycle of the SAME canonical turn.  The runtime continues and the
    # objective completes instead of terminalizing on a false wait.
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            if len(calls) == 1:
                self._terminality_controller.set_attempt_state(
                    "attempt-allow", status="pending", permission_state="pending",
                )
                return WAITING_TOOL_RESULT
            candidate = _completion_fixture(self, "allowed final", tmp)
            self.messages.append({"role": "assistant", "content": candidate})
            return candidate

        agent.run = types.MethodType(fake_run, agent)
        first = agent.run_terminal_turn(
            "Do the external effect",
            turn_id="allow-turn",
            required_postconditions=("effect verified",),
            execution_required=True,
        )
        assert first != WAITING_TOOL_RESULT
        result = agent.terminality_result("allow-turn")
        assert result["terminal_state"] == "success_verified"
        assert len(calls) == 2


def test_non_tool_wait_types_do_not_probe_or_activate_pending_tools():
    waits = (
        (
            WaitType.WAIT_PROVIDER_CONTINUATION.value,
            "provider_continuation_active",
            "active",
        ),
        (
            WaitType.WAIT_RECONCILIATION.value,
            "reconciliation_not_executed_frontier_preserved",
            "unresolved",
        ),
    )
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        for index, (wait_type, reason_code, presentation) in enumerate(waits):
            agent = OSIAAgent(lang="en")

            def forbidden_probe(*_args, **_kwargs):
                raise AssertionError("non-tool wait probed pending tool state")

            agent.pending_tool_attempt_record = types.MethodType(
                forbidden_probe, agent,
            )
            agent.activate_pending_tool_attempt = types.MethodType(
                forbidden_probe, agent,
            )
            decision = TerminalityDecision(
                "wait",
                reason_code=reason_code,
                wait_type=wait_type,
            )
            with patch.object(
                TurnTerminalityController, "begin_invocation", return_value="",
            ), patch.object(
                TurnTerminalityController, "evaluate", return_value=decision,
            ):
                result = agent.run_terminal_turn(
                    "Continue the exact durable operation",
                    turn_id=f"typed-nontool-wait-{index}",
                    required_postconditions=("effect verified",),
                    execution_required=True,
                )
            assert result == execution_state_text(presentation, "en")


def test_pending_tool_wait_exposes_sentinel_only_with_a_proven_attempt():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent(lang="en")
        agent.pending_tool_attempt_record = types.MethodType(
            lambda _self: {"tool_attempt_id": "attempt-proven"}, agent,
        )

        def forbidden_activation(*_args, **_kwargs):
            raise AssertionError("terminality loop activated the tool directly")

        agent.activate_pending_tool_attempt = types.MethodType(
            forbidden_activation, agent,
        )
        decision = TerminalityDecision(
            "wait",
            reason_code="correlated_tool_attempt_active",
            wait_type=WaitType.WAIT_PENDING_TOOL.value,
        )
        with patch.object(
            TurnTerminalityController, "begin_invocation", return_value="",
        ), patch.object(
            TurnTerminalityController, "evaluate", return_value=decision,
        ):
            result = agent.run_terminal_turn(
                "Continue the exact durable operation",
                turn_id="typed-pending-tool-wait",
                required_postconditions=("effect verified",),
                execution_required=True,
            )
        assert result == WAITING_TOOL_RESULT


def test_structural_wait_keeps_its_typed_reason():
    # WOR-303: the typed structural reason is recorded internally, but it is
    # not a terminal.  The same canonical turn continues and completes.
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent(lang="en")
        wait_decision = TerminalityDecision(
            "wait",
            reason_code="permission_pending_without_tool_attempt",
            wait_type=WaitType.WAIT_STRUCTURAL.value,
        )
        original_evaluate = TurnTerminalityController.evaluate
        original_begin = TurnTerminalityController.begin_invocation
        status = {"evaluate_pending": True, "begin_pending": True}
        calls = []

        def evaluate(controller, **kwargs):
            if status["evaluate_pending"]:
                status["evaluate_pending"] = False
                return wait_decision
            return original_evaluate(controller, **kwargs)

        def begin(controller, **kwargs):
            if status["begin_pending"]:
                status["begin_pending"] = False
                return ""
            return original_begin(controller, **kwargs)

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            candidate = _completion_fixture(self, "verified", tmp)
            self.messages.append({"role": "assistant", "content": candidate})
            return candidate

        agent.run = types.MethodType(fake_run, agent)
        with patch.object(
            TurnTerminalityController, "begin_invocation", new=begin,
        ), patch.object(
            TurnTerminalityController, "evaluate", new=evaluate,
        ):
            final = agent.run_terminal_turn(
                "Continue the exact durable operation",
                turn_id="typed-structural-wait",
                required_postconditions=("effect verified",),
                execution_required=True,
            )
        assert final == _fixture_summary(tmp)
        result = agent.terminality_result("typed-structural-wait")
        assert result["terminal_state"] == "success_verified"
        assert result["reason_code"] != "WAIT_WITHOUT_PENDING_TOOL"


def test_resumed_verified_receipt_proves_preserved_strategy_postcondition():
    goal = derive_semantic_goal("Open Calculator")
    strategy = ExecutionStrategy(goal, ())
    mechanism = mechanism_for_tool(
        strategy, "open_application", {"name": "Calculator"},
    )
    decision = strategy.select(
        mechanism,
        {"tool": "open_application", "arguments": {"name": "Calculator"}},
    )
    strategy.record(
        decision, "ambiguous", failure_reason="owner_still_running",
    )
    spec = get_tool_spec("open_application")
    record = ToolExecutionRecord(
        "Calculator is foreground and focused",
        False,
        ExecutionOutcome.SUCCESS_VERIFIED,
        1,
        True,
        False,
        effective_contract(spec, {"name": "Calculator"}, "Open Calculator"),
        "attempt-resumed",
    )
    record_resumed_tool_outcome(strategy, "open_application", record)
    assert strategy.succeeded is True
    assert strategy.frozen_reason == ""


def test_negative_resumed_observation_cannot_prove_search_postcondition():
    goal = derive_semantic_goal("Search for requirements.txt")
    strategy = ExecutionStrategy(goal, ())
    mechanism = StrategyMechanism(
        "tool:search_files",
        StrategyLayer.DIRECT_CAPABILITY,
        "pending read-only search",
        covers_goal=True,
    )
    decision = strategy.select(
        mechanism,
        {"tool": "search_files", "arguments": {"pattern": "requirements.txt"}},
    )
    strategy.record(
        decision, "ambiguous", failure_reason="owner_still_running",
    )
    spec = get_tool_spec("search_files")
    record = ToolExecutionRecord(
        "(no files matching 'requirements.txt' under C:\\workspace)",
        False,
        ExecutionOutcome.SUCCESS_VERIFIED,
        1,
        True,
        False,
        effective_contract(
            spec,
            {"directory": "C:\\workspace", "pattern": "requirements.txt"},
            "Search for requirements.txt",
        ),
        "attempt-negative-search",
    )
    record_resumed_tool_outcome(strategy, "search_files", record)
    assert strategy.succeeded is False
    assert strategy.proven_postconditions == {}
    assert strategy.frozen_reason == ""


def test_unknown_effect_reconciles_before_blocking_mutation_replay():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.record_typed_evidence(
            outcome="outcome_unknown",
            evidence={"dispatch": "possible"},
            attempt_id="attempt-unknown",
        )
        decision = controller.evaluate(provider_route="p:m")
        assert decision.next_strategy == "reconcile_unknown_read_only"
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=decision.next_strategy,
            postcondition=decision.postcondition,
        )
        assert invocation
        controller.record_provider_invocation_completed(invocation)
        # WOR-303: reconciliation guidance repeats while the possible effect
        # remains unresolved; there is no "one reconciliation" quota.
        repeated = controller.evaluate(provider_route="p:m")
        assert repeated.action == "continue"
        assert repeated.next_strategy == "reconcile_unknown_read_only"
        assert not controller.reserve_final(
            "blocked_verified",
            reason_code="safe_reconciliation_frontier_exhausted",
            provider_route="p:m",
            final_text="must not finalize without reconciliation evidence",
        )


def test_unknown_effects_keep_offering_reconciliation_without_cap():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        for attempt_id in ("attempt-a", "attempt-b"):
            controller.record_typed_evidence(
                outcome="outcome_unknown",
                evidence={"dispatch": attempt_id},
                attempt_id=attempt_id,
            )
        first = controller.evaluate(provider_route="p:m")
        first_invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=first.next_strategy,
            postcondition=first.postcondition,
        )
        controller.record_provider_invocation_completed(first_invocation)
        second = controller.evaluate(provider_route="p:m")
        assert second.action == "continue"
        second_invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=second.next_strategy,
            postcondition=second.postcondition,
        )
        controller.record_provider_invocation_completed(second_invocation)
        # WOR-303: unresolved possible effects keep receiving factual
        # read-only reconciliation guidance; no count exhausts the offer.
        third = controller.evaluate(provider_route="p:m")
        assert third.action == "continue"
        assert third.next_strategy == "reconcile_unknown_read_only"


def test_verified_postcondition_retires_reconciled_unknown_effect():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.record_typed_evidence(
            outcome="outcome_unknown",
            evidence={"dispatch": "possible"},
            postcondition="done",
            attempt_id="attempt-unknown",
        )
        controller.set_attempt_state(
            "attempt-unknown", status="waiting_verification",
        )
        controller.prove_postcondition(
            "done", {"read_only_verification": "effect_present"},
        )
        assert controller.evaluate().terminal_state == "success_verified"
        assert controller.snapshot()["unknown_effects"] == {}


def test_productive_long_mission_has_no_invocation_cap():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_strategy_frontier(("safe-strategy",))
        invocation_ids = []
        for revision in range(12):
            decision = controller.evaluate(provider_route="p:m")
            assert decision.action == "continue"
            invocation = controller.begin_invocation(
                provider_route="p:m",
                strategy_id=decision.next_strategy,
                postcondition=decision.postcondition,
            )
            invocation_ids.append(invocation)
            controller.record_provider_invocation_completed(invocation)
            controller.record_typed_evidence(
                outcome="success_unverified",
                evidence={"revision": revision},
            )
        assert len(set(invocation_ids)) == 12
        controller.prove_postcondition("done", {"verified": True})
        assert controller.evaluate().terminal_state == "success_verified"


def test_repeated_provider_invocation_keeps_objective_active():
    # WOR-303: repeated provider segments with no attempt and no evidence never
    # terminalize an unfinished objective and never destroy the safe frontier.
    # The same canonical turn keeps receiving the same safe continuation.
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_strategy_frontier(("only-safe-strategy",))
        seen = []
        for _round in range(8):
            decision = controller.evaluate(provider_route="p:m")
            assert decision.action == "continue"
            assert decision.next_strategy == "only-safe-strategy"
            seen.append(decision.next_strategy)
            invocation = controller.begin_invocation(
                provider_route="p:m",
                strategy_id=decision.next_strategy,
                postcondition=decision.postcondition,
            )
            assert invocation
            controller.record_provider_invocation_completed(invocation)
        snapshot = controller.snapshot()
        assert len(seen) == 8
        assert snapshot["strategy_frontier"] == ["only-safe-strategy"]
        assert snapshot["terminal_state"] == ""
        assert snapshot["remaining_work"] == ["done"]
        # An empty frontier still continues with a factual replan instruction.
        controller.set_strategy_frontier(())
        replan = controller.evaluate(provider_route="p:m")
        assert replan.action == "continue"
        assert replan.next_strategy == "provider_replan_required"
        assert controller.snapshot()["terminal_state"] == ""


def test_new_correlation_id_without_new_semantic_evidence_does_not_unlock_retry():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.record_typed_evidence(
            outcome="failed_before_effect",
            evidence={"source": "provider_native", "identity": "command"},
            attempt_id="attempt-1",
        )
        revision = controller.evidence_revision
        controller.record_typed_evidence(
            outcome="failed_before_effect",
            evidence={"source": "provider_native", "identity": "command"},
            attempt_id="attempt-2",
        )
        assert controller.evidence_revision == revision
        assert len(controller.snapshot()["typed_evidence"]) == 2


def test_cancellation_planning_and_after_possible_effect_are_authoritative():
    with tempfile.TemporaryDirectory() as tmp:
        planning = _controller(tmp, turn="cancel-planning")
        planning.request_cancellation(effect_possible=False)
        assert planning.evaluate().terminal_state == "user_cancelled"

        effect = _controller(tmp, turn="cancel-effect")
        effect.request_cancellation(effect_possible=True)
        reconcile = effect.evaluate(provider_route="p:m")
        assert reconcile.terminal_state == "user_cancelled"
        assert effect.snapshot()["unknown_effects"]
        effect.reconcile_unknown(
            "cancellation_possible_effect",
            outcome="cancelled_proven",
            evidence={"effect": "absent"},
        )
        assert effect.evaluate().terminal_state == "user_cancelled"


def test_resolved_historical_receipt_does_not_make_later_cancel_ambiguous():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp, turn="cancel-after-resolved-receipt")
        controller.set_attempt_state("attempt-done", status="pending")
        controller.record_typed_evidence(
            outcome="success_verified",
            evidence={"verified": True},
            attempt_id="attempt-done",
        )
        agent = OSIAAgent()
        interrupted = []
        agent.client = types.SimpleNamespace(
            conversation_id="conversation-cancel",
            provider=types.SimpleNamespace(
                cancel_conversation=interrupted.append,
            ),
        )
        agent._terminality_controller = controller
        assert agent.request_turn_cancellation(controller.turn_id)
        snapshot = controller.snapshot()
        assert snapshot["cancellation"]["effect_possible"] is False
        assert snapshot["unknown_effects"] == {}
        assert interrupted == ["conversation-cancel"]


def test_late_provider_result_after_planning_cancellation_is_ignored():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        streamed = []

        def fake_run(self, _text, **_kwargs):
            self.request_turn_cancellation("cancel-late", effect_possible=False)
            self._terminality_controller.prove_postcondition(
                "effect verified", {"late": "provider-result"},
            )
            self.messages.append({
                "role": "assistant", "content": "late provider final",
            })
            return "late provider final"

        agent.run = types.MethodType(fake_run, agent)
        final = agent.run_terminal_turn(
            "Do the external effect",
            turn_id="cancel-late",
            required_postconditions=("effect verified",),
            execution_required=True,
            on_sentence=streamed.append,
        )
        assert final != "late provider final"
        assert final == ""
        assert streamed == []
        assert agent.terminality_result("cancel-late")["terminal_state"] == (
            "user_cancelled"
        )
        assert "late provider final" not in [
            item["content"] for item in agent.exported_history()
            if item.get("role") == "assistant"
        ]


def test_cancellation_queued_before_controller_creation_skips_provider():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            return "must not run"

        agent.run = types.MethodType(fake_run, agent)
        assert agent.request_turn_cancellation("cancel-before-controller")
        final = agent.run_terminal_turn(
            "Explain something",
            turn_id="cancel-before-controller",
        )
        assert calls == []
        assert agent.terminality_result(
            "cancel-before-controller",
        )["terminal_state"] == "user_cancelled"
        assert final != "must not run"


def test_hard_safety_block_never_reinvokes_and_final_reservation_is_once():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.mark_hard_safety_block("system_destruction_absolute")
        decision = controller.evaluate()
        assert decision.terminal_state == "hard_safety_block"
        assert controller.reserve_final(
            TerminalState.HARD_SAFETY_BLOCK.value,
            reason_code=decision.reason_code,
        )
        assert not controller.reserve_final(
            TerminalState.HARD_SAFETY_BLOCK.value,
            reason_code=decision.reason_code,
        )
        assert controller.snapshot()["invocations"] == []


def test_durable_final_reservation_restores_the_same_canonical_text():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp, turn="durable-final")
        controller.prove_postcondition("done", {"receipt": "verified"})
        decision = controller.evaluate()
        assert controller.reserve_final(
            decision.terminal_state,
            reason_code=decision.reason_code,
            final_text="canonical durable final",
        )

        restored = _controller(tmp, turn="durable-final")
        agent = OSIAAgent()
        assert agent._reserve_terminal_final(
            restored, restored.evaluate(), "different provisional text",
        ) == "canonical durable final"
        result = agent.terminality_result("durable-final")
        assert result["reservation_reused"] is True
        assert result["final_text"] == "canonical durable final"


def test_native_provider_completed_status_is_not_osia_goal_proof():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []

        class MetadataClient:
            def consume_invocation_metadata(self):
                return {
                    "native_turn_id": "native-turn",
                    "native_status": "completed",
                }

        agent.client = MetadataClient()

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            self.messages.append({"role": "assistant", "content": "native done"})
            if len(calls) >= 3:
                self.request_turn_cancellation(
                    "native-not-terminal", effect_possible=False,
                )
            return "native done"

        agent.run = types.MethodType(fake_run, agent)
        agent.run_terminal_turn(
            "Do the external effect",
            turn_id="native-not-terminal",
            required_postconditions=("OSIA goal verified",),
            execution_required=True,
        )
        result = agent.terminality_result("native-not-terminal")
        # WOR-303: provider completion/narration is never a goal proof.  Three
        # consecutive prose-only segments keep the same canonical turn active;
        # only the explicit user cancellation ends it.
        assert result["terminal_state"] != "success_verified"
        assert result["terminal_state"] == "user_cancelled"
        assert result["reason_code"] != "provider_cannot_progress_no_usable_path"
        assert len(calls) >= 3


def test_successful_tool_receipt_requires_authoritative_strategy_proof():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        spec = get_tool_spec("search_files")
        record = ToolExecutionRecord(
            "No files found matching README.md",
            False,
            ExecutionOutcome.SUCCESS_VERIFIED,
            1,
            True,
            False,
            effective_contract(
                spec,
                {"path": ".", "pattern": "README.md"},
                "Search for README.md",
            ),
            "attempt-search",
        )
        controller.record_execution_record(
            "search_files", record, postcondition="done",
        )
        assert controller.snapshot()["remaining_work"] == ["done"]
        controller.set_strategy_snapshot({
            "alternatives_still_applicable": [],
            "proven_postconditions": {},
            "succeeded": False,
        })
        assert controller.snapshot()["remaining_work"] == ["done"]


def test_scheduled_action_narration_alone_cannot_be_success_verified():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            self.messages.append({"role": "assistant", "content": "I will open it."})
            if len(calls) >= 3:
                self.request_turn_cancellation(
                    "scheduled-narration", effect_possible=False,
                )
            return "I will open it."

        agent.run = types.MethodType(fake_run, agent)
        agent.run_terminal_turn(
            "Open the requested application",
            turn_id="scheduled-narration",
            required_postconditions=("application visibly open",),
            execution_required=True,
        )
        result = agent.terminality_result("scheduled-narration")
        # WOR-303: future-work narration continues the same canonical turn; it
        # is neither a success proof nor a count-based blocker.
        assert result["terminal_state"] == "user_cancelled"
        assert len(calls) >= 3




def test_multiple_internal_invocations_emit_one_mobile_persistence_and_tts_final():
    async def exercise(tmp):
        agent = OSIAAgent()
        calls = []
        persisted = []
        tts = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            candidate = "premature" if len(calls) == 1 else _completion_fixture(self, "canonical final", tmp)
            self.messages.append({"role": "assistant", "content": candidate})
            return candidate

        agent.run = types.MethodType(fake_run, agent)

        class Channel:
            def __init__(self):
                self.frames = []

            async def send_json(self, frame):
                self.frames.append(dict(frame))

        class TTSSession:
            def __init__(self, *_args, **_kwargs):
                self.finished = []
                self.marked = 0
                self.cancelled = False
                self.task = types.SimpleNamespace(done=lambda: False)
                tts.append(self)

            def start(self):
                return None

            def enqueue(self, _text):
                return None

            def finish_input(self, text):
                self.finished.append(str(text))

            def mark_text_final_sent(self):
                self.marked += 1

            def cancel(self):
                self.cancelled = True

        channel = Channel()
        meta = server._new_turn_meta("wor253-conversation", "wor253-request")
        meta["tts_enabled"] = True
        with (
            patch("osia.turn_terminality.CONFIG_DIR", tmp),
            patch("osia.config.load_settings", return_value={}),
            patch.object(server, "_agent_needs_model_apply", return_value=False),
            patch.object(server, "_persist_accepted_user_message", return_value=True),
            patch.object(
                server,
                "_persist_completed_assistant_message",
                side_effect=lambda _conv, text, *_args: persisted.append(text) or True,
            ),
            patch.object(server, "_ResponseTTSSession", TTSSession),
            patch.object(server, "_track_response_audio_task", return_value=True),
        ):
            await server._run_agent_and_respond(
                channel,
                "Open the requested application",
                [],
                "wor253-conversation",
                get_agent=lambda _conv: agent,
                turn_meta=meta,
            )
        responses = [frame for frame in channel.frames if frame.get("type") == "response"]
        terminals = [
            frame for frame in channel.frames
            if frame.get("type") == "turn_status" and frame.get("terminal")
        ]
        assert len(calls) == 2
        assert persisted == [_fixture_summary(tmp)]
        assert len(responses) == 1
        assert len(terminals) == 1
        assert tts[0].finished == [_fixture_summary(tmp)]
        assert tts[0].marked == 1

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(exercise(tmp))
