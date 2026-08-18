from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields

import pytest

from benchmarks.wor260.phase4b import _request_for, critic_scenarios
from osia.adaptive_critic import CriticEvaluation, CriticRequest, public_structure_field_names


def _request() -> CriticRequest:
    return _request_for(critic_scenarios()[0])


def test_request_is_compact_structured_and_frozen() -> None:
    request = _request()
    assert request.candidate_plans
    assert request.typed_evidence_summaries
    assert request.evaluation_criteria
    with pytest.raises(FrozenInstanceError):
        request.turn_id = "different"  # type: ignore[misc]


def test_request_excludes_raw_conversation_provider_payload_and_secrets() -> None:
    names = {item.name for item in fields(CriticRequest)}
    forbidden = {
        "messages",
        "conversation",
        "raw_conversation",
        "provider_payload",
        "api_key",
        "access_token",
        "pairing_token",
        "filesystem_contents",
        "tools",
    }
    assert names.isdisjoint(forbidden)


def test_request_payload_contains_only_opaque_ids_and_compact_summaries() -> None:
    payload = str(asdict(_request())).lower()
    sentinels = (
        "secret-api-key",
        "pairing-token-secret",
        "unrelated conversation",
        "c:\\users\\private",
        "authorization: bearer",
        "chain of thought",
    )
    assert not any(item in payload for item in sentinels)


def test_evaluation_contract_has_no_execution_authority_fields() -> None:
    names = {item.name for item in fields(CriticEvaluation)}
    forbidden = {
        "tool_name",
        "tool_args",
        "dispatch",
        "grant_permission",
        "permit",
        "goal_contract",
        "outcome_unknown",
        "terminality",
        "mark_success_verified",
        "mark_blocked_verified",
        "enable_vision",
    }
    assert names.isdisjoint(forbidden)


def test_no_structure_contains_private_reasoning_or_free_text_justification() -> None:
    names = public_structure_field_names()
    flattened = {field for values in names.values() for field in values}
    assert "chain_of_thought" not in flattened
    assert "private_reasoning" not in flattened
    assert "hidden_reasoning" not in flattened
    assert "justification" not in flattened


def test_request_rejects_candidate_that_is_also_hard_rejected() -> None:
    request = _request()
    from dataclasses import replace
    from osia.adaptive_critic import CandidateRejectionSummary

    with pytest.raises(ValueError, match="rejected_candidates"):
        replace(
            request,
            already_rejected_candidates=(
                CandidateRejectionSummary(
                    candidate_id=request.candidate_plans[0].candidate_id,
                    reason_codes=("permission_disallowed",),
                ),
            ),
        )
