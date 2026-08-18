"""Controlled WOR-260 Phase-5A adaptive-reasoning integration.

The integration is deterministic, default-off, and limited to the reasoning
parameter of an already-planned provider request.  It owns no permissions,
dispatch, identity, outcome, modality, or terminality decisions and performs no
provider call or persistence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any, Mapping

from osia.adaptive_reasoning import (
    AmbiguityLevel,
    GoalComplexity,
    Irreversibility,
    OutcomeUnknownComplexity,
    ProgressSignal,
    ProviderParameterBehavior,
    ReasoningDecision,
    ReasoningDecisionContext,
    ReasoningPhase,
    SideEffectRisk,
    capability_from_current_registry,
    determine_reasoning_decision,
)


ADAPTIVE_REASONING_GATE_ENV = "OSIA_ADAPTIVE_REASONING_AUTO"
ADAPTIVE_REASONING_BINDING_SCHEMA = "wor260-phase5a-turn-context-v1"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_UNKNOWN = frozenset({"", "unknown", "none", "null", "unavailable"})
_DIRECT_MARKERS = (
    "direct", "api", "mcp", "deep_link", "dom", "structured", "native_tool",
)
_CONTRADICTION_MARKERS = ("contradict", "conflict", "mismatch")
_STALE_MARKERS = ("stale", "invalidated_ref")


@dataclass(frozen=True)
class BoundReasoningContext:
    schema: str
    turn_id: str
    revision: int
    goal_fingerprint: str
    provider_id: str
    model_id: str
    conversation_id: str
    source: str
    decision_context: ReasoningDecisionContext
    last_decision: ReasoningDecision | None = None


@dataclass(frozen=True)
class AdaptiveReasoningIntegration:
    request: Mapping[str, Any]
    bound_context: BoundReasoningContext | None
    decision: ReasoningDecision | None
    applied: bool
    reason_code: str


def adaptive_reasoning_auto_enabled() -> bool:
    """Return true only for the explicit internal/test gate values."""

    return os.environ.get(ADAPTIVE_REASONING_GATE_ENV, "").strip().casefold() in _TRUE_VALUES


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _known(value: object) -> bool:
    return _clean(value).casefold() not in _UNKNOWN


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _state_revision(
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
) -> int:
    return max(
        _safe_int(terminality_snapshot.get("evidence_revision")),
        _safe_int(terminality_snapshot.get("progress_revision")),
        _safe_int(terminality_snapshot.get("strategy_attempt_revision")),
        _safe_int(strategy_snapshot.get("evidence_revision")),
    )


def reasoning_context_revision(
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
) -> int:
    """Return the canonical revision used for turn-context binding."""

    if not isinstance(terminality_snapshot, Mapping) or not isinstance(
        strategy_snapshot, Mapping
    ):
        return 0
    return _state_revision(terminality_snapshot, strategy_snapshot)


def _planner_hint(value: object) -> dict[str, object]:
    """Extract only the bounded Phase-3 JSON hint, never provider/user text."""

    text = str(value or "")
    marker = "Structured hint: "
    start = text.rfind(marker)
    if start < 0:
        return {}
    candidate = text[start + len(marker):].strip()
    if candidate.endswith("]"):
        candidate = candidate[:-1].rstrip()
    try:
        raw = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("event") != "adaptive_phase3_next_strategy_hint":
        return {}
    allowed = {
        "remaining_goal_node", "remaining_goal_nodes", "verified_postconditions",
        "unverified_postconditions", "recommended_strategy_family",
        "recommended_capability_id", "required_observation", "hard_constraints",
        "do_not_replay", "explanation_codes", "world_revision", "evidence_revision",
    }
    return {key: raw[key] for key in allowed if key in raw}


def _bounded_strings(value: object, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item for item in (_clean(raw)[:160] for raw in value[:maximum]) if item
    )


def _current_mechanism(
    strategy_snapshot: Mapping[str, object],
    planner_hint: Mapping[str, object],
) -> tuple[str, str, bool]:
    capability = _clean(planner_hint.get("recommended_capability_id"))
    family = _clean(planner_hint.get("recommended_strategy_family"))
    current = _clean(strategy_snapshot.get("current_mechanism"))
    if not capability:
        capability = current
    mechanisms = tuple(strategy_snapshot.get("mechanisms") or ())
    selected = next((
        item for item in mechanisms
        if isinstance(item, Mapping)
        and _clean(item.get("mechanism_id")) == capability
    ), None)
    if not family and isinstance(selected, Mapping):
        family = _clean(selected.get("label") or selected.get("reason_selected"))
    mutation = bool(selected.get("mutation")) if isinstance(selected, Mapping) else False
    return capability, family, mutation


def _phase(
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
    planner_hint: Mapping[str, object],
) -> ReasoningPhase:
    if terminality_snapshot.get("unknown_effects"):
        return ReasoningPhase.RECONCILIATION
    next_family = _clean(planner_hint.get("recommended_strategy_family")).casefold()
    if "reconcile" in next_family:
        return ReasoningPhase.RECONCILIATION
    if strategy_snapshot.get("terminal_blocker"):
        return ReasoningPhase.TERMINAL_BLOCK_REVIEW
    attempts = tuple(strategy_snapshot.get("attempts") or ())
    if attempts or _safe_int(terminality_snapshot.get("strategy_attempt_revision")):
        return ReasoningPhase.REPLANNING
    return ReasoningPhase.INITIAL_PLANNING


def _risk(mutation: bool, phase: ReasoningPhase) -> tuple[SideEffectRisk, Irreversibility]:
    if not mutation:
        return SideEffectRisk.NONE, Irreversibility.NONE
    if phase == ReasoningPhase.RECONCILIATION:
        return SideEffectRisk.HIGH, Irreversibility.DIFFICULT
    return SideEffectRisk.MODERATE, Irreversibility.REVERSIBLE


def _previous_decision(
    previous: BoundReasoningContext | None,
    *,
    turn_id: str,
    provider_id: str,
    model_id: str,
    conversation_id: str,
    revision: int,
) -> ReasoningDecision | None:
    if not validate_bound_reasoning_context(
        previous,
        turn_id=turn_id,
        revision=previous.revision if previous is not None else revision,
        provider_id=provider_id,
        model_id=model_id,
        conversation_id=conversation_id,
    ):
        return None
    if previous is None or previous.revision > revision:
        return None
    return previous.last_decision


def build_bound_reasoning_context(
    *,
    semantic_mission: object,
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
    provider_id: str,
    model_id: str,
    conversation_id: str = "",
    planner_context: str = "",
    previous_context: BoundReasoningContext | None = None,
) -> BoundReasoningContext | None:
    """Build an immutable, turn-owned context from canonical snapshots."""

    if not isinstance(terminality_snapshot, Mapping) or not isinstance(
        strategy_snapshot, Mapping
    ):
        return None
    turn_id = _clean(terminality_snapshot.get("turn_id"))
    provider = _clean(provider_id).casefold()
    model = _clean(model_id)
    conversation = _clean(conversation_id)
    if not turn_id or not provider or not model or semantic_mission is None:
        return None

    try:
        from osia.capability_adapters import adapt_model_provider_capabilities
        from osia.goal_contract import goal_contract_from_semantic_mission
        from osia.verifier import verify_goal_node
        from osia.world_state import WorldStateInputs, build_world_state_view

        goal_contract = goal_contract_from_semantic_mission(semantic_mission)
        model_capability = adapt_model_provider_capabilities(provider, model)
        revision = _state_revision(terminality_snapshot, strategy_snapshot)
        world = build_world_state_view(
            goal_contract,
            terminality_snapshot=terminality_snapshot,
            strategy_snapshot=strategy_snapshot,
            inputs=WorldStateInputs(
                effective_model_capabilities=(
                    ("text", "image")
                    if model_capability.vision_supported is True else ("text",)
                ),
                vision_available=model_capability.vision_supported,
                revision=max(1, revision),
            ),
        )
        if not world.active_goal_node_id:
            return None
        node = next(
            item for item in goal_contract.nodes
            if item.node_id == world.active_goal_node_id
        )
        verification = verify_goal_node(node, world, world.typed_evidence)
        capability = capability_from_current_registry(provider, model)
        hint = _planner_hint(planner_context)
        phase = _phase(terminality_snapshot, strategy_snapshot, hint)
        selected_capability, selected_family, mutation = _current_mechanism(
            strategy_snapshot, hint
        )
        side_effect_risk, irreversibility = _risk(mutation, phase)
        frontier = _bounded_strings(
            strategy_snapshot.get("alternatives_still_applicable")
        )
        hard_constraints = _bounded_strings(hint.get("hard_constraints"))
        explanation_codes = _bounded_strings(hint.get("explanation_codes"))
        typed_evidence = tuple(terminality_snapshot.get("typed_evidence") or ())
        evidence_text = tuple(
            _clean(item).casefold() if not isinstance(item, Mapping) else
            " ".join(_clean(value).casefold() for value in item.values())
            for item in typed_evidence[-64:]
        )
        contradiction_count = sum(
            any(marker in item for marker in _CONTRADICTION_MARKERS)
            for item in evidence_text
        )
        contradiction_count += sum(
            any(marker in code.casefold() for marker in _CONTRADICTION_MARKERS)
            for code in explanation_codes
        )
        stale_count = len(tuple(strategy_snapshot.get("stale_mechanisms") or ()))
        stale_count += sum(
            any(marker in code.casefold() for marker in _STALE_MARKERS)
            for code in explanation_codes
        )
        unknown_count = len(tuple(terminality_snapshot.get("unknown_effects") or ()))
        if unknown_count > 1 or unknown_count and contradiction_count:
            unknown_complexity = OutcomeUnknownComplexity.COMPLEX
        elif unknown_count:
            unknown_complexity = OutcomeUnknownComplexity.SIMPLE
        else:
            unknown_complexity = OutcomeUnknownComplexity.NONE
        replan_count = max(
            len(tuple(strategy_snapshot.get("attempts") or ())),
            _safe_int(terminality_snapshot.get("strategy_attempt_revision")),
        )
        previous_decision = _previous_decision(
            previous_context,
            turn_id=turn_id,
            provider_id=provider,
            model_id=model,
            conversation_id=conversation,
            revision=revision,
        )
        previous_revision = previous_context.revision if previous_context else -1
        if previous_context is not None and previous_revision < revision:
            progress = ProgressSignal.CLEAR
        elif replan_count > 1:
            progress = ProgressSignal.STALLED
        elif replan_count:
            progress = ProgressSignal.PARTIAL
        else:
            progress = ProgressSignal.UNKNOWN
        applications = {
            _clean(getattr(item, "required_application", "")).casefold()
            for item in getattr(goal_contract, "nodes", ())
            if _known(getattr(item, "required_application", ""))
        }
        remaining_count = len(world.remaining_goal_node_ids)
        dependency_count = max(0, remaining_count - 1)
        if dependency_count >= 2:
            goal_complexity = GoalComplexity.DEPENDENCY_HEAVY
        elif dependency_count or remaining_count >= 2:
            goal_complexity = GoalComplexity.MULTI_STEP
        else:
            goal_complexity = GoalComplexity.SIMPLE
        direct = any(
            marker in f"{selected_capability} {selected_family}".casefold()
            for marker in _DIRECT_MARKERS
        )
        candidate_count = len(frontier) or int(bool(selected_capability))
        unresolved_permissions = int(
            _clean(terminality_snapshot.get("permission_state")).casefold()
            == "pending"
        )
        account_unknown = int(any(
            "account" in item.casefold() for item in hard_constraints
        ))
        incomplete = sum(not _known(value) for value in (
            world.current_application,
            world.current_surface,
            world.current_account_profile,
        ))
        context = ReasoningDecisionContext(
            schema_version="1.0",
            provider_id=provider,
            model_id=model,
            user_reasoning_mode="auto",
            explicit_turn_reasoning=None,
            supported_reasoning_values=capability.supported_osia_values,
            provider_default_behavior=capability.provider_default_behavior,
            phase=phase,
            goal_complexity=goal_complexity,
            candidate_count=candidate_count,
            close_candidate_count=0,
            ambiguity_level=(
                AmbiguityLevel.HIGH if contradiction_count
                else AmbiguityLevel.MEDIUM if len(hard_constraints) >= 2
                else AmbiguityLevel.LOW if hard_constraints
                else AmbiguityLevel.NONE
            ),
            contradiction_count=contradiction_count,
            unresolved_constraint_count=len(hard_constraints),
            remaining_goal_node_count=remaining_count,
            replan_count=replan_count,
            repeated_replan_count=max(0, replan_count - 1),
            stale_ref_count=stale_count,
            outcome_unknown_count=unknown_count,
            outcome_unknown_complexity=unknown_complexity,
            verifier_status=_clean(getattr(verification, "status", "unverified")),
            required_observation_count=int(bool(_clean(hint.get("required_observation")))),
            evidence_strength=_clean(
                getattr(verification, "verifier_strength", "none")
            ),
            evidence_conflict=bool(contradiction_count),
            side_effect_risk=side_effect_risk,
            irreversibility=irreversibility,
            cross_application_count=len(applications),
            previous_reasoning_decision=previous_decision,
            progress_signal=progress,
            exact_candidate_count=int(candidate_count == 1),
            exact_direct_capability=direct,
            semantic_effect_exact=bool(direct and not hard_constraints),
            surface_account_exact=bool(
                _known(world.current_surface) and _known(world.current_account_profile)
            ),
            unresolved_permission_count=unresolved_permissions,
            unresolved_account_binding_count=account_unknown,
            dependency_count=dependency_count,
            incomplete_world_state_count=incomplete,
        )
        return BoundReasoningContext(
            schema=ADAPTIVE_REASONING_BINDING_SCHEMA,
            turn_id=turn_id,
            revision=revision,
            goal_fingerprint=_clean(goal_contract.fingerprint),
            provider_id=provider,
            model_id=model,
            conversation_id=conversation,
            source="phase3_planner" if hint else "canonical_snapshots",
            decision_context=context,
        )
    except Exception:
        return None


def validate_bound_reasoning_context(
    context: BoundReasoningContext | None,
    *,
    turn_id: str,
    revision: int,
    provider_id: str,
    model_id: str,
    conversation_id: str = "",
) -> bool:
    """Reject stale, cross-turn, cross-conversation, or cross-pair state."""

    return bool(
        isinstance(context, BoundReasoningContext)
        and context.schema == ADAPTIVE_REASONING_BINDING_SCHEMA
        and context.turn_id == _clean(turn_id)
        and context.revision == _safe_int(revision)
        and context.provider_id == _clean(provider_id).casefold()
        and context.model_id == _clean(model_id)
        and context.conversation_id == _clean(conversation_id)
        and context.decision_context.provider_id == context.provider_id
        and context.decision_context.model_id == context.model_id
    )


def integrate_adaptive_reasoning(
    legacy_request: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    bound_context: BoundReasoningContext | None,
    turn_id: str,
    revision: int,
    conversation_id: str = "",
) -> AdaptiveReasoningIntegration:
    """Apply Adaptive Auto through the existing WOR-209 request builder."""

    legacy = dict(legacy_request)
    diagnostic = dict(legacy.get("diagnostic") or {})
    if not adaptive_reasoning_auto_enabled():
        return AdaptiveReasoningIntegration(legacy, bound_context, None, False, "gate_off")
    if (
        diagnostic.get("mapping_state") != "accepted"
        or _clean(diagnostic.get("requested_reasoning")).casefold() != "auto"
        or _clean(diagnostic.get("effective_reasoning")).casefold() != "auto"
    ):
        return AdaptiveReasoningIntegration(
            legacy, bound_context, None, False, "explicit_or_rejected_reasoning"
        )
    if not validate_bound_reasoning_context(
        bound_context,
        turn_id=turn_id,
        revision=revision,
        provider_id=provider_id,
        model_id=model_id,
        conversation_id=conversation_id,
    ):
        return AdaptiveReasoningIntegration(
            legacy, None, None, False, "context_binding_mismatch"
        )
    try:
        capability = capability_from_current_registry(provider_id, model_id)
        context = replace(
            bound_context.decision_context,
            user_reasoning_mode="auto",
            explicit_turn_reasoning=None,
            supported_reasoning_values=capability.supported_osia_values,
            provider_default_behavior=capability.provider_default_behavior,
        )
        decision = determine_reasoning_decision(context, capability)
        updated = replace(bound_context, decision_context=context, last_decision=decision)
        if (
            not decision.adaptive_allowed
            or decision.provider_parameter_behavior
            != ProviderParameterBehavior.EMIT_NATIVE_VALUE
            or not decision.selected_osia_level
        ):
            return AdaptiveReasoningIntegration(
                legacy, updated, decision, False,
                decision.unsupported_reason or "provider_default_preserved",
            )
        value = capability.value_for(decision.selected_osia_level)
        if value is None or value.native_value != decision.selected_native_value:
            return AdaptiveReasoningIntegration(
                legacy, updated, decision, False, "native_value_validation_failed"
            )

        from osia.reasoning import build_reasoning_request

        selected_request = build_reasoning_request(
            provider_id,
            model_id,
            settings={
                "default": decision.selected_osia_level,
                "providers": {},
                "models": {},
            },
        )
        selected_diagnostic = dict(selected_request.get("diagnostic") or {})
        if (
            selected_diagnostic.get("mapping_state") != "accepted"
            or selected_diagnostic.get("effective_reasoning")
            != decision.selected_osia_level
            or selected_diagnostic.get("provider_parameter_name")
            != value.provider_parameter_name
            or selected_diagnostic.get("provider_parameter_value")
            != value.native_value
        ):
            return AdaptiveReasoningIntegration(
                legacy, updated, decision, False, "wor209_mapping_validation_failed"
            )
        selected_diagnostic.update({
            "requested_reasoning": "auto",
            "effective_reasoning": decision.selected_osia_level,
            "selection_source": "adaptive_auto",
            "adaptive_difficulty": decision.difficulty_class.value,
            "adaptive_context_source": bound_context.source,
            "adaptive_context_revision": bound_context.revision,
        })
        return AdaptiveReasoningIntegration(
            {
                "kwargs": dict(selected_request.get("kwargs") or {}),
                "diagnostic": selected_diagnostic,
            },
            updated,
            decision,
            True,
            "adaptive_supported_value_applied",
        )
    except Exception:
        return AdaptiveReasoningIntegration(
            legacy, bound_context, None, False, "adaptive_error_legacy_fallback"
        )


def public_structure_field_names() -> dict[str, tuple[str, ...]]:
    return {
        "binding": tuple(BoundReasoningContext.__dataclass_fields__),
        "integration": tuple(AdaptiveReasoningIntegration.__dataclass_fields__),
    }


__all__ = [
    "ADAPTIVE_REASONING_BINDING_SCHEMA",
    "ADAPTIVE_REASONING_GATE_ENV",
    "AdaptiveReasoningIntegration",
    "BoundReasoningContext",
    "adaptive_reasoning_auto_enabled",
    "build_bound_reasoning_context",
    "integrate_adaptive_reasoning",
    "public_structure_field_names",
    "reasoning_context_revision",
    "validate_bound_reasoning_context",
]
