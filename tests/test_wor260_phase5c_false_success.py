from __future__ import annotations

import types

import pytest

from osia.adaptive_runtime import assess_adaptive_terminal_success
from osia.agent import OSIAAgent
from osia.execution_strategy import build_execution_strategy
from osia.goal_contract import goal_contract_from_semantic_mission
from osia.tool_attempts import WAITING_TOOL_RESULT
from osia.turn_terminality import TurnTerminalityController
from osia.verifier import VerificationStatus, verify_goal_node
from osia.world_state import build_world_state_view, postcondition_id


_DISPLAY_PROMPT = (
    "Open the Windows Display settings page. Do not change any setting."
)


def _physical_regression_snapshots():
    strategy = build_execution_strategy(
        _DISPLAY_PROMPT,
        {"run_command", "open_uri", "inspect_desktop_surfaces"},
    )
    contract = goal_contract_from_semantic_mission(strategy.mission)
    display_node, no_change_node = contract.nodes
    display_postcondition = display_node.observable_postconditions[0].description
    no_change_postcondition = (
        no_change_node.observable_postconditions[0].description
    )
    attempt_id = "attempt-phase5b-run-command"
    terminality = {
        "turn_id": "turn-phase5b-sanitized",
        "proven_postconditions": {
            display_postcondition: "claimed-provider-completion",
            no_change_postcondition: "exact-independent-proof",
        },
        "typed_evidence": (
            {
                "outcome": "success_verified",
                "attempt_id": attempt_id,
                "postcondition": display_postcondition,
                "evidence_token": "provider-dispatch-token",
                "reason_code": "provider_native_completed",
            },
            {
                "outcome": "success_verified",
                "attempt_id": "attempt-independent-observation",
                "postcondition": no_change_postcondition,
                "evidence_token": "exact-state-token",
                "reason_code": "exact_final_state_observation",
            },
        ),
        "unknown_effects": {
            attempt_id: {
                "outcome": "outcome_unknown",
                "attempt_id": attempt_id,
                "postcondition": display_postcondition,
                "evidence_token": "unresolved-effect-token",
                "reason_code": "physical_surface_unresolved",
            },
        },
        "active_attempts": {},
        "permission_state": "allowed",
        "evidence_revision": 4,
    }
    snapshot = strategy.snapshot()
    snapshot["attempts"] = ({
        "attempt_id": attempt_id,
        "node_id": display_node.node_id,
        "mechanism_id": "direct:run_command",
        "status": "succeeded",
    },)
    return contract, terminality, snapshot


def _assessment_snapshots(
    *,
    first_reason: str,
    second_reason: str,
    first_outcome: str = "success_verified",
    second_outcome: str = "success_verified",
    first_revision: int = 4,
    second_revision: int = 4,
    unknown: bool = False,
    mechanism_id: str = "direct:run_command",
):
    contract, _, strategy = _physical_regression_snapshots()
    first, second = contract.nodes
    first_postcondition = first.observable_postconditions[0].description
    second_postcondition = second.observable_postconditions[0].description
    attempt_id = "attempt-first"
    terminality = {
        "turn_id": "turn-phase5c-variant",
        "proven_postconditions": {
            first_postcondition: "claimed-first",
            second_postcondition: "claimed-second",
        },
        "typed_evidence": (
            {
                "outcome": first_outcome,
                "attempt_id": attempt_id,
                "postcondition": first_postcondition,
                "evidence_token": "first-token",
                "reason_code": first_reason,
                "evidence_revision": first_revision,
            },
            {
                "outcome": second_outcome,
                "attempt_id": "attempt-second",
                "postcondition": second_postcondition,
                "evidence_token": "second-token",
                "reason_code": second_reason,
                "evidence_revision": second_revision,
            },
        ),
        "unknown_effects": ({
            attempt_id: {
                "outcome": "outcome_unknown",
                "attempt_id": attempt_id,
                "postcondition": first_postcondition,
                "evidence_token": "unknown-token",
                "reason_code": "effect_outcome_unresolved",
                "evidence_revision": 4,
            },
        } if unknown else {}),
        "active_attempts": {},
        "permission_state": "allowed",
        "evidence_revision": max(first_revision, second_revision, 4),
    }
    strategy["attempts"] = ({
        "attempt_id": attempt_id,
        "node_id": first.node_id,
        "mechanism_id": mechanism_id,
        "status": "succeeded",
    },)
    return contract, terminality, strategy


def _assessment(**kwargs):
    contract, terminality, strategy = _assessment_snapshots(**kwargs)
    return assess_adaptive_terminal_success(
        semantic_mission=build_execution_strategy(
            _DISPLAY_PROMPT,
            {"run_command", "open_uri", "inspect_desktop_surfaces"},
        ).mission,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
    )


def test_phase5b_claimed_success_preserves_one_of_two_and_unknown_effect():
    contract, terminality, strategy = _physical_regression_snapshots()
    display_node, no_change_node = contract.nodes
    display_id = postcondition_id(
        display_node.node_id,
        display_node.observable_postconditions[0].description,
    )
    no_change_id = postcondition_id(
        no_change_node.node_id,
        no_change_node.observable_postconditions[0].description,
    )

    world = build_world_state_view(
        contract,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy,
    )
    results = tuple(
        verify_goal_node(node, world, world.typed_evidence)
        for node in contract.nodes
    )

    assert world.verified_postconditions == (no_change_id,)
    assert world.remaining_postconditions == (display_id,)
    assert results[0].status == VerificationStatus.OUTCOME_UNKNOWN.value
    assert results[1].status == VerificationStatus.VERIFIED.value
    assert not all(
        item.status == VerificationStatus.VERIFIED.value for item in results
    )


