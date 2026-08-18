from __future__ import annotations

from dataclasses import replace

from benchmarks.wor260.phase4b import (
    _base_evaluation,
    _constraints_for,
    _evaluation_mapping,
    _request_for,
    critic_scenarios,
)
from osia.adaptive_critic import (
    CriticVerdict,
    DecisionConfidence,
    validate_critic_evaluation,
)


def _request(name: str = "close_two_tied_routes"):
    scenario = next(item for item in critic_scenarios() if item.scenario_id == name)
    request = _request_for(scenario)
    return request, _constraints_for(request)


def _valid_preference():
    request, constraints = _request()
    evaluation = _base_evaluation(
        request,
        verdict=CriticVerdict.PREFER_CANDIDATE,
        preferred_candidate_id="candidate-alternative",
        confidence=DecisionConfidence.HIGH,
    )
    return request, constraints, evaluation


def test_valid_structured_advisory_is_accepted_but_not_applied() -> None:
    request, constraints, evaluation = _valid_preference()
    result = validate_critic_evaluation(request, evaluation, constraints)
    assert result.accepted
    assert result.evaluation == evaluation
    assert not hasattr(result, "dispatch")
    assert not hasattr(result, "terminality")


def test_unknown_candidate_is_rejected() -> None:
    request, constraints, evaluation = _valid_preference()
    result = validate_critic_evaluation(
        request,
        replace(evaluation, preferred_candidate_id="candidate-invented"),
        constraints,
    )
    assert not result.accepted
    assert "unknown_candidate_id" in result.rejection_codes


def test_hard_rejected_candidate_cannot_be_revived() -> None:
    request, constraints = _request("permission_hard_reject")
    rejected = request.already_rejected_candidates[0].candidate_id
    evaluation = _base_evaluation(
        request,
        verdict=CriticVerdict.PREFER_CANDIDATE,
        preferred_candidate_id=rejected,
    )
    result = validate_critic_evaluation(request, evaluation, constraints)
    assert not result.accepted
    assert "attempt_to_revive_hard_rejected_candidate" in result.rejection_codes


def test_stale_evaluation_and_world_revisions_are_rejected() -> None:
    request, constraints, evaluation = _valid_preference()
    stale_evaluation = validate_critic_evaluation(
        request,
        replace(evaluation, evaluation_revision=evaluation.evaluation_revision + 1),
        constraints,
    )
    stale_world = validate_critic_evaluation(
        request,
        replace(evaluation, world_state_revision=evaluation.world_state_revision + 1),
        constraints,
    )
    assert "stale_evaluation_revision" in stale_evaluation.rejection_codes
    assert "stale_world_state_revision" in stale_world.rejection_codes


def test_cross_turn_and_cross_request_responses_are_rejected() -> None:
    request, constraints, evaluation = _valid_preference()
    for changed in (
        replace(evaluation, turn_id="turn-other"),
        replace(evaluation, request_id="request-other"),
    ):
        result = validate_critic_evaluation(request, changed, constraints)
        assert not result.accepted
        assert "cross_turn_or_request_response" in result.rejection_codes


def test_goal_contract_and_goal_node_cannot_be_mutated() -> None:
    request, constraints, evaluation = _valid_preference()
    for changed in (
        replace(evaluation, goal_contract_fingerprint="goal-other"),
        replace(evaluation, goal_node_id="node-other"),
    ):
        result = validate_critic_evaluation(request, changed, constraints)
        assert not result.accepted
        assert "goal_contract_or_node_mismatch" in result.rejection_codes


def test_invented_postcondition_and_observation_are_rejected() -> None:
    request, constraints, evaluation = _valid_preference()
    invented_postcondition = validate_critic_evaluation(
        request,
        replace(evaluation, supported_postcondition_ids=("postcondition-fake",)),
        constraints,
    )
    invented_observation = validate_critic_evaluation(
        request,
        replace(evaluation, required_observation_types=("enable_vision",)),
        constraints,
    )
    assert "invented_postcondition" in invented_postcondition.rejection_codes
    assert "incompatible_or_invented_observation" in invented_observation.rejection_codes


def test_invented_constraint_is_rejected() -> None:
    request, constraints, evaluation = _valid_preference()
    result = validate_critic_evaluation(
        request,
        replace(evaluation, violated_constraint_codes=("grant_permission",)),
        constraints,
    )
    assert not result.accepted
    assert "invented_constraint" in result.rejection_codes


def test_unknown_authority_fields_are_malformed_and_fail_open() -> None:
    request, constraints, evaluation = _valid_preference()
    forbidden_fields = (
        "grant_permission",
        "account_session",
        "capability_id",
        "outcome_unknown",
        "enable_vision",
        "dispatch",
        "terminality",
        "goal_contract",
    )
    for field in forbidden_fields:
        mapping = _evaluation_mapping(evaluation)
        mapping[field] = "invented"
        result = validate_critic_evaluation(request, mapping, constraints)
        assert not result.accepted
        assert result.fallback_to_no_critic
        assert result.rejection_codes == ("malformed_critic_evaluation",)


def test_malformed_enum_array_and_revision_are_rejected_without_exception() -> None:
    request, constraints, evaluation = _valid_preference()
    mutations = (
        {"verdict": "dispatch_now"},
        {"rejected_candidate_ids": "candidate-primary"},
        {"evaluation_revision": "one"},
    )
    for mutation in mutations:
        mapping = _evaluation_mapping(evaluation)
        mapping.update(mutation)
        result = validate_critic_evaluation(request, mapping, constraints)
        assert not result.accepted
        assert result.fallback_to_no_critic


def test_verdict_shapes_are_strict() -> None:
    request, constraints, evaluation = _valid_preference()
    missing_candidate = validate_critic_evaluation(
        request,
        replace(evaluation, preferred_candidate_id=None),
        constraints,
    )
    missing_observation = validate_critic_evaluation(
        request,
        replace(
            evaluation,
            verdict=CriticVerdict.REQUIRE_OBSERVATION,
            preferred_candidate_id=None,
        ),
        constraints,
    )
    assert "preferred_candidate_required" in missing_candidate.rejection_codes
    assert "required_observation_missing" in missing_observation.rejection_codes


def test_overconfident_wrong_but_admissible_candidate_remains_advisory() -> None:
    request, constraints, evaluation = _valid_preference()
    overconfident = replace(
        evaluation,
        preferred_candidate_id="candidate-primary",
        confidence_class=DecisionConfidence.HIGH,
    )
    result = validate_critic_evaluation(request, overconfident, constraints)
    assert result.accepted
    assert result.evaluation == overconfident
    assert not hasattr(result, "executable_payload")


def test_authoritative_permission_account_and_unknown_state_mismatch_wins() -> None:
    request, constraints, evaluation = _valid_preference()
    mismatches = (
        replace(constraints, permission_state=constraints.permission_state.REQUIRED),
        replace(
            constraints,
            account_binding_state=constraints.account_binding_state.MISMATCH,
        ),
        replace(constraints, outcome_unknown_count=constraints.outcome_unknown_count + 1),
    )
    for mismatch in mismatches:
        result = validate_critic_evaluation(request, evaluation, mismatch)
        assert not result.accepted
        assert "authoritative_state_mismatch" in result.rejection_codes


def test_invalid_evaluation_never_fails_the_turn_contract() -> None:
    request, constraints = _request()
    result = validate_critic_evaluation(request, object(), constraints)  # type: ignore[arg-type]
    assert not result.accepted
    assert result.fallback_to_no_critic
    assert result.evaluation is None
