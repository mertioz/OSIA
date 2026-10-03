"""Immutable GoalContract authority derived from one semantic Chat mission.

The contract is a deterministic, serialization-safe projection: execution can
update evidence and world state without rewriting the user's semantic target or
negative constraints.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from osia.execution_strategy import (
    SemanticActionNode,
    SemanticMission,
    explicit_negative_constraints,
    is_desktop_visual_read_node,
)
from osia.tool_contracts import SideEffectClass


GOAL_CONTRACT_SCHEMA_VERSION = "1.0"
NOT_SPECIFIED = "not_specified"


class ConditionOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    OBSERVABLE = "observable"


class EvidenceRequirement(str, Enum):
    """Evidence modality explicitly required by the user's semantic goal."""

    NONE = "none"
    STRUCTURED = "structured"
    VISUAL = "visual"
    MIXED = "mixed"
    ANY_RELIABLE = "any_reliable"


@dataclass(frozen=True)
class GoalConstraint:
    """One explicit constraint not already represented by a node field."""

    name: str
    value: str
    source: str = "semantic_mission"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class StateCondition:
    """A serialization-safe required or forbidden state assertion."""

    attribute: str
    operator: str
    value: str
    scope: str = NOT_SPECIFIED

    def semantic_key(self) -> tuple[str, str, str, str]:
        return tuple(
            " ".join(str(item or "").casefold().split())
            for item in (self.attribute, self.operator, self.value, self.scope)
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "attribute": self.attribute,
            "operator": self.operator,
            "value": self.value,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class ObservablePostcondition:
    """An expected observation bound to the semantic target and surface."""

    description: str
    target: str = NOT_SPECIFIED
    application: str = NOT_SPECIFIED
    surface: str = NOT_SPECIFIED
    account_profile: str = NOT_SPECIFIED
    source: str = "semantic_node"

    def to_dict(self) -> dict[str, str]:
        return {
            "description": self.description,
            "target": self.target,
            "application": self.application,
            "surface": self.surface,
            "account_profile": self.account_profile,
            "source": self.source,
        }


@dataclass(frozen=True)
class GoalExecutionBinding:
    """Immutable Chat turn and selected provider identity."""

    conversation_id: str = NOT_SPECIFIED
    request_id: str = NOT_SPECIFIED
    turn_id: str = NOT_SPECIFIED
    run_id: str = NOT_SPECIFIED
    provider_id: str = NOT_SPECIFIED
    model_id: str = NOT_SPECIFIED
    effective_reasoning: str = NOT_SPECIFIED

    def to_dict(self) -> dict[str, str]:
        return {
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "effective_reasoning": self.effective_reasoning,
        }


@dataclass(frozen=True)
class GoalNodeContract:
    node_id: str
    objective: str
    target: str
    application: str
    required_surface: str
    required_account_profile: str
    constraints: tuple[GoalConstraint, ...]
    desired_state: tuple[StateCondition, ...]
    forbidden_false_success_states: tuple[StateCondition, ...]
    observable_postconditions: tuple[ObservablePostcondition, ...]
    side_effect_class: str
    read_only: bool
    mutation: bool
    source_semantic_node_fingerprint: str
    evidence_requirement: str = EvidenceRequirement.ANY_RELIABLE.value
    target_hint: dict[str, Any] | None = None
    conditional_mode: str = ""
    conditional_branches: tuple[dict[str, Any], ...] = ()
    requires_observation_before_execution: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "objective": self.objective,
            "target": self.target,
            "application": self.application,
            "required_surface": self.required_surface,
            "required_account_profile": self.required_account_profile,
            "constraints": [item.to_dict() for item in self.constraints],
            "desired_state": [item.to_dict() for item in self.desired_state],
            "forbidden_false_success_states": [
                item.to_dict() for item in self.forbidden_false_success_states
            ],
            "observable_postconditions": [
                item.to_dict() for item in self.observable_postconditions
            ],
            "side_effect_class": self.side_effect_class,
            "read_only": self.read_only,
            "mutation": self.mutation,
            "source_semantic_node_fingerprint": (
                self.source_semantic_node_fingerprint
            ),
            "evidence_requirement": self.evidence_requirement,
            "target_hint": dict(self.target_hint or {}),
            "conditional_mode": self.conditional_mode,
            "conditional_branches": [
                dict(item) for item in self.conditional_branches
            ],
            "requires_observation_before_execution": (
                self.requires_observation_before_execution
            ),
        }

    def semantic_dict(self) -> dict[str, Any]:
        """Return canonical content without legacy/source identity material."""

        payload = self.to_dict()
        payload.pop("node_id", None)
        payload.pop("source_semantic_node_fingerprint", None)
        return payload


