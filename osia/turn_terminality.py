#!/usr/bin/env python3
"""Durable, prose-independent user-turn terminality (WOR-253).

One provider completion is only an invocation boundary.  This module owns the
larger user turn, records typed execution evidence, and permits a canonical
final only after a verified terminal state has been reserved exactly once.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from osia.config import CONFIG_DIR


class TerminalState(str, Enum):
    SUCCESS_VERIFIED = "success_verified"
    BLOCKED_VERIFIED = "blocked_verified"
    USER_CANCELLED = "user_cancelled"
    HARD_SAFETY_BLOCK = "hard_safety_block"


TERMINAL_STATES = frozenset(item.value for item in TerminalState)
_STRATEGY_ATTEMPT_EVIDENCE_STATUSES = frozenset({
    "ambiguous_effect", "failed", "inapplicable", "progressed", "stagnated",
    "stale_target", "succeeded",
})
_MAX_PROVIDER_GUIDANCE_WITHOUT_ATTEMPT = 1


@dataclass(frozen=True)
class TerminalityDecision:
    action: str
    terminal_state: str = ""
    reason_code: str = ""
    next_strategy: str = ""
    postcondition: str = ""

    @property
    def terminal(self) -> bool:
        return self.terminal_state in TERMINAL_STATES


_LOCKS_GUARD = threading.Lock()
_TURN_LOCKS: dict[str, threading.RLock] = {}


def _turn_lock(turn_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _TURN_LOCKS.setdefault(turn_id, threading.RLock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: object, maximum: int = 512) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _fingerprint(value: object) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
            default=lambda _item: "<unsupported>",
        ).encode("utf-8")
    except Exception:
        encoded = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _safe_turn_key(turn_id: str) -> str:
    return hashlib.sha256(turn_id.encode("utf-8")).hexdigest()


class TurnTerminalityController:
    """Durable state machine for one stable user ``turn_id``.

    Provider narrative is retained only as provisional context.  Completion
    evidence comes from typed receipts, postcondition verification, correlated
    cancellation, or an absolute policy barrier.
    """

    VERSION = 1

    def __init__(
        self,
        *,
        turn_id: str,
        objective: str,
        required_postconditions: Iterable[str] = (),
        execution_required: bool,
        storage_dir: str | Path | None = None,
    ) -> None:
        stable_turn_id = _bounded(turn_id, 256)
        if not stable_turn_id:
            raise ValueError("turn_id is required")
        semantic_objective = _bounded(objective, 4000)
        if not semantic_objective:
            raise ValueError("objective is required")
        postconditions = [
            _bounded(item, 512) for item in required_postconditions
            if _bounded(item, 512)
        ]
        if not postconditions:
            postconditions = [
                "the requested external effect is observably complete"
                if execution_required else
                "a coherent answer directly satisfies the informational request"
            ]
        root = Path(storage_dir) if storage_dir is not None else Path(CONFIG_DIR) / "turn_terminality"
        self.turn_id = stable_turn_id
        self._root = root
        self._path = root / f"{_safe_turn_key(stable_turn_id)}.json"
        self._lock = _turn_lock(stable_turn_id)
        with self._lock, self._process_lock():
            loaded = self._load()
            if loaded is not None:
                if loaded.get("objective_fingerprint") != _fingerprint(semantic_objective):
                    raise ValueError("turn_id is already bound to another objective")
                self._state = loaded
                if self._recover_interrupted_invocations():
                    self._persist()
            else:
                self._state = {
                    "version": self.VERSION,
                    "turn_id": stable_turn_id,
                    "objective": semantic_objective,
                    "objective_fingerprint": _fingerprint(semantic_objective),
                    "execution_required": bool(execution_required),
                    "required_postconditions": postconditions,
                    "proven_postconditions": {},
                    "remaining_work": list(postconditions),
                    "typed_evidence": [],
                    "evidence_tokens": [],
                    "active_attempts": {},
                    "permission_state": "none",
                    "unknown_effects": {},
                    "strategy_snapshot": {},
                    "strategy_frontier": [],
                    "attempted_strategy_tuples": [],
                    "strategy_attempt_evidence": [],
                    "strategy_attempt_revision": 0,
                    "invocations": [],
                    "evidence_revision": 0,
                    "progress_revision": 0,
                    "cancellation": {
                        "requested": False,
                        "effect_possible": False,
                        "requested_at": "",
                    },
                    "hard_safety_reason": "",
                    "terminal_state": "",
                    "terminal_reason": "",
                    "final_reservation": {},
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                self._persist()

    @property
    def objective(self) -> str:
        return str(self._state["objective"])

    @property
    def execution_required(self) -> bool:
        return bool(self._state.get("execution_required"))

    @property
    def evidence_revision(self) -> int:
        return int(self._state.get("evidence_revision") or 0)

    @property
    def progress_revision(self) -> int:
        return int(self._state.get("progress_revision") or 0)

    @property
    def terminal_state(self) -> str:
        return str(self._state.get("terminal_state") or "")

    @property
    def final_reserved(self) -> bool:
        return bool(self._state.get("final_reservation"))

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _load(self) -> dict | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise RuntimeError("turn terminality state is unreadable") from exc
        if not isinstance(data, dict) or data.get("turn_id") != self.turn_id:
            raise RuntimeError("turn terminality state is invalid")
        return data

    def _refresh(self) -> None:
        loaded = self._load()
        if loaded is not None:
            self._state = loaded

    def _persist(self) -> None:
        self._state["updated_at"] = _now()
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            self._path.name + f".{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_text(
            json.dumps(self._state, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self._path)

    @contextmanager
    def _process_lock(self):
        """Serialize durable reservations across OSIA process restarts/races."""

        self._root.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _recover_interrupted_invocations(self) -> bool:
        """Conservatively reconcile provider work interrupted by a restart."""

        changed = False
        unknown = self._state.setdefault("unknown_effects", {})
        for item in self._state.get("invocations") or []:
            if (
                item.get("status") == "active"
                and int(item.get("owner_pid") or 0) != os.getpid()
            ):
                item["status"] = "interrupted_outcome_unknown"
                item["completed_at"] = _now()
                invocation_id = str(item.get("invocation_id") or "")
                if invocation_id and invocation_id not in unknown:
                    unknown[invocation_id] = {
                        "outcome": "outcome_unknown",
                        "reason_code": "provider_process_interrupted",
                        "recorded_at": _now(),
                    }
                    self._state["evidence_revision"] = int(
                        self._state.get("evidence_revision") or 0
                    ) + 1
                changed = True
        return changed

    def begin_invocation(
        self,
        *,
        provider_route: str,
        strategy_id: str,
        postcondition: str = "",
    ) -> str:
        """Reserve a provider segment without claiming a mechanism attempt."""

        route = _bounded(provider_route, 160) or "default"
        strategy = _bounded(strategy_id, 160) or "provider_initial"
        with self._lock, self._process_lock():
            self._refresh()
            cancellation = self._state.get("cancellation") or {}
            if (
                self.terminal_state
                or self._state.get("hard_safety_reason")
                or any(
                    item.get("status") == "active"
                    for item in self._state.get("invocations") or []
                )
                or self._state.get("active_attempts")
                or self._state.get("permission_state") == "pending"
                or (
                    cancellation.get("requested")
                    and not cancellation.get("effect_possible")
                )
                or (
                    self._state.get("unknown_effects")
                    and strategy != "reconcile_unknown_read_only"
                )
            ):
                return ""
            required = list(self._state.get("required_postconditions") or [])
            target = _bounded(postcondition, 512) or (
                str((self._state.get("remaining_work") or required or [""])[0])
            )
            invocation_id = "inv_" + secrets.token_urlsafe(18)
            self._state.setdefault("invocations", []).append({
                "invocation_id": invocation_id,
                "provider_route": route,
                "strategy_id": strategy,
                "postcondition": target,
                "evidence_revision": self.evidence_revision,
                "strategy_attempt_revision": int(
                    self._state.get("strategy_attempt_revision") or 0
                ),
                "owner_pid": os.getpid(),
                "status": "active",
                "started_at": _now(),
                "completed_at": "",
                "native_turn_id": "",
                "native_status": "",
            })
            self._state["invocations"] = self._state["invocations"][-128:]
            self._persist()
            return invocation_id

    def record_provider_invocation_completed(
        self,
        invocation_id: str,
        *,
        native_turn_id: str = "",
        native_status: str = "",
        response_fingerprint: str = "",
    ) -> None:
        """Record an invocation boundary without treating it as turn success."""

        with self._lock:
            self._refresh()
            for item in reversed(self._state.setdefault("invocations", [])):
                if item.get("invocation_id") == invocation_id:
                    item["status"] = "provider_invocation_completed"
                    item["completed_at"] = _now()
                    item["native_turn_id"] = _bounded(native_turn_id, 192)
                    item["native_status"] = _bounded(native_status, 80)
                    item["response_fingerprint"] = _bounded(response_fingerprint, 64)
                    break
            self._persist()

    def set_strategy_snapshot(self, snapshot: dict | None) -> None:
        with self._lock:
            self._refresh()
            safe = dict(snapshot) if isinstance(snapshot, dict) else {}
            self._state["strategy_snapshot"] = safe
            frontier = safe.get("alternatives_still_applicable") or []
            self._state["strategy_frontier"] = [
                _bounded(item, 160) for item in frontier if _bounded(item, 160)
            ][:64]
            if (
                self.execution_required
                and not safe.get("mechanisms")
                and not self._state["strategy_frontier"]
            ):
                self._state["strategy_frontier"] = ["provider_capability_selection"]
            attempt_evidence = self._state.setdefault(
                "strategy_attempt_evidence", [],
            )
            added_attempts = 0
            for item in safe.get("attempts") or []:
                if not isinstance(item, dict):
                    continue
                status = _bounded(item.get("status"), 80)
                if status not in _STRATEGY_ATTEMPT_EVIDENCE_STATUSES:
                    continue
                identity = {
                    "attempt_id": _bounded(item.get("attempt_id"), 192),
                    "mechanism_id": _bounded(item.get("mechanism_id"), 160),
                    "node_id": _bounded(item.get("node_id"), 160),
                    "status": status,
                    "reason_code": _bounded(
                        item.get("failure_or_stagnation_reason"), 160,
                    ),
                }
                if identity in attempt_evidence:
                    continue
                attempt_evidence.append(identity)
                added_attempts += 1
            if added_attempts:
                self._state["strategy_attempt_evidence"] = attempt_evidence[-128:]
                self._state["strategy_attempt_revision"] = int(
                    self._state.get("strategy_attempt_revision") or 0
                ) + added_attempts
            proven = safe.get("proven_postconditions") or {}
            if isinstance(proven, dict):
                for postcondition, evidence in proven.items():
                    self.prove_postcondition(str(postcondition), str(evidence))
            self._persist()

    def set_strategy_frontier(self, strategy_ids: Iterable[str]) -> None:
        with self._lock:
            self._refresh()
            self._state["strategy_frontier"] = [
                _bounded(item, 160) for item in strategy_ids if _bounded(item, 160)
            ][:64]
            self._persist()

    def record_typed_evidence(
        self,
        *,
        outcome: str,
        evidence: object = "",
        postcondition: str = "",
        attempt_id: str = "",
        reason_code: str = "",
        useful_progress: bool = False,
    ) -> None:
        normalized = _bounded(outcome, 80).lower()
        evidence_token = _fingerprint(evidence) if evidence not in (None, "") else ""
        entry = {
            "outcome": normalized,
            "evidence_token": evidence_token,
            "postcondition": _bounded(postcondition, 512),
            "attempt_id": _bounded(attempt_id, 192),
            "reason_code": _bounded(reason_code, 160),
            "recorded_at": _now(),
        }
        with self._lock:
            self._refresh()
            evidence_items = self._state.setdefault("typed_evidence", [])
            identity = {key: entry[key] for key in (
                "outcome", "evidence_token", "postcondition", "attempt_id", "reason_code",
            )}
            if any(all(item.get(key) == value for key, value in identity.items()) for item in evidence_items):
                return
            evidence_items.append(entry)
            self._state["typed_evidence"] = evidence_items[-128:]
            evidence_tokens = self._state.setdefault("evidence_tokens", [])
            if evidence_token and evidence_token not in evidence_tokens:
                evidence_tokens.append(evidence_token)
                self._state["evidence_tokens"] = evidence_tokens[-128:]
                self._state["evidence_revision"] = self.evidence_revision + 1
            if useful_progress:
                self._state["progress_revision"] = self.progress_revision + 1
            if normalized in {"outcome_unknown", "unknown_after_start"}:
                key = entry["attempt_id"] or evidence_token or "unknown_effect"
                self._state.setdefault("unknown_effects", {})[key] = entry
            elif normalized == "success_unverified":
                key = entry["attempt_id"] or evidence_token or "unverified_effect"
                self._state.setdefault("active_attempts", {}).pop(key, None)
                self._state.setdefault("unknown_effects", {})[key] = entry
            elif normalized in {
                "success_verified",
                "failed_before_effect",
                "confirmed_failure_before_effect",
                "rejected_before_execution",
                "cancelled_proven",
            }:
                key = entry["attempt_id"]
                if key:
                    self._state.setdefault("active_attempts", {}).pop(key, None)
                    self._state.setdefault("unknown_effects", {}).pop(key, None)
            if (
                normalized in {
                    "success_verified",
                    "success_unverified",
                    "failed_before_effect",
                    "confirmed_failure_before_effect",
                    "rejected_before_execution",
                    "cancelled_proven",
                }
                and self._state.get("permission_state") == "pending"
                and not self._state.get("active_attempts")
            ):
                self._state["permission_state"] = "resolved"
            if normalized == "hard_safety_block" or entry["reason_code"] in {
                "block_system_destruction", "system_destruction_absolute",
            }:
                self._state["hard_safety_reason"] = entry["reason_code"] or normalized
            self._persist()

    def set_attempt_state(
        self,
        attempt_id: str,
        *,
        status: str,
        permission_state: str = "",
    ) -> None:
        key = _bounded(attempt_id, 192)
        if not key:
            return
        normalized = _bounded(status, 80).lower()
        with self._lock:
            self._refresh()
            active = self._state.setdefault("active_attempts", {})
            if normalized in {"pending", "running", "accepted", "waiting_verification", "active"}:
                active[key] = {"status": normalized, "updated_at": _now()}
            else:
                active.pop(key, None)
            if permission_state:
                self._state["permission_state"] = _bounded(permission_state, 80)
            self._persist()

    def set_permission_state(self, state: str) -> None:
        with self._lock:
            self._refresh()
            self._state["permission_state"] = _bounded(state, 80) or "none"
            self._persist()

    def require_execution(self, postcondition: str = "") -> None:
        """Promote a turn when a structured execution transition is observed."""

        with self._lock:
            self._refresh()
            if self.execution_required:
                return
            previous = list(self._state.get("required_postconditions") or [])
            target = _bounded(postcondition, 512) or "the requested external effect is observably complete"
            informational = "a coherent answer directly satisfies the informational request"
            required = [target if item == informational else item for item in previous]
            self._state["execution_required"] = True
            self._state["required_postconditions"] = required or [target]
            proven = self._state.get("proven_postconditions") or {}
            self._state["remaining_work"] = [item for item in self._state["required_postconditions"] if item not in proven]
            self._persist()

    def record_execution_record(
        self,
        tool_name: str,
        execution_record,
        *,
        postcondition: str = "",
    ) -> None:
        """Project one typed ToolExecutionRecord into the turn ledger."""

        outcome_value = getattr(getattr(execution_record, "outcome", None), "value", None)
        outcome = str(outcome_value or getattr(execution_record, "outcome", "") or "")
        reason_code = str(getattr(execution_record, "reason_code", "") or "")
        attempt_id = str(getattr(execution_record, "tool_attempt_id", "") or "")
        result = getattr(execution_record, "result", "")
        self.require_execution(postcondition)
        if "BLOCK_SYSTEM_DESTRUCTION" in str(result) or reason_code in {
            "block_system_destruction", "system_destruction_absolute",
        }:
            self.mark_hard_safety_block(reason_code or "block_system_destruction")
        if reason_code.startswith("permission_"):
            permission = reason_code.removeprefix("permission_")
            self.set_permission_state(permission)
        if outcome in {"pending"} and attempt_id:
            self.set_attempt_state(attempt_id, status="pending", permission_state=(
                "pending" if reason_code.startswith("permission_") else ""
            ))
        elif outcome in {"outcome_unknown"} and attempt_id:
            self.set_attempt_state(attempt_id, status="waiting_verification")
        self.record_typed_evidence(
            outcome=outcome,
            evidence=result,
            postcondition=postcondition,
            attempt_id=attempt_id,
            reason_code=reason_code,
            useful_progress=outcome in {"success_verified", "success_unverified"},
        )

    def reconcile_unknown(self, attempt_id: str, *, outcome: str, evidence: object) -> None:
        key = _bounded(attempt_id, 192)
        with self._lock:
            self._refresh()
            unknown = self._state.setdefault("unknown_effects", {})
            if key not in unknown:
                return
            unknown.pop(key, None)
            if not unknown and (self._state.get("cancellation") or {}).get("requested"):
                self._state["cancellation"]["effect_possible"] = False
            self._persist()
        self.record_typed_evidence(
            outcome=outcome, evidence=evidence, attempt_id=key,
            reason_code="unknown_effect_reconciled", useful_progress=True,
        )

    def prove_postcondition(self, postcondition: str, evidence: object) -> bool:
        target = _bounded(postcondition, 512)
        if evidence in (None, ""):
            return False
        token = _fingerprint(evidence)
        with self._lock:
            self._refresh()
            required = list(self._state.get("required_postconditions") or [])
            if not target and len(required) == 1:
                target = str(required[0])
            if target not in required:
                return False
            proven = self._state.setdefault("proven_postconditions", {})
            if target not in proven:
                proven[target] = token
                self._state["evidence_revision"] = self.evidence_revision + 1
                self._state["progress_revision"] = self.progress_revision + 1
            unknown_effects = self._state.setdefault("unknown_effects", {})
            reconciled_attempts = {
                attempt_id
                for attempt_id, item in unknown_effects.items()
                if len(required) == 1
                or str((item or {}).get("postcondition") or "") == target
            }
            for attempt_id in reconciled_attempts:
                unknown_effects.pop(attempt_id, None)
            active = self._state.setdefault("active_attempts", {})
            for attempt_id in reconciled_attempts:
                active.pop(attempt_id, None)
            cancellation = self._state.get("cancellation") or {}
            if cancellation.get("requested") and not unknown_effects:
                cancellation["effect_possible"] = False
            self._state["remaining_work"] = [item for item in required if item not in proven]
            self._persist()
        return True

    def request_cancellation(self, *, effect_possible: bool) -> None:
        with self._lock:
            self._refresh()
            cancellation = self._state.setdefault("cancellation", {})
            cancellation.update({
                "requested": True,
                "effect_possible": bool(effect_possible),
                "requested_at": _now(),
            })
            if effect_possible and not self._state.get("unknown_effects"):
                self._state.setdefault("unknown_effects", {})[
                    "cancellation_possible_effect"
                ] = {
                    "outcome": "outcome_unknown",
                    "reason_code": "cancelled_effect_requires_reconciliation",
                    "recorded_at": _now(),
                }
            self._persist()

    def mark_hard_safety_block(self, reason_code: str) -> None:
        with self._lock:
            self._refresh()
            self._state["hard_safety_reason"] = _bounded(reason_code, 160) or "absolute_safety_barrier"
            self._persist()

    def _provider_guidance_count(
        self,
        provider_route: str,
        strategy_id: str,
        postcondition: str,
    ) -> int:
        revision = int(self._state.get("strategy_attempt_revision") or 0)
        return sum(
            1
            for item in self._state.get("invocations") or []
            if item.get("status") == "provider_invocation_completed"
            and str(item.get("provider_route") or "") == provider_route
            and str(item.get("strategy_id") or "") == strategy_id
            and str(item.get("postcondition") or "") == postcondition
            and int(item.get("strategy_attempt_revision") or 0) == revision
        )

    def _next_strategy(self, provider_route: str) -> tuple[str, str]:
        remaining_work = list(self._state.get("remaining_work") or [])
        if not remaining_work:
            return "", ""
        frontier = list(self._state.get("strategy_frontier") or [])
        for postcondition in remaining_work:
            for strategy_id in frontier:
                if self._provider_guidance_count(
                    provider_route, str(strategy_id), str(postcondition),
                ) < _MAX_PROVIDER_GUIDANCE_WITHOUT_ATTEMPT:
                    return str(strategy_id), str(postcondition)
        return "", ""

    def _next_reconciliation(self, provider_route: str) -> tuple[str, str]:
        route = _bounded(provider_route, 160) or "default"
        strategy = "reconcile_unknown_read_only"
        for unknown_key in sorted(self._state.get("unknown_effects") or {}):
            target = "reconcile:" + unknown_key
            if self._provider_guidance_count(
                route, strategy, target,
            ) < _MAX_PROVIDER_GUIDANCE_WITHOUT_ATTEMPT:
                return strategy, target
        return "", ""

    def evaluate(self, *, informational_answer_satisfied: bool = False, provider_route: str = "default") -> TerminalityDecision:
        with self._lock:
            self._refresh()
            if self.terminal_state:
                return TerminalityDecision("terminal", self.terminal_state, self._state.get("terminal_reason", ""))
            if self._state.get("hard_safety_reason"):
                return TerminalityDecision(
                    "terminal", TerminalState.HARD_SAFETY_BLOCK.value,
                    str(self._state["hard_safety_reason"]),
                )
            cancellation = self._state.get("cancellation") or {}
            if cancellation.get("requested"):
                if not cancellation.get("effect_possible") and not self._state.get("unknown_effects"):
                    return TerminalityDecision(
                        "terminal", TerminalState.USER_CANCELLED.value,
                        "authoritative_user_cancellation",
                    )
            if (
                any(
                    item.get("status") == "active"
                    for item in self._state.get("invocations") or []
                )
                or self._state.get("active_attempts")
                or self._state.get("permission_state") == "pending"
            ):
                return TerminalityDecision("wait", reason_code="operation_or_permission_active")
            if self._state.get("unknown_effects"):
                strategy, target = self._next_reconciliation(provider_route)
                if strategy:
                    return TerminalityDecision(
                        "continue",
                        reason_code="outcome_unknown_requires_reconciliation",
                        next_strategy=strategy,
                        postcondition=target,
                    )
                return TerminalityDecision(
                    "wait",
                    reason_code="reconciliation_not_executed_frontier_preserved",
                )
            if not self.execution_required and informational_answer_satisfied:
                postcondition = str(self._state["required_postconditions"][0])
                self.prove_postcondition(postcondition, "coherent_informational_answer")
            if not self._state.get("remaining_work"):
                return TerminalityDecision(
                    "terminal", TerminalState.SUCCESS_VERIFIED.value,
                    "all_required_postconditions_proven",
                )
            strategy, postcondition = self._next_strategy(_bounded(provider_route, 160) or "default")
            if strategy:
                return TerminalityDecision(
                    "continue", reason_code="safe_semantic_strategy_remains",
                    next_strategy=strategy, postcondition=postcondition,
                )
            if self._state.get("strategy_frontier"):
                return TerminalityDecision(
                    "wait",
                    reason_code="provider_no_progress_strategy_frontier_preserved",
                )
            return TerminalityDecision(
                "terminal", TerminalState.BLOCKED_VERIFIED.value,
                "safe_semantic_frontier_exhausted",
            )

    def continuation_context(self, decision: TerminalityDecision) -> str:
        state = self.snapshot()
        payload = {
            "event": "continue_same_user_turn",
            "turn_id": self.turn_id,
            "objective": state["objective"],
            "proven_postconditions": state.get("proven_postconditions") or {},
            "remaining_work": state.get("remaining_work") or [],
            "recent_typed_evidence": (state.get("typed_evidence") or [])[-8:],
            "active_attempts": state.get("active_attempts") or {},
            "permission_state": state.get("permission_state") or "none",
            "unknown_effects": state.get("unknown_effects") or {},
            "attempts_not_to_replay": (
                state.get("strategy_attempt_evidence") or []
            )[-32:],
            "next_strategy": decision.next_strategy,
            "target_postcondition": decision.postcondition,
        }
        return (
            "[Internal terminality continuation. This is the same user turn; "
            "the original objective remains authoritative. Do not repeat proven "
            "steps or an attempted tuple without new evidence. Provider prose is "
            "not completion evidence. Structured state: "
            + json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            + "]"
        )

    def reserve_final(
        self,
        state: str,
        *,
        reason_code: str = "",
        provider_route: str = "default",
        final_text: str = "",
    ) -> bool:
        """Durable compare-and-set for the sole canonical assistant final."""

        if state not in TERMINAL_STATES:
            return False
        with self._lock, self._process_lock():
            self._refresh()
            if self._state.get("final_reservation"):
                return False
            if state == TerminalState.SUCCESS_VERIFIED.value and self._state.get("remaining_work"):
                return False
            if state == TerminalState.USER_CANCELLED.value and not (
                self._state.get("cancellation") or {}
            ).get("requested"):
                return False
            if state == TerminalState.HARD_SAFETY_BLOCK.value and not self._state.get("hard_safety_reason"):
                return False
            if state == TerminalState.BLOCKED_VERIFIED.value and (
                self._state.get("active_attempts")
                or self._state.get("permission_state") == "pending"
                or self._state.get("strategy_frontier")
                or self._state.get("unknown_effects")
                or self._next_strategy(
                    _bounded(provider_route, 160) or "default"
                ) != ("", "")
            ):
                return False
            self._state["terminal_state"] = state
            self._state["terminal_reason"] = _bounded(reason_code, 160)
            self._state["final_reservation"] = {
                "reservation_id": "final_" + secrets.token_urlsafe(18),
                "reserved_at": _now(),
                "final_text": str(final_text or "")[:32_000],
            }
            self._persist()
            return True


__all__ = [
    "TERMINAL_STATES",
    "TerminalState",
    "TerminalityDecision",
    "TurnTerminalityController",
]
