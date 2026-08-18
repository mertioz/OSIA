"""Immutable WOR-260 Phase-3 view of already-authoritative runtime state.

The structures in this module are ephemeral projections.  They do not read or
write durable state, discover capabilities, dispatch work, grant permission,
or decide whole-turn terminality.  Callers must supply snapshots copied from
the existing authorities.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from osia.tool_contracts import SideEffectClass


_capability_adapters = importlib.import_module("osia.capability_adapters")
UNKNOWN_METADATA = _capability_adapters.UNKNOWN_METADATA
VerifierStrength = _capability_adapters.VerifierStrength
_goal_contract = importlib.import_module("osia.goal_contract")
GoalContract = _goal_contract.GoalContract
GoalNodeContract = _goal_contract.GoalNodeContract


WORLD_STATE_SCHEMA_VERSION = "1.0"
UNKNOWN_STATE = UNKNOWN_METADATA
_VERIFIER_RANK = {
    VerifierStrength.NONE.value: 0,
    VerifierStrength.WEAK_UNSTRUCTURED.value: 1,
    VerifierStrength.PROVIDER_REPORTED.value: 2,
    VerifierStrength.STRUCTURED_RESULT.value: 3,
    VerifierStrength.OBSERVABLE_STATE.value: 4,
    VerifierStrength.EXACT_POSTCONDITION.value: 5,
}
_PROVIDER_ONLY_REASON_PREFIXES = (
    "provider_native_",
    "provider_typed_",
)
_EXACT_EVIDENCE_REASON_MARKERS = (
    "exact_final_state",
    "exact_postcondition",
    "independent_final_state",
)
_CONTRADICTION_STATES = frozenset({
    "contradicted",
    "postcondition_absent",
    "target_not_observed",
    "required_postcondition_contradicted",
    "observable_state_mismatch",
})
_MUTATING_EFFECTS = frozenset({
    SideEffectClass.REVERSIBLE_LOCAL.value,
    SideEffectClass.PERSISTENT_LOCAL.value,
    SideEffectClass.EXTERNAL_MUTATION.value,
    SideEffectClass.IRREVERSIBLE_EXTERNAL.value,
    SideEffectClass.DYNAMICALLY_CLASSIFIED.value,
    SideEffectClass.UNKNOWN_HIGH_RISK.value,
})


class DispatchState(str, Enum):
    NOT_DISPATCHED = "not_dispatched"
    REJECTED_BEFORE_DISPATCH = "rejected_before_dispatch"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class EffectState(str, Enum):
    NO_EFFECT = "no_effect"
    READ_ONLY = "read_only"
    EFFECT_CONFIRMED = "effect_confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UNKNOWN = "unknown"


class EvidenceStaleState(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StateFact:
    name: str
    value: str
    source_authority: str


@dataclass(frozen=True)
class PermissionFact:
    capability_id: str
    state: str
    request_id: str = ""
    source_authority: str = "existing_permission_state"


@dataclass(frozen=True)
class EffectStateFact:
    attempt_id: str
    capability_id: str
    equivalence_class: str
    effect_state: str
    goal_node_id: str = ""


@dataclass(frozen=True)
class StateSummary:
    authority: str = UNKNOWN_STATE
    identity: str = UNKNOWN_STATE
    surface: str = UNKNOWN_STATE
    epoch: str = UNKNOWN_STATE
    freshness: str = UNKNOWN_STATE
    status: str = UNKNOWN_STATE


@dataclass(frozen=True)
class StrategySnapshotView:
    current_mechanism: str
    current_layer: int | None
    frontier_capability_ids: tuple[str, ...]
    attempted_capability_ids: tuple[str, ...]
    failed_before_effect_capability_ids: tuple[str, ...]
    stale_capability_ids: tuple[str, ...]
    frozen_reason: str
    terminal_blocker: str


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    source_type: str
    source_attempt_id: str
    goal_node_id: str
    capability_id: str
    capability_family: str
    observation_type: str
    verifier_strength: str
    semantic_effect: str
    observed_state: str
    supported_postcondition_ids: tuple[str, ...]
    dispatch_state: str
    effect_state: str
    stale_state: str
    evidence_revision: int


@dataclass(frozen=True)
class WorldDelta:
    from_revision: int
    to_revision: int
    newly_verified_goal_nodes: tuple[str, ...] = ()
    newly_verified_postconditions: tuple[str, ...] = ()
    newly_contradicted_postconditions: tuple[str, ...] = ()
    newly_invalidated_refs: tuple[str, ...] = ()
    resolved_unknown_effects: tuple[str, ...] = ()
    newly_observed_applications: tuple[str, ...] = ()
    newly_observed_surfaces: tuple[str, ...] = ()
    new_account_profile_bindings: tuple[str, ...] = ()
    new_capability_availability_facts: tuple[str, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorldStateView:
    schema_version: str
    turn_id: str
    goal_contract_fingerprint: str
    active_goal_node_id: str
    completed_goal_node_ids: tuple[str, ...]
    remaining_goal_node_ids: tuple[str, ...]
    current_application: str
    current_surface: str
    current_account_profile: str
    browser_state_summary: StateSummary
    desktop_state_summary: StateSummary
    effective_model_capabilities: tuple[str, ...]
    vision_available: bool | None
    permission_states: tuple[PermissionFact, ...]
    active_attempt_ids: tuple[str, ...]
    pending_permission_ids: tuple[str, ...]
    known_effect_states: tuple[EffectStateFact, ...]
    outcome_unknown_equivalence_classes: tuple[str, ...]
    stale_refs: tuple[str, ...]
    accessibility_quality: str
    typed_observations: tuple[StateFact, ...]
    typed_evidence: tuple[EvidenceItem, ...]
    verified_postconditions: tuple[str, ...]
    remaining_postconditions: tuple[str, ...]
    contradicted_postconditions: tuple[str, ...]
    strategy_snapshot: StrategySnapshotView
    available_strategy_families: tuple[str, ...]
    revision: int
    evidence_revision: int


@dataclass(frozen=True)
class WorldStateInputs:
    """Already-resolved ephemeral facts that are absent from durable ledgers."""

    current_application: str = UNKNOWN_STATE
    current_surface: str = UNKNOWN_STATE
    current_account_profile: str = UNKNOWN_STATE
    browser_state_summary: StateSummary = StateSummary()
    desktop_state_summary: StateSummary = StateSummary()
    effective_model_capabilities: tuple[str, ...] = ()
    vision_available: bool | None = None
    permission_states: tuple[PermissionFact, ...] = ()
    pending_permission_ids: tuple[str, ...] = ()
    known_effect_states: tuple[EffectStateFact, ...] = ()
    stale_refs: tuple[str, ...] = ()
    accessibility_quality: str = UNKNOWN_STATE
    typed_observations: tuple[StateFact, ...] = ()
    contradicted_postconditions: tuple[str, ...] = ()
    revision: int = 1


_FAILED_BEFORE_EFFECT = frozenset({
    "failed_before_effect",
    "confirmed_failure_before_effect",
    "rejected_before_execution",
})
_OUTCOME_UNKNOWN = frozenset({
    "outcome_unknown", "unknown_after_start", "success_unverified",
})
_MUTATING_EFFECTS = frozenset({
    SideEffectClass.REVERSIBLE_LOCAL.value,
    SideEffectClass.PERSISTENT_LOCAL.value,
    SideEffectClass.EXTERNAL_MUTATION.value,
    SideEffectClass.IRREVERSIBLE_EXTERNAL.value,
    SideEffectClass.DYNAMICALLY_CLASSIFIED.value,
    SideEffectClass.UNKNOWN_HIGH_RISK.value,
})


def _clean(value: object, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _normal(value: object) -> str:
    return _clean(value).casefold()


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()[:24]


def postcondition_id(node_id: str, description: str) -> str:
    return _stable_id("pc_", {
        "goal_node_id": _clean(node_id),
        "description": _clean(description),
    })


def goal_node_equivalence_class(node: GoalNodeContract) -> str:
    """Stable cross-mechanism identity for mechanisms bound to one goal node."""

    return _stable_id("goal_effect_", {
        "node_id": node.node_id,
        "target": node.target,
        "application": node.application,
        "surface": node.required_surface,
        "account": node.required_account_profile,
        "side_effect": node.side_effect_class,
        "postconditions": [
            item.description for item in node.observable_postconditions
        ],
    })


def _postcondition_index(
    contract: GoalContract,
) -> tuple[dict[str, tuple[tuple[str, str], ...]], dict[str, str]]:
    collected: dict[str, list[tuple[str, str]]] = {}
    by_id: dict[str, str] = {}
    for node in contract.nodes:
        for item in node.observable_postconditions:
            identity = postcondition_id(node.node_id, item.description)
            collected.setdefault(_normal(item.description), []).append(
                (identity, node.node_id)
            )
            by_id[identity] = item.description
    by_description = {
        key: tuple(values) for key, values in collected.items()
    }
    return by_description, by_id


def _strategy_view(snapshot: Mapping[str, object]) -> StrategySnapshotView:
    attempts = tuple(
        item for item in snapshot.get("attempts", ())
        if isinstance(item, Mapping)
    )
    attempted = tuple(dict.fromkeys(
        _clean(item.get("mechanism_id"))
        for item in attempts if _clean(item.get("mechanism_id"))
    ))
    failed = tuple(dict.fromkeys(
        _clean(item.get("mechanism_id"))
        for item in attempts
        if _normal(item.get("status")) in {
            "failed", "stagnated", "inapplicable", "blocked"
        }
        and _normal(item.get("failure_or_stagnation_reason")) not in {
            "ambiguous_effect", "outcome_unknown", "unknown_after_start"
        }
        and _clean(item.get("mechanism_id"))
    ))
    stale = tuple(dict.fromkeys((
        *(
            _clean(item.get("mechanism_id"))
            for item in attempts
            if _normal(item.get("status")) == "stale_target"
        ),
        *(
            _clean(key)
            for key in (snapshot.get("stale_mechanisms") or {})
        ),
    )))
    return StrategySnapshotView(
        current_mechanism=_clean(snapshot.get("current_mechanism")),
        current_layer=(
            int(snapshot["current_layer"])
            if snapshot.get("current_layer") is not None else None
        ),
        frontier_capability_ids=tuple(
            _clean(item) for item in
            snapshot.get("alternatives_still_applicable", ())
            if _clean(item)
        ),
        attempted_capability_ids=attempted,
        failed_before_effect_capability_ids=failed,
        stale_capability_ids=stale,
        frozen_reason=_clean(snapshot.get("frozen_reason")),
        terminal_blocker=_clean(snapshot.get("terminal_blocker")),
    )


def _outcome_shape(outcome: str) -> tuple[str, str, str]:
    normalized = _normal(outcome).replace(" ", "_")
    if normalized in _FAILED_BEFORE_EFFECT:
        return (
            DispatchState.REJECTED_BEFORE_DISPATCH.value,
            EffectState.NO_EFFECT.value,
            VerifierStrength.STRUCTURED_RESULT.value,
        )
    if normalized in _OUTCOME_UNKNOWN:
        return (
            DispatchState.DISPATCHED.value,
            EffectState.OUTCOME_UNKNOWN.value,
            VerifierStrength.PROVIDER_REPORTED.value,
        )
    if normalized == "success_verified":
        return (
            DispatchState.COMPLETED.value,
            EffectState.EFFECT_CONFIRMED.value,
            VerifierStrength.OBSERVABLE_STATE.value,
        )
    if normalized in {"read_only", "observed", "reconciled"}:
        return (
            DispatchState.COMPLETED.value,
            EffectState.READ_ONLY.value,
            VerifierStrength.STRUCTURED_RESULT.value,
        )
    if normalized in {"pending", "running", "dispatch_started", "accepted"}:
        return (
            DispatchState.DISPATCHED.value,
            EffectState.UNKNOWN.value,
            VerifierStrength.NONE.value,
        )
    return (
        DispatchState.UNKNOWN.value,
        EffectState.UNKNOWN.value,
        VerifierStrength.NONE.value,
    )


def _typed_outcome_shape(
    outcome: str,
    reason_code: str,
) -> tuple[str, str, str]:
    dispatch, effect, strength = _outcome_shape(outcome)
    reason = _normal(reason_code).replace(" ", "_")
    normalized_outcome = _normal(outcome).replace(" ", "_")
    if normalized_outcome == "success_verified" and reason.startswith(
        _PROVIDER_ONLY_REASON_PREFIXES
    ):
        strength = VerifierStrength.PROVIDER_REPORTED.value
    elif normalized_outcome == "success_verified" and any(
        marker in reason for marker in _EXACT_EVIDENCE_REASON_MARKERS
    ):
        strength = VerifierStrength.EXACT_POSTCONDITION.value
    return dispatch, effect, strength


def _node_for_attempt(
    attempt_id: str,
    strategy_snapshot: Mapping[str, object],
) -> tuple[str, str]:
    for item in strategy_snapshot.get("attempts", ()):
        if not isinstance(item, Mapping):
            continue
        if _clean(item.get("attempt_id")) == attempt_id:
            return (
                _clean(item.get("node_id")),
                _clean(item.get("mechanism_id")),
            )
    return "", ""


def _normalized_evidence(
    contract: GoalContract,
    terminality_snapshot: Mapping[str, object],
    strategy_snapshot: Mapping[str, object],
) -> tuple[EvidenceItem, ...]:
    by_description, _ = _postcondition_index(contract)
    proven = terminality_snapshot.get("proven_postconditions") or {}
    evidence_revision = int(
        terminality_snapshot.get("evidence_revision") or 0
    )
    normalized: list[EvidenceItem] = []
    typed_items = tuple(
        item for item in (terminality_snapshot.get("typed_evidence") or ())
        if isinstance(item, Mapping)
    )
    typed_postconditions = {
        _normal(item.get("postcondition")) for item in typed_items
        if _normal(item.get("postcondition"))
    }

    for description, token in sorted(proven.items()):
        matches = by_description.get(_normal(description), ())
        if len(matches) != 1:
            continue
        # A typed receipt is the stronger provenance source.  Do not turn its
        # accompanying terminality claim into an independent exact proof.
        if _normal(description) in typed_postconditions:
            continue
        match = matches[0]
        identity, node_id = match
        evidence_id = _stable_id("evidence_", {
            "source": "turn_terminality_proven_postcondition",
            "postcondition": identity,
            "token": _clean(token),
        })
        normalized.append(EvidenceItem(
            evidence_id=evidence_id,
            source_type="turn_terminality_proven_postcondition",
            source_attempt_id="",
            goal_node_id=node_id,
            capability_id="",
            capability_family=UNKNOWN_STATE,
            observation_type="exact_postcondition_proof",
            verifier_strength=VerifierStrength.EXACT_POSTCONDITION.value,
            semantic_effect="postcondition_verified",
            observed_state="verified",
            supported_postcondition_ids=(identity,),
            dispatch_state=DispatchState.COMPLETED.value,
            effect_state=EffectState.EFFECT_CONFIRMED.value,
            stale_state=EvidenceStaleState.FRESH.value,
            evidence_revision=evidence_revision,
        ))

    for index, item in enumerate(typed_items):
        outcome = _clean(item.get("outcome"))
        attempt_id = _clean(item.get("attempt_id"))
        node_id, capability_id = _node_for_attempt(
            attempt_id, strategy_snapshot
        )
        postcondition = _clean(item.get("postcondition"))
        matches = by_description.get(_normal(postcondition), ())
        match = next((
            candidate for candidate in matches
            if node_id and candidate[1] == node_id
        ), matches[0] if len(matches) == 1 else None)
        supported = (match[0],) if match is not None else ()
        if match is not None and not node_id:
            node_id = match[1]
        reason = _clean(item.get("reason_code"))
        dispatch, effect, strength = _typed_outcome_shape(outcome, reason)
        stale = (
            EvidenceStaleState.STALE.value
            if "stale" in _normal(reason) else EvidenceStaleState.FRESH.value
        )
        evidence_id = _stable_id("evidence_", {
            "source": "turn_terminality_typed_evidence",
            "index": index,
            "outcome": outcome,
            "attempt": attempt_id,
            "token": _clean(item.get("evidence_token")),
            "reason": reason,
        })
        normalized.append(EvidenceItem(
            evidence_id=evidence_id,
            source_type="turn_terminality_typed_evidence",
            source_attempt_id=attempt_id,
            goal_node_id=node_id,
            capability_id=capability_id,
            capability_family=UNKNOWN_STATE,
            observation_type=(
                "provider_or_tool_outcome" if outcome else "unknown"
            ),
            verifier_strength=strength,
            semantic_effect=(
                "goal_node:" + node_id if node_id else UNKNOWN_STATE
            ),
            observed_state=reason or outcome or UNKNOWN_STATE,
            supported_postcondition_ids=supported,
            dispatch_state=dispatch,
            effect_state=effect,
            stale_state=stale,
            evidence_revision=int(
                item.get("evidence_revision") or evidence_revision
            ),
        ))

    unknown_effects = terminality_snapshot.get("unknown_effects") or {}
    if isinstance(unknown_effects, Mapping):
        for key, raw in sorted(unknown_effects.items(), key=lambda pair: str(pair[0])):
            item = raw if isinstance(raw, Mapping) else {}
            attempt_id = _clean(item.get("attempt_id")) or _clean(key)
            node_id, capability_id = _node_for_attempt(
                attempt_id, strategy_snapshot
            )
            postcondition = _clean(item.get("postcondition"))
            matches = by_description.get(_normal(postcondition), ())
            match = next((
                candidate for candidate in matches
                if node_id and candidate[1] == node_id
            ), matches[0] if len(matches) == 1 else None)
            supported = (match[0],) if match is not None else ()
            if match is not None and not node_id:
                node_id = match[1]
            reason = _clean(item.get("reason_code"))
            outcome = _clean(item.get("outcome")) or "outcome_unknown"
            dispatch, effect, strength = _outcome_shape("outcome_unknown")
            evidence_id = _stable_id("evidence_", {
                "source": "turn_terminality_unknown_effect",
                "key": _clean(key),
                "attempt": attempt_id,
                "token": _clean(item.get("evidence_token")),
                "reason": reason,
            })
            normalized.append(EvidenceItem(
                evidence_id=evidence_id,
                source_type="turn_terminality_unknown_effect",
                source_attempt_id=attempt_id,
                goal_node_id=node_id,
                capability_id=capability_id,
                capability_family=UNKNOWN_STATE,
                observation_type="unresolved_effect_state",
                verifier_strength=strength,
                semantic_effect=(
                    "goal_node:" + node_id if node_id else UNKNOWN_STATE
                ),
                observed_state=reason or outcome,
                supported_postcondition_ids=supported,
                dispatch_state=dispatch,
                effect_state=effect,
                stale_state=EvidenceStaleState.FRESH.value,
                evidence_revision=int(
                    item.get("evidence_revision") or evidence_revision
                ),
            ))

    for index, item in enumerate(strategy_snapshot.get("attempts") or ()):
        if not isinstance(item, Mapping):
            continue
        capability_id = _clean(item.get("mechanism_id"))
        node_id = _clean(item.get("node_id"))
        status = _clean(item.get("status"))
        reason = _clean(item.get("failure_or_stagnation_reason"))
        if not capability_id and not status:
            continue
        dispatch, effect, strength = _outcome_shape(
            "outcome_unknown"
            if _normal(status) == "ambiguous_effect"
            else "failed_before_effect"
            if _normal(status) in {"failed", "stagnated", "inapplicable"}
            else "success_verified"
            if _normal(status) == "succeeded"
            else status
        )
        stale = (
            EvidenceStaleState.STALE.value
            if _normal(status) == "stale_target"
            else EvidenceStaleState.FRESH.value
        )
        evidence_id = _stable_id("evidence_", {
            "source": "execution_strategy_attempt",
            "index": index,
            "attempt": _clean(item.get("attempt_id")),
            "capability": capability_id,
            "status": status,
            "reason": reason,
        })
        normalized.append(EvidenceItem(
            evidence_id=evidence_id,
            source_type="execution_strategy_attempt",
            source_attempt_id=_clean(item.get("attempt_id")),
            goal_node_id=node_id,
            capability_id=capability_id,
            capability_family=UNKNOWN_STATE,
            observation_type="strategy_attempt_state",
            verifier_strength=strength,
            semantic_effect=(
                "goal_node:" + node_id if node_id else UNKNOWN_STATE
            ),
            observed_state=reason or status or UNKNOWN_STATE,
            supported_postcondition_ids=(),
            dispatch_state=dispatch,
            effect_state=effect,
            stale_state=stale,
            evidence_revision=evidence_revision,
        ))
    unique = {item.evidence_id: item for item in normalized}
    return tuple(unique[key] for key in sorted(unique))


def _verified_postconditions_from_evidence(
    contract: GoalContract,
    evidence: Iterable[EvidenceItem],
) -> tuple[str, ...]:
    node_by_postcondition = {
        postcondition_id(node.node_id, item.description): node
        for node in contract.nodes
        for item in node.observable_postconditions
    }
    verified: list[str] = []
    supplied = tuple(evidence)
    for identity, node in node_by_postcondition.items():
        relevant = tuple(
            item for item in supplied
            if identity in item.supported_postcondition_ids
            and item.stale_state != EvidenceStaleState.STALE.value
        )
        if not relevant:
            continue
        latest_revision = max(item.evidence_revision for item in relevant)
        current = tuple(
            item for item in relevant
            if item.evidence_revision >= latest_revision
        )
        if any(
            _normal(item.observed_state).replace(" ", "_")
            in _CONTRADICTION_STATES
            for item in current
        ):
            continue
        required_rank = (
            _VERIFIER_RANK[VerifierStrength.OBSERVABLE_STATE.value]
            if node.side_effect_class in _MUTATING_EFFECTS
            else _VERIFIER_RANK[VerifierStrength.STRUCTURED_RESULT.value]
        )
        if any(
            _VERIFIER_RANK.get(item.verifier_strength, 0) >= required_rank
            for item in current
        ):
            verified.append(identity)
    return tuple(sorted(verified))


def _derived_permission_facts(
    snapshot: Mapping[str, object],
    inputs: WorldStateInputs,
) -> tuple[PermissionFact, ...]:
    if inputs.permission_states:
        return inputs.permission_states
    state = _normal(snapshot.get("permission_state"))
    if state in {"allowed", "granted", "full_access"}:
        normalized = "permitted"
    elif state in {"denied", "user_denied", "revoked"}:
        normalized = "disallowed"
    elif state == "pending":
        normalized = "permission_required"
    else:
        normalized = "unknown"
    return (PermissionFact("*", normalized),)


def _derived_effect_facts(
    contract: GoalContract,
    evidence: Iterable[EvidenceItem],
    inputs: WorldStateInputs,
) -> tuple[EffectStateFact, ...]:
    facts = list(inputs.known_effect_states)
    nodes = {item.node_id: item for item in contract.nodes}
    for item in evidence:
        if item.effect_state == EffectState.UNKNOWN.value:
            continue
        node = nodes.get(item.goal_node_id)
        equivalence = (
            goal_node_equivalence_class(node)
            if node is not None else UNKNOWN_STATE
        )
        facts.append(EffectStateFact(
            attempt_id=item.source_attempt_id,
            capability_id=item.capability_id,
            equivalence_class=equivalence,
            effect_state=item.effect_state,
            goal_node_id=item.goal_node_id,
        ))
    unique = {
        (
            item.attempt_id,
            item.capability_id,
            item.equivalence_class,
            item.effect_state,
            item.goal_node_id,
        ): item
        for item in facts
    }
    return tuple(unique[key] for key in sorted(unique))


def _accessibility_quality(
    inputs: WorldStateInputs,
    evidence: Iterable[EvidenceItem],
) -> str:
    if _normal(inputs.accessibility_quality) not in {"", UNKNOWN_STATE}:
        return inputs.accessibility_quality
    states = {_normal(item.observed_state) for item in evidence}
    if any("target_missing" in item for item in states):
        return "target_missing"
    if any("accessibility_poor" in item or "poor_uia" in item for item in states):
        return "poor"
    return UNKNOWN_STATE


def build_world_state_view(
    goal_contract: GoalContract,
    *,
    terminality_snapshot: Mapping[str, Any],
    strategy_snapshot: Mapping[str, Any],
    inputs: WorldStateInputs | None = None,
) -> WorldStateView:
    """Build one reconstructable view from copied canonical-authority data."""

    if not isinstance(goal_contract, GoalContract):
        raise TypeError("goal_contract_required")
    if not isinstance(terminality_snapshot, Mapping):
        raise TypeError("terminality_snapshot_required")
    if not isinstance(strategy_snapshot, Mapping):
        raise TypeError("strategy_snapshot_required")
    supplied = inputs or WorldStateInputs()
    evidence = _normalized_evidence(
        goal_contract, terminality_snapshot, strategy_snapshot
    )
    _, by_id = _postcondition_index(goal_contract)
    verified = _verified_postconditions_from_evidence(goal_contract, evidence)
    contradicted = tuple(sorted(dict.fromkeys(
        item for item in supplied.contradicted_postconditions if item in by_id
    )))
    completed: list[str] = []
    for node in goal_contract.nodes:
        required = tuple(
            postcondition_id(node.node_id, item.description)
            for item in node.observable_postconditions
        )
        legacy_node = next((
            item for item in
            ((strategy_snapshot.get("mission") or {}).get("action_nodes") or ())
            if isinstance(item, Mapping)
            and _clean(item.get("node_id")) == node.node_id
        ), None)
        if (
            required and all(item in verified for item in required)
        ) or (
            not required
            and isinstance(legacy_node, Mapping)
            and _normal(legacy_node.get("status")) == "succeeded"
        ):
            completed.append(node.node_id)
    remaining_nodes = tuple(
        node.node_id for node in goal_contract.nodes
        if node.node_id not in completed
    )
    active = remaining_nodes[0] if remaining_nodes else ""
    remaining_postconditions = tuple(sorted(
        identity for identity in by_id
        if identity not in verified and identity not in contradicted
    ))
    effects = _derived_effect_facts(goal_contract, evidence, supplied)
    unknown_classes = tuple(sorted(dict.fromkeys(
        item.equivalence_class for item in effects
        if item.effect_state == EffectState.OUTCOME_UNKNOWN.value
    )))
    active_attempts = tuple(sorted(
        _clean(item) for item in
        (terminality_snapshot.get("active_attempts") or {})
        if _clean(item)
    ))
    pending_permission_ids = supplied.pending_permission_ids
    if not pending_permission_ids and _normal(
        terminality_snapshot.get("permission_state")
    ) == "pending":
        pending_permission_ids = active_attempts
    strategy_view = _strategy_view(strategy_snapshot)
    families = tuple(dict.fromkeys(
        _clean(item.get("label"))
        for item in strategy_snapshot.get("mechanisms", ())
        if isinstance(item, Mapping) and _clean(item.get("label"))
    ))
    if not families:
        families = tuple(dict.fromkeys(
            _clean(item.get("reason_selected"))
            for item in strategy_snapshot.get("mechanisms", ())
            if isinstance(item, Mapping) and _clean(item.get("reason_selected"))
        ))
    return WorldStateView(
        schema_version=WORLD_STATE_SCHEMA_VERSION,
        turn_id=_clean(terminality_snapshot.get("turn_id")),
        goal_contract_fingerprint=goal_contract.fingerprint,
        active_goal_node_id=active,
        completed_goal_node_ids=tuple(completed),
        remaining_goal_node_ids=remaining_nodes,
        current_application=_clean(
            supplied.current_application, fallback=UNKNOWN_STATE
        ),
        current_surface=_clean(
            supplied.current_surface, fallback=UNKNOWN_STATE
        ),
        current_account_profile=_clean(
            supplied.current_account_profile, fallback=UNKNOWN_STATE
        ),
        browser_state_summary=supplied.browser_state_summary,
        desktop_state_summary=supplied.desktop_state_summary,
        effective_model_capabilities=tuple(
            _clean(item) for item in supplied.effective_model_capabilities
            if _clean(item)
        ),
        vision_available=supplied.vision_available,
        permission_states=_derived_permission_facts(
            terminality_snapshot, supplied
        ),
        active_attempt_ids=active_attempts,
        pending_permission_ids=tuple(pending_permission_ids),
        known_effect_states=effects,
        outcome_unknown_equivalence_classes=unknown_classes,
        stale_refs=tuple(dict.fromkeys((
            *supplied.stale_refs,
            *strategy_view.stale_capability_ids,
        ))),
        accessibility_quality=_accessibility_quality(supplied, evidence),
        typed_observations=supplied.typed_observations,
        typed_evidence=evidence,
        verified_postconditions=verified,
        remaining_postconditions=remaining_postconditions,
        contradicted_postconditions=contradicted,
        strategy_snapshot=strategy_view,
        available_strategy_families=families,
        revision=max(1, int(supplied.revision)),
        evidence_revision=int(
            terminality_snapshot.get("evidence_revision") or 0
        ),
    )


__all__ = [
    "DispatchState",
    "EffectState",
    "EffectStateFact",
    "EvidenceItem",
    "EvidenceStaleState",
    "PermissionFact",
    "StateFact",
    "StateSummary",
    "StrategySnapshotView",
    "UNKNOWN_STATE",
    "WORLD_STATE_SCHEMA_VERSION",
    "WorldDelta",
    "WorldStateInputs",
    "WorldStateView",
    "build_world_state_view",
    "goal_node_equivalence_class",
    "postcondition_id",
]
