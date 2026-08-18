from __future__ import annotations

import hashlib

from benchmarks.wor260.phase5a import run_phase5a_benchmark
from osia.capability_federation import federated_capabilities
from osia.tool_contracts import static_tool_prompt_block


def test_all_request_differences_are_reasoning_fields_only():
    report = run_phase5a_benchmark(timing_iterations=1)

    assert report.metrics.reasoning_policy.payload_containment_rate == 1.0
    assert all(item.payload_diff_contained for item in report.outcomes)
    assert all(
        not item.payload_diff_paths
        for item in report.outcomes
        if not item.reasoning_enabled or item.group == "isolation"
    )


def test_non_configurable_and_unknown_payloads_are_byte_equivalent():
    report = run_phase5a_benchmark(timing_iterations=1)
    selected = {
        item.scenario_id: item
        for item in report.outcomes
        if item.scenario_id in {
            "capability_non_configurable", "capability_unknown_pair",
        }
    }

    assert set(selected) == {
        "capability_non_configurable", "capability_unknown_pair",
    }
    assert all(item.payload_diff_paths == () for item in selected.values())


def test_provider_prompt_and_tool_inventory_identity_are_unchanged():
    prompt = static_tool_prompt_block().encode("utf-8")

    assert len(prompt) == 11_609
    assert len(prompt.decode("utf-8").splitlines()) == 97
    assert hashlib.sha256(prompt).hexdigest() == (
        "7bc8974fe969caedfb5a48387c0326d5b1d021be60442f77ac4f51ed4769170a"
    )
    assert len(federated_capabilities("openai")) == 95
    assert len(federated_capabilities("chatgpt_account")) == 98


def test_invariant_payload_fields_never_appear_in_diff_paths():
    report = run_phase5a_benchmark(timing_iterations=1)
    forbidden = {"model", "messages", "tools", "temperature"}

    assert all(
        forbidden.isdisjoint(item.payload_diff_paths) for item in report.outcomes
    )


def test_chatgpt_account_exact_metadata_reuses_effort_payload_path():
    report = run_phase5a_benchmark(timing_iterations=1)
    outcome = next(
        item for item in report.outcomes
        if item.scenario_id == "capability_chatgpt_metadata"
    )

    assert outcome.applied is True
    assert outcome.payload_diff_paths == ("effort",)
    assert outcome.payload_diff_contained is True