@dataclass(frozen=True)
class GoalContract:
    schema_version: str
    root_objective: str
    legacy_fingerprint: str
    nodes: tuple[GoalNodeContract, ...]
    global_constraints: tuple[GoalConstraint, ...]
    execution_binding: GoalExecutionBinding
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "root_objective": self.root_objective,
            "legacy_fingerprint": self.legacy_fingerprint,
            "nodes": [item.to_dict() for item in self.nodes],
            "global_constraints": [
                item.to_dict() for item in self.global_constraints
            ],
            "execution_binding": self.execution_binding.to_dict(),
            "fingerprint": self.fingerprint,
        }

    def semantic_dict(self) -> dict[str, Any]:
        """Return the exact content used for the adaptive fingerprint."""

        return {
            "schema_version": self.schema_version,
            "root_objective": self.root_objective,
            "nodes": [item.semantic_dict() for item in self.nodes],
            "global_constraints": [
                item.to_dict() for item in self.global_constraints
            ],
            "execution_binding": self.execution_binding.to_dict(),
        }


class GoalContractValidationError(ValueError):
    """Stable diagnostic failure used only by sidecar tests/evaluation."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(dict.fromkeys(str(item) for item in errors if item))
        super().__init__(self.errors[0] if self.errors else "invalid_goal_contract")


def _clean(value: object, *, fallback: str = NOT_SPECIFIED) -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _normalized_semantic_text(value: object) -> str:
    text = " ".join(str(value or "").casefold().split())
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )


def evidence_requirement_for_node(node: SemanticActionNode) -> EvidenceRequirement:
    """Classify requested evidence without making Vision globally primary."""

    text = _normalized_semantic_text(
        " ".join((node.source_clause, node.target_object))
    )
    appearance = bool(re.search(
        r"\b(?:background|theme|colour|color|shade|appearance|looks?\s+like|"
        r"visually|visual|image|photo|fond|theme|couleur|teinte|apparence|"
        r"a\s+quoi\s+ressemble|what(?:\s+do)?\s+you\s+see|"
        r"tu\s+vois|vous\s+voyez|que\s+vois)\b",
        text,
    ))
    visual_target_cue = bool(
        node.requested_action
        and re.search(
            r"\b(?:visible|visually|visuel|visuellement|red|green|blue|"
            r"yellow|orange|purple|pink|black|white|grey|gray|rouge|vert|"
            r"bleu|jaune|violet|rose|noir|blanc|gris)\b",
            text,
        )
    )
    visual = bool(
        is_desktop_visual_read_node(node) or appearance or visual_target_cue
    )
    if visual and node.requested_action:
        return EvidenceRequirement.MIXED
    if visual:
        return EvidenceRequirement.VISUAL
    structured = bool(re.search(
        r"\b(?:cpu|gpu|npu|memory|ram|usage|utilisation|api|connected|"
        r"connecte|status|statut|version|pid|process|running|en\s+cours)\b",
        text,
    ))
    if structured:
        return EvidenceRequirement.STRUCTURED
    return EvidenceRequirement.ANY_RELIABLE


def _node_side_effect(node: SemanticActionNode) -> str:
    """Conservatively project action semantics onto the existing OSIA enum."""

    action = _clean(node.requested_action, fallback="").casefold()
    if (
        action == "search"
        or not node.requires_execution
        or is_desktop_visual_read_node(node)
    ):
        return SideEffectClass.READ_ONLY.value
    if action in {"open", "navigate", "activate", "type"}:
        return SideEffectClass.REVERSIBLE_LOCAL.value
    if action == "send":
        return SideEffectClass.EXTERNAL_MUTATION.value
    return SideEffectClass.UNKNOWN_HIGH_RISK.value


def _node_postconditions(
    node: SemanticActionNode,
    *,
    target: str,
    application: str,
    surface: str,
    account: str,
) -> tuple[ObservablePostcondition, ...]:
    values = tuple(
        _clean(item, fallback="") for item in node.required_postconditions
        if _clean(item, fallback="")
    )
    if not values:
        fallback = _clean(node.observable_success_postcondition, fallback="")
        values = (fallback,) if fallback else ()
    return tuple(
        ObservablePostcondition(
            description=value,
            target=target,
            application=application,
            surface=surface,
            account_profile=account,
        )
        for value in values
    )


def _state_contracts(
    postconditions: tuple[ObservablePostcondition, ...],
    *,
    scope: str,
) -> tuple[tuple[StateCondition, ...], tuple[StateCondition, ...]]:
    desired = tuple(
        StateCondition(
            attribute="observable_postcondition",
            operator=ConditionOperator.EQUALS.value,
            value=item.description,
            scope=scope,
        )
        for item in postconditions
    )
    forbidden = tuple(
        StateCondition(
            attribute="observable_postcondition",
            operator=ConditionOperator.NOT_EQUALS.value,
            value=item.description,
            scope=scope,
        )
        for item in postconditions
    )
    return desired, forbidden


def _execution_binding(value: dict | None) -> GoalExecutionBinding:
    raw = value if isinstance(value, dict) else {}
    return GoalExecutionBinding(**{
        key: _clean(raw.get(key))
        for key in GoalExecutionBinding.__dataclass_fields__
    })


def _stable_node_id(node: SemanticActionNode, index: int) -> str:
    existing = _clean(node.node_id, fallback="")
    if existing:
        return existing
    material = json.dumps(
        {
            "index": index,
            "fingerprint": _clean(node.fingerprint, fallback=""),
            "objective": _clean(node.source_clause, fallback=""),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "node_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _contract_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def goal_contract_from_semantic_mission(
    mission: SemanticMission,
    *,
    execution_binding: dict | None = None,
) -> GoalContract:
    """Project one canonical semantic mission into immutable runtime authority."""

    if not isinstance(mission, SemanticMission):
        raise TypeError("mission_must_be_semantic_mission")

    global_constraints = tuple(
        GoalConstraint(
            name=str(item.get("name") or ""),
            value=str(item.get("value") or ""),
            source=str(item.get("source") or "user_objective"),
        )
        for item in explicit_negative_constraints(mission.objective)
        if str(item.get("name") or "") and str(item.get("value") or "")
    )
    nodes: list[GoalNodeContract] = []
    for index, source in enumerate(mission.action_nodes):
        target = _clean(source.target_object)
        application = _clean(source.required_application)
        surface = _clean(source.required_surface)
        account = _clean(source.required_account)
        objective = _clean(source.source_clause, fallback=mission.objective)
        postconditions = _node_postconditions(
            source,
            target=target,
            application=application,
            surface=surface,
            account=account,
        )
        desired, forbidden = _state_contracts(
            postconditions,
            scope=_stable_node_id(source, index),
        )
        if surface != NOT_SPECIFIED:
            forbidden = (*forbidden,
                StateCondition(
                    attribute="surface",
                    operator=ConditionOperator.NOT_EQUALS.value,
                    value=surface,
                    scope=application,
                ),
            )
        side_effect_class = _node_side_effect(source)
        nodes.append(GoalNodeContract(
            node_id=_stable_node_id(source, index),
            objective=objective,
            target=target,
            application=application,
            required_surface=surface,
            required_account_profile=account,
            constraints=global_constraints,
            desired_state=desired,
            forbidden_false_success_states=forbidden,
            observable_postconditions=postconditions,
            side_effect_class=side_effect_class,
            read_only=side_effect_class == SideEffectClass.READ_ONLY.value,
            mutation=side_effect_class != SideEffectClass.READ_ONLY.value,
            source_semantic_node_fingerprint=_clean(
                source.fingerprint, fallback=""
            ),
            evidence_requirement=evidence_requirement_for_node(source).value,
            target_hint=dict(source.target_hint or {}),
            conditional_mode=str(source.conditional_mode or ""),
            conditional_branches=tuple(
                dict(item) for item in source.conditional_branches
                if isinstance(item, dict)
            ),
            requires_observation_before_execution=(
                source.requires_observation_before_execution is True
            ),
        ))

    root_objective = _clean(mission.objective, fallback="")
    provisional = GoalContract(
        schema_version=GOAL_CONTRACT_SCHEMA_VERSION,
        root_objective=root_objective,
        legacy_fingerprint=_clean(mission.fingerprint, fallback=""),
        nodes=tuple(nodes),
        global_constraints=global_constraints,
        execution_binding=_execution_binding(execution_binding),
        fingerprint="",
    )
    contract = GoalContract(
        schema_version=provisional.schema_version,
        root_objective=provisional.root_objective,
        legacy_fingerprint=provisional.legacy_fingerprint,
        nodes=provisional.nodes,
        global_constraints=provisional.global_constraints,
        execution_binding=provisional.execution_binding,
        fingerprint=_contract_fingerprint(provisional.semantic_dict()),
    )
    return validate_goal_contract(contract)


def goal_contract_errors(contract: GoalContract) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(contract, GoalContract):
        return ("invalid_contract_type",)
    if contract.schema_version != GOAL_CONTRACT_SCHEMA_VERSION:
        errors.append("invalid_schema_version")
    if not contract.root_objective:
        errors.append("missing_root_objective")
    if not contract.nodes:
        errors.append("missing_goal_nodes")
    binding_values = tuple(contract.execution_binding.to_dict().values())
    binding_is_unspecified = all(
        value == NOT_SPECIFIED for value in binding_values
    )
    if not binding_is_unspecified and any(
        value == NOT_SPECIFIED or not value for value in binding_values
    ):
        errors.append("incomplete_execution_binding")

    node_ids: set[str] = set()
    valid_effects = {item.value for item in SideEffectClass}
    valid_operators = {item.value for item in ConditionOperator}
    valid_evidence_requirements = {item.value for item in EvidenceRequirement}
    for node in contract.nodes:
        if not node.node_id:
            errors.append("missing_node_identity")
        elif node.node_id in node_ids:
            errors.append("duplicate_node_identity")
        node_ids.add(node.node_id)
        if node.side_effect_class not in valid_effects:
            errors.append("invalid_side_effect_class")
        if node.evidence_requirement not in valid_evidence_requirements:
            errors.append("invalid_evidence_requirement")
        hint = node.target_hint or {}
        if not isinstance(hint, dict):
            errors.append("invalid_target_hint")
        elif hint:
            try:
                confidence = float(hint.get("confidence"))
            except (TypeError, ValueError, OverflowError):
                confidence = -1.0
            if not 0.0 <= confidence <= 1.0:
                errors.append("invalid_target_hint_confidence")
            if not str(hint.get("source") or ""):
                errors.append("missing_target_hint_source")
            hinted = _clean(
                hint.get("normalized_value"), fallback="",
            )
            if (
                hinted
                and node.application != NOT_SPECIFIED
                and _normalized_semantic_text(hinted)
                != _normalized_semantic_text(node.application)
            ):
                errors.append("target_hint_application_mismatch")
        if node.read_only == node.mutation:
            errors.append("invalid_read_only_mutation_state")
        if node.read_only != (
            node.side_effect_class == SideEffectClass.READ_ONLY.value
        ):
            errors.append("side_effect_read_only_mismatch")
        if node.conditional_mode:
            if node.conditional_mode != "exactly_one_observed_branch":
                errors.append("invalid_conditional_mode")
            branches = [
                item for item in node.conditional_branches
                if isinstance(item, dict)
            ]
            if len(branches) < 2:
                errors.append("conditional_branches_missing")
            branch_ids = {
                str(item.get("branch_id") or "") for item in branches
            }
            if "" in branch_ids or len(branch_ids) != len(branches):
                errors.append("conditional_branch_identity_invalid")
            if any(
                not str(item.get("target") or "")
                or str(item.get("requested_action") or "") not in {
                    "activate", "observe",
                }
                or not str(item.get("condition") or "")
                for item in branches
            ):
                errors.append("conditional_branch_invalid")
            if not node.requires_observation_before_execution:
                errors.append("conditional_observation_requirement_missing")
        elif node.conditional_branches:
            errors.append("conditional_mode_missing")

        desired = {item.semantic_key() for item in node.desired_state}
        forbidden = {
            item.semantic_key() for item in node.forbidden_false_success_states
        }
        if desired.intersection(forbidden):
            errors.append("required_state_is_also_forbidden")
        for state in (*node.desired_state, *node.forbidden_false_success_states):
            if state.operator not in valid_operators:
                errors.append("invalid_state_operator")
            if not state.attribute or not state.value:
                errors.append("state_condition_has_no_semantic_meaning")
        for postcondition in node.observable_postconditions:
            meaningful = any(
                value and value != NOT_SPECIFIED
                for value in (
                    postcondition.description,
                    postcondition.target,
                    postcondition.application,
                    postcondition.surface,
                    postcondition.account_profile,
                )
            )
            if not meaningful:
                errors.append("postcondition_has_no_semantic_meaning")

    expected = _contract_fingerprint(contract.semantic_dict())
    if contract.fingerprint != expected:
        errors.append("goal_contract_fingerprint_mismatch")
    return tuple(dict.fromkeys(errors))


def goal_contract_from_dict(value: dict) -> GoalContract:
    """Restore and validate one exact serialized canonical GoalContract."""

    if not isinstance(value, dict):
        raise GoalContractValidationError(("invalid_contract_payload",))

    def constraints(raw) -> tuple[GoalConstraint, ...]:
        return tuple(
            GoalConstraint(
                name=_clean(item.get("name"), fallback=""),
                value=_clean(item.get("value"), fallback=""),
                source=_clean(item.get("source"), fallback=""),
            )
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
        )

    def states(raw) -> tuple[StateCondition, ...]:
        return tuple(
            StateCondition(
                attribute=_clean(item.get("attribute"), fallback=""),
                operator=_clean(item.get("operator"), fallback=""),
                value=_clean(item.get("value"), fallback=""),
                scope=_clean(item.get("scope")),
            )
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
        )

    def postconditions(raw) -> tuple[ObservablePostcondition, ...]:
        return tuple(
            ObservablePostcondition(
                description=_clean(item.get("description"), fallback=""),
                target=_clean(item.get("target")),
                application=_clean(item.get("application")),
                surface=_clean(item.get("surface")),
                account_profile=_clean(item.get("account_profile")),
                source=_clean(item.get("source"), fallback=""),
            )
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
        )

    nodes = []
    migrated_evidence_requirement = False
    migrated_target_hint = False
    migrated_conditional_metadata = False
    for item in value.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        side_effect_class = _clean(
            item.get("side_effect_class"), fallback=""
        )
        raw_requirement = _clean(
            item.get("evidence_requirement"), fallback=""
        )
        if not raw_requirement:
            migrated_evidence_requirement = True
            legacy_probe = SemanticActionNode(
                target_object=_clean(item.get("target"), fallback=""),
                required_application=_clean(
                    item.get("application"), fallback=""
                ),
                required_surface=_clean(
                    item.get("required_surface"), fallback=""
                ),
                required_account=_clean(
                    item.get("required_account_profile"), fallback=""
                ),
                requested_action="",
                observable_success_postcondition="",
                fingerprint="",
                requires_execution=True,
                source_clause=_clean(item.get("objective"), fallback=""),
            )
            raw_requirement = evidence_requirement_for_node(legacy_probe).value
        raw_target_hint = item.get("target_hint")
        if not isinstance(raw_target_hint, dict):
            migrated_target_hint = True
            legacy_application = _clean(
                item.get("application"), fallback="",
            )
            raw_target_hint = (
                {
                    "raw_text": legacy_application,
                    "normalized_value": legacy_application,
                    "source": "legacy_goal_contract",
                    "confidence": 0.5,
                    "ambiguity": True,
                    "exactness": False,
                    "span": [0, 0],
                    "candidates": [legacy_application],
                }
                if legacy_application else {}
            )
        if any(
            key not in item
            for key in (
                "conditional_mode", "conditional_branches",
                "requires_observation_before_execution",
            )
        ):
            migrated_conditional_metadata = True
        nodes.append(GoalNodeContract(
            node_id=_clean(item.get("node_id"), fallback=""),
            objective=_clean(item.get("objective"), fallback=""),
            target=_clean(item.get("target")),
            application=_clean(item.get("application")),
            required_surface=_clean(item.get("required_surface")),
            required_account_profile=_clean(
                item.get("required_account_profile")
            ),
            constraints=constraints(item.get("constraints")),
            desired_state=states(item.get("desired_state")),
            forbidden_false_success_states=states(
                item.get("forbidden_false_success_states")
            ),
            observable_postconditions=postconditions(
                item.get("observable_postconditions")
            ),
            side_effect_class=side_effect_class,
            read_only=item.get("read_only") is True,
            mutation=item.get("mutation") is True,
            source_semantic_node_fingerprint=_clean(
                item.get("source_semantic_node_fingerprint"), fallback=""
            ),
            evidence_requirement=raw_requirement,
            target_hint=dict(raw_target_hint),
            conditional_mode=_clean(
                item.get("conditional_mode"), fallback="",
            ),
            conditional_branches=tuple(
                dict(branch) for branch in (
                    item.get("conditional_branches") or []
                )
                if isinstance(branch, dict)
            ),
            requires_observation_before_execution=(
                item.get("requires_observation_before_execution") is True
            ),
        ))
    binding_raw = value.get("execution_binding")
    contract = GoalContract(
        schema_version=_clean(value.get("schema_version"), fallback=""),
        root_objective=_clean(value.get("root_objective"), fallback=""),
        legacy_fingerprint=_clean(
            value.get("legacy_fingerprint"), fallback=""
        ),
        nodes=tuple(nodes),
        global_constraints=constraints(value.get("global_constraints")),
        execution_binding=_execution_binding(
            binding_raw if isinstance(binding_raw, dict) else None
        ),
        fingerprint=_clean(value.get("fingerprint"), fallback=""),
    )
    if (
        migrated_evidence_requirement
        or migrated_target_hint
        or migrated_conditional_metadata
    ):
        contract = GoalContract(
            schema_version=contract.schema_version,
            root_objective=contract.root_objective,
            legacy_fingerprint=contract.legacy_fingerprint,
            nodes=contract.nodes,
            global_constraints=contract.global_constraints,
            execution_binding=contract.execution_binding,
            fingerprint=_contract_fingerprint(contract.semantic_dict()),
        )
    return validate_goal_contract(contract)


def validate_goal_contract(contract: GoalContract) -> GoalContract:
    errors = goal_contract_errors(contract)
    if errors:
        raise GoalContractValidationError(errors)
    return contract


__all__ = [
    "ConditionOperator",
    "EvidenceRequirement",
    "GOAL_CONTRACT_SCHEMA_VERSION",
    "GoalConstraint",
    "GoalContract",
    "GoalExecutionBinding",
    "GoalContractValidationError",
    "GoalNodeContract",
    "NOT_SPECIFIED",
    "ObservablePostcondition",
    "StateCondition",
    "goal_contract_errors",
    "evidence_requirement_for_node",
    "goal_contract_from_dict",
    "goal_contract_from_semantic_mission",
    "validate_goal_contract",
]
