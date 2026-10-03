"""Deterministic WOR-305 Batch D/E final projection and parser ordering tests."""

from __future__ import annotations

from types import SimpleNamespace

from osia.agent import OSIAAgent
from osia.turn_terminality import TerminalState, TurnTerminalityController


def _controller(tmp_path, turn_id="de-turn"):
    controller = TurnTerminalityController(
        turn_id=turn_id,
        objective="Complete the requested task.",
        required_postconditions=("original_objective_satisfied",),
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.bind_outcome_contract()
    return controller


def _record_process(controller, tmp_path, *, exit_code, label):
    controller.record_typed_evidence(
        outcome="command_completed",
        evidence=f"process receipt {label}",
        attempt_id=f"process-{label}",
        facts=[{
            "kind": "process",
            "target": {
                "command": "build-tool --check",
                "cwd": str(tmp_path),
            },
            "properties": {
                "exit_code": exit_code,
                "output_truncated": False,
                "stdout": "",
                "stderr": "",
                "output": "",
            },
        }],
        effectful=True,
    )


def _final_text(agent, controller, decision, candidate):
    return agent._terminal_final_text(
        decision.terminal_state,
        candidate,
        reason_code=decision.reason_code,
        snapshot=controller.snapshot(),
    )


def test_model_error_text_is_not_a_provider_fact_blocker(tmp_path):
    controller = _controller(tmp_path)
    text = "ERROR: I could not install the app."

    assert controller.accept_completion("  " + text + "  ") == text
    decision = controller.evaluate()
    assert decision.terminal_state == TerminalState.SUCCESS_VERIFIED.value
    assert controller.snapshot()["typed_evidence"] == []

def test_terminal_turn_preserves_selected_model_error_text_without_inventing_blocker(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path))
    answer = "ERROR: I could not complete the requested sample setup."

    class Completions:
        def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=answer, tool_calls=None,
            ))])

    agent = OSIAAgent()
    agent.provider = "ollama"
    agent.model = "fixture-model"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent._resolve_route = lambda _text: ("medium", [])

    assert agent.run_terminal_turn(
        "Prepare the sample project.", turn_id="explicit-error-projection",
    ) == answer
    result = agent.terminality_result("explicit-error-projection")
    assert result["terminal_state"] == TerminalState.SUCCESS_VERIFIED.value
    assert result["final_text"] == answer

def test_informational_and_general_prose_are_selected_model_completions(tmp_path):
    examples = (
        "The capital is Paris.",
        "This answer contains the word error in the middle.",
        "I couldn't complete that installation.",
        "error: is lowercase and is not the canonical marker.",
    )
    for index, answer in enumerate(examples):
        controller = _controller(tmp_path, f"informational-{index}")
        assert controller.accept_completion(answer) == answer
        decision = controller.evaluate()
        assert decision.terminal_state == TerminalState.SUCCESS_VERIFIED.value
        assert decision.reason_code == "selected_model_completed"
        assert _final_text(OSIAAgent(), controller, decision, answer) == answer
        assert "No successful process execution was observed." not in answer


def test_model_error_text_does_not_erase_a_successful_process_fact(tmp_path):
    controller = _controller(tmp_path)
    _record_process(controller, tmp_path, exit_code=0, label="success")
    answer = "ERROR: The requested install did not complete."

    assert controller.accept_completion(answer) == answer
    decision = controller.evaluate()
    assert decision.terminal_state == TerminalState.SUCCESS_VERIFIED.value
    facts = [
        fact
        for entry in controller.snapshot()["typed_evidence"]
        for fact in entry.get("facts", [])
        if fact.get("kind") == "process"
    ]
    assert [fact["properties"]["exit_code"] for fact in facts] == [0]
    assert controller.snapshot()["completion_errors"] == []

def test_failed_process_then_successful_recovery_keeps_both_facts(tmp_path):
    controller = _controller(tmp_path)
    _record_process(controller, tmp_path, exit_code=1, label="failed")
    _record_process(controller, tmp_path, exit_code=0, label="recovered")
    answer = "The build check passed after the correction."

    assert controller.accept_completion(answer) == answer
    assert controller.evaluate().terminal_state == TerminalState.SUCCESS_VERIFIED.value
    codes = [
        fact["properties"]["exit_code"]
        for entry in controller.snapshot()["typed_evidence"]
        for fact in entry.get("facts", [])
        if fact.get("kind") == "process"
    ]
    assert codes == [1, 0]


def test_mixed_process_facts_remain_factual_and_all_failed_results_continue(tmp_path):
    mixed = _controller(tmp_path, "mixed-process")
    _record_process(mixed, tmp_path, exit_code=0, label="mixed-success")
    _record_process(mixed, tmp_path, exit_code=1, label="mixed-failure")
    answer = "The successful process completed; a later check returned a nonzero code."
    assert mixed.accept_completion(answer) == answer
    assert mixed.evaluate().terminal_state == TerminalState.SUCCESS_VERIFIED.value
    assert [
        fact["properties"]["exit_code"]
        for entry in mixed.snapshot()["typed_evidence"]
        for fact in entry.get("facts", [])
        if fact.get("kind") == "process"
    ] == [0, 1]

    failed = _controller(tmp_path, "all-failed-process")
    _record_process(failed, tmp_path, exit_code=1, label="only-failure")
    assert failed.accept_completion("The requested check is complete.") == ""
    decision = failed.evaluate()
    assert decision.action == "continue"
    assert decision.terminal_state == ""
    assert "exited with codes: 1" in failed.snapshot()["completion_feedback"]
    assert [
        fact["properties"]["exit_code"]
        for entry in failed.snapshot()["typed_evidence"]
        for fact in entry.get("facts", [])
        if fact.get("kind") == "process"
    ] == [1]

