"""Current completion notes are optional; dispatch facts remain factual."""

import json

import pytest

from osia.outcome_contract import (
    CLOSE_MARKER,
    OPEN_MARKER,
    CompletionValidationError,
    declare_outcomes,
    evaluate_completion,
    new_outcome_contract,
    parse_completion,
    verified_fact_summary,
)


def _receipt(outcome, facts, *, stale=False):
    return {
        "turn_id": "turn-a",
        "outcome": outcome,
        "facts": facts,
        "stale": stale,
    }


def test_plain_selected_model_prose_needs_no_completion_envelope():
    answer = "I have not verified the APK installation."
    assert parse_completion(answer).answer == answer


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Everything is complete.", "Everything is complete."),
        (f"Done.{OPEN_MARKER}{{}}{CLOSE_MARKER}",
         f"Done.{OPEN_MARKER}{{}}{CLOSE_MARKER}"),
        (f"Done.{OPEN_MARKER}" + json.dumps({"outcomes": []}) + CLOSE_MARKER,
         "Done."),
        (f"Done.{OPEN_MARKER}" + json.dumps({"outcomes": []}) + CLOSE_MARKER + " after",
         f"Done.{OPEN_MARKER}" + json.dumps({"outcomes": []}) + CLOSE_MARKER + " after"),
    ],
)
def test_optional_or_malformed_envelope_does_not_become_a_completion_gate(text, expected):
    assert parse_completion(text).answer == expected


def test_optional_result_notes_do_not_create_receipts_or_block_a_model_final():
    contract = new_outcome_contract(
        "Build and install the requested project",
        ("APK installed on Pixel",),
    )
    contract = declare_outcomes(contract, [
        {"id": "build", "result": "APK builds"},
    ])
    proposal = parse_completion("I have not verified installation.")
    assessment = evaluate_completion(contract, proposal, {}, turn_id="turn-a")

    assert assessment.complete
    assert assessment.contract == contract
    assert assessment.errors == ()
    assert verified_fact_summary(assessment) == ""


def test_outcome_unknown_remains_a_fact_and_does_not_veto_a_new_final():
    contract = new_outcome_contract("Run the requested action")
    unknown = _receipt(
        "outcome_unknown",
        [{"kind": "process", "properties": {}}],
    )
    assessment = evaluate_completion(
        contract,
        parse_completion("The selected model is finished."),
        {"unknown-receipt": unknown},
        turn_id="turn-a",
    )

    assert assessment.complete
    assert assessment.contract == contract
    assert verified_fact_summary(assessment) == ""


def test_actual_failed_process_receipt_stays_a_factual_contradiction():
    contract = new_outcome_contract("Build the requested project")
    failed = _receipt(
        "command_completed",
        [{"kind": "process", "properties": {"exit_code": 1}}],
    )
    assessment = evaluate_completion(
        contract,
        parse_completion("The build succeeded."),
        {"failed-build": failed},
        turn_id="turn-a",
    )

    assert not assessment.complete
    assert assessment.errors == ("no_successful_process_receipt",)
    assert "exited with codes: 1" in assessment.factual_report


def test_only_stale_receipts_produce_a_stale_fact_caveat():
    contract = new_outcome_contract("Open the requested page")
    stale = _receipt(
        "success_verified",
        [{"kind": "observation", "properties": {"url": "https://example.test/"}}],
        stale=True,
    )
    assessment = evaluate_completion(
        contract,
        parse_completion("The page is open."),
        {"old-page": stale},
        turn_id="turn-a",
    )

    assert not assessment.complete
    assert assessment.errors == ("only_stale_receipts",)
    assert "stale" in assessment.factual_report.lower()


def test_error_text_prefix_is_not_a_provider_fact_blocker():
    contract = new_outcome_contract("Run the requested action")
    assessment = evaluate_completion(
        contract, parse_completion("ERROR: Provider unavailable."), {},
        turn_id="turn-a",
    )
    assert assessment.complete
    assert assessment.errors == ()
    assert verified_fact_summary(assessment) == ""
