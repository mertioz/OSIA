from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from benchmarks.wor260.phase2 import (
    benchmark_scenarios,
    fixture_descriptor,
    fixture_goal_contract,
    fixture_node,
)
from osia.capability_adapters import (
    CapabilityFamily,
    ModalityRequirement,
    RuntimeAvailability,
    VerifierStrength,
)
from osia.capability_search import (
    AttemptEffectState,
    CandidateEligibility,
    CapabilityPermissionState,
    CapabilitySearchLimits,
    PersonalBrowserAuthority,
    PermissionState,
    ShadowPlanningContext,
    StaleReference,
    search_capabilities,
)
from osia.tool_contracts import SideEffectClass


def _scenario(scenario_id: str):
    return next(
        item for item in benchmark_scenarios()
        if item.scenario_id == scenario_id
    )


def _result(scenario_id: str, *, full_scan: bool = False):
    scenario = _scenario(scenario_id)
    return search_capabilities(
        scenario.context,
        limits=CapabilitySearchLimits(
            retrieval_limit=scenario.limits.retrieval_limit,
            top_k=scenario.limits.top_k,
        ),
        full_scan=full_scan,
    )


def test_shadow_planning_context_and_search_records_are_immutable():
    context = _scenario("direct_api_exact_vs_ui").context
    result = search_capabilities(context)
    with pytest.raises(FrozenInstanceError):
        context.current_surface = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.top_candidates[0].eligibility = "changed"  # type: ignore[misc]


def test_hard_rejection_precedes_score_and_cannot_be_revived():
    scenario = _scenario("steam_library_vs_store")
    result = search_capabilities(scenario.context, full_scan=True)
    rejected = {item.capability_id: item for item in result.hard_rejected}
    store = rejected["browser:steam_store_search"]
    assert "required_surface_mismatch" in store.hard_rejection_reasons
    assert store.soft_score > 0
    assert store not in result.top_candidates
    assert result.top_candidates[0].capability_id == "uia:steam_library_search"


@pytest.mark.parametrize(
    ("state", "expected_eligibility", "reason"),
    (
        (PermissionState.PERMITTED.value, CandidateEligibility.ELIGIBLE.value, ""),
        (PermissionState.PERMISSION_REQUIRED.value, CandidateEligibility.UNRESOLVED.value, "permission_required"),
        (PermissionState.DISALLOWED.value, CandidateEligibility.HARD_REJECTED.value, "permission_disallowed"),
        (PermissionState.UNKNOWN.value, CandidateEligibility.UNRESOLVED.value, "permission_unknown"),
    ),
)
def test_permission_state_is_explicit_and_unknown_is_never_permitted(
    state: str, expected_eligibility: str, reason: str
):
    base = _scenario("permission_permitted")
    capability_id = base.context.capabilities[0].capability_id
    context = replace(
        base.context,
        permission_states=(CapabilityPermissionState(capability_id, state),),
    )
    result = search_capabilities(context, full_scan=True)
    candidates = (*result.eligible, *result.unresolved, *result.hard_rejected)
    candidate = next(item for item in candidates if item.capability_id == capability_id)
    assert candidate.permission_state == state
    assert candidate.eligibility == expected_eligibility
    if reason == "permission_disallowed":
        assert reason in candidate.hard_rejection_reasons
    elif reason:
        assert reason in candidate.unresolved_reasons


def test_unknown_availability_does_not_outrank_equivalent_available_candidate():
    result = _result("unknown_availability_vs_proven_available", full_scan=True)
    assert result.top_candidates[0].capability_id == "mcp:report_alpha_available"
    unknown = next(
        item for item in result.unresolved
        if item.capability_id == "api:report_alpha_unknown"
    )
    assert "runtime_unknown" in unknown.unresolved_reasons


