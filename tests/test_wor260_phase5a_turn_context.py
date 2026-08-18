from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from osia.adaptive_reasoning_runtime import (
    BoundReasoningContext,
    build_bound_reasoning_context,
    public_structure_field_names,
    validate_bound_reasoning_context,
)
from osia.execution_strategy import build_execution_strategy


def _build(*, turn_id="turn-a", revision=2, conversation="conversation-a", planner=""):
    strategy = build_execution_strategy("Read the current catalog item")
    terminality = {
        "turn_id": turn_id,
        "proven_postconditions": {},
        "typed_evidence": (),
        "active_attempts": {},
        "permission_state": "allowed",
        "evidence_revision": revision,
    }
    return build_bound_reasoning_context(
        semantic_mission=strategy.mission,
        terminality_snapshot=terminality,
        strategy_snapshot=strategy.snapshot(),
        provider_id="openai",
        model_id="gpt-5.5",
        conversation_id=conversation,
        planner_context=planner,
    )


def test_context_is_immutable_compact_and_contains_no_raw_reasoning():
    context = _build()
    assert isinstance(context, BoundReasoningContext)
    with pytest.raises(FrozenInstanceError):
        context.turn_id = "changed"

    names = set(public_structure_field_names()["binding"])
    assert {
        "turn_id", "revision", "goal_fingerprint", "provider_id",
        "model_id", "conversation_id", "decision_context",
    }.issubset(names)
    assert names.isdisjoint({
        "messages", "prompt", "conversation", "chain_of_thought", "cot",
        "raw_reasoning", "provider_response",
    })


@pytest.mark.parametrize(
    "changes",
    [
        {"turn_id": "turn-b"},
        {"revision": 3},
        {"provider_id": "groq"},
        {"model_id": "other-model"},
        {"conversation_id": "conversation-b"},
    ],
)
def test_exact_turn_revision_pair_and_conversation_binding(changes):
    context = _build()
    coordinates = {
        "turn_id": "turn-a",
        "revision": 2,
        "provider_id": "openai",
        "model_id": "gpt-5.5",
        "conversation_id": "conversation-a",
    }
    assert validate_bound_reasoning_context(context, **coordinates)
    coordinates.update(changes)
    assert not validate_bound_reasoning_context(context, **coordinates)


def test_only_bounded_phase3_json_is_consumed():
    hint = (
        "legacy secret prose must not be copied\n"
        "[Adaptive planner advisory for the next provider decision only. "
        "Structured hint: {\"event\":\"adaptive_phase3_next_strategy_hint\","
        "\"recommended_strategy_family\":\"direct_api\","
        "\"recommended_capability_id\":\"api:read\","
        "\"hard_constraints\":[\"account exact\"]}]"
    )
    context = _build(planner=hint)

    assert context.source == "phase3_planner"
    assert context.decision_context.exact_direct_capability is True
    assert "legacy secret prose" not in repr(context)


def test_insufficient_or_malformed_state_returns_no_context():
    assert build_bound_reasoning_context(
        semantic_mission=object(),
        terminality_snapshot={},
        strategy_snapshot={},
        provider_id="openai",
        model_id="gpt-5.5",
    ) is None
