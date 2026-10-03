from __future__ import annotations

import copy
import json

from osia.capability_adapters import (
    CapabilityFamily,
    ModalityRequirement,
    Reversibility,
    RuntimeAvailability,
    UNKNOWN_METADATA,
    VerifierStrength,
    adapt_federated_capabilities,
    adapt_mcp_metadata_snapshot,
    adapt_model_provider_capabilities,
    adapt_tool_spec,
    build_desktop_capability_descriptor,
    model_capability_descriptor,
)
from osia.capability_federation import federated_capabilities
from osia.mcp_client.registry import registered_names
from osia.tool_contracts import (
    STATIC_TOOL_REGISTRY,
    SideEffectClass,
    dynamic_tool_specs,
    static_tool_prompt_block,
)


def test_family_vocabulary_covers_current_architecture_without_app_types():
    assert {item.value for item in CapabilityFamily} == {
        "osia_native",
        "provider_native",
        "mcp",
        "direct_api",
        "deep_link",
        "browser_dom",
        "desktop_uia",
        "desktop_keyboard",
        "vision_observation",
        "vision_actuation_local",
        "vision_actuation_global",
    }


def test_tool_spec_adapter_retains_ids_and_does_not_mutate_registry_or_prompt():
    names_before = STATIC_TOOL_REGISTRY.names()
    specs_before = STATIC_TOOL_REGISTRY.values()
    prompt_before = static_tool_prompt_block()

    adapted = tuple(adapt_tool_spec(spec) for spec in specs_before)

    assert tuple(item.capability_id for item in adapted) == names_before
    assert STATIC_TOOL_REGISTRY.names() == names_before
    assert STATIC_TOOL_REGISTRY.values() == specs_before
    assert static_tool_prompt_block() == prompt_before
    assert all(
        item.runtime_availability == RuntimeAvailability.UNKNOWN.value
        for item in adapted
    )


def test_federation_adapter_preserves_exact_identity_order_and_legacy_reference():
    legacy = federated_capabilities("chatgpt_account")
    adapted = adapt_federated_capabilities("chatgpt_account", legacy)

    assert tuple(item.capability_id for item in adapted) == tuple(
        item.identity for item in legacy
    )
    assert tuple(item.legacy_descriptor for item in adapted) == legacy
    assert all(
        item.runtime_availability == RuntimeAvailability.UNKNOWN.value
        for item, source in zip(adapted, legacy)
        if source.source == "osia"
    )
    assert [item.family for item in adapted[-3:]] == [
        CapabilityFamily.PROVIDER_NATIVE.value,
        CapabilityFamily.PROVIDER_NATIVE.value,
        CapabilityFamily.PROVIDER_NATIVE.value,
    ]


def test_unknown_mcp_metadata_stays_unknown_high_risk_and_side_effect_free():
    snapshot = [{
        "name": "perform_action",
        "description": "Perform one connector action",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    }]
    source_before = copy.deepcopy(snapshot)
    dynamic_before = dynamic_tool_specs()
    registered_before = registered_names()

    (descriptor,) = adapt_mcp_metadata_snapshot("fixture", snapshot)

    assert snapshot == source_before
    assert dynamic_tool_specs() == dynamic_before
    assert registered_names() == registered_before
    assert descriptor.capability_id == "mcp__fixture__perform_action"
    assert descriptor.runtime_availability == RuntimeAvailability.UNKNOWN.value
    assert descriptor.required_account_profile == UNKNOWN_METADATA
    assert descriptor.side_effect_class == SideEffectClass.UNKNOWN_HIGH_RISK.value
    assert descriptor.reversibility == Reversibility.UNKNOWN.value
    assert descriptor.permission_requirements[0].confirmation_policy == (
        "confirmation_required"
    )


