from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from benchmarks.wor260.schema import (
    BENCHMARK_SCHEMA_VERSION,
    CapabilityFixture,
    ScenarioValidationError,
    TARGET_COVERAGE_MATRIX,
    seed_scenarios,
    validate_scenario_spec,
)
from osia.capability_adapters import RuntimeAvailability


def test_target_coverage_matrix_is_explicit_and_totals_152():
    assert sum(item.target_count for item in TARGET_COVERAGE_MATRIX) == 152
    assert len({item.category_id for item in TARGET_COVERAGE_MATRIX}) == len(
        TARGET_COVERAGE_MATRIX
    )
    assert {item.category_id for item in TARGET_COVERAGE_MATRIX} == {
        "direct_api_mcp",
        "browser_dom",
        "desktop_uia",
        "keyboard_deep_link",
        "vision",
        "evidence_unknown",
        "permissions_identity",
        "continuation_multistep",
        "reasoning_retrieval",
    }


def test_seed_set_is_small_deterministic_and_covers_eleven_high_signal_cases():
    first = seed_scenarios()
    second = seed_scenarios()

    assert first == second
    assert len(first) == 11
    assert {item.scenario_id for item in first} == {
        "direct_api_exact_match",
        "deep_link_exact_match",
        "steam_library_false_web_fallback_rejected",
        "desktop_uia_rich_exact_target",
        "accessibility_poor_local_vision",
        "non_vision_model_excludes_vision",
        "stale_ref_requires_reresolution",
        "outcome_unknown_prevents_mutation_replay",
        "permission_required_action",
        "partial_multistep_success",
        "terminality_same_turn_continuation",
    }
    assert all(item.schema_version == BENCHMARK_SCHEMA_VERSION for item in first)


def test_seed_scenarios_are_serialization_safe_and_immutable():
    scenario = seed_scenarios()[0]
    encoded = json.dumps(scenario.to_dict(), sort_keys=True)

    assert scenario.scenario_id in encoded
    with pytest.raises(FrozenInstanceError):
        scenario.scenario_id = "changed"
    with pytest.raises(FrozenInstanceError):
        scenario.maximum_budgets.tool_calls = 999


def test_false_steam_store_fallback_is_explicitly_forbidden():
    scenario = next(
        item for item in seed_scenarios()
        if item.scenario_id == "steam_library_false_web_fallback_rejected"
    )

    assert scenario.expected_goal_contract.nodes[0].required_surface == "library"
    assert scenario.forbidden_capabilities == ("dom:steam_store",)
    assert scenario.forbidden_transitions[0].reason_code == (
        "required_surface_changed"
    )
    assert scenario.expected_terminal_state == "blocked_verified"


def test_non_vision_fixture_excludes_vision_without_treating_it_as_available():
    scenario = next(
        item for item in seed_scenarios()
        if item.scenario_id == "non_vision_model_excludes_vision"
    )

    assert scenario.available_capabilities[0].runtime_availability == (
        RuntimeAvailability.UNAVAILABLE.value
    )
    assert scenario.available_capabilities[0].capability_id in (
        scenario.forbidden_capabilities
    )
    assert scenario.acceptable_strategy_families == ()


def test_outcome_unknown_fixture_forbids_replay_and_caps_external_mutations():
    scenario = next(
        item for item in seed_scenarios()
        if item.scenario_id == "outcome_unknown_prevents_mutation_replay"
    )

    assert scenario.injected_receipts_events[0].outcome == "outcome_unknown"
    assert scenario.side_effect_constraints.replay_after_unknown_forbidden is True
    assert scenario.side_effect_constraints.maximum_external_mutations == 1
    assert scenario.forbidden_transitions[0].reason_code == (
        "mutation_replay_frozen"
    )


def test_multistep_seeds_retain_two_ordered_goal_nodes():
    scenarios = {
        item.scenario_id: item for item in seed_scenarios()
        if item.scenario_id in {
            "partial_multistep_success",
            "terminality_same_turn_continuation",
        }
    }

    for scenario in scenarios.values():
        assert [node.application for node in scenario.expected_goal_contract.nodes] == [
            "steam",
            "spotify",
        ]
        assert [item.capability_id for item in scenario.available_capabilities] == [
            "route:steam",
            "route:spotify",
        ]


def test_schema_validation_rejects_duplicate_capabilities_and_bad_terminal():
    scenario = seed_scenarios()[0]
    duplicate = replace(
        scenario,
        available_capabilities=(
            scenario.available_capabilities[0],
            CapabilityFixture(
                capability_id=scenario.available_capabilities[0].capability_id,
                family="direct_api",
                runtime_availability="available",
                side_effect_class="read_only",
            ),
        ),
        expected_terminal_state="model_says_done",
    )

    with pytest.raises(ScenarioValidationError) as exc:
        validate_scenario_spec(duplicate)
    assert "duplicate_capability_id" in exc.value.errors
    assert "invalid_expected_terminal_state" in exc.value.errors