def test_no_process_receipts_do_not_invent_process_claims(tmp_path):
    controller = _controller(tmp_path)
    answer = "The capital is Paris."
    assert controller.accept_completion(answer) == answer
    assert controller.evaluate().terminal_state == TerminalState.SUCCESS_VERIFIED.value
    assert "process" not in controller.snapshot()["completion_text"].lower()
    assert "No successful process execution was observed." not in controller.snapshot()["completion_text"]


def test_cancellation_winner_is_read_back_after_final_cas_loss(tmp_path, monkeypatch):
    controller = _controller(tmp_path, "cancel-race")
    controller.accept_completion("A normal final candidate.")
    decision = controller.evaluate()
    reserve = controller.reserve_final

    def cancellation_wins(*args, **kwargs):
        assert controller.try_request_cancellation(effect_possible=False)
        return reserve(*args, **kwargs)

    monkeypatch.setattr(controller, "reserve_final", cancellation_wins)
    agent = OSIAAgent()
    projected = agent._reserve_terminal_final(
        controller, decision, "A normal final candidate.",
    )

    result = agent.terminality_result("cancel-race")
    snapshot = controller.snapshot()
    assert result["terminal_state"] == TerminalState.USER_CANCELLED.value
    assert result["reason_code"] == "authoritative_user_cancellation"
    assert result["reservation_id"] == snapshot["final_reservation"]["reservation_id"]
    assert result["final_text"] == snapshot["final_reservation"]["final_text"] == ""
    assert projected == result["final_text"]  # The cancellation winner's canonical empty result.


def test_provider_blocker_winner_is_read_back_and_never_replaced(tmp_path, monkeypatch):
    controller = _controller(tmp_path, "provider-race")
    controller.accept_completion("A normal final candidate.")
    decision = controller.evaluate()
    reserve = controller.reserve_final
    winner_text = "ERROR: Provider quota is exhausted."

    def provider_blocker_wins(_state, **_kwargs):
        controller.mark_provider_fact_blocker("provider_hard_quota_exhausted")
        winner = controller.evaluate()
        assert winner.terminal_state == TerminalState.BLOCKED_VERIFIED.value
        assert reserve(
            winner.terminal_state,
            reason_code=winner.reason_code,
            provider_route="fixture",
            final_text=winner_text,
        )
        return False

    monkeypatch.setattr(controller, "reserve_final", provider_blocker_wins)
    agent = OSIAAgent()
    projected = agent._reserve_terminal_final(
        controller, decision, "A normal final candidate.",
        provider_route="fixture",
    )

    result = agent.terminality_result("provider-race")
    snapshot = controller.snapshot()
    assert result["terminal_state"] == TerminalState.BLOCKED_VERIFIED.value
    assert result["reason_code"] == "provider_hard_quota_exhausted"
    assert result["final_text"] == projected == winner_text
    assert result["reservation_reused"] is True
    assert snapshot["final_reservation"]["final_text"] == winner_text
    assert snapshot["final_reservation"]["reservation_id"] == result["reservation_id"]


def test_normal_final_winner_is_unchanged_and_existing_winner_is_reused(tmp_path):
    controller = _controller(tmp_path, "normal-final")
    answer = "The capital is Paris."
    controller.accept_completion(answer)
    decision = controller.evaluate()
    agent = OSIAAgent()

    assert agent._reserve_terminal_final(controller, decision, answer) == answer
    first = agent.terminality_result("normal-final")
    reservation = controller.snapshot()["final_reservation"]
    assert first["terminal_state"] == TerminalState.SUCCESS_VERIFIED.value
    assert first["final_text"] == answer
    assert first["reservation_id"] == reservation["reservation_id"]

    restored = _controller(tmp_path, "normal-final")
    other_agent = OSIAAgent()
    assert other_agent._reserve_terminal_final(
        restored, restored.evaluate(), "replacement final must not win",
    ) == answer
    reused = other_agent.terminality_result("normal-final")
    assert reused["reservation_reused"] is True
    assert reused["reservation_id"] == first["reservation_id"]
    assert restored.snapshot()["final_reservation"]["final_text"] == answer
    assert not restored.reserve_final(
        TerminalState.SUCCESS_VERIFIED.value,
        reason_code="selected_model_completed",
        final_text="replacement final must not win",
    )
    assert restored.snapshot()["final_reservation"]["final_text"] == answer


def test_failed_cas_without_durable_winner_returns_factual_error(tmp_path, monkeypatch):
    controller = _controller(tmp_path, "reservation-failure")
    controller.accept_completion("A normal final candidate.")
    decision = controller.evaluate()
    monkeypatch.setattr(controller, "reserve_final", lambda *_args, **_kwargs: False)
    agent = OSIAAgent()

    result = agent._reserve_terminal_final(
        controller, decision, "A normal final candidate.",
    )
    assert result == "ERROR: The terminal result could not be persisted."
    assert agent.terminality_result("reservation-failure") is None
    assert agent.last_run_diagnostic["reason_code"] == "terminal_final_reservation_unavailable"


def test_reservation_storage_exception_returns_factual_error(tmp_path, monkeypatch):
    controller = _controller(tmp_path, "reservation-storage-error")
    controller.accept_completion("A normal final candidate.")
    decision = controller.evaluate()

    def storage_failure(*_args, **_kwargs):
        raise OSError("fixture persistence failure")

    monkeypatch.setattr(controller, "reserve_final", storage_failure)
    agent = OSIAAgent()
    assert agent._reserve_terminal_final(
        controller, decision, "A normal final candidate.",
    ) == "ERROR: The terminal result could not be persisted."
    assert agent.terminality_result("reservation-storage-error") is None
    assert agent.last_run_diagnostic["reason_code"] == "terminal_final_reservation_unavailable"