def test_nonvision_authority_removes_vision_candidate_without_model_name_inference():
    result = _result("target_missing_nonvision", full_scan=True)
    observation = next(
        item for item in result.hard_rejected
        if item.capability_id == "vision:observe_control"
    )
    assert "modality_unavailable" in observation.hard_rejection_reasons
    assert observation.modality_match == "unavailable"

    contradictory = replace(
        _scenario("target_missing_with_vision").context,
        authoritative_vision_available=True,
        effective_model_modalities=("text",),
    )
    conservative = search_capabilities(contradictory, full_scan=True)
    assert any(
        item.capability_id == "vision:observe_control"
        and "modality_unavailable" in item.hard_rejection_reasons
        for item in conservative.hard_rejected
    )


def test_personal_browser_exact_authority_wrong_tab_and_unknown_are_distinct():
    exact = _result("personal_browser_exact_tab", full_scan=True)
    assert exact.top_candidates[0].eligibility == CandidateEligibility.ELIGIBLE.value

    wrong = _result("personal_browser_wrong_tab", full_scan=True)
    assert "personal_browser_authority_mismatch" in (
        wrong.hard_rejected[0].hard_rejection_reasons
    )

    unknown = _result("personal_browser_unknown_authority", full_scan=True)
    assert "personal_browser_authority_unknown" in (
        unknown.unresolved[0].unresolved_reasons
    )


def test_stale_reference_cannot_actuate_without_safe_reresolution():
    rejected = _result("stale_ref_no_direct_actuation", full_scan=True)
    candidate = rejected.hard_rejected[0]
    assert "stale_ref_cannot_actuate" in candidate.hard_rejection_reasons

    resolvable = _result("desktop_stale_ref_resolve", full_scan=True)
    stale = next(
        item for item in resolvable.unresolved
        if item.capability_id == "uia:stale_save"
    )
    assert "stale_ref_requires_reresolution" in stale.unresolved_reasons


def test_outcome_unknown_hard_rejects_equivalent_mutation_but_not_reconcile():
    result = _result("outcome_unknown_blocks_mutation_replay", full_scan=True)
    mutation = next(
        item for item in result.hard_rejected
        if item.capability_id == "api:send_message"
    )
    assert "outcome_unknown_replay_blocked" in mutation.hard_rejection_reasons
    assert any(
        item.capability_id == "api:reconcile_message"
        for item in result.eligible
    )


def test_full_scan_restores_eligible_candidate_and_records_reason():
    scenario = _scenario("full_scan_recovery")
    limits = CapabilitySearchLimits(
        retrieval_limit=scenario.limits.retrieval_limit,
        top_k=scenario.limits.top_k,
    )
    initial = search_capabilities(scenario.context, limits=limits)
    assert "opaque:7f3a" not in initial.initially_retrieved
    full = search_capabilities(scenario.context, limits=limits, full_scan=True)
    assert full.full_scan_performed
    assert full.full_scan_recovered_ids == ("opaque:7f3a",)
    restored = next(item for item in full.eligible if item.capability_id == "opaque:7f3a")
    assert restored.restored_by_full_scan
    assert "full_scan_recovered_candidate" in restored.explanation_codes


def test_exact_deterministic_route_is_force_retained_before_top_k():
    scenario = _scenario("deterministic_candidate_force_retained")
    result = search_capabilities(
        scenario.context,
        limits=CapabilitySearchLimits(retrieval_limit=1, top_k=2),
    )
    assert "route:opaque_deterministic" in result.force_retained
    assert "route:opaque_deterministic" in result.initially_retrieved


def test_search_result_contains_no_descriptor_handle_or_tool_payload():
    result = _result("direct_api_exact_vs_ui", full_scan=True)
    assert not hasattr(result, "descriptors")
    assert not hasattr(result, "tool_payload")
    for candidate in (*result.eligible, *result.unresolved, *result.hard_rejected):
        assert not hasattr(candidate, "execute")
        assert not hasattr(candidate, "tool_payload")
