"""Controlled WOR-260 Phase-3 bridge from verified state to one advisory hint.

The bridge is default-off and fail-open to the existing legacy continuation.
It never dispatches, creates a permit/grant, mutates terminality, calls a model,
persists planner state, or returns an executable tool payload.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from osia.tool_contracts import SideEffectClass, get_tool_spec
from osia.verifier import (
    VerificationResult,
    VerificationStatus,
    verify_goal_node,
)
from osia.world_state import (
    EffectState,
    PermissionFact,
    StateFact,
    WorldStateInputs,
    WorldStateView,
    build_world_state_view,
    goal_node_equivalence_class,
    postcondition_id,
)


_adaptive_planner = importlib.import_module("osia.adaptive_planner")
AdaptivePlan = _adaptive_planner.AdaptivePlan
LegacyDecisionSnapshot = _adaptive_planner.LegacyDecisionSnapshot
PlannerLimits = _adaptive_planner.PlannerLimits
plan_shadow = _adaptive_planner.plan_shadow

_capability_adapters = importlib.import_module("osia.capability_adapters")
AdaptiveCapabilityDescriptor = _capability_adapters.AdaptiveCapabilityDescriptor
CAPABILITY_DESCRIPTOR_SCHEMA_VERSION = (
    _capability_adapters.CAPABILITY_DESCRIPTOR_SCHEMA_VERSION
)
CapabilityFamily = _capability_adapters.CapabilityFamily
DeterministicConstraint = _capability_adapters.DeterministicConstraint
ModalityRequirement = _capability_adapters.ModalityRequirement
PermissionRequirement = _capability_adapters.PermissionRequirement
Reversibility = _capability_adapters.Reversibility
RuntimeAvailability = _capability_adapters.RuntimeAvailability
UNKNOWN_METADATA = _capability_adapters.UNKNOWN_METADATA
VerifierStrength = _capability_adapters.VerifierStrength
adapt_model_provider_capabilities = (
    _capability_adapters.adapt_model_provider_capabilities
)
adapt_tool_spec = _capability_adapters.adapt_tool_spec

_capability_search = importlib.import_module("osia.capability_search")
AttemptEffectState = _capability_search.AttemptEffectState
CapabilityPermissionState = _capability_search.CapabilityPermissionState
CapabilityRuntimeState = _capability_search.CapabilityRuntimeState
PermissionState = _capability_search.PermissionState
ShadowPlanningContext = _capability_search.ShadowPlanningContext
StaleReference = _capability_search.StaleReference
TypedObservation = _capability_search.TypedObservation

_goal_contract = importlib.import_module("osia.goal_contract")
GoalContract = _goal_contract.GoalContract
GoalNodeContract = _goal_contract.GoalNodeContract
NOT_SPECIFIED = _goal_contract.NOT_SPECIFIED
goal_contract_from_semantic_mission = (
    _goal_contract.goal_contract_from_semantic_mission
)


PHASE3_GATE_ENV = "OSIA_ADAPTIVE_INTELLIGENCE_PHASE3"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_MUTATING_EFFECTS = frozenset({
    SideEffectClass.REVERSIBLE_LOCAL.value,
    SideEffectClass.PERSISTENT_LOCAL.value,
    SideEffectClass.EXTERNAL_MUTATION.value,
    SideEffectClass.IRREVERSIBLE_EXTERNAL.value,
    SideEffectClass.DYNAMICALLY_CLASSIFIED.value,
    SideEffectClass.UNKNOWN_HIGH_RISK.value,
})
_BLOCKING_UNRESOLVED_MARKERS = (
    "account_binding_unknown",
    "vision_authority_unknown",
    "runtime_unknown",
    "surface_binding_unknown",
)


@dataclass(frozen=True)
class AdaptiveContinuationProposal:
    selected_plan: AdaptivePlan | None
    selected_next_capability: str
    selected_strategy_family: str
    required_observation: str
    unresolved_constraints: tuple[str, ...]
    fallback_to_legacy: bool
    explanation_codes: tuple[str, ...]
    remaining_goal_node_ids: tuple[str, ...] = ()
    verified_postcondition_ids: tuple[str, ...] = ()
    unverified_postcondition_ids: tuple[str, ...] = ()
    do_not_replay_capability_ids: tuple[str, ...] = ()
    readiness_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdaptiveRuntimeEvaluation:
    world_state: WorldStateView
    verification_result: VerificationResult
    proposal: AdaptiveContinuationProposal


@dataclass(frozen=True)
class AdaptiveTerminalSuccessAssessment:
    eligible: bool
    world_state: WorldStateView
    verification_results: tuple[VerificationResult, ...]
    verified_postcondition_ids: tuple[str, ...]
    unverified_postcondition_ids: tuple[str, ...]
    unresolved_effect_attempt_ids: tuple[str, ...]
    explanation_codes: tuple[str, ...]


def adaptive_intelligence_phase3_enabled() -> bool:
    return os.environ.get(PHASE3_GATE_ENV, "").strip().casefold() in _TRUE_VALUES


def _clean(value: object, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _normal(value: object) -> str:
    return _clean(value).casefold()


def _known(value: object) -> bool:
    return _normal(value) not in {"", UNKNOWN_METADATA, NOT_SPECIFIED}


def _constraint(name: str, value: object, authority: str) -> DeterministicConstraint:
    return DeterministicConstraint(
        name=name,
        value=_clean(value, fallback=UNKNOWN_METADATA),
        source_authority=authority,
    )


def _node_map(contract: GoalContract) -> dict[str, GoalNodeContract]:
    return {item.node_id: item for item in contract.nodes}


def _mechanism_family(layer: int) -> tuple[str, tuple[str, ...], str]:
    mapping = {
        0: (
            CapabilityFamily.OSIA_NATIVE.value,
            (ModalityRequirement.NONE.value,),
            VerifierStrength.STRUCTURED_RESULT.value,
        ),
        1: (
            CapabilityFamily.DEEP_LINK.value,
            (ModalityRequirement.NONE.value,),
            VerifierStrength.STRUCTURED_RESULT.value,
        ),
        2: (
            CapabilityFamily.BROWSER_DOM.value,
            (ModalityRequirement.BROWSER_DOM.value,),
            VerifierStrength.OBSERVABLE_STATE.value,
        ),
        3: (
            CapabilityFamily.DESKTOP_UIA.value,
            (ModalityRequirement.ACCESSIBILITY.value,),
            VerifierStrength.OBSERVABLE_STATE.value,
        ),
        4: (
            CapabilityFamily.DESKTOP_KEYBOARD.value,
            (ModalityRequirement.KEYBOARD_WITH_OBSERVABLE_FOCUS.value,),
            VerifierStrength.OBSERVABLE_STATE.value,
        ),
        5: (
            CapabilityFamily.VISION_ACTUATION_LOCAL.value,
            (ModalityRequirement.VISION.value, ModalityRequirement.LOCAL_REGION.value),
            VerifierStrength.WEAK_UNSTRUCTURED.value,
        ),
        6: (
            CapabilityFamily.VISION_ACTUATION_GLOBAL.value,
            (ModalityRequirement.VISION.value, ModalityRequirement.GLOBAL_COORDINATES.value),
            VerifierStrength.WEAK_UNSTRUCTURED.value,
        ),
    }
    return mapping.get(int(layer), (
        CapabilityFamily.OSIA_NATIVE.value,
        (ModalityRequirement.UNKNOWN.value,),
        VerifierStrength.WEAK_UNSTRUCTURED.value,
    ))


def _reversibility(effect: str) -> str:
    if effect == SideEffectClass.READ_ONLY.value:
        return Reversibility.NOT_APPLICABLE.value
    if effect == SideEffectClass.REVERSIBLE_LOCAL.value:
        return Reversibility.REVERSIBLE.value
    if effect == SideEffectClass.PERSISTENT_LOCAL.value:
        return Reversibility.CONDITIONALLY_REVERSIBLE.value
    if effect in {
        SideEffectClass.EXTERNAL_MUTATION.value,
        SideEffectClass.IRREVERSIBLE_EXTERNAL.value,
    }:
        return Reversibility.IRREVERSIBLE.value
    return Reversibility.UNKNOWN.value


def _descriptor_for_mechanism(
    raw: Mapping[str, object],
    node: GoalNodeContract,
    *,
    frontier: frozenset[str],
) -> AdaptiveCapabilityDescriptor:
    capability_id = _clean(raw.get("mechanism_id"))
    layer = int(raw.get("layer") or 0)
    family, modalities, verifier = _mechanism_family(layer)
    authority = "osia.execution_strategy.ExecutionStrategy.snapshot"
    effect = node.side_effect_class
    surface = _clean(raw.get("surface")) or node.required_surface
    account = _clean(raw.get("account")) or node.required_account_profile
    application = _clean(raw.get("application")) or node.application
    semantic_effects = tuple(dict.fromkeys(
        item for item in (
            node.objective,
            node.target,
            node.application,
            node.required_surface,
        ) if _known(item)
    ))
    constraints = [
        _constraint("goal_node_id", node.node_id, authority),
        _constraint("step_role", "act", authority),
        _constraint("intended_action", node.objective, authority),
        _constraint("execution_strategy_layer", layer, authority),
        _constraint("legacy_covers_goal", raw.get("covers_goal"), authority),
    ]
    for name, value in (
        ("exact_target", node.target),
        ("exact_application", application),
        ("exact_surface", surface),
        ("exact_account_profile", account),
    ):
        if _known(value):
            constraints.append(_constraint(name, value, authority))
    if family == CapabilityFamily.VISION_ACTUATION_GLOBAL.value:
        constraints.append(_constraint(
            "global_vision_last_resort", True, authority
        ))
    available = bool(raw.get("applicable", True)) and capability_id in frontier
    return AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=capability_id,
        family=family,
        semantic_inputs=(),
        semantic_effects=semantic_effects,
        preconditions=(),
        permission_requirements=(PermissionRequirement(
            category="existing_permission_policy",
            confirmation_policy="existing_policy_enforced",
        ),),
        supported_surfaces=(surface,) if _known(surface) else (UNKNOWN_METADATA,),
        required_account_profile=(
            account if _known(account) else UNKNOWN_METADATA
        ),
        modality_requirements=modalities,
        runtime_availability=(
            RuntimeAvailability.AVAILABLE.value
            if available else RuntimeAvailability.UNAVAILABLE.value
        ),
        verifier_strength=verifier,
        side_effect_class=effect,
        reversibility=_reversibility(effect),
        anti_replay_equivalence_class=goal_node_equivalence_class(node),
        deterministic_constraints=tuple(constraints),
        source_authority=authority,
        idempotence=UNKNOWN_METADATA,
        retry_policy=UNKNOWN_METADATA,
        description=_clean(raw.get("reason_selected"), fallback=capability_id),
        latency_class=f"legacy_layer_{layer}",
    )


def _read_only_descriptor(
    capability_id: str,
    node: GoalNodeContract,
    *,
    family: str,
    roles: tuple[str, ...],
    available_capability_ids: frozenset[str],
    vision_available: bool | None,
) -> AdaptiveCapabilityDescriptor | None:
    spec = get_tool_spec(capability_id)
    if spec is None or capability_id not in available_capability_ids:
        return None
    if family == CapabilityFamily.VISION_OBSERVATION.value and vision_available is not True:
        return None
    descriptor = adapt_tool_spec(
        spec,
        family=family,
        runtime_availability=RuntimeAvailability.AVAILABLE,
        source_authority=(
            "osia.tool_contracts.ToolSpec+WOR260Phase3GoalNodeBinding"
        ),
    )
    authority = descriptor.source_authority
    constraints = list(descriptor.deterministic_constraints)
    constraints.extend(
        _constraint("step_role", role, authority) for role in roles
    )
    for name, value in (
        ("goal_node_id", node.node_id),
        ("exact_target", node.target),
        ("exact_application", node.application),
        ("exact_surface", node.required_surface),
        ("exact_account_profile", node.required_account_profile),
        ("intended_action", node.objective),
    ):
        if name == "goal_node_id" or _known(value):
            constraints.append(_constraint(name, value, authority))
    modalities = (
        (ModalityRequirement.VISION.value,)
        if family == CapabilityFamily.VISION_OBSERVATION.value
        else (ModalityRequirement.ACCESSIBILITY.value,)
    )
    is_vision = family == CapabilityFamily.VISION_OBSERVATION.value
    return replace(
        descriptor,
        semantic_effects=tuple(dict.fromkeys((
            node.objective,
            node.target,
            (
                "visual observation and semantic verdict with optional target bbox"
                if is_vision else
                "structured accessibility state observation"
            ),
        ))),
        supported_surfaces=(
            (node.required_surface,)
            if _known(node.required_surface) else ("desktop",)
        ),
        required_account_profile=(
            node.required_account_profile
            if _known(node.required_account_profile) else UNKNOWN_METADATA
        ),
        modality_requirements=modalities,
        verifier_strength=VerifierStrength.OBSERVABLE_STATE.value,
        side_effect_class=SideEffectClass.READ_ONLY.value,
        reversibility=Reversibility.NOT_APPLICABLE.value,
        anti_replay_equivalence_class=goal_node_equivalence_class(node),
        deterministic_constraints=tuple(constraints),
        description=(
            "READ_ONLY approximate pixel evidence bound to one fresh local target; "
            "requires a Vision-capable model, respects privacy/capture protection, "
            "and may return an optional refinement bbox"
            if is_vision else
            "READ_ONLY structured accessibility evidence for the current goal node"
        ),
    )


def _reconciliation_descriptor(
    node: GoalNodeContract,
    capability_id: str,
) -> AdaptiveCapabilityDescriptor:
    authority = "osia.turn_terminality.TerminalityDecision"
    return AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=capability_id,
        family=CapabilityFamily.OSIA_NATIVE.value,
        semantic_inputs=(),
        semantic_effects=(node.objective, node.target, "reconcile unknown effect"),
        preconditions=("existing_unknown_effect",),
        permission_requirements=(PermissionRequirement(
            "none", "no_confirmation"
        ),),
        supported_surfaces=(
            node.required_surface if _known(node.required_surface) else UNKNOWN_METADATA,
        ),
        required_account_profile=(
            node.required_account_profile
            if _known(node.required_account_profile) else UNKNOWN_METADATA
        ),
        modality_requirements=(ModalityRequirement.NONE.value,),
        runtime_availability=RuntimeAvailability.AVAILABLE.value,
        verifier_strength=VerifierStrength.OBSERVABLE_STATE.value,
        side_effect_class=SideEffectClass.READ_ONLY.value,
        reversibility=Reversibility.NOT_APPLICABLE.value,
        anti_replay_equivalence_class=goal_node_equivalence_class(node),
        deterministic_constraints=(
            _constraint("goal_node_id", node.node_id, authority),
            _constraint("step_role", "reconcile", authority),
            _constraint("exact_target", node.target, authority),
            _constraint("intended_action", node.objective, authority),
        ),
        source_authority=authority,
        description="read-only reconciliation required by canonical terminality",
    )


def capability_descriptors_from_strategy_snapshot(
    goal_contract: GoalContract,
    strategy_snapshot: Mapping[str, object],
    *,
    available_capability_ids: Iterable[str] = (),
    vision_available: bool | None = None,
    legacy_next_strategy: str = "",
) -> tuple[AdaptiveCapabilityDescriptor, ...]:
    """Project only explicit strategy/tool facts; never discover or dispatch."""

    nodes = _node_map(goal_contract)
    active = next((
        item for item in goal_contract.nodes
        if _normal(item.node_id) not in {
            _normal(raw.get("node_id"))
            for raw in ((strategy_snapshot.get("mission") or {}).get("action_nodes") or ())
            if isinstance(raw, Mapping)
            and _normal(raw.get("status")) == "succeeded"
        }
    ), goal_contract.nodes[0])
    frontier = frozenset(
        _clean(item) for item in
        strategy_snapshot.get("alternatives_still_applicable", ())
        if _clean(item)
    )
    descriptors: list[AdaptiveCapabilityDescriptor] = []
    for raw in strategy_snapshot.get("mechanisms", ()):
        if not isinstance(raw, Mapping) or not _clean(raw.get("mechanism_id")):
            continue
        node = nodes.get(_clean(raw.get("node_id"))) or active
        descriptors.append(_descriptor_for_mechanism(
            raw, node, frontier=frontier
        ))
    available = frozenset(_clean(item) for item in available_capability_ids)
    resolver = _read_only_descriptor(
        "inspect_desktop_surfaces",
        active,
        family=CapabilityFamily.DESKTOP_UIA.value,
        roles=("resolve", "verify"),
        available_capability_ids=available,
        vision_available=vision_available,
    )
    if resolver is not None:
        descriptors.append(resolver)
    observer = _read_only_descriptor(
        "request_visual_observation",
        active,
        family=CapabilityFamily.VISION_OBSERVATION.value,
        roles=("observe", "verify"),
        available_capability_ids=available,
        vision_available=vision_available,
    )
    if observer is not None:
        descriptors.append(observer)
    if legacy_next_strategy == "reconcile_unknown_read_only":
        descriptors.append(_reconciliation_descriptor(
            active, legacy_next_strategy
        ))
    unique = {item.capability_id: item for item in descriptors}
    return tuple(unique[key] for key in sorted(unique))


def _remaining_contract(
    contract: GoalContract,
    world: WorldStateView,
    verification: VerificationResult,
) -> GoalContract:
    satisfied = set(world.verified_postconditions)
    satisfied.update(verification.satisfied_postcondition_ids)
    completed = set(world.completed_goal_node_ids)
    if verification.status == VerificationStatus.VERIFIED.value:
        completed.add(verification.goal_node_id)
    nodes: list[GoalNodeContract] = []
    for node in contract.nodes:
        if node.node_id in completed:
            nodes.append(node)
            continue
        remaining_postconditions = tuple(
            item for item in node.observable_postconditions
            if postcondition_id(node.node_id, item.description) not in satisfied
        )
        nodes.append(replace(
            node, observable_postconditions=remaining_postconditions
        ))
    provisional = replace(contract, nodes=tuple(nodes), fingerprint="")
    raw = json.dumps(
        provisional.semantic_dict(), ensure_ascii=True,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return replace(
        provisional, fingerprint=hashlib.sha256(raw).hexdigest()
    )


def _permission_bindings(
    world: WorldStateView,
) -> tuple[CapabilityPermissionState, ...]:
    values = []
    for item in world.permission_states:
        state = _normal(item.state)
        if state not in {entry.value for entry in PermissionState}:
            state = PermissionState.UNKNOWN.value
        values.append(CapabilityPermissionState(item.capability_id, state))
    return tuple(values)


def _shadow_context(
    contract: GoalContract,
    world: WorldStateView,
    verification: VerificationResult,
    descriptors: tuple[AdaptiveCapabilityDescriptor, ...],
) -> ShadowPlanningContext:
    completed = set(world.completed_goal_node_ids)
    if verification.status == VerificationStatus.VERIFIED.value:
        completed.add(verification.goal_node_id)
    remaining = tuple(
        item.node_id for item in contract.nodes
        if item.node_id not in completed
    )
    active = (
        world.active_goal_node_id
        if world.active_goal_node_id in remaining
        else remaining[0] if remaining else contract.nodes[-1].node_id
    )
    resolver_id = (
        "inspect_desktop_surfaces"
        if any(item.capability_id == "inspect_desktop_surfaces" for item in descriptors)
        else ""
    )
    stale = tuple(
        StaleReference(item, item, resolver_id)
        for item in world.stale_refs
    )
    effects = tuple(
        AttemptEffectState(
            item.capability_id,
            item.equivalence_class,
            "outcome_unknown" if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
            else item.effect_state,
        )
        for item in world.known_effect_states
        if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
    )
    runtime = tuple(
        CapabilityRuntimeState(
            item.capability_id,
            RuntimeAvailability.UNAVAILABLE.value
            if item.capability_id in
            world.strategy_snapshot.failed_before_effect_capability_ids
            else item.runtime_availability,
        )
        for item in descriptors
    )
    modalities = (
        ("text", "image") if world.vision_available is True
        else ("text",) if world.vision_available is False else ()
    )
    return ShadowPlanningContext(
        goal_contract=contract,
        active_goal_node_id=active,
        capabilities=descriptors,
        current_application=world.current_application,
        current_surface=world.current_surface,
        current_account_profile=world.current_account_profile,
        effective_model_modalities=modalities,
        authoritative_vision_available=world.vision_available,
        permission_states=_permission_bindings(world),
        runtime_states=runtime,
        stale_references=stale,
        accessibility_quality=world.accessibility_quality,
        previous_attempt_effects=effects,
        completed_goal_node_ids=tuple(
            item.node_id for item in contract.nodes if item.node_id in completed
        ),
        typed_observations=tuple(
            TypedObservation(item.name, item.value, item.source_authority)
            for item in world.typed_observations
        ),
    )


def _fallback_proposal(
    world: WorldStateView,
    verification: VerificationResult,
    *codes: str,
) -> AdaptiveContinuationProposal:
    return AdaptiveContinuationProposal(
        selected_plan=None,
        selected_next_capability="",
        selected_strategy_family="",
        required_observation=(
            verification.required_observation_types[0]
            if verification.required_observation_types else ""
        ),
        unresolved_constraints=verification.unsatisfied_postcondition_ids,
        fallback_to_legacy=True,
        explanation_codes=tuple(dict.fromkeys((
            "legacy_fallback", *codes,
        ))),
        remaining_goal_node_ids=world.remaining_goal_node_ids,
        verified_postcondition_ids=world.verified_postconditions,
        unverified_postcondition_ids=verification.unsatisfied_postcondition_ids,
        readiness_signals=tuple(item for item in dict.fromkeys((
            "unresolved_outcome_unknown"
            if verification.status == VerificationStatus.OUTCOME_UNKNOWN.value
            else "",
            "contradiction"
            if verification.status == VerificationStatus.CONTRADICTED.value
            else "",
        )) if item),
    )


def evaluate_adaptive_continuation(
    goal_contract: GoalContract,
    world_state: WorldStateView,
    verification_result: VerificationResult,
    capability_descriptors: Iterable[AdaptiveCapabilityDescriptor],
    legacy_strategy_snapshot: LegacyDecisionSnapshot,
    *,
    limits: PlannerLimits | None = None,
) -> AdaptiveContinuationProposal:
    """Propose remaining work only, with legacy fallback on every uncertainty."""

    if not isinstance(goal_contract, GoalContract):
        raise TypeError("goal_contract_required")
    if not isinstance(world_state, WorldStateView):
        raise TypeError("world_state_required")
    if not isinstance(verification_result, VerificationResult):
        raise TypeError("verification_result_required")
    if not isinstance(legacy_strategy_snapshot, LegacyDecisionSnapshot):
        raise TypeError("legacy_strategy_snapshot_required")
    if verification_result.goal_node_id != world_state.active_goal_node_id:
        return _fallback_proposal(
            world_state, verification_result, "verification_node_mismatch"
        )
    descriptors = tuple(capability_descriptors)
    if not descriptors:
        return _fallback_proposal(
            world_state, verification_result, "missing_capability_metadata"
        )
    if any(not isinstance(item, AdaptiveCapabilityDescriptor) for item in descriptors):
        return _fallback_proposal(
            world_state, verification_result, "malformed_capability_metadata"
        )
    active_node = next((
        item for item in goal_contract.nodes
        if item.node_id == world_state.active_goal_node_id
    ), None)
    if active_node is None:
        return _fallback_proposal(
            world_state, verification_result, "active_goal_node_missing"
        )
    if (
        _known(active_node.required_account_profile)
        and _normal(world_state.current_account_profile) in {
            "", UNKNOWN_METADATA, NOT_SPECIFIED
        }
    ):
        return _fallback_proposal(
            world_state, verification_result, "account_identity_unresolved"
        )
    if (
        _known(active_node.required_account_profile)
        and _known(world_state.current_account_profile)
        and _normal(world_state.current_account_profile)
        != _normal(active_node.required_account_profile)
    ):
        return _fallback_proposal(
            world_state, verification_result, "account_identity_mismatch"
        )
    remaining_contract = _remaining_contract(
        goal_contract, world_state, verification_result
    )
    context = _shadow_context(
        remaining_contract, world_state, verification_result, descriptors
    )
    try:
        result = plan_shadow(
            context,
            limits=limits,
            legacy_snapshot=legacy_strategy_snapshot,
        )
    except Exception:
        return _fallback_proposal(
            world_state, verification_result, "planner_failed"
        )
    selected = result.selected_plan
    if selected is None or not selected.ordered_steps:
        return _fallback_proposal(
            world_state, verification_result, "no_safe_adaptive_candidate"
        )
    if selected.goal_contract_fingerprint != remaining_contract.fingerprint:
        return _fallback_proposal(
            world_state, verification_result, "malformed_adaptive_plan"
        )
    if any(
        any(marker in item for marker in _BLOCKING_UNRESOLVED_MARKERS)
        for item in selected.unresolved_constraints
    ):
        return _fallback_proposal(
            world_state, verification_result, "required_runtime_identity_unresolved"
        )
    first = selected.ordered_steps[0]
    descriptor = next((
        item for item in descriptors
        if item.capability_id == first.capability_id
    ), None)
    if descriptor is None:
        return _fallback_proposal(
            world_state, verification_result, "selected_descriptor_missing"
        )
    if descriptor.runtime_availability != RuntimeAvailability.AVAILABLE.value:
        return _fallback_proposal(
            world_state, verification_result, "selected_runtime_not_available"
        )
    if (
        ModalityRequirement.VISION.value in descriptor.modality_requirements
        and world_state.vision_available is not True
    ):
        return _fallback_proposal(
            world_state, verification_result, "selected_modality_unresolved"
        )
    if (
        first.role == "act"
        and first.capability_id not in
        world_state.strategy_snapshot.frontier_capability_ids
    ):
        return _fallback_proposal(
            world_state, verification_result,
            "execution_strategy_admissibility_not_proven",
        )
    required_observation = (
        first.capability_id if first.role == "observe"
        else verification_result.required_observation_types[0]
        if verification_result.required_observation_types
        and any(item.role == "observe" for item in selected.ordered_steps)
        else ""
    )
    readiness = []
    if selected.unresolved_constraints:
        readiness.append("ambiguity")
    if len(selected.ordered_steps) >= 4:
        readiness.append("plan_complexity")
    if verification_result.status == VerificationStatus.CONTRADICTED.value:
        readiness.append("contradiction")
    if verification_result.status == VerificationStatus.OUTCOME_UNKNOWN.value:
        readiness.append("unresolved_outcome_unknown")
    if world_state.revision > 1:
        readiness.append("repeated_replanning")
    return AdaptiveContinuationProposal(
        selected_plan=selected,
        selected_next_capability=first.capability_id,
        selected_strategy_family=first.family,
        required_observation=required_observation,
        unresolved_constraints=selected.unresolved_constraints,
        fallback_to_legacy=False,
        explanation_codes=tuple(dict.fromkeys((
            "adaptive_phase3_advisory",
            "remaining_work_only",
            "execution_strategy_still_enforces",
            *verification_result.explanation_codes,
            *selected.explanation_codes,
        ))),
        remaining_goal_node_ids=world_state.remaining_goal_node_ids,
        verified_postcondition_ids=tuple(sorted(dict.fromkeys((
            *world_state.verified_postconditions,
            *verification_result.satisfied_postcondition_ids,
        )))),
        unverified_postcondition_ids=verification_result.unsatisfied_postcondition_ids,
        readiness_signals=tuple(readiness),
    )


def assess_adaptive_terminal_success(
    *,
    semantic_mission,
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
    world_inputs: WorldStateInputs | None = None,
) -> AdaptiveTerminalSuccessAssessment:
    """Require sufficient evidence for every GoalContract postcondition."""

    goal_contract = goal_contract_from_semantic_mission(semantic_mission)
    world = build_world_state_view(
        goal_contract,
        terminality_snapshot=terminality_snapshot,
        strategy_snapshot=strategy_snapshot,
        inputs=world_inputs,
    )
    results = tuple(
        verify_goal_node(node, world, world.typed_evidence)
        for node in goal_contract.nodes
    )
    required = tuple(sorted(
        postcondition_id(node.node_id, item.description)
        for node in goal_contract.nodes
        for item in node.observable_postconditions
    ))
    verified = tuple(sorted(dict.fromkeys(
        identity
        for result in results
        if result.status == VerificationStatus.VERIFIED.value
        for identity in result.satisfied_postcondition_ids
    )))
    unverified = tuple(
        identity for identity in required if identity not in verified
    )
    resolved_unknowns = {
        attempt_id
        for result in results
        for attempt_id in result.world_delta.resolved_unknown_effects
    }
    unresolved_unknowns = tuple(sorted(dict.fromkeys(
        item.attempt_id for item in world.known_effect_states
        if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
        and item.attempt_id not in resolved_unknowns
    )))
    eligible = bool(required) and not unverified and not unresolved_unknowns and all(
        result.status == VerificationStatus.VERIFIED.value
        for result in results
    )
    explanations = ["adaptive_terminal_success_evidence_gate"]
    if eligible:
        explanations.append("all_goal_contract_postconditions_verified")
    else:
        if unverified:
            explanations.append("required_postcondition_unverified")
        if unresolved_unknowns:
            explanations.append("relevant_effect_unresolved")
        if any(
            result.status == VerificationStatus.CONTRADICTED.value
            for result in results
        ):
            explanations.append("required_postcondition_contradicted")
    return AdaptiveTerminalSuccessAssessment(
        eligible=eligible,
        world_state=world,
        verification_results=results,
        verified_postcondition_ids=verified,
        unverified_postcondition_ids=unverified,
        unresolved_effect_attempt_ids=unresolved_unknowns,
        explanation_codes=tuple(explanations),
    )


def _legacy_snapshot(
    strategy_snapshot: Mapping[str, object],
    next_strategy: str,
) -> LegacyDecisionSnapshot:
    return LegacyDecisionSnapshot(
        first_mechanism_id=_clean(next_strategy),
        first_family="existing_execution_strategy",
        semantic_plan=tuple(
            _clean(item) for item in
            strategy_snapshot.get("alternatives_still_applicable", ())
            if _clean(item)
        ),
        step_count=len(strategy_snapshot.get("attempts") or ()),
        verifier_strength=VerifierStrength.NONE.value,
        frontier_capability_ids=tuple(
            _clean(item) for item in
            strategy_snapshot.get("alternatives_still_applicable", ())
            if _clean(item)
        ),
    )


def _compact_hint(
    world: WorldStateView,
    verification: VerificationResult,
    proposal: AdaptiveContinuationProposal,
) -> str:
    payload = {
        "event": "adaptive_phase3_next_strategy_hint",
        "remaining_goal_node": world.active_goal_node_id,
        "remaining_goal_nodes": list(proposal.remaining_goal_node_ids),
        "verified_postconditions": list(proposal.verified_postcondition_ids),
        "unverified_postconditions": list(proposal.unverified_postcondition_ids),
        "recommended_strategy_family": proposal.selected_strategy_family,
        "recommended_capability_id": proposal.selected_next_capability,
        "required_observation": proposal.required_observation,
        "hard_constraints": list(proposal.unresolved_constraints),
        "do_not_replay": [],  # Legacy projection; no historical authority.
        "explanation_codes": list(proposal.explanation_codes),
        "world_revision": world.revision,
        "evidence_revision": world.evidence_revision,
    }
    return (
        "[Internal adaptive continuation hint. Advisory only: the existing "
        "ExecutionStrategy, permission policy, dispatcher, target freshness, "
        "and TurnTerminalityController remain authoritative. Structured hint: "
        + json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        + "]"
    )


def phase3_continuation_context(
    legacy_context: str,
    *,
    semantic_mission,
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
    legacy_next_strategy: str,
    legacy_postcondition: str,
    available_capability_ids: Iterable[str] = (),
    provider: str = "",
    model_id: str = "",
    world_inputs: WorldStateInputs | None = None,
    capability_descriptors: Iterable[AdaptiveCapabilityDescriptor] = (),
) -> str:
    """Return legacy bytes when OFF/error; append one bounded hint when ON."""

    legacy = str(legacy_context)
    if not adaptive_intelligence_phase3_enabled():
        return legacy
    try:
        goal_contract = goal_contract_from_semantic_mission(semantic_mission)
        model_capabilities = adapt_model_provider_capabilities(provider, model_id)
        supplied_inputs = world_inputs or WorldStateInputs(
            effective_model_capabilities=(
                ("text", "image")
                if model_capabilities.vision_supported is True
                else ("text",)
            ),
            vision_available=model_capabilities.vision_supported,
        )
        world = build_world_state_view(
            goal_contract,
            terminality_snapshot=terminality_snapshot,
            strategy_snapshot=strategy_snapshot,
            inputs=supplied_inputs,
        )
        if not world.active_goal_node_id:
            return legacy
        node = next(
            item for item in goal_contract.nodes
            if item.node_id == world.active_goal_node_id
        )
        verification = verify_goal_node(node, world, world.typed_evidence)
        descriptors = tuple(capability_descriptors) or (
            capability_descriptors_from_strategy_snapshot(
                goal_contract,
                strategy_snapshot,
                available_capability_ids=available_capability_ids,
                vision_available=model_capabilities.vision_supported,
                legacy_next_strategy=legacy_next_strategy,
            )
        )
        proposal = evaluate_adaptive_continuation(
            goal_contract,
            world,
            verification,
            descriptors,
            _legacy_snapshot(strategy_snapshot, legacy_next_strategy),
        )
        if (
            not isinstance(proposal, AdaptiveContinuationProposal)
            or proposal.fallback_to_legacy
            or not proposal.selected_next_capability
        ):
            return legacy
        # The caller keeps the canonical decision/strategy variables.  Only a
        # compact advisory hint is appended to its already-built context.
        return legacy + "\n" + _compact_hint(world, verification, proposal)
    except Exception:
        return legacy


__all__ = [
    "AdaptiveContinuationProposal",
    "AdaptiveRuntimeEvaluation",
    "AdaptiveTerminalSuccessAssessment",
    "PHASE3_GATE_ENV",
    "adaptive_intelligence_phase3_enabled",
    "assess_adaptive_terminal_success",
    "capability_descriptors_from_strategy_snapshot",
    "evaluate_adaptive_continuation",
    "phase3_continuation_context",
]
