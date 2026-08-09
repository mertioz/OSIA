import asyncio
import json
import os
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from osia.agent import OSIAAgent
from osia.autonomous.orchestrator import Orchestrator
from osia.execution_strategy import (
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    derive_semantic_goal,
    mechanism_for_tool,
    record_resumed_tool_outcome,
)
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
    TurnTerminalityController,
)


def _controller(tmp, *, turn="turn-1", execution=True, postconditions=("done",)):
    return TurnTerminalityController(
        turn_id=turn,
        objective="original objective",
        required_postconditions=postconditions,
        execution_required=execution,
        storage_dir=Path(tmp),
    )


def test_provider_completion_reinvokes_same_turn_and_exposes_one_final():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []
        streamed = []

        def fake_run(self, text, **kwargs):
            calls.append(text)
            candidate = "premature narration" if len(calls) == 1 else "verified final"
            self.messages.append({"role": "user", "content": text})
            self.messages.append({"role": "assistant", "content": candidate})
            if len(calls) == 2:
                self._terminality_controller.prove_postcondition(
                    "effect verified", {"receipt": "typed-success"},
                )
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
        assert final == "verified final"
        assert len(calls) == 2
        assert streamed == ["verified final"]
        assert agent.terminality_result("same-turn")["terminal_state"] == "success_verified"
        assert [
            item["content"] for item in agent.exported_history()
            if item.get("role") == "assistant"
        ] == ["verified final"]


def test_completed_pending_receipt_gets_automatic_final_synthesis_segment():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            if len(calls) == 1:
                self._terminality_controller.prove_postcondition(
                    "effect verified", {"receipt": "completed"},
                )
                return WAITING_TOOL_RESULT
            self.messages.append({
                "role": "assistant", "content": "synthesized final",
            })
            return "synthesized final"

        agent.run = types.MethodType(fake_run, agent)
        final = agent.run_terminal_turn(
            "Do the external effect",
            turn_id="pending-finished-before-evaluate",
            required_postconditions=("effect verified",),
            execution_required=True,
        )
        assert final == "synthesized final"
        assert len(calls) == 2
        assert "final synthesis" in calls[1]


def test_simple_informational_answer_completes_in_one_invocation_with_streaming():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "osia.turn_terminality.CONFIG_DIR", tmp,
    ):
        agent = OSIAAgent()
        calls = []
        streamed = []

        def fake_run(self, text, **kwargs):
            calls.append(text)
            self.messages.append({"role": "assistant", "content": "Paris."})
            kwargs["on_sentence"]("Paris.")
            return "Paris."

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


def test_terminal_async_receipt_releases_attempt_and_permission_wait():
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
        assert snapshot["active_attempts"] == {}
        assert snapshot["permission_state"] == "resolved"


def test_unverified_async_success_requires_read_only_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_attempt_state("attempt-effect", status="pending")
        controller.record_typed_evidence(
            outcome="success_unverified",
            evidence={"dispatch": "accepted"},
            attempt_id="attempt-effect",
        )
        decision = controller.evaluate(provider_route="p:m")
        assert controller.snapshot()["active_attempts"] == {}
        assert decision.next_strategy == "reconcile_unknown_read_only"


def test_initial_provider_tuple_is_durable_and_not_replayed_on_reentry():
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
        assert not restored.begin_invocation(
            provider_route="p:m",
            strategy_id="provider_initial",
            postcondition="done",
        )


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


def test_allow_receipt_resumes_same_turn_exactly_once():
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
            self.messages.append({"role": "assistant", "content": "allowed final"})
            return "allowed final"

        agent.run = types.MethodType(fake_run, agent)
        first = agent.run_terminal_turn(
            "Do the external effect",
            turn_id="allow-turn",
            required_postconditions=("effect verified",),
            execution_required=True,
        )
        assert first == WAITING_TOOL_RESULT
        controller = agent._terminality_controller
        strategy = agent._terminality_strategy
        controller.set_permission_state("allowed")
        controller.set_attempt_state("attempt-allow", status="completed")
        controller.record_typed_evidence(
            outcome="success_verified",
            evidence={"permit": "consumed-once"},
            attempt_id="attempt-allow",
        )
        controller.prove_postcondition(
            "effect verified", {"permit": "consumed-once"},
        )
        final = agent._drive_terminal_turn(
            controller,
            strategy,
            initial_input="<tool_result>allowed</tool_result>",
            initial_strategy_id="tool_receipt_continuation",
            history=None,
            on_sentence=None,
            on_first_token=None,
            current_conversation_id="conversation",
            turn_context=None,
            canonical_output=True,
        )
        assert final == "allowed final"
        assert len(calls) == 2
        assert agent.terminality_result("allow-turn")["terminal_state"] == "success_verified"


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
        blocked = controller.evaluate(provider_route="p:m")
        assert blocked.terminal_state == "blocked_verified"
        assert blocked.reason_code == "safe_reconciliation_frontier_exhausted"
        assert controller.reserve_final(
            blocked.terminal_state,
            reason_code=blocked.reason_code,
            provider_route="p:m",
            final_text="The effect remains unknown; safe verification is exhausted.",
        )


