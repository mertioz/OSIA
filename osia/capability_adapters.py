"""Pure WOR-260 capability metadata sidecars.

Nothing in this module participates in runtime selection, authorization,
dispatch, prompts, or terminality.  Adapters only project explicitly supplied
metadata or existing read-only registries into immutable descriptive records.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from osia.capability_federation import CapabilityDescriptor
from osia.tool_contracts import (
    ConfirmationPolicy,
    IdempotencyClass,
    ProgressEvidencePolicy,
    RetryPolicy,
    SideEffectClass,
    ToolSpec,
)


CAPABILITY_DESCRIPTOR_SCHEMA_VERSION = "1.0"
UNKNOWN_METADATA = "unknown"


class CapabilityFamily(str, Enum):
    OSIA_NATIVE = "osia_native"
    PROVIDER_NATIVE = "provider_native"
    MCP = "mcp"
    DIRECT_API = "direct_api"
    DEEP_LINK = "deep_link"
    BROWSER_DOM = "browser_dom"
    DESKTOP_UIA = "desktop_uia"
    DESKTOP_KEYBOARD = "desktop_keyboard"
    VISION_OBSERVATION = "vision_observation"
    VISION_ACTUATION_LOCAL = "vision_actuation_local"
    VISION_ACTUATION_GLOBAL = "vision_actuation_global"


class RuntimeAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class VerifierStrength(str, Enum):
    """Descriptive evidence strength; never a terminality authority."""

    NONE = "none"
    WEAK_UNSTRUCTURED = "weak_unstructured"
    PROVIDER_REPORTED = "provider_reported"
    STRUCTURED_RESULT = "structured_result"
    OBSERVABLE_STATE = "observable_state"
    EXACT_POSTCONDITION = "exact_postcondition"


class Reversibility(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    REVERSIBLE = "reversible"
    CONDITIONALLY_REVERSIBLE = "conditionally_reversible"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class ModalityRequirement(str, Enum):
    NONE = "none"
    TEXT = "text"
    BROWSER_DOM = "browser_dom"
    ACCESSIBILITY = "accessibility"
    KEYBOARD_WITH_OBSERVABLE_FOCUS = "keyboard_with_observable_focus"
    VISION = "vision"
    LOCAL_REGION = "local_region"
    GLOBAL_COORDINATES = "global_coordinates"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SemanticInput:
    name: str
    required: bool
    schema_types: tuple[str, ...]
    enum_values: tuple[str, ...] = ()
    format: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "schema_types": list(self.schema_types),
            "enum_values": list(self.enum_values),
            "format": self.format,
        }


@dataclass(frozen=True)
class PermissionRequirement:
    category: str
    confirmation_policy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "confirmation_policy": self.confirmation_policy,
        }


@dataclass(frozen=True)
class DeterministicConstraint:
    name: str
    value: str
    source_authority: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value,
            "source_authority": self.source_authority,
        }


@dataclass(frozen=True)
class AuthoritativeModelCapabilities:
    provider: str
    model_id: str
    metadata_known: bool
    vision_supported: bool | None
    reasoning_controls: tuple[str, ...]
    provider_native_capabilities: tuple[str, ...]
    source_authority: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "metadata_known": self.metadata_known,
            "vision_supported": self.vision_supported,
            "reasoning_controls": list(self.reasoning_controls),
            "provider_native_capabilities": list(
                self.provider_native_capabilities
            ),
            "source_authority": self.source_authority,
        }


@dataclass(frozen=True)
class AdaptiveCapabilityDescriptor:
    """Immutable hard/soft metadata wrapper around an optional legacy record."""

    schema_version: str
    capability_id: str
    family: str
    semantic_inputs: tuple[SemanticInput, ...]
    semantic_effects: tuple[str, ...]
    preconditions: tuple[str, ...]
    permission_requirements: tuple[PermissionRequirement, ...]
    supported_surfaces: tuple[str, ...]
    required_account_profile: str
    modality_requirements: tuple[str, ...]
    runtime_availability: str
    verifier_strength: str
    side_effect_class: str
    reversibility: str
    anti_replay_equivalence_class: str
    deterministic_constraints: tuple[DeterministicConstraint, ...]
    source_authority: str
    idempotence: str = UNKNOWN_METADATA
    retry_policy: str = UNKNOWN_METADATA
    live_eligibility_authority: str = UNKNOWN_METADATA
    description: str = ""
    latency_class: str = UNKNOWN_METADATA
    cost_class: str = UNKNOWN_METADATA
    reliability_prior: str = UNKNOWN_METADATA
    ergonomic_preference: str = UNKNOWN_METADATA
    legacy_descriptor: CapabilityDescriptor | None = None

    def hard_metadata_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "family": self.family,
            "semantic_inputs": [item.to_dict() for item in self.semantic_inputs],
            "semantic_effects": list(self.semantic_effects),
            "preconditions": list(self.preconditions),
            "permission_requirements": [
                item.to_dict() for item in self.permission_requirements
            ],
            "supported_surfaces": list(self.supported_surfaces),
            "required_account_profile": self.required_account_profile,
            "modality_requirements": list(self.modality_requirements),
            "runtime_availability": self.runtime_availability,
            "verifier_strength": self.verifier_strength,
            "side_effect_class": self.side_effect_class,
            "reversibility": self.reversibility,
            "anti_replay_equivalence_class": (
                self.anti_replay_equivalence_class
            ),
            "deterministic_constraints": [
                item.to_dict() for item in self.deterministic_constraints
            ],
            "source_authority": self.source_authority,
            "idempotence": self.idempotence,
            "retry_policy": self.retry_policy,
            "live_eligibility_authority": self.live_eligibility_authority,
            "description": self.description,
        }

    def soft_metadata_dict(self) -> dict[str, str]:
        return {
            "latency_class": self.latency_class,
            "cost_class": self.cost_class,
            "reliability_prior": self.reliability_prior,
            "ergonomic_preference": self.ergonomic_preference,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.hard_metadata_dict()
        payload["soft_metadata"] = self.soft_metadata_dict()
        payload["legacy_reference"] = (
            asdict(self.legacy_descriptor)
            if self.legacy_descriptor is not None
            else None
        )
        return payload


class CapabilityMetadataError(ValueError):
    pass


def _clean(value: object, *, fallback: str = UNKNOWN_METADATA) -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _enum_value(value: object, enum_type: type[Enum]) -> str:
    candidate = value.value if isinstance(value, enum_type) else str(value or "")
    try:
        return str(enum_type(candidate).value)
    except ValueError as exc:
        raise CapabilityMetadataError(
            f"invalid_{enum_type.__name__.lower()}"
        ) from exc


def _schema_types(schema: object) -> tuple[str, ...]:
    if not isinstance(schema, Mapping):
        return (UNKNOWN_METADATA,)
    kinds: list[str] = []
    direct = schema.get("type")
    if isinstance(direct, str) and direct:
        kinds.append(direct)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if not isinstance(branches, (list, tuple)):
            continue
        for branch in branches:
            if not isinstance(branch, Mapping):
                continue
            kind = branch.get("type")
            if isinstance(kind, str) and kind not in kinds and kind != "null":
                kinds.append(kind)
    return tuple(kinds) or (UNKNOWN_METADATA,)


def _semantic_inputs(schema: object) -> tuple[SemanticInput, ...]:
    if not isinstance(schema, Mapping):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    raw_required = schema.get("required")
    required = {
        str(item) for item in (
            raw_required if isinstance(raw_required, (list, tuple)) else ()
        )
        if isinstance(item, str)
    }
    inputs = []
    for raw_name, raw_schema in properties.items():
        name = str(raw_name)
        field_schema = raw_schema if isinstance(raw_schema, Mapping) else {}
        raw_enum = field_schema.get("enum")
        enum_values = tuple(
            _clean(item, fallback="")
            for item in (raw_enum if isinstance(raw_enum, (list, tuple)) else ())
            if _clean(item, fallback="")
        )
        inputs.append(SemanticInput(
            name=name,
            required=name in required,
            schema_types=_schema_types(field_schema),
            enum_values=enum_values,
            format=_clean(field_schema.get("format"), fallback=""),
        ))
    return tuple(inputs)


def _input_fingerprint(inputs: tuple[SemanticInput, ...]) -> str:
    encoded = json.dumps(
        [item.to_dict() for item in inputs],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reversibility(side_effect: str) -> str:
    mapping = {
        SideEffectClass.READ_ONLY.value: Reversibility.NOT_APPLICABLE.value,
        SideEffectClass.REVERSIBLE_LOCAL.value: Reversibility.REVERSIBLE.value,
        SideEffectClass.PERSISTENT_LOCAL.value: (
            Reversibility.CONDITIONALLY_REVERSIBLE.value
        ),
        SideEffectClass.IRREVERSIBLE_EXTERNAL.value: (
            Reversibility.IRREVERSIBLE.value
        ),
    }
    return mapping.get(side_effect, Reversibility.UNKNOWN.value)


def _tool_verifier_strength(spec: ToolSpec) -> str:
    # A successful tool return is never promoted to exact_postcondition here.
    if spec.progress_evidence in {
        ProgressEvidencePolicy.VERIFIED_EFFECT,
        ProgressEvidencePolicy.DOCUMENT_ARTIFACT,
        ProgressEvidencePolicy.VERIFIED_FORM_STATE,
    }:
        return VerifierStrength.OBSERVABLE_STATE.value
    if spec.verified_success:
        return VerifierStrength.STRUCTURED_RESULT.value
    if spec.postcondition_required:
        return VerifierStrength.NONE.value
    return VerifierStrength.WEAK_UNSTRUCTURED.value


def _tool_surfaces(spec: ToolSpec) -> tuple[str, ...]:
    """Surfaces where the capability is usable (WOR-301).

    WOR-303: there is one canonical Chat execution surface plus the Scheduled
    temporal trigger.  Every capability is usable from Chat (OSIA is
    permanently full-access), so historical eligibility flags are never
    authoritative for dispatch.
    """

    del spec
    return ("chat", "scheduled")


def _runtime_value(value: object) -> str:
    return _enum_value(value, RuntimeAvailability)


def _family_value(value: object) -> str:
    return _enum_value(value, CapabilityFamily)


def _constraint(name: str, value: object, authority: str) -> DeterministicConstraint:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = _clean(value)
    return DeterministicConstraint(name, rendered, authority)


def validate_capability_descriptor(
    descriptor: AdaptiveCapabilityDescriptor,
) -> AdaptiveCapabilityDescriptor:
    if descriptor.schema_version != CAPABILITY_DESCRIPTOR_SCHEMA_VERSION:
        raise CapabilityMetadataError("invalid_schema_version")
    if not descriptor.capability_id:
        raise CapabilityMetadataError("missing_capability_id")
    _family_value(descriptor.family)
    _runtime_value(descriptor.runtime_availability)
    _enum_value(descriptor.verifier_strength, VerifierStrength)
    _enum_value(descriptor.reversibility, Reversibility)
    if descriptor.side_effect_class not in {item.value for item in SideEffectClass}:
        raise CapabilityMetadataError("invalid_side_effect_class")
    return descriptor


def adapt_tool_spec(
    spec: ToolSpec,
    *,
    legacy_descriptor: CapabilityDescriptor | None = None,
    family: CapabilityFamily | str | None = None,
    runtime_availability: RuntimeAvailability | str | None = None,
    source_authority: str = "osia.tool_contracts.ToolSpec",
) -> AdaptiveCapabilityDescriptor:
    """Project one ToolSpec without modifying or registering it."""

    if not isinstance(spec, ToolSpec):
        raise TypeError("spec_must_be_tool_spec")
    selected_family = family or (
        CapabilityFamily.MCP if spec.dynamic else CapabilityFamily.OSIA_NATIVE
    )
    if runtime_availability is None:
        # Static federation exposure is not a live dependency/application
        # health probe.  Connected MCP entries are the only ToolSpec-backed
        # legacy records whose presence currently proves runtime availability.
        availability = (
            RuntimeAvailability.AVAILABLE.value
            if legacy_descriptor is not None
            and legacy_descriptor.source == "mcp"
            and legacy_descriptor.available
            else RuntimeAvailability.UNAVAILABLE.value
            if legacy_descriptor is not None
            and legacy_descriptor.source == "mcp"
            else RuntimeAvailability.UNKNOWN.value
        )
    else:
        availability = _runtime_value(runtime_availability)
    inputs = _semantic_inputs(spec.input_schema)
    constraints = (
        _constraint("input_schema_fingerprint", _input_fingerprint(inputs), source_authority),
        _constraint("result_policy", spec.result_policy.value, source_authority),
        _constraint("observability", spec.observability.value, source_authority),
        _constraint("progress_evidence", spec.progress_evidence.value, source_authority),
        _constraint(
            "deterministic_test_status",
            spec.deterministic_test_status.value,
            source_authority,
        ),
        _constraint(
            "controlled_integration_status",
            spec.controlled_integration_status.value,
            source_authority,
        ),
        _constraint(
            "real_validation_requirement",
            spec.real_validation_requirement.value,
            source_authority,
        ),
        _constraint("verified_success", spec.verified_success, source_authority),
        _constraint(
            "postcondition_required", spec.postcondition_required, source_authority
        ),
        *((
            _constraint(
                "legacy_inventory_available",
                legacy_descriptor.available,
                "osia.capability_federation.CapabilityDescriptor",
            ),
        ) if legacy_descriptor is not None else ()),
    )
    descriptor = AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=spec.name,
        family=_family_value(selected_family),
        semantic_inputs=inputs,
        semantic_effects=(
            (_clean(spec.capability, fallback=""),)
            if _clean(spec.capability, fallback="")
            else ()
        ),
        preconditions=tuple(str(item) for item in spec.preconditions),
        permission_requirements=(PermissionRequirement(
            category=_clean(spec.permission_category, fallback="none"),
            confirmation_policy=spec.confirmation.value,
        ),),
        supported_surfaces=_tool_surfaces(spec),
        required_account_profile=UNKNOWN_METADATA,
        modality_requirements=(ModalityRequirement.UNKNOWN.value,),
        runtime_availability=availability,
        verifier_strength=_tool_verifier_strength(spec),
        side_effect_class=spec.side_effect.value,
        reversibility=_reversibility(spec.side_effect.value),
        anti_replay_equivalence_class=(
            _clean(legacy_descriptor.equivalence_class)
            if legacy_descriptor is not None
            else UNKNOWN_METADATA
        ),
        deterministic_constraints=constraints,
        source_authority=source_authority,
        idempotence=spec.idempotency.value,
        retry_policy=spec.retry.value,
        description=_clean(spec.capability, fallback=""),
        latency_class=(
            _clean(legacy_descriptor.latency_hint)
            if legacy_descriptor is not None
            else UNKNOWN_METADATA
        ),
        cost_class=(
            _clean(legacy_descriptor.cost_hint)
            if legacy_descriptor is not None
            else UNKNOWN_METADATA
        ),
        legacy_descriptor=legacy_descriptor,
    )
    return validate_capability_descriptor(descriptor)


def adapt_legacy_capability_descriptor(
    legacy: CapabilityDescriptor,
    *,
    tool_spec: ToolSpec | None = None,
) -> AdaptiveCapabilityDescriptor:
    """Wrap an active federation record while preserving its exact identity."""

    if not isinstance(legacy, CapabilityDescriptor):
        raise TypeError("legacy_must_be_capability_descriptor")
    if tool_spec is not None:
        family = (
            CapabilityFamily.MCP
            if legacy.source == "mcp"
            else CapabilityFamily.OSIA_NATIVE
        )
        return adapt_tool_spec(
            tool_spec,
            legacy_descriptor=legacy,
            family=family,
            source_authority=(
                "osia.capability_federation.CapabilityDescriptor+"
                "osia.tool_contracts.ToolSpec"
            ),
        )

    effect = (
        legacy.side_effect_class
        if legacy.side_effect_class in {item.value for item in SideEffectClass}
        else SideEffectClass.UNKNOWN_HIGH_RISK.value
    )
    observability = _clean(legacy.observability)
    verifier = (
        VerifierStrength.PROVIDER_REPORTED.value
        if "provider" in legacy.source or "typed_item" in observability
        else VerifierStrength.WEAK_UNSTRUCTURED.value
    )
    descriptor = AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=legacy.identity,
        family=CapabilityFamily.PROVIDER_NATIVE.value,
        semantic_inputs=(),
        semantic_effects=(
            (_clean(legacy.equivalence_class),)
            if legacy.equivalence_class
            else ()
        ),
        preconditions=(),
        permission_requirements=(PermissionRequirement(
            category=_clean(legacy.permission_requirement),
            confirmation_policy=_clean(legacy.permission_requirement),
        ),),
        supported_surfaces=(UNKNOWN_METADATA,),
        required_account_profile=UNKNOWN_METADATA,
        modality_requirements=(ModalityRequirement.UNKNOWN.value,),
        runtime_availability=(
            RuntimeAvailability.AVAILABLE.value
            if legacy.available
            else RuntimeAvailability.UNAVAILABLE.value
        ),
        verifier_strength=verifier,
        side_effect_class=effect,
        reversibility=_reversibility(effect),
        anti_replay_equivalence_class=_clean(legacy.equivalence_class),
        deterministic_constraints=(
            _constraint("mechanism", legacy.mechanism, "osia.capability_federation"),
            _constraint(
                "deterministic_strength",
                legacy.deterministic_strength,
                "osia.capability_federation",
            ),
            _constraint(
                "reconciliation", legacy.reconciliation, "osia.capability_federation"
            ),
        ),
        source_authority="osia.capability_federation.CapabilityDescriptor",
        idempotence=_clean(legacy.idempotence),
        retry_policy=UNKNOWN_METADATA,
        description="",
        latency_class=_clean(legacy.latency_hint),
        cost_class=_clean(legacy.cost_hint),
        legacy_descriptor=legacy,
    )
    return validate_capability_descriptor(descriptor)


def adapt_federated_capabilities(
    provider: str,
    descriptors: Iterable[CapabilityDescriptor] | None = None,
) -> tuple[AdaptiveCapabilityDescriptor, ...]:
    """Adapt a supplied/current federation snapshot without reordering it."""

    if descriptors is None:
        from osia.capability_federation import federated_capabilities

        source = federated_capabilities(str(provider or ""))
    else:
        source = tuple(descriptors)
    from osia.tool_contracts import get_tool_spec

    adapted = []
    for legacy in source:
        spec = (
            get_tool_spec(legacy.identity)
            if legacy.source in {"osia", "mcp"}
            else None
        )
        adapted.append(
            adapt_legacy_capability_descriptor(legacy, tool_spec=spec)
        )
    return tuple(adapted)


def adapt_mcp_metadata_snapshot(
    connector_id: str,
    tools: Iterable[Mapping[str, Any]],
    *,
    runtime_availability: RuntimeAvailability | str = RuntimeAvailability.UNKNOWN,
) -> tuple[AdaptiveCapabilityDescriptor, ...]:
    """Purely adapt explicit MCP list-tools metadata; never connect/register."""

    from osia.mcp_client.registry import (
        _valid_server_id,
        _valid_tool_name,
        classify_tool,
        tool_full_name,
    )

    authority = (
        "explicit_mcp_metadata_snapshot+"
        "osia.mcp_client.registry.classify_tool"
    )
    availability = _runtime_value(runtime_availability)
    descriptors = []
    if not _valid_server_id(str(connector_id or "")):
        return ()
    source_tools = tuple(tools)
    valid_names = [
        _clean(raw.get("name"), fallback="")
        for raw in source_tools
        if isinstance(raw, Mapping)
        and _valid_tool_name(_clean(raw.get("name"), fallback=""))
    ]
    duplicate_names = {
        name for name, count in Counter(valid_names).items() if count > 1
    }
    for raw in source_tools:
        if not isinstance(raw, Mapping):
            continue
        name = _clean(raw.get("name"), fallback="")
        if not name or not _valid_tool_name(name) or name in duplicate_names:
            continue
        annotations = (
            raw.get("annotations")
            if isinstance(raw.get("annotations"), Mapping)
            else {}
        )
        structural = bool(
            all(
                key not in annotations or isinstance(annotations.get(key), bool)
                for key in ("readOnlyHint", "destructiveHint")
            )
            and (
                "readOnlyHint" in annotations
                or annotations.get("destructiveHint") is True
            )
        )
        is_write = classify_tool(name, dict(annotations))
        if structural and not is_write:
            effect = SideEffectClass.READ_ONLY.value
            idempotence = IdempotencyClass.IDEMPOTENT.value
            retry = RetryPolicy.READ_ONCE_AFTER_CONFIRMED_FAILURE.value
            confirmation = ConfirmationPolicy.NO_CONFIRMATION.value
        elif structural:
            effect = SideEffectClass.EXTERNAL_MUTATION.value
            idempotence = IdempotencyClass.UNKNOWN.value
            retry = RetryPolicy.NEVER_AFTER_DISPATCH.value
            confirmation = ConfirmationPolicy.CONFIRMATION_REQUIRED.value
        else:
            effect = SideEffectClass.UNKNOWN_HIGH_RISK.value
            idempotence = IdempotencyClass.UNKNOWN.value
            retry = RetryPolicy.NEVER_AFTER_DISPATCH.value
            confirmation = ConfirmationPolicy.CONFIRMATION_REQUIRED.value
        inputs = _semantic_inputs(raw.get("inputSchema") or {})
        annotation_constraints = tuple(
            _constraint(f"mcp_annotation.{key}", annotations[key], authority)
            for key in sorted(annotations)
            if key in {
                "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"
            }
        )
        descriptor = AdaptiveCapabilityDescriptor(
            schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
            capability_id=tool_full_name(connector_id, name),
            family=CapabilityFamily.MCP.value,
            semantic_inputs=inputs,
            semantic_effects=(
                (_clean(raw.get("description"), fallback=""),)
                if _clean(raw.get("description"), fallback="")
                else ()
            ),
            preconditions=("connector_connected",),
            permission_requirements=(PermissionRequirement(
                category="none" if not is_write else "mcp_write",
                confirmation_policy=confirmation,
            ),),
            supported_surfaces=(UNKNOWN_METADATA,),
            required_account_profile=UNKNOWN_METADATA,
            modality_requirements=(ModalityRequirement.UNKNOWN.value,),
            runtime_availability=availability,
            verifier_strength=VerifierStrength.WEAK_UNSTRUCTURED.value,
            side_effect_class=effect,
            reversibility=_reversibility(effect),
            anti_replay_equivalence_class=UNKNOWN_METADATA,
            deterministic_constraints=(
                _constraint("input_schema_fingerprint", _input_fingerprint(inputs), authority),
                *annotation_constraints,
            ),
            source_authority=authority,
            idempotence=idempotence,
            retry_policy=retry,
            description=_clean(raw.get("description"), fallback=""),
        )
        descriptors.append(validate_capability_descriptor(descriptor))
    return tuple(descriptors)


def adapt_browser_capability(
    spec: ToolSpec,
    *,
    state_ref: object | None = None,
    runtime_availability: RuntimeAvailability | str = RuntimeAvailability.UNKNOWN,
) -> AdaptiveCapabilityDescriptor:
    """Build browser metadata without launching or binding a browser target."""

    base = adapt_tool_spec(
        spec,
        family=CapabilityFamily.BROWSER_DOM,
        runtime_availability=runtime_availability,
        source_authority="osia.tool_contracts.ToolSpec",
    )
    constraints = list(base.deterministic_constraints)
    authority = "osia.tool_contracts.ToolSpec"
    if state_ref is not None:
        authority += "+osia.tools.browser_state.BrowserStateRef"
        for name in ("schema_version", "page_generation", "document_epoch"):
            value = getattr(state_ref, name, None)
            if value is not None:
                constraints.append(_constraint(name, value, authority))
        constraints.append(_constraint(
            "state_ref_revalidation_required", True, authority
        ))
    modalities = [ModalityRequirement.BROWSER_DOM.value]
    descriptor = replace(
        base,
        supported_surfaces=("browser",),
        modality_requirements=tuple(modalities),
        verifier_strength=(
            VerifierStrength.OBSERVABLE_STATE.value
            if state_ref is not None
            else base.verifier_strength
        ),
        deterministic_constraints=tuple(constraints),
        source_authority=authority,
    )
    return validate_capability_descriptor(descriptor)


_DESKTOP_FAMILIES = frozenset({
    CapabilityFamily.DEEP_LINK,
    CapabilityFamily.DESKTOP_UIA,
    CapabilityFamily.DESKTOP_KEYBOARD,
    CapabilityFamily.VISION_OBSERVATION,
    CapabilityFamily.VISION_ACTUATION_LOCAL,
    CapabilityFamily.VISION_ACTUATION_GLOBAL,
})


def build_desktop_capability_descriptor(
    capability_id: str,
    family: CapabilityFamily | str,
    *,
    surface_ref: object | None = None,
    model_capabilities: AuthoritativeModelCapabilities | None = None,
    runtime_availability: RuntimeAvailability | str = RuntimeAvailability.UNKNOWN,
    deterministic_metadata: Mapping[str, object] | None = None,
    side_effect_class: SideEffectClass | str | None = None,
) -> AdaptiveCapabilityDescriptor:
    """Describe an existing desktop mechanism without observing or acting."""

    family_value = _family_value(family)
    selected_family = CapabilityFamily(family_value)
    if selected_family not in _DESKTOP_FAMILIES:
        raise CapabilityMetadataError("unsupported_desktop_family")
    authority = "WOR-248 ExecutionStrategy desktop mechanism metadata"
    effect = (
        side_effect_class.value
        if isinstance(side_effect_class, SideEffectClass)
        else str(side_effect_class or SideEffectClass.DYNAMICALLY_CLASSIFIED.value)
    )
    if effect not in {item.value for item in SideEffectClass}:
        raise CapabilityMetadataError("invalid_side_effect_class")
    verifier = VerifierStrength.WEAK_UNSTRUCTURED.value
    modalities: tuple[str, ...]
    preconditions: tuple[str, ...]
    semantic_effect = "desktop action"
    if selected_family is CapabilityFamily.DEEP_LINK:
        verifier = VerifierStrength.STRUCTURED_RESULT.value
        modalities = (ModalityRequirement.NONE.value,)
        preconditions = ("deterministic_route_available",)
        semantic_effect = "deterministic route activation"
    elif selected_family is CapabilityFamily.DESKTOP_UIA:
        verifier = VerifierStrength.OBSERVABLE_STATE.value
        modalities = (ModalityRequirement.ACCESSIBILITY.value,)
        preconditions = ("live_surface_ref_required",)
        semantic_effect = "native accessibility action"
    elif selected_family is CapabilityFamily.DESKTOP_KEYBOARD:
        verifier = VerifierStrength.OBSERVABLE_STATE.value
        modalities = (ModalityRequirement.KEYBOARD_WITH_OBSERVABLE_FOCUS.value,)
        preconditions = ("observable_focus_required",)
        semantic_effect = "keyboard navigation with observable focus"
    elif selected_family is CapabilityFamily.VISION_OBSERVATION:
        if side_effect_class is None:
            effect = SideEffectClass.READ_ONLY.value
        modalities = (ModalityRequirement.VISION.value,)
        preconditions = ("authoritative_model_vision_support_required",)
        semantic_effect = "visual observation"
    elif selected_family is CapabilityFamily.VISION_ACTUATION_LOCAL:
        modalities = (
            ModalityRequirement.VISION.value,
            ModalityRequirement.LOCAL_REGION.value,
        )
        preconditions = (
            "authoritative_model_vision_support_required",
            "live_local_surface_required",
        )
        semantic_effect = "local visual actuation"
    else:
        modalities = (
            ModalityRequirement.VISION.value,
            ModalityRequirement.GLOBAL_COORDINATES.value,
        )
        preconditions = (
            "authoritative_model_vision_support_required",
            "global_vision_last_resort",
        )
        semantic_effect = "global coordinate actuation"

    availability = _runtime_value(runtime_availability)
    constraints: list[DeterministicConstraint] = []
    if ModalityRequirement.VISION.value in modalities:
        support = (
            model_capabilities.vision_supported
            if model_capabilities is not None
            else None
        )
        constraints.append(_constraint(
            "authoritative_model_vision_support",
            UNKNOWN_METADATA if support is None else support,
            (
                model_capabilities.source_authority
                if model_capabilities is not None
                else authority
            ),
        ))
        if support is False:
            availability = RuntimeAvailability.UNAVAILABLE.value
    supported_surfaces = ("desktop",)
    if surface_ref is not None:
        kind = getattr(getattr(surface_ref, "surface_kind", None), "value", None)
        supported_surfaces = (_clean(kind, fallback="desktop"),)
        structural_epoch = getattr(surface_ref, "structural_epoch", None)
        if structural_epoch is not None:
            constraints.append(_constraint(
                "structural_epoch", structural_epoch,
                "osia.desktop_surface.DesktopSurfaceRef",
            ))
        constraints.append(_constraint(
            "surface_ref_revalidation_required", True,
            "osia.desktop_surface.DesktopSurfaceRef",
        ))
        authority += "+osia.desktop_surface.DesktopSurfaceRef"
    metadata = deterministic_metadata or {}
    for raw_name, value in sorted(
        metadata.items(), key=lambda item: str(item[0])
    ):
        constraints.append(_constraint(str(raw_name), value, authority))

    permission = (
        PermissionRequirement("none", ConfirmationPolicy.NO_CONFIRMATION.value)
        if effect == SideEffectClass.READ_ONLY.value
        else PermissionRequirement(UNKNOWN_METADATA, UNKNOWN_METADATA)
    )
    descriptor = AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=_clean(capability_id, fallback=""),
        family=family_value,
        semantic_inputs=(),
        semantic_effects=(semantic_effect,),
        preconditions=preconditions,
        permission_requirements=(permission,),
        supported_surfaces=supported_surfaces,
        required_account_profile=UNKNOWN_METADATA,
        modality_requirements=modalities,
        runtime_availability=availability,
        verifier_strength=verifier,
        side_effect_class=effect,
        reversibility=_reversibility(effect),
        anti_replay_equivalence_class=UNKNOWN_METADATA,
        deterministic_constraints=tuple(constraints),
        source_authority=authority,
        idempotence=UNKNOWN_METADATA,
        retry_policy=UNKNOWN_METADATA,
        description=semantic_effect,
    )
    return validate_capability_descriptor(descriptor)


def adapt_model_provider_capabilities(
    provider: str,
    model_id: str,
    *,
    model_metadata: Mapping[str, Any] | None = None,
    provider_native_capabilities: Iterable[str] | None = None,
) -> AuthoritativeModelCapabilities:
    """Read exact metadata only; unknown model names stay explicitly unknown."""

    provider_id = _clean(provider, fallback="").casefold()
    exact_model_id = _clean(model_id, fallback="")
    metadata = model_metadata if isinstance(model_metadata, Mapping) else None
    source_parts: list[str] = []
    if metadata is None and provider_id != "chatgpt_account":
        from osia.models import HARDCODED_MODELS

        metadata = next(
            (
                item for item in HARDCODED_MODELS
                if str(item.get("provider") or "").casefold() == provider_id
                and str(item.get("id") or "") == exact_model_id
            ),
            None,
        )
        if metadata is not None:
            source_parts.append("osia.models exact registry entry")
    elif metadata is not None:
        metadata_id = str(metadata.get("id") or exact_model_id)
        metadata_provider = str(metadata.get("provider") or provider_id).casefold()
        if metadata_id != exact_model_id or metadata_provider != provider_id:
            metadata = None
        else:
            source_parts.append("explicit authoritative model metadata snapshot")

    from osia.model_capabilities import effective_route_capabilities

    effective = effective_route_capabilities(
        provider_id,
        exact_model_id,
        model_metadata=metadata,
    )
    vision = effective.vision_supported
    reasoning_controls = effective.reasoning_controls
    if effective.metadata_known and metadata is None:
        source_parts.append(effective.source_authority)
        metadata = {
            "id": effective.model_id,
            "provider": effective.provider_id,
            "inputModalities": list(effective.input_modalities),
        }

    if not reasoning_controls and provider_id != "chatgpt_account":
        from osia.reasoning import MODEL_REASONING_CAPABILITIES

        capability = MODEL_REASONING_CAPABILITIES.get((provider_id, exact_model_id))
        if capability:
            reasoning_controls = tuple(
                str(item.get("id") or "")
                for item in capability.get("options") or ()
                if str(item.get("id") or "")
            )
            source_parts.append("osia.reasoning exact pair registry")

    if provider_native_capabilities is None:
        from osia.capability_federation import federated_capabilities

        native = tuple(
            item.identity
            for item in federated_capabilities(provider_id)
            if item.source in {"provider_native", "provider_hosted"}
        )
        if native:
            source_parts.append("osia.capability_federation current snapshot")
    else:
        native = tuple(str(item) for item in provider_native_capabilities)
        source_parts.append("explicit provider-native capability snapshot")

    return AuthoritativeModelCapabilities(
        provider=provider_id,
        model_id=exact_model_id,
        metadata_known=metadata is not None,
        vision_supported=vision,
        reasoning_controls=reasoning_controls,
        provider_native_capabilities=native,
        source_authority="+".join(source_parts) or UNKNOWN_METADATA,
    )


def model_capability_descriptor(
    capabilities: AuthoritativeModelCapabilities,
) -> AdaptiveCapabilityDescriptor:
    """Describe exact model/provider support without inferring availability."""

    if not isinstance(capabilities, AuthoritativeModelCapabilities):
        raise TypeError("invalid_model_capabilities")
    modalities = [ModalityRequirement.TEXT.value]
    if capabilities.vision_supported is True:
        modalities.append(ModalityRequirement.VISION.value)
    elif capabilities.vision_supported is None:
        modalities.append(ModalityRequirement.UNKNOWN.value)
    constraints = (
        _constraint(
            "metadata_known", capabilities.metadata_known,
            capabilities.source_authority,
        ),
        _constraint(
            "vision_supported",
            (
                UNKNOWN_METADATA
                if capabilities.vision_supported is None
                else capabilities.vision_supported
            ),
            capabilities.source_authority,
        ),
        _constraint(
            "reasoning_controls",
            ",".join(capabilities.reasoning_controls) or UNKNOWN_METADATA,
            capabilities.source_authority,
        ),
        _constraint(
            "provider_native_capabilities",
            ",".join(capabilities.provider_native_capabilities) or UNKNOWN_METADATA,
            capabilities.source_authority,
        ),
    )
    descriptor = AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=(
            f"model:{capabilities.provider}:{capabilities.model_id}"
        ),
        family=CapabilityFamily.PROVIDER_NATIVE.value,
        semantic_inputs=(),
        semantic_effects=("model inference",),
        preconditions=("provider_model_route_available",),
        permission_requirements=(PermissionRequirement(
            "none", ConfirmationPolicy.NO_CONFIRMATION.value,
        ),),
        supported_surfaces=(UNKNOWN_METADATA,),
        required_account_profile=UNKNOWN_METADATA,
        modality_requirements=tuple(modalities),
        runtime_availability=RuntimeAvailability.UNKNOWN.value,
        verifier_strength=VerifierStrength.NONE.value,
        side_effect_class=SideEffectClass.READ_ONLY.value,
        reversibility=Reversibility.NOT_APPLICABLE.value,
        anti_replay_equivalence_class=UNKNOWN_METADATA,
        deterministic_constraints=constraints,
        source_authority=capabilities.source_authority,
        idempotence=IdempotencyClass.IDEMPOTENT.value,
        retry_policy=UNKNOWN_METADATA,
        description="authoritative model/provider metadata projection",
    )
    return validate_capability_descriptor(descriptor)


__all__ = [
    "AdaptiveCapabilityDescriptor",
    "AuthoritativeModelCapabilities",
    "CAPABILITY_DESCRIPTOR_SCHEMA_VERSION",
    "CapabilityFamily",
    "CapabilityMetadataError",
    "DeterministicConstraint",
    "ModalityRequirement",
    "PermissionRequirement",
    "Reversibility",
    "RuntimeAvailability",
    "SemanticInput",
    "UNKNOWN_METADATA",
    "VerifierStrength",
    "adapt_browser_capability",
    "adapt_federated_capabilities",
    "adapt_legacy_capability_descriptor",
    "adapt_mcp_metadata_snapshot",
    "adapt_model_provider_capabilities",
    "adapt_tool_spec",
    "build_desktop_capability_descriptor",
    "model_capability_descriptor",
    "validate_capability_descriptor",
]
