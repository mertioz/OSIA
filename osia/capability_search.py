"""Pure WOR-260 capability retrieval and eligibility sidecar.

The active execution engine does not import this module.  It consumes only
explicitly supplied immutable facts and capability descriptors; it performs no
discovery, authorization, persistence, network access, or dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from osia.capability_adapters import (
    AdaptiveCapabilityDescriptor,
    CapabilityFamily,
    ModalityRequirement,
    Reversibility,
    RuntimeAvailability,
    UNKNOWN_METADATA,
    VerifierStrength,
)
from osia.goal_contract import (
    GoalContract,
    GoalNodeContract,
    NOT_SPECIFIED,
)
from osia.tool_contracts import SideEffectClass


class PermissionState(str, Enum):
    PERMITTED = "permitted"
    PERMISSION_REQUIRED = "permission_required"
    DISALLOWED = "disallowed"
    UNKNOWN = "unknown"


class CandidateEligibility(str, Enum):
    ELIGIBLE = "eligible"
    UNRESOLVED = "unresolved"
    HARD_REJECTED = "hard_rejected"


class StepRole(str, Enum):
    OBSERVE = "observe"
    DISCOVER = "discover"
    RESOLVE = "resolve"
    ACT = "act"
    VERIFY = "verify"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class CapabilityPermissionState:
    capability_id: str
    state: str


@dataclass(frozen=True)
class CapabilityRuntimeState:
    capability_id: str
    availability: str


@dataclass(frozen=True)
class StaleReference:
    capability_id: str
    reference_id: str
    safe_reresolution_capability_id: str = ""


@dataclass(frozen=True)
class AttemptEffectState:
    capability_id: str
    equivalence_class: str
    outcome: str


@dataclass(frozen=True)
class TypedObservation:
    name: str
    value: str
    source_authority: str


@dataclass(frozen=True)
class DeterministicRoute:
    route_id: str
    capability_id: str
    application: str = UNKNOWN_METADATA
    surface: str = UNKNOWN_METADATA
    target: str = UNKNOWN_METADATA
    available: bool = True


@dataclass(frozen=True)
class PersonalBrowserAuthority:
    capability_id: str
    tab_id: str
    session_id: str
    authorized: bool | None


@dataclass(frozen=True)
class ShadowPlanningContext:
    """Immutable, ephemeral planning facts supplied by a caller or fixture."""

    goal_contract: GoalContract
    active_goal_node_id: str
    capabilities: tuple[AdaptiveCapabilityDescriptor, ...]
    current_application: str = UNKNOWN_METADATA
    current_surface: str = UNKNOWN_METADATA
    current_account_profile: str = UNKNOWN_METADATA
    effective_model_modalities: tuple[str, ...] = ()
    authoritative_vision_available: bool | None = None
    permission_states: tuple[CapabilityPermissionState, ...] = ()
    runtime_states: tuple[CapabilityRuntimeState, ...] = ()
    stale_references: tuple[StaleReference, ...] = ()
    accessibility_quality: str = UNKNOWN_METADATA
    previous_attempt_effects: tuple[AttemptEffectState, ...] = ()
    completed_goal_node_ids: tuple[str, ...] = ()
    typed_observations: tuple[TypedObservation, ...] = ()
    deterministic_routes: tuple[DeterministicRoute, ...] = ()
    personal_browser_authorities: tuple[PersonalBrowserAuthority, ...] = ()

    def active_goal_node(self) -> GoalNodeContract:
        for node in self.goal_contract.nodes:
            if node.node_id == self.active_goal_node_id:
                return node
        raise ValueError("active_goal_node_not_in_contract")


@dataclass(frozen=True)
class SoftScoreComponent:
    name: str
    value: int


@dataclass(frozen=True)
class SearchCandidate:
    capability_id: str
    family: str
    eligibility: str
    hard_rejection_reasons: tuple[str, ...]
    unresolved_reasons: tuple[str, ...]
    semantic_match: str
    application_match: str
    surface_match: str
    account_match: str
    modality_match: str
    runtime_availability: str
    permission_state: str
    verifier_strength: str
    side_effect_fit: str
    reversibility: str
    deterministic_constraints: tuple[tuple[str, str], ...]
    soft_score_components: tuple[SoftScoreComponent, ...]
    explanation_codes: tuple[str, ...]
    source_authority: str
    suggested_roles: tuple[str, ...]
    restored_by_full_scan: bool = False

    @property
    def soft_score(self) -> int:
        return sum(item.value for item in self.soft_score_components)


@dataclass(frozen=True)
class CapabilitySearchResult:
    goal_node_id: str
    total_capabilities_seen: int
    initially_retrieved: tuple[str, ...]
    force_retained: tuple[str, ...]
    eligible: tuple[SearchCandidate, ...]
    unresolved: tuple[SearchCandidate, ...]
    hard_rejected: tuple[SearchCandidate, ...]
    top_candidates: tuple[SearchCandidate, ...]
    full_scan_performed: bool
    full_scan_recovered_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class CapabilitySearchLimits:
    retrieval_limit: int = 12
    top_k: int = 6

    def __post_init__(self) -> None:
        if self.retrieval_limit < 1 or self.top_k < 1:
            raise ValueError("search_limits_must_be_positive")


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_UNKNOWN_VALUES = frozenset({"", UNKNOWN_METADATA, NOT_SPECIFIED})
_GENERIC_SURFACES = frozenset({
    "browser", "desktop", "chat", "autonomous", "scheduled", "native",
})
_MUTATING_EFFECTS = frozenset({
    SideEffectClass.REVERSIBLE_LOCAL.value,
    SideEffectClass.PERSISTENT_LOCAL.value,
    SideEffectClass.EXTERNAL_MUTATION.value,
    SideEffectClass.IRREVERSIBLE_EXTERNAL.value,
    SideEffectClass.DYNAMICALLY_CLASSIFIED.value,
    SideEffectClass.UNKNOWN_HIGH_RISK.value,
})


def _normal(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _known(value: object) -> bool:
    return _normal(value) not in _UNKNOWN_VALUES


def _tokens(*values: object) -> frozenset[str]:
    return frozenset(
        token
        for value in values
        for token in _TOKEN_RE.findall(_normal(value))
        if len(token) > 1
    )


def _constraint_map(
    descriptor: AdaptiveCapabilityDescriptor,
) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for item in descriptor.deterministic_constraints:
        values.setdefault(_normal(item.name), []).append(_normal(item.value))
    return {name: tuple(items) for name, items in values.items()}


def _constraint_values(
    constraints: dict[str, tuple[str, ...]], *names: str
) -> tuple[str, ...]:
    return tuple(
        value
        for name in names
        for value in constraints.get(_normal(name), ())
        if _known(value)
    )


def _roles(
    descriptor: AdaptiveCapabilityDescriptor,
    constraints: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    explicit = tuple(
        value for value in _constraint_values(constraints, "step_role", "role")
        if value in {item.value for item in StepRole}
    )
    if explicit:
        return tuple(dict.fromkeys(explicit))
    text = " ".join((
        descriptor.capability_id,
        descriptor.description,
        *descriptor.semantic_effects,
    )).casefold()
    family = descriptor.family
    if family == CapabilityFamily.VISION_OBSERVATION.value:
        return (StepRole.OBSERVE.value,)
    for marker, role in (
        ("reconcile", StepRole.RECONCILE.value),
        ("verify", StepRole.VERIFY.value),
        ("resolve", StepRole.RESOLVE.value),
        ("discover", StepRole.DISCOVER.value),
        ("observe", StepRole.OBSERVE.value),
    ):
        if marker in text:
            return (role,)
    return (StepRole.ACT.value,)


def _binding_value(
    capability_id: str,
    bindings: Iterable[object],
    value_name: str,
) -> str | None:
    exact = None
    wildcard = None
    for item in bindings:
        item_id = getattr(item, "capability_id", "")
        if item_id == capability_id:
            exact = str(getattr(item, value_name))
        elif item_id == "*":
            wildcard = str(getattr(item, value_name))
    return exact if exact is not None else wildcard


def _permission_state(
    descriptor: AdaptiveCapabilityDescriptor,
    context: ShadowPlanningContext,
) -> str:
    supplied = _binding_value(
        descriptor.capability_id, context.permission_states, "state"
    )
    if supplied is not None:
        try:
            return PermissionState(supplied).value
        except ValueError:
            return PermissionState.UNKNOWN.value
    if descriptor.permission_requirements and all(
        _normal(item.category) in {"none", "no_permission"}
        and _normal(item.confirmation_policy) in {
            "none", "no_confirmation", "not_required",
        }
        for item in descriptor.permission_requirements
    ):
        return PermissionState.PERMITTED.value
    if any(
        "required" in _normal(item.confirmation_policy)
        for item in descriptor.permission_requirements
    ):
        return PermissionState.PERMISSION_REQUIRED.value
    return PermissionState.UNKNOWN.value


def _runtime_state(
    descriptor: AdaptiveCapabilityDescriptor,
    context: ShadowPlanningContext,
) -> str:
    supplied = _binding_value(
        descriptor.capability_id, context.runtime_states, "availability"
    )
    candidate = supplied or descriptor.runtime_availability
    try:
        return RuntimeAvailability(candidate).value
    except ValueError:
        return RuntimeAvailability.UNKNOWN.value


def _semantic_relation(goal_value: str, candidate_values: Iterable[str]) -> str:
    goal = _normal(goal_value)
    values = tuple(_normal(item) for item in candidate_values if _known(item))
    if not _known(goal) or not values:
        return "unknown"
    if any(goal == item or goal in item or item in goal for item in values):
        return "exact"
    return "mismatch"


def _vision_state(context: ShadowPlanningContext) -> str:
    modalities = {_normal(item) for item in context.effective_model_modalities}
    if modalities and "image" not in modalities:
        return "unavailable"
    if context.authoritative_vision_available is False:
        return "unavailable"
    if context.authoritative_vision_available is True:
        return "available"
    if modalities:
        return "available"
    return "unknown"


def _personal_browser_state(
    descriptor: AdaptiveCapabilityDescriptor,
    context: ShadowPlanningContext,
) -> str:
    authority = next((
        item for item in context.personal_browser_authorities
        if item.capability_id == descriptor.capability_id
    ), None)
    if authority is None:
        authority = next((
            item for item in context.personal_browser_authorities
            if item.capability_id == "*"
        ), None)
    if authority is None or authority.authorized is None:
        return "unknown"
    if authority.authorized is False:
        return "mismatch"
    constraints = _constraint_map(descriptor)
    expected_tab = _constraint_values(constraints, "tab_id", "exact_tab_id")
    expected_session = _constraint_values(
        constraints, "session_id", "exact_session_id"
    )
    if expected_tab and _normal(authority.tab_id) not in expected_tab:
        return "mismatch"
    if expected_session and _normal(authority.session_id) not in expected_session:
        return "mismatch"
    return "exact"


def _stale_reference(
    descriptor: AdaptiveCapabilityDescriptor,
    context: ShadowPlanningContext,
) -> StaleReference | None:
    return next((
        item for item in context.stale_references
        if item.capability_id in {descriptor.capability_id, "*"}
    ), None)


def _replay_is_blocked(
    descriptor: AdaptiveCapabilityDescriptor,
    context: ShadowPlanningContext,
) -> bool:
    equivalence = _normal(descriptor.anti_replay_equivalence_class)
    for effect in context.previous_attempt_effects:
        if _normal(effect.outcome) != "outcome_unknown":
            continue
        same_capability = effect.capability_id == descriptor.capability_id
        same_equivalence = (
            _known(equivalence)
            and _normal(effect.equivalence_class) == equivalence
        )
        if same_capability or same_equivalence:
            return True
    return False


def _verifier_score(value: str) -> int:
    return {
        VerifierStrength.NONE.value: 0,
        VerifierStrength.WEAK_UNSTRUCTURED.value: 1,
        VerifierStrength.PROVIDER_REPORTED.value: 2,
        VerifierStrength.STRUCTURED_RESULT.value: 5,
        VerifierStrength.OBSERVABLE_STATE.value: 8,
        VerifierStrength.EXACT_POSTCONDITION.value: 12,
    }.get(value, 0)


def _evaluate_candidate(
    node: GoalNodeContract,
    descriptor: AdaptiveCapabilityDescriptor,
    context: ShadowPlanningContext,
) -> SearchCandidate:
    constraints = _constraint_map(descriptor)
    roles = _roles(descriptor, constraints)
    hard: list[str] = []
    unresolved: list[str] = []
    explanations: list[str] = []
    score: list[SoftScoreComponent] = []

    goal_tokens = _tokens(
        node.objective, node.target, node.application, node.required_surface
    )
    capability_tokens = _tokens(
        descriptor.capability_id,
        descriptor.description,
        *descriptor.semantic_effects,
    )
    overlap = len(goal_tokens.intersection(capability_tokens))
    target_tokens = _tokens(node.target)
    semantic_match = "exact" if overlap and (
        (target_tokens and target_tokens.issubset(capability_tokens))
        or overlap >= max(1, min(2, len(goal_tokens)))
    ) else "partial" if overlap else "unknown"
    if overlap:
        score.append(SoftScoreComponent("semantic_effect_match", min(30, 8 * overlap)))
        explanations.append("exact_semantic_effect" if semantic_match == "exact" else "semantic_effect_overlap")

    semantic_checks = (
        ("target", node.target, _constraint_values(constraints, "target", "exact_target"), "semantic_target_mismatch"),
        ("application", node.application, _constraint_values(constraints, "application", "exact_application"), "application_mismatch"),
        ("action", node.objective, _constraint_values(constraints, "action", "intended_action"), "intended_action_mismatch"),
    )
    relation_by_name: dict[str, str] = {}
    for name, goal_value, candidate_values, reason in semantic_checks:
        relation = _semantic_relation(goal_value, candidate_values)
        relation_by_name[name] = relation
        if relation == "mismatch":
            hard.append(reason)
        elif relation == "exact":
            score.append(SoftScoreComponent(f"exact_{name}_match", 14))

    surface_values = (
        *_constraint_values(constraints, "surface", "exact_surface"),
        *tuple(
            _normal(item) for item in descriptor.supported_surfaces
            if _normal(item) not in _GENERIC_SURFACES and _known(item)
        ),
    )
    surface_match = _semantic_relation(node.required_surface, surface_values)
    if surface_match == "mismatch":
        hard.append("required_surface_mismatch")
    elif surface_match == "exact":
        score.append(SoftScoreComponent("surface_match", 18))
        explanations.append("exact_surface_match")
    elif _known(node.required_surface):
        if _normal(context.current_surface) == _normal(node.required_surface):
            surface_match = "current_surface"
            score.append(SoftScoreComponent("current_surface_match", 8))
        else:
            unresolved.append("surface_binding_unknown")

    application_match = relation_by_name["application"]
    if application_match == "exact":
        score.append(SoftScoreComponent("application_match", 12))
        explanations.append("exact_application_match")
    elif application_match == "unknown" and _known(node.application):
        if _normal(context.current_application) == _normal(node.application):
            application_match = "current_application"
            score.append(SoftScoreComponent("current_application_match", 6))

    account_values = (
        *_constraint_values(constraints, "account", "account_profile", "exact_account_profile"),
        *(() if not _known(descriptor.required_account_profile) else (_normal(descriptor.required_account_profile),)),
    )
    account_match = _semantic_relation(node.required_account_profile, account_values)
    if account_match == "mismatch":
        hard.append("account_mismatch")
        explanations.append("account_mismatch")
    elif account_match == "exact":
        score.append(SoftScoreComponent("account_match", 16))
        explanations.append("exact_account_match")
    elif _known(node.required_account_profile):
        if _normal(context.current_account_profile) == _normal(node.required_account_profile):
            account_match = "current_account"
            score.append(SoftScoreComponent("current_account_match", 8))
        else:
            unresolved.append("account_binding_unknown")

    permission = _permission_state(descriptor, context)
    if permission == PermissionState.DISALLOWED.value:
        hard.append("permission_disallowed")
    elif permission == PermissionState.PERMISSION_REQUIRED.value:
        unresolved.append("permission_required")
        explanations.append("permission_required")
    elif permission == PermissionState.UNKNOWN.value:
        unresolved.append("permission_unknown")

    runtime = _runtime_state(descriptor, context)
    if runtime == RuntimeAvailability.UNAVAILABLE.value:
        hard.append("runtime_unavailable")
    elif runtime == RuntimeAvailability.UNKNOWN.value:
        unresolved.append("runtime_unknown")
        explanations.append("runtime_unknown")
    else:
        score.append(SoftScoreComponent("availability_confidence", 10))

    modality_match = "not_required"
    if ModalityRequirement.VISION.value in descriptor.modality_requirements:
        modality_match = _vision_state(context)
        if modality_match == "unavailable":
            hard.append("modality_unavailable")
            explanations.append("modality_unavailable")
        elif modality_match == "unknown":
            unresolved.append("vision_authority_unknown")
        else:
            score.append(SoftScoreComponent("modality_match", 8))

    if descriptor.family == CapabilityFamily.PERSONAL_BROWSER.value:
        personal_state = _personal_browser_state(descriptor, context)
        if personal_state == "mismatch":
            hard.append("personal_browser_authority_mismatch")
        elif personal_state == "unknown":
            unresolved.append("personal_browser_authority_unknown")
        else:
            score.append(SoftScoreComponent("personal_browser_authority", 16))

    stale = _stale_reference(descriptor, context)
    if stale is not None and StepRole.ACT.value in roles:
        if stale.safe_reresolution_capability_id:
            unresolved.append("stale_ref_requires_reresolution")
            explanations.append("stale_ref_requires_reresolution")
        else:
            hard.append("stale_ref_cannot_actuate")

    if descriptor.side_effect_class in _MUTATING_EFFECTS and _replay_is_blocked(
        descriptor, context
    ):
        hard.append("outcome_unknown_replay_blocked")
        explanations.append("outcome_unknown_replay_blocked")

    node_effect = _normal(node.side_effect_class)
    descriptor_effect = _normal(descriptor.side_effect_class)
    side_effect_fit = "compatible"
    if StepRole.ACT.value in roles:
        if node_effect == SideEffectClass.READ_ONLY.value and descriptor_effect in _MUTATING_EFFECTS:
            hard.append("side_effect_mismatch")
            side_effect_fit = "mismatch"
        elif node_effect in _MUTATING_EFFECTS and descriptor_effect == SideEffectClass.READ_ONLY.value:
            hard.append("side_effect_mismatch")
            side_effect_fit = "mismatch"
        else:
            score.append(SoftScoreComponent("side_effect_suitability", 5))

    if (
        context.accessibility_quality == "target_missing"
        and descriptor.family == CapabilityFamily.DESKTOP_UIA.value
        and StepRole.ACT.value in roles
    ):
        unresolved.append("target_missing_requires_observation")
        explanations.append("target_missing_requires_observation")

    if (
        StepRole.ACT.value in roles
        and "true" in _constraint_values(constraints, "requires_observation")
    ):
        unresolved.append("requires_observation")
        explanations.append("requires_observation")

    claimed_postconditions = _constraint_values(
        constraints, "postcondition", "exact_postcondition"
    )
    for required in node.observable_postconditions:
        relation = _semantic_relation(required.description, claimed_postconditions)
        if relation == "mismatch":
            hard.append("required_postcondition_mismatch")

    verifier_points = _verifier_score(descriptor.verifier_strength)
    if verifier_points:
        score.append(SoftScoreComponent("verifier_strength", verifier_points))
    if descriptor.verifier_strength in {
        VerifierStrength.OBSERVABLE_STATE.value,
        VerifierStrength.EXACT_POSTCONDITION.value,
    }:
        explanations.append("strong_observable_postcondition")

    if descriptor.family in {
        CapabilityFamily.DIRECT_API.value,
        CapabilityFamily.MCP.value,
        CapabilityFamily.PROVIDER_NATIVE.value,
        CapabilityFamily.OSIA_NATIVE.value,
    } and semantic_match != "unknown":
        score.append(SoftScoreComponent("direct_structured_capability", 8))
        explanations.append("direct_structured_capability")
    if descriptor.family == CapabilityFamily.DEEP_LINK.value:
        score.append(SoftScoreComponent("deep_link_exact_route", 10))
        explanations.append("deep_link_exact_route")
    if descriptor.reversibility in {
        Reversibility.REVERSIBLE.value,
        Reversibility.NOT_APPLICABLE.value,
    }:
        score.append(SoftScoreComponent("reversibility", 3))
    if descriptor.family == CapabilityFamily.VISION_ACTUATION_GLOBAL.value:
        score.append(SoftScoreComponent("global_coordinates_last_resort", -40))
        explanations.append("global_coordinates_last_resort")
    if (
        context.accessibility_quality == "target_missing"
        and StepRole.OBSERVE.value in roles
    ):
        score.append(SoftScoreComponent("observation_information_gain", 14))
    if descriptor.latency_class not in _UNKNOWN_VALUES:
        latency = _normal(descriptor.latency_class)
        score.append(SoftScoreComponent(
            "known_latency_class", 1 if latency in {"low", "local"} else -1
        ))

    hard = list(dict.fromkeys(hard))
    unresolved = list(dict.fromkeys(unresolved))
    if hard:
        eligibility = CandidateEligibility.HARD_REJECTED.value
    elif unresolved:
        eligibility = CandidateEligibility.UNRESOLVED.value
    else:
        eligibility = CandidateEligibility.ELIGIBLE.value
    return SearchCandidate(
        capability_id=descriptor.capability_id,
        family=descriptor.family,
        eligibility=eligibility,
        hard_rejection_reasons=tuple(hard),
        unresolved_reasons=tuple(unresolved),
        semantic_match=semantic_match,
        application_match=application_match,
        surface_match=surface_match,
        account_match=account_match,
        modality_match=modality_match,
        runtime_availability=runtime,
        permission_state=permission,
        verifier_strength=descriptor.verifier_strength,
        side_effect_fit=side_effect_fit,
        reversibility=descriptor.reversibility,
        deterministic_constraints=tuple(
            (item.name, item.value) for item in descriptor.deterministic_constraints
        ),
        soft_score_components=tuple(score),
        explanation_codes=tuple(dict.fromkeys(explanations)),
        source_authority=descriptor.source_authority,
        suggested_roles=roles,
    )


def _structural_score(
    node: GoalNodeContract,
    descriptor: AdaptiveCapabilityDescriptor,
) -> int:
    goal_tokens = _tokens(
        node.objective, node.target, node.application, node.required_surface
    )
    capability_tokens = _tokens(
        descriptor.capability_id,
        descriptor.description,
        *descriptor.semantic_effects,
        *descriptor.supported_surfaces,
    )
    return len(goal_tokens.intersection(capability_tokens))


def _initial_retrieval(
    node: GoalNodeContract,
    context: ShadowPlanningContext,
    limits: CapabilitySearchLimits,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    force: set[str] = {
        route.capability_id
        for route in context.deterministic_routes
        if route.available
    }
    need_roles: set[str] = set()
    if context.accessibility_quality == "target_missing":
        need_roles.update({StepRole.OBSERVE.value, StepRole.RESOLVE.value})
    if context.stale_references:
        need_roles.add(StepRole.RESOLVE.value)
    if any(
        _normal(item.outcome) == "outcome_unknown"
        for item in context.previous_attempt_effects
    ):
        need_roles.add(StepRole.RECONCILE.value)
    if node.observable_postconditions:
        need_roles.add(StepRole.VERIFY.value)

    ranked: list[tuple[int, str]] = []
    for descriptor in context.capabilities:
        constraints = _constraint_map(descriptor)
        roles = set(_roles(descriptor, constraints))
        exact_surfaces = {
            _normal(item) for item in descriptor.supported_surfaces
            if _known(item)
        }
        if _normal(node.required_surface) in exact_surfaces:
            force.add(descriptor.capability_id)
        if roles.intersection(need_roles):
            force.add(descriptor.capability_id)
        ranked.append((_structural_score(node, descriptor), descriptor.capability_id))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = {item[1] for item in ranked[:limits.retrieval_limit]}
    selected.update(force)
    ordered = tuple(
        descriptor.capability_id for descriptor in context.capabilities
        if descriptor.capability_id in selected
    )
    return ordered, tuple(sorted(force))


def _candidate_sort_key(candidate: SearchCandidate) -> tuple[int, int, str]:
    eligibility_rank = {
        CandidateEligibility.ELIGIBLE.value: 0,
        CandidateEligibility.UNRESOLVED.value: 1,
        CandidateEligibility.HARD_REJECTED.value: 2,
    }[candidate.eligibility]
    return eligibility_rank, -candidate.soft_score, candidate.capability_id


def search_capabilities(
    context: ShadowPlanningContext,
    *,
    goal_node: GoalNodeContract | None = None,
    limits: CapabilitySearchLimits | None = None,
    full_scan: bool = False,
) -> CapabilitySearchResult:
    """Retrieve, hard-filter, and rank candidates without executable output."""

    if not isinstance(context, ShadowPlanningContext):
        raise TypeError("context_must_be_shadow_planning_context")
    selected_node = goal_node or context.active_goal_node()
    selected_limits = limits or CapabilitySearchLimits()
    initial_ids, force_retained = _initial_retrieval(
        selected_node, context, selected_limits
    )
    initial_set = set(initial_ids)
    descriptors = (
        context.capabilities
        if full_scan
        else tuple(
            item for item in context.capabilities
            if item.capability_id in initial_set
        )
    )
    candidates: list[SearchCandidate] = []
    recovered: list[str] = []
    for descriptor in descriptors:
        candidate = _evaluate_candidate(selected_node, descriptor, context)
        if (
            full_scan
            and descriptor.capability_id not in initial_set
            and candidate.eligibility != CandidateEligibility.HARD_REJECTED.value
        ):
            candidate = replace(
                candidate,
                restored_by_full_scan=True,
                explanation_codes=tuple(dict.fromkeys((
                    *candidate.explanation_codes,
                    "full_scan_recovered_candidate",
                ))),
            )
            recovered.append(descriptor.capability_id)
        candidates.append(candidate)
    ordered = tuple(sorted(candidates, key=_candidate_sort_key))
    eligible = tuple(
        item for item in ordered
        if item.eligibility == CandidateEligibility.ELIGIBLE.value
    )
    unresolved = tuple(
        item for item in ordered
        if item.eligibility == CandidateEligibility.UNRESOLVED.value
    )
    rejected = tuple(
        item for item in ordered
        if item.eligibility == CandidateEligibility.HARD_REJECTED.value
    )
    selectable = (*eligible, *unresolved)
    diagnostics = [
        "hard_eligibility_before_scoring",
        "unknown_permission_not_granted",
        "no_executable_handles",
    ]
    if full_scan:
        diagnostics.append("full_descriptor_scan_performed")
    if recovered:
        diagnostics.append("full_scan_recovered_candidates")
    return CapabilitySearchResult(
        goal_node_id=selected_node.node_id,
        total_capabilities_seen=len(context.capabilities),
        initially_retrieved=initial_ids,
        force_retained=force_retained,
        eligible=eligible,
        unresolved=unresolved,
        hard_rejected=rejected,
        top_candidates=tuple(selectable[:selected_limits.top_k]),
        full_scan_performed=full_scan,
        full_scan_recovered_ids=tuple(recovered),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "AttemptEffectState",
    "CandidateEligibility",
    "CapabilityPermissionState",
    "CapabilityRuntimeState",
    "CapabilitySearchLimits",
    "CapabilitySearchResult",
    "DeterministicRoute",
    "PermissionState",
    "PersonalBrowserAuthority",
    "SearchCandidate",
    "ShadowPlanningContext",
    "SoftScoreComponent",
    "StaleReference",
    "StepRole",
    "TypedObservation",
    "search_capabilities",
]
