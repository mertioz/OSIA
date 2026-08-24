"""Deterministic WOR-269 observers and clocks (no sleeps, no live I/O)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from osia.action_truth import (
    ActionRecord,
    ObservationClassification,
    ObservationResult,
    transition_action_truth,
)


@dataclass
class FakeClock:
    current: float = 100.0

    def time(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += float(seconds)


class ScriptedObserver:
    def __init__(self, *results: ObservationResult, name: str = "scripted"):
        self.name = name
        self.results = list(results)

    def supports(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> bool:
        return True

    def observe(
        self, action: ActionRecord, current_world_state: Mapping[str, Any]
    ) -> ObservationResult:
        if not self.results:
            return ObservationResult(
                ObservationClassification.FAILED,
                self.name,
                "scripted_observer_exhausted",
            )
        return self.results.pop(0)


class MemoryActionStore:
    """Small restartable store implementing the coordinator protocol."""

    def __init__(self, attempts: list[dict[str, Any]]):
        self.attempts = {
            str(item["tool_attempt_id"]): json.loads(json.dumps(item))
            for item in attempts
        }

    def list_action_attempts(self, action_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(json.dumps(item))
            for item in self.attempts.values()
            if (item.get("action_truth") or {}).get("action_id") == action_id
        ]

    def list_unresolved_action_attempts(
        self, *, conversation_id: str = "", turn_id: str = ""
    ) -> list[dict[str, Any]]:
        result = []
        for item in self.attempts.values():
            truth = item.get("action_truth") or {}
            if conversation_id and item.get("conversation_id") != conversation_id:
                continue
            if turn_id and item.get("turn_id") != turn_id:
                continue
            if truth.get("effect_truth") in {"possible", "observed"}:
                result.append(json.loads(json.dumps(item)))
        return result

    def update_action_truth(
        self,
        attempt_id: str,
        *,
        state: str,
        reason_code: str,
        evidence=None,
        observer=None,
        reconciliation_status: str = "",
        exact: bool = False,
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        item = self.attempts[attempt_id]
        item["action_truth"] = transition_action_truth(
            item["action_truth"],
            state=state,
            reason_code=reason_code,
            evidence=evidence,
            observer=observer,
            reconciliation_status=reconciliation_status or None,
            exact=exact,
            occurred_at=occurred_at,
        )
        return json.loads(json.dumps(item))

    def restarted(self) -> "MemoryActionStore":
        return MemoryActionStore(list(self.attempts.values()))


class FakeBrowser:
    def __init__(self, safe_url: str, *, stale: bool = False):
        self.safe_url = safe_url
        self.stale = stale
        self.read_count = 0

    def snapshot(self) -> dict[str, Any]:
        self.read_count += 1
        if self.stale:
            return {
                "ok": False,
                "error": {"code": "page_target_stale"},
                "receipt": {"outcome": "confirmed_failure_before_effect"},
            }
        return {
            "ok": True,
            "data": {
                "safe_url": self.safe_url,
                "state": {
                    "browser_session_id": "browser_12345678",
                    "context_id": "context_12345678",
                    "page_id": "page_12345678",
                    "page_generation": 4,
                    "document_epoch": 9,
                },
            },
            "receipt": {
                "outcome": "confirmed_success",
                "reason_code": "browser_state_observed",
            },
        }


class FakeDesktop:
    def __init__(
        self,
        *,
        outcome: str,
        reason_code: str,
        no_partial_effect: bool = False,
    ):
        self.outcome = outcome
        self.reason_code = reason_code
        self.no_partial_effect = no_partial_effect
        self.read_count = 0

    def snapshot(self, _target_application: str = "") -> dict[str, Any]:
        self.read_count += 1
        return {
            "receipt": {
                "outcome": self.outcome,
                "reason_code": self.reason_code,
                "no_partial_effect": self.no_partial_effect,
                "window_generation": "generation_12345678",
                "observed_window_surface_ref": "surface_12345678",
                "observed_at": 101.0,
            }
        }


def make_attempt(action_truth: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_attempt_id": "attempt_12345678",
        "conversation_id": "conversation_12345678",
        "turn_id": "turn_12345678",
        "request_id": "request_12345678",
        "created_at": 100.0,
        "updated_at": 100.0,
        "action_truth": action_truth,
    }