def test_one_of_two_without_unknown_effect_is_never_full_success():
    result = _assessment(
        first_reason="exact_final_state_observation",
        second_reason="provider_native_completed",
    )

    assert result.eligible is False
    assert len(result.verified_postcondition_ids) == 1
    assert len(result.unverified_postcondition_ids) == 1
    assert result.unresolved_effect_attempt_ids == ()


@pytest.mark.parametrize("mechanism_id", (
    "direct:provider_api",
    "deep_link:open_uri",
    "direct:run_command",
    "browser:dom",
    "native_accessibility:desktop_uia",
))
def test_mechanism_success_without_observable_proof_never_verifies_goal(
    mechanism_id,
):
    result = _assessment(
        first_reason="provider_native_completed",
        second_reason="provider_native_completed",
        mechanism_id=mechanism_id,
    )

    assert result.eligible is False
    assert result.verified_postcondition_ids == ()
    assert len(result.unverified_postcondition_ids) == 2


def test_two_of_two_exact_current_postconditions_remain_success_eligible():
    result = _assessment(
        first_reason="exact_final_state_observation",
        second_reason="exact_final_state_observation",
    )

    assert result.eligible is True
    assert len(result.verified_postcondition_ids) == 2
    assert result.unverified_postcondition_ids == ()
    assert result.unresolved_effect_attempt_ids == ()


def test_newer_exact_final_state_can_resolve_an_uncertain_dispatch():
    result = _assessment(
        first_reason="exact_final_state_observation",
        second_reason="exact_final_state_observation",
        first_revision=5,
        second_revision=5,
        unknown=True,
    )

    assert result.eligible is True
    assert result.unresolved_effect_attempt_ids == ()
    assert "exact_final_state_resolves_unknown_effect" in (
        result.verification_results[0].explanation_codes
    )


def test_current_contradiction_forbids_full_success():
    result = _assessment(
        first_reason="exact_final_state_observation",
        second_reason="required_postcondition_contradicted",
        second_revision=5,
    )

    assert result.eligible is False
    assert result.verification_results[1].status == (
        VerificationStatus.CONTRADICTED.value
    )


def test_stale_postcondition_evidence_does_not_prove_current_state():
    result = _assessment(
        first_reason="exact_final_state_observation",
        second_reason="stale_exact_final_state_observation",
        second_revision=5,
    )

    assert result.eligible is False
    assert len(result.verified_postcondition_ids) == 1
    assert len(result.unverified_postcondition_ids) == 1


def test_completed_earlier_node_is_preserved_while_current_node_is_incomplete():
    result = _assessment(
        first_reason="exact_final_state_observation",
        second_reason="provider_native_completed",
    )
    first, second = result.world_state.completed_goal_node_ids, (
        result.world_state.active_goal_node_id
    )

    assert len(first) == 1
    assert second
    assert result.eligible is False


def _drive_with_claimed_provider_success(tmp_path, monkeypatch, *, adaptive: bool):
    monkeypatch.setenv(
        "OSIA_ADAPTIVE_INTELLIGENCE_PHASE3", "1" if adaptive else "0",
    )
    monkeypatch.setenv(
        "OSIA_ADAPTIVE_REASONING_AUTO", "1" if adaptive else "0",
    )
    strategy = build_execution_strategy(
        _DISPLAY_PROMPT,
        {"run_command", "open_uri", "inspect_desktop_surfaces"},
    )
    first, second = strategy.mission.action_nodes
    controller = TurnTerminalityController(
        turn_id="turn-phase5c-agent-guard",
        objective=_DISPLAY_PROMPT,
        required_postconditions=strategy.required_postconditions,
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.record_typed_evidence(
        outcome="success_verified",
        evidence={"provider": "completed"},
        postcondition=first.observable_success_postcondition,
        attempt_id="attempt-run-command",
        reason_code="provider_native_completed",
    )
    controller.prove_postcondition(
        first.observable_success_postcondition,
        {"provider": "completed"},
    )
    controller.record_typed_evidence(
        outcome="success_verified",
        evidence={"state": "exact"},
        postcondition=second.observable_success_postcondition,
        attempt_id="attempt-independent",
        reason_code="exact_final_state_observation",
    )
    controller.prove_postcondition(
        second.observable_success_postcondition,
        {"state": "exact"},
    )
    agent = OSIAAgent(model="fixture", lang="en")

    def fake_run(self, _text, **_kwargs):
        candidate = "I opened Display."
        self.messages.append({"role": "assistant", "content": candidate})
        return candidate

    agent.run = types.MethodType(fake_run, agent)
    final = agent._drive_terminal_turn(
        controller,
        strategy,
        initial_input=_DISPLAY_PROMPT,
        initial_strategy_id="provider_initial",
        history=None,
        on_sentence=None,
        on_first_token=None,
        current_conversation_id="conversation-phase5c",
        turn_context=None,
        canonical_output=True,
    )
    return agent, controller, final


def test_adaptive_agent_bridge_refuses_false_success_reservation(
    tmp_path, monkeypatch,
):
    agent, controller, final = _drive_with_claimed_provider_success(
        tmp_path, monkeypatch, adaptive=True,
    )

    assert final == WAITING_TOOL_RESULT
    assert not controller.snapshot()["final_reservation"]
    assert controller.snapshot()["terminal_state"] == ""
    assert agent.terminality_result(controller.turn_id) is None


def test_gate_off_preserves_legacy_terminal_behavior_byte_for_byte(
    tmp_path, monkeypatch,
):
    agent, controller, final = _drive_with_claimed_provider_success(
        tmp_path, monkeypatch, adaptive=False,
    )

    assert final == "I opened Display."
    assert controller.snapshot()["terminal_state"] == "success_verified"
    assert agent.terminality_result(controller.turn_id)["final_text"] == final
