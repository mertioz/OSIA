"""Optional result notes and post-execution facts, never a procedural contract.

The selected model decides completion against the original request. Result notes
cannot grant execution authority, erase that request, or manufacture tool facts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Iterable, Mapping

OPEN_MARKER = "<osia_completion>"
CLOSE_MARKER = "</osia_completion>"


class CompletionValidationError(ValueError):
    """Invalid optional metadata or empty final answer."""


@dataclass(frozen=True)
class OutcomeRequirement:
    outcome_id: str
    result: str

    def to_dict(self) -> dict:
        return {"id": self.outcome_id, "result": self.result}


@dataclass(frozen=True)
class OutcomeContract:
    objective: str
    requirements: tuple[OutcomeRequirement, ...] = ()
    declared: bool = False

    def to_dict(self) -> dict:
        return {"objective": self.objective,
                "requirements": [r.to_dict() for r in self.requirements],
                "declared": self.declared}

    @classmethod
    def from_dict(cls, value: Mapping) -> "OutcomeContract":
        # Old persisted predicates are deliberately discarded on recovery.
        return cls(_text(value.get("objective"), "objective"),
                   tuple(_requirement(row) for row in value.get("requirements", ())),
                   bool(value.get("declared")))


@dataclass(frozen=True)
class CompletionProposal:
    answer: str


@dataclass(frozen=True)
class CompletionAssessment:
    contract: OutcomeContract
    complete: bool
    errors: tuple[str, ...] = ()
    factual_report: str = ""


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise CompletionValidationError("invalid_" + name)
    return value


def normalize_fact_path(value: str) -> str:
    """Use the same project-relative path identity for requirements and facts."""
    path = Path(_text(value, "fact_path"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    try:
        resolved = str(path.resolve(strict=False))
    except (OSError, ValueError, RuntimeError) as exc:
        raise CompletionValidationError("invalid_fact_path") from exc
    return os.path.normcase(resolved) if sys.platform == "win32" else resolved


def _requirement(row):
    if isinstance(row, str):
        result = _text(row, "outcome_result")
        return OutcomeRequirement("result_" + hashlib.sha256(result.encode()).hexdigest(), result)
    if not isinstance(row, Mapping):
        raise CompletionValidationError("invalid_outcome_requirement")
    return OutcomeRequirement(_text(row.get("id"), "outcome_id"),
                              _text(row.get("result"), "outcome_result"))


def new_outcome_contract(objective: str, required_postconditions: Iterable = ()) -> OutcomeContract:
    return OutcomeContract(_text(objective, "objective"),
                           tuple(_requirement(row) for row in required_postconditions))


def declare_outcomes(contract: OutcomeContract, rows: Iterable[Mapping]) -> OutcomeContract:
    """Append optional result notes; omission never removes retained results."""
    if not isinstance(rows, (list, tuple)):
        raise CompletionValidationError("invalid_outcome_notes")
    requirements = list(contract.requirements)
    for row in rows:
        item = _requirement(row)
        if item not in requirements:
            requirements.append(item)
    return OutcomeContract(contract.objective, tuple(requirements), True)


def parse_completion(text: str) -> CompletionProposal:
    # Older prompts may still emit an envelope. It is optional metadata, not
    # a source of evidence or a checklist the model must reproduce.
    answer = _text(text, "completion_answer").strip()
    if answer.endswith(CLOSE_MARKER) and OPEN_MARKER in answer:
        start = answer.rfind(OPEN_MARKER)
        try:
            payload = json.loads(answer[start + len(OPEN_MARKER):-len(CLOSE_MARKER)])
        except (ValueError, RecursionError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("outcomes"), list):
            answer = answer[:start].rstrip()
    return CompletionProposal(_text(answer, "completion_answer"))


def evaluate_completion(contract, proposal, receipts, *, turn_id: str) -> CompletionAssessment:
    """Report factual contradictions for the canonical model to consider.

    No command matching, inferred intent, model-declared predicates, or
    synthetic observations. This helper neither proves arbitrary prose nor
    terminalizes a turn. Contradictory process or stale-target facts are fed
    back to the same selected model for the next decision.
    """
    # Text prefixes are not technical facts. The caller decides how to handle
    # actual provider exceptions and typed ToolAttempt/ActionTruth outcomes.
    current = [r for r in receipts.values() if isinstance(r, Mapping)
               and r.get("turn_id") == turn_id and not r.get("superseded") and r.get("facts")]
    processes = [f for r in current if not r.get("stale")
                 and r.get("outcome") == "command_completed"
                 for f in r.get("facts", ()) if isinstance(f, Mapping)
                 and f.get("kind") == "process"
                 and type(f.get("properties", {}).get("exit_code")) is int]
    if processes and all(f["properties"]["exit_code"] != 0 for f in processes):
        codes = ", ".join(str(f["properties"]["exit_code"]) for f in processes)
        return CompletionAssessment(contract, False, ("no_successful_process_receipt",),
            "ERROR: Recorded commands exited with codes: " + codes +
            ". No successful process execution was observed.")
    if current and all(r.get("stale") for r in current):
        return CompletionAssessment(contract, False, ("only_stale_receipts",),
            "ERROR: The recorded results are stale; the current result is unverified.")
    return CompletionAssessment(contract, True)


def completion_instruction(contract: OutcomeContract | None = None) -> str:
    text = (
        "The original user objective remains authoritative, including results absent from optional notes. "
        "Choose tools and their order freely. declare_outcomes is optional result metadata only; "
        "no declaration or completion envelope is needed. Decide when the requested work is complete "
        "from actual tool results and observed state, then answer normally. "
        "Failures are feedback for repair; unknown outcomes are uncertainty, not proof of success. "
        "Do not claim success contradicted by receipts. Distinguish completed work from unverified results."
    )
    if contract is not None:
        text += " Original objective and optional result notes: " + json.dumps(contract.to_dict(), ensure_ascii=False)
    return text


def verified_fact_summary(assessment: CompletionAssessment) -> str:
    """Compatibility projection: never turn optional result notes into facts."""
    return assessment.factual_report


def declaration_instruction(contract: OutcomeContract | None = None) -> str:
    """Compatibility entry point; declarations are no longer required."""
    return completion_instruction(contract)