def test_each_unknown_effect_gets_one_read_only_reconciliation_before_blocking():
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
        assert second.postcondition != first.postcondition
        second_invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=second.next_strategy,
            postcondition=second.postcondition,
        )
        controller.record_provider_invocation_completed(second_invocation)
        blocked = controller.evaluate(provider_route="p:m")
        assert blocked.terminal_state == "blocked_verified"


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
                evidence={"progress_revision": revision},
                useful_progress=True,
            )
        assert len(set(invocation_ids)) == 12
        controller.prove_postcondition("done", {"verified": True})
        assert controller.evaluate().terminal_state == "success_verified"


def test_repeated_no_progress_tuple_runs_once_then_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        controller = _controller(tmp)
        controller.set_strategy_frontier(("only-safe-strategy",))
        first = controller.evaluate(provider_route="p:m")
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=first.next_strategy,
            postcondition=first.postcondition,
        )
        assert invocation
        controller.record_provider_invocation_completed(invocation)
        blocked = controller.evaluate(provider_route="p:m")
        assert blocked.terminal_state == "blocked_verified"


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
        assert reconcile.next_strategy == "reconcile_unknown_read_only"
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
        assert streamed == [final]
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
            return "native done"

        agent.run = types.MethodType(fake_run, agent)
        agent.run_terminal_turn(
            "Do the external effect",
            turn_id="native-not-terminal",
            required_postconditions=("OSIA goal verified",),
            execution_required=True,
        )
        result = agent.terminality_result("native-not-terminal")
        assert result["terminal_state"] == "blocked_verified"
        assert len(calls) > 1


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

        def fake_run(self, text, **_kwargs):
            self.messages.append({"role": "assistant", "content": "I will open it."})
            return "I will open it."

        agent.run = types.MethodType(fake_run, agent)
        agent.run_terminal_turn(
            "Open the requested application",
            turn_id="scheduled-narration",
            required_postconditions=("application visibly open",),
            execution_required=True,
        )
        assert agent.terminality_result(
            "scheduled-narration"
        )["terminal_state"] == "blocked_verified"


def test_autonomous_numeric_budget_does_not_hide_new_semantic_frontier():
    class Agent:
        provider = "provider"
        model = "model"
        last_execution_strategy_snapshot = {
            "goal": {"requires_execution": True},
            "attempts": [],
            "alternatives_still_applicable": ["dom"],
            "evidence_tokens": [],
            "postcondition_evidence": [],
            "succeeded": False,
            "frozen_reason": "",
        }

    task = {
        "id": "task-1",
        "success_criterion": "observable result",
        "semantic_attempt_tuples": [],
        "semantic_evidence_tokens": [],
        "semantic_evidence_revision": 0,
    }
    orchestrator = Orchestrator(Agent())
    candidate = orchestrator._capture_semantic_frontier(task)
    assert candidate[1] == "dom"
    orchestrator._claim_semantic_strategy(task, candidate)
    assert orchestrator._capture_semantic_frontier(task) is None
    Agent.last_execution_strategy_snapshot["evidence_tokens"] = ["new evidence"]
    assert orchestrator._capture_semantic_frontier(task)[1] == "dom"


def test_multiple_internal_invocations_emit_one_mobile_persistence_and_tts_final():
    async def exercise(tmp):
        agent = OSIAAgent()
        calls = []
        persisted = []
        tts = []

        def fake_run(self, text, **_kwargs):
            calls.append(text)
            candidate = "premature" if len(calls) == 1 else "canonical final"
            self.messages.append({"role": "assistant", "content": candidate})
            if len(calls) == 2:
                remaining = self._terminality_controller.snapshot()["remaining_work"]
                self._terminality_controller.prove_postcondition(
                    remaining[0], {"receipt": "verified"},
                )
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
        assert persisted == ["canonical final"]
        assert len(responses) == 1
        assert len(terminals) == 1
        assert tts[0].finished == ["canonical final"]
        assert tts[0].marked == 1

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(exercise(tmp))