def test_structural_mcp_read_only_hint_maps_only_to_existing_safe_semantics():
    (descriptor,) = adapt_mcp_metadata_snapshot(
        "fixture",
        [{
            "name": "lookup",
            "description": "Look up one record",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        }],
        runtime_availability=RuntimeAvailability.AVAILABLE,
    )

    assert descriptor.side_effect_class == SideEffectClass.READ_ONLY.value
    assert descriptor.reversibility == Reversibility.NOT_APPLICABLE.value
    assert descriptor.runtime_availability == RuntimeAvailability.AVAILABLE.value
    assert descriptor.idempotence == "idempotent"


def test_vision_uses_exact_authoritative_model_metadata_not_model_name_guessing():
    known_vision = adapt_model_provider_capabilities("openai", "gpt-5.4")
    known_text = adapt_model_provider_capabilities("deepseek", "deepseek-v4-pro")
    deceptive_unknown = adapt_model_provider_capabilities(
        "openai", "gpt-vision-looking-but-unknown"
    )

    assert known_vision.metadata_known is True
    assert known_vision.vision_supported is True
    assert known_text.metadata_known is True
    assert known_text.vision_supported is False
    assert deceptive_unknown.metadata_known is False
    assert deceptive_unknown.vision_supported is None
    assert ModalityRequirement.UNKNOWN.value in (
        model_capability_descriptor(deceptive_unknown).modality_requirements
    )


def test_account_model_uses_only_supplied_runtime_metadata_and_current_native_presence():
    capabilities = adapt_model_provider_capabilities(
        "chatgpt_account",
        "account-model",
        model_metadata={
            "id": "account-model",
            "inputModalities": ["text", "image"],
            "supportedReasoningEfforts": [{"id": "low"}, {"id": "high"}],
        },
    )

    assert capabilities.metadata_known is True
    assert capabilities.vision_supported is True
    assert capabilities.reasoning_controls == ("low", "high")
    assert capabilities.provider_native_capabilities == (
        "webSearch",
        "commandExecution",
        "fileChange",
    )


def test_non_vision_model_makes_vision_mechanism_unavailable_not_safe():
    model = adapt_model_provider_capabilities("deepseek", "deepseek-v4-pro")
    descriptor = build_desktop_capability_descriptor(
        "vision:local",
        CapabilityFamily.VISION_ACTUATION_LOCAL,
        model_capabilities=model,
    )

    assert descriptor.runtime_availability == RuntimeAvailability.UNAVAILABLE.value
    assert ModalityRequirement.VISION.value in descriptor.modality_requirements
    assert descriptor.side_effect_class == SideEffectClass.DYNAMICALLY_CLASSIFIED.value
    assert descriptor.reversibility == Reversibility.UNKNOWN.value


def test_unknown_effect_and_reversibility_never_default_to_read_only_or_safe():
    unknown_model = adapt_model_provider_capabilities("openai", "unknown-model")
    descriptor = build_desktop_capability_descriptor(
        "global:coordinates",
        CapabilityFamily.VISION_ACTUATION_GLOBAL,
        model_capabilities=unknown_model,
    )

    assert descriptor.runtime_availability == RuntimeAvailability.UNKNOWN.value
    assert descriptor.side_effect_class != SideEffectClass.READ_ONLY.value
    assert descriptor.reversibility == Reversibility.UNKNOWN.value
    assert descriptor.permission_requirements[0].category == UNKNOWN_METADATA


def test_tool_success_metadata_never_becomes_exact_postcondition_verification():
    spec = STATIC_TOOL_REGISTRY.get("write_file")
    assert spec is not None

    descriptor = adapt_tool_spec(spec)

    assert descriptor.verifier_strength in {
        VerifierStrength.STRUCTURED_RESULT.value,
        VerifierStrength.OBSERVABLE_STATE.value,
    }
    assert descriptor.verifier_strength != VerifierStrength.EXACT_POSTCONDITION.value
    json.dumps(descriptor.to_dict(), sort_keys=True)
